import json
import subprocess
import sys
import os

def run_command(cmd):
    print(f"🏃 Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result

def apply_field_override(override, project_id, database_id):
    """Exempt one field from single-field indexing; any other override is refused, never half-applied."""
    collection, path = override.get("collectionGroup"), override["fieldPath"]
    if override.get("indexes") != []:
        print(f"❌ Unsupported field override for {collection}.{path}: only an exemption (\"indexes\": []) is applied")
        return False
    result = run_command([
        "gcloud", "firestore", "indexes", "fields", "update", path,
        f"--collection-group={collection}",
        f"--project={project_id}",
        f"--database={database_id}",
        "--disable-indexes",
        "--async",
    ])
    if result.returncode == 0:
        print(f"✅ Single-field indexes disabled for {collection}.{path}")
        return True
    print(f"❌ Error exempting {collection}.{path}:")
    print(result.stderr)
    return False


def deploy_indexes(file_path, project_id, database_id="us-production"):
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        sys.exit(1)

    with open(file_path, 'r') as f:
        indexes = json.load(f)

    print(f"🚀 Deploying {len(indexes)} indexes to project {project_id} (database={database_id})...")

    failures = []
    for idx in indexes:
        collection = idx.get("collectionGroup")
        if "fieldPath" in idx and "fields" not in idx:
            # A single-field override (Firebase's fieldOverrides entry): `"indexes": []` exempts the field.
            if not apply_field_override(idx, project_id, database_id):
                failures.append(f"{collection}.{idx['fieldPath']}")
            continue
        fields = idx.get("fields", [])
        query_scope = idx.get("queryScope", "COLLECTION").lower()

        cmd = [
            "gcloud", "firestore", "indexes", "composite", "create",
            f"--collection-group={collection}",
            f"--project={project_id}",
            f"--database={database_id}",
            f"--query-scope={query_scope}",
            "--async"
        ]

        if any(field.get("vectorConfig") for field in fields):
            # A vector index is created by its own gcloud script (e.g. create_drive_vector_indexes.sh).
            # Dropping only the vector field would create a stray composite of the remaining fields.
            print(f"⚠️ Skipping vector index for {collection} (created by its scripts/infrastructure script)")
            continue

        for field in fields:
            path = field.get("fieldPath")
            order = field.get("order")
            array = field.get("arrayConfig")

            if order:
                cmd.append(f"--field-config=field-path={path},order={order.lower()}")
            elif array:
                cmd.append(f"--field-config=field-path={path},array-config={array.lower()}")

        result = run_command(cmd)
        
        if result.returncode == 0:
            print(f"✅ Index creation initiated for {collection}")
        elif "already exists" in result.stderr.lower():
            print(f"ℹ️ Index for {collection} already exists, skipping.")
        else:
            print(f"❌ Error creating index for {collection}:")
            print(result.stderr)
            failures.append(collection)

    return failures

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python deploy_firestore_indexes.py <json_file> <project_id> [database_id]")
        sys.exit(1)

    database_id = sys.argv[3] if len(sys.argv) > 3 else "us-production"
    failed = deploy_indexes(sys.argv[1], sys.argv[2], database_id)
    if failed:
        print(f"❌ {len(failed)} index creation(s) failed: {', '.join(failed)}")
        sys.exit(1)

import json
import subprocess
import sys
import os

def run_command(cmd):
    print(f"🏃 Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result

def deploy_indexes(file_path, project_id):
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        sys.exit(1)

    with open(file_path, 'r') as f:
        indexes = json.load(f)

    print(f"🚀 Deploying {len(indexes)} indexes to project {project_id}...")

    for idx in indexes:
        collection = idx.get("collectionGroup")
        fields = idx.get("fields", [])
        query_scope = idx.get("queryScope", "COLLECTION").lower()

        cmd = [
            "gcloud", "firestore", "indexes", "composite", "create",
            f"--collection-group={collection}",
            f"--project={project_id}",
            f"--query-scope={query_scope}",
            "--async"
        ]

        for field in fields:
            path = field.get("fieldPath")
            order = field.get("order")
            vector = field.get("vectorConfig")

            if vector:
                # Vector config is complex in CLI, skipping for now or handling specifically
                # For this project, we mainly need the composite ones for sessions
                print(f"⚠️ Skipping vector index for {collection} (CLI complexity)")
                continue
            
            if order:
                cmd.append(f"--field-config=field-path={path},order={order.lower()}")

        result = run_command(cmd)
        
        if result.returncode == 0:
            print(f"✅ Index creation initiated for {collection}")
        elif "already exists" in result.stderr.lower():
            print(f"ℹ️ Index for {collection} already exists, skipping.")
        else:
            print(f"❌ Error creating index for {collection}:")
            print(result.stderr)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python deploy_firestore_indexes.py <json_file> <project_id>")
        sys.exit(1)
    
    deploy_indexes(sys.argv[1], sys.argv[2])

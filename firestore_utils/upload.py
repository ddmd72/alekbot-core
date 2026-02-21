"""Firestore document uploader.

Usage:
  python firestore_utils/upload.py <collection_name> <document_name> [--format groovy|json]

Behavior:
  - groovy: reads uploads/<document_name>.groovy and uploads to {content: <file_text>}
  - json: reads uploads/<document_name>.json and uploads full JSON document
  - document_id is derived from document_name
"""

import argparse
import json
import os
import sys
from pathlib import Path

from google.cloud import firestore

_DEFAULT_DATABASE = os.environ.get("FIRESTORE_DATABASE", "us-production")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload Firestore document from file")
    parser.add_argument("collection", help="Firestore collection name")
    parser.add_argument("document_name", help="Document name (file stem, without extension)")
    parser.add_argument(
        "--format",
        choices=["groovy", "json"],
        default=None,
        help="Input format override (default: groovy)",
    )
    parser.add_argument(
        "--database",
        default=_DEFAULT_DATABASE,
        help="Firestore database ID (default: $FIRESTORE_DATABASE env var).",
    )
    return parser.parse_args()


def _infer_format(override: str | None) -> str:
    if override:
        return override
    return "groovy"


def upload_document(collection: str, document_name: str, file_format: str, database: str = _DEFAULT_DATABASE) -> str:
    # Require explicit confirmation when writing to non-development collections
    if not collection.startswith("development_"):
        answer = input(f"⚠️  PRODUCTION write to '{collection}' (db={database}). Type 'YES' to continue: ")
        if answer.strip() != "YES":
            print("Aborted.")
            sys.exit(1)

    db = firestore.Client(database=database)
    document_id = document_name
    doc_ref = db.collection(collection).document(document_id)

    uploads_dir = Path("firestore_utils") / "uploads"
    if file_format == "json":
        file_path = uploads_dir / f"{document_name}.json"
    else:
        file_path = uploads_dir / f"{document_name}.groovy"

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if file_format == "groovy":
        content = file_path.read_text(encoding="utf-8")
        
        # Check if document exists and has token_id (Token v3)
        existing_doc = doc_ref.get()
        if existing_doc.exists and existing_doc.to_dict().get("token_id"):
            # Token v3 document - only update content field (merge behavior)
            payload = {
                "content": content,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
            doc_ref.update(payload)
            print(f"  ℹ️  Updated content field only (Token v3 document)")
        else:
            # Legacy document or new document - full overwrite
            payload = {
                "content": content,
                "updated_at": firestore.SERVER_TIMESTAMP,
                "uploaded_by": "local_script",
                "source_file": str(file_path),
            }
            doc_ref.set(payload, merge=False)
    else:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON file must contain a JSON object")
        payload.setdefault("updated_at", firestore.SERVER_TIMESTAMP)
        payload.setdefault("uploaded_by", "local_script")
        payload.setdefault("source_file", str(file_path))
        
        # JSON always does full overwrite
        doc_ref.set(payload, merge=False)
    
    return document_id


def main() -> None:
    args = _parse_args()
    file_format = _infer_format(args.format)
    document_id = upload_document(args.collection, args.document_name, file_format, database=args.database)

    uploads_dir = Path("firestore_utils") / "uploads"
    ext = "json" if file_format == "json" else "groovy"
    file_path = uploads_dir / f"{args.document_name}.{ext}"
    print(f"✅ Uploaded {args.collection}/{document_id} from {file_path} (database: {args.database})")


if __name__ == "__main__":
    main()
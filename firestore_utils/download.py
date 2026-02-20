"""Firestore document downloader.

Usage:
  python firestore_utils/download.py <collection_name> <document_id> [--format groovy|json] [--output path]
  python firestore_utils/download.py <collection_name> --list  # List all documents

Default behavior:
  - format=groovy
  - output file name = firestore_utils/downloads/<document_id>.groovy
  - reads only the "content" field for groovy

JSON format:
  - output file name = firestore_utils/downloads/<document_id>.json
  - writes full document data as JSON
"""

import argparse
import json
from pathlib import Path

from google.cloud import firestore


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Firestore document to file")
    parser.add_argument("collection", help="Firestore collection name")
    parser.add_argument("document_id", nargs="?", default=None, help="Firestore document ID")
    parser.add_argument(
        "--format",
        choices=["groovy", "json"],
        default="groovy",
        help="Output format (groovy=content field only, json=full document).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path (default: firestore_utils/downloads/<document_id>.groovy|json).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all documents in collection (instead of downloading).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print debug information.",
    )
    parser.add_argument(
        "--database",
        default="us-production",
        help="Firestore database ID (default: us-production).",
    )
    return parser.parse_args()


def list_collection(collection: str, verbose: bool = False, database: str = "us-production") -> None:
    """List all documents in a collection."""
    db = firestore.Client(database=database)
    
    if verbose:
        print(f"🔍 [DEBUG] Project: {db.project}")
        print(f"🔍 [DEBUG] Collection: {collection}")
    
    docs = db.collection(collection).stream()
    
    doc_list = []
    for doc in docs:
        doc_list.append(doc.id)
    
    if not doc_list:
        print(f"⚠️  Collection '{collection}' is empty or does not exist")
        print(f"\n💡 Available collections (examples):")
        print(f"  - dev_prompt_system_tokens")
        print(f"  - dev_prompt_blueprints_v3")
        print(f"  - dev_prompt_agent_profiles")
        print(f"  - dev_prompt_user_token_overrides")
        return
    
    print(f"📋 Documents in '{collection}' ({len(doc_list)}):\n")
    for doc_id in sorted(doc_list):
        print(f"  - {doc_id}")
    
    print(f"\n💡 To download:")
    print(f"  python firestore_utils/download.py {collection} <document_id>")


def download_document(collection: str, document_id: str, output_format: str, output_path: Path, verbose: bool = False, database: str = "us-production") -> None:
    db = firestore.Client(database=database)
    
    if verbose:
        print(f"🔍 [DEBUG] Project: {db.project}")
        print(f"🔍 [DEBUG] Collection: {collection}")
        print(f"🔍 [DEBUG] Document ID: {document_id}")
    
    doc_ref = db.collection(collection).document(document_id)
    snapshot = doc_ref.get()

    if not snapshot.exists:
        print(f"\n❌ Document not found: {collection}/{document_id}")
        print(f"\n💡 Listing available documents in '{collection}':\n")
        list_collection(collection, verbose=False)
        raise ValueError(f"Document not found: {collection}/{document_id}")

    data = snapshot.to_dict() or {}
    
    if verbose:
        print(f"🔍 [DEBUG] Document fields: {list(data.keys())}")

    if output_format == "groovy":
        content = data.get("content")
        if content is None:
            print(f"\n⚠️  Document missing 'content' field")
            print(f"📋 Available fields: {list(data.keys())}")
            raise ValueError("Document missing 'content' field (required for groovy format)")
        output_path.write_text(content, encoding="utf-8")
    else:
        output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    
    # List mode
    if args.list:
        if args.document_id:
            print("⚠️  Warning: --list ignores document_id argument")
        list_collection(args.collection, verbose=args.verbose, database=args.database)
        return
    
    # Download mode
    if not args.document_id:
        print("❌ Error: document_id is required (or use --list to see available documents)")
        print(f"\n💡 Usage:")
        print(f"  python firestore_utils/download.py {args.collection} <document_id>")
        print(f"  python firestore_utils/download.py {args.collection} --list")
        exit(1)
    
    extension = "groovy" if args.format == "groovy" else "json"
    default_name = f"{args.document_id}.{extension}"
    output_path = Path(args.output or Path("firestore_utils") / "downloads" / default_name)
    
    # Ensure downloads directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    download_document(args.collection, args.document_id, args.format, output_path, verbose=args.verbose, database=args.database)

    print(f"✅ Downloaded {args.collection}/{args.document_id} → {output_path} (database: {args.database})")


if __name__ == "__main__":
    main()

import asyncio
import argparse
import json
import time
from pathlib import Path

from google.cloud import firestore

from src.config.environment import EnvironmentConfig


async def backup_collection(collection_name: str, output_path: Path) -> int:
    db = firestore.AsyncClient()
    collection = db.collection(collection_name)
    documents = []

    async for doc in collection.stream():
        data = doc.to_dict()
        data["_doc_id"] = doc.id
        documents.append(data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(documents, indent=2, ensure_ascii=False, default=str)
    )
    return len(documents)


async def backup_dev_users(output_dir: Path) -> None:
    config = EnvironmentConfig()
    prefix = config.firestore_collection_prefix
    collection_name = f"{prefix}users"
    timestamp = int(time.time())
    output_path = output_dir / f"{collection_name}_backup_{timestamp}.json"

    print(f"📦 Backing up {collection_name} to {output_path}...")
    count = await backup_collection(collection_name, output_path)
    print(f"✅ Backup complete: {count} documents")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default="reports/dev_baseline",
        help="Directory to store backup JSON",
    )
    args = parser.parse_args()
    asyncio.run(backup_dev_users(Path(args.output_dir)))


if __name__ == "__main__":
    main()
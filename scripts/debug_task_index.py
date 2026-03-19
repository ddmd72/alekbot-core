"""
Debug script: inspect task_search_index and task_config collections.
Usage: python scripts/debug_task_index.py
"""
import asyncio
import os
from collections import Counter
from dotenv import load_dotenv

load_dotenv()

import google.auth
from google.cloud import firestore_v1 as firestore

ENV = os.getenv("APP_ENV", "development")
DB_NAME = os.getenv("FIRESTORE_DATABASE", "us-production")
PROJECT_ID = os.getenv("GCP_PROJECT_ID") or os.getenv("FIREBASE_PROJECT_ID") or google.auth.default()[1]

SEARCH_COLLECTION = f"{ENV}_task_search_index"
CONFIG_COLLECTION = f"{ENV}_task_config"


async def main():
    db = firestore.AsyncClient(project=PROJECT_ID, database=DB_NAME)

    # ── task_search_index ──────────────────────────────────────────────
    print(f"\n=== {SEARCH_COLLECTION} ===")
    docs = await db.collection(SEARCH_COLLECTION).get()
    print(f"Total documents: {len(docs)}")

    task_ids = [d.id for d in docs]
    # Check for actual Firestore-level duplicates (impossible with set(), but let's verify)
    dupe_ids = [tid for tid, cnt in Counter(task_ids).items() if cnt > 1]
    print(f"Duplicate doc IDs: {len(dupe_ids)}")

    # Break down by user
    by_user: dict = {}
    by_list: dict = {}
    for doc in docs:
        data = doc.to_dict()
        uid = data.get("user_id", "unknown")[:8]
        lid = data.get("list_id", "unknown")[:8]
        lst_name = data.get("list_name", "?")
        by_user[uid] = by_user.get(uid, 0) + 1
        key = f"{lid} ({lst_name})"
        by_list[key] = by_list.get(key, 0) + 1

    print("\nBy user:")
    for uid, cnt in sorted(by_user.items(), key=lambda x: -x[1]):
        print(f"  {uid}: {cnt} tasks")

    print("\nBy list:")
    for lst, cnt in sorted(by_list.items(), key=lambda x: -x[1]):
        print(f"  {lst}: {cnt} tasks")

    # ── task_config ────────────────────────────────────────────────────
    print(f"\n=== {CONFIG_COLLECTION} ===")
    configs = await db.collection(CONFIG_COLLECTION).get()
    for cfg in configs:
        data = cfg.to_dict()
        subs = data.get("subscriptions", [])
        print(f"\nUser doc: {cfg.id[:16]}...")
        print(f"  primary_list_id: {str(data.get('primary_list_id', ''))[:16]}...")
        print(f"  subscriptions: {len(subs)}")
        for s in subs:
            print(f"    sub_id={str(s.get('sub_id',''))[:8]}  list_id={str(s.get('list_id',''))[:8]}  expires={s.get('expires_at')}")

    db.close()


if __name__ == "__main__":
    asyncio.run(main())

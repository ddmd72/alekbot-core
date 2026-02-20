#!/usr/bin/env python3
"""
Direct check if glove data exists in development_facts collection.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings
from src.adapters.firestore_repo import FirestoreFactRepository

async def main():
    print("🔍 Direct check for glove data in development_facts...")

    # Load settings
    settings = load_settings()
    env_config = settings["ENVIRONMENT_CONFIG"]

    # Initialize services
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])
    repo = FirestoreFactRepository(db_client, env_config)

    # Direct query for facts containing "glove" or "перчатки"
    print("\n🔍 Querying for facts containing 'glove'...")
    docs = repo.facts_col.where("text", ">=", "glove").where("text", "<=", "glove" + "\uf8ff").stream()
    count = 0
    async for doc in docs:
        data = doc.to_dict()
        text = data.get('text', '')
        if 'glove' in text.lower() or 'перчатки' in text.lower():
            count += 1
            print(f"✅ Found: {text}")
            if count >= 5:  # Show first 5
                break

    if count == 0:
        print("❌ No facts found containing 'glove' or 'перчатки'")

    # Also check by ID
    print(f"\n🔍 Checking specific fact ID 'fact_bio_002'...")
    fact = await repo.get_fact_by_id("fact_bio_002")
    if fact:
        print(f"✅ Found fact_bio_002: {fact.text}")
        print(f"   Tags: {fact.tags}")
        print(f"   Owner: {fact.owner_id}")
        print(f"   Current: {fact.is_current}")
    else:
        print("❌ fact_bio_002 not found")

if __name__ == "__main__":
    asyncio.run(main())

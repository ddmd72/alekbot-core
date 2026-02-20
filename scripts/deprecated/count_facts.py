#!/usr/bin/env python3
"""
Count facts in production vs development collections.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings

async def main():
    print("🔢 Counting facts in collections...")

    # Load settings
    settings = load_settings()

    # Initialize services
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])

    # Count production facts
    prod_facts_col = db_client.collection('facts')
    prod_count = 0
    prod_owner_count = 0

    print("🔍 Counting production facts...")
    async for doc in prod_facts_col.stream():
        prod_count += 1
        data = doc.to_dict()
        if data.get('owner_id') == 'U_DMYTRO_CORE':
            prod_owner_count += 1

    print(f"📊 PRODUCTION 'facts': {prod_count} total, {prod_owner_count} for U_DMYTRO_CORE")

    # Count development facts
    dev_facts_col = db_client.collection('development_facts')
    dev_count = 0
    dev_owner_count = 0

    print("🔍 Counting development facts...")
    async for doc in dev_facts_col.stream():
        dev_count += 1
        data = doc.to_dict()
        if data.get('owner_id') == 'U_DMYTRO_CORE':
            dev_owner_count += 1

    print(f"📊 DEVELOPMENT 'development_facts': {dev_count} total, {dev_owner_count} for U_DMYTRO_CORE")

    if prod_owner_count != dev_owner_count:
        print(f"⚠️ MISMATCH! Production has {prod_owner_count}, Development has {dev_owner_count}")
    else:
        print("✅ Counts match!")

if __name__ == "__main__":
    asyncio.run(main())

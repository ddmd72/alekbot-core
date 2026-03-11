#!/usr/bin/env python3
"""
Check if glove data exists in PRODUCTION facts collection.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings

async def main():
    print("🔍 Checking glove data in PRODUCTION facts collection...")

    # Load settings
    settings = load_settings()

    # Initialize services
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])

    # Check production facts collection directly
    prod_facts_col = db_client.collection('facts')

    print("🔍 Querying production facts for glove data...")

    # Get all facts and search for glove data
    docs = prod_facts_col.where("owner_id", "==", "YOUR_USER_ID").where("is_current", "==", True).stream()

    found_glove_facts = []
    async for doc in docs:
        data = doc.to_dict()
        text = data.get('text', '')
        if 'glove' in text.lower() or 'перчатки' in text.lower():
            found_glove_facts.append({
                'id': doc.id,
                'text': text,
                'tags': data.get('tags', [])
            })

    if found_glove_facts:
        print(f"✅ Found {len(found_glove_facts)} glove facts in PRODUCTION:")
        for fact in found_glove_facts:
            print(f"  ID: {fact['id']}")
            print(f"  Text: {fact['text']}")
            print(f"  Tags: {fact['tags']}")
            print()
    else:
        print("❌ No glove facts found in PRODUCTION collection")

    # Check specific fact
    print("🔍 Checking specific fact ID 'fact_bio_002' in production...")
    doc = await prod_facts_col.document("fact_bio_002").get()
    if doc.exists:
        data = doc.to_dict()
        print(f"✅ Found fact_bio_002: {data.get('text', '')}")
        print(f"   Owner: {data.get('owner_id', '')}")
        print(f"   Current: {data.get('is_current', '')}")
    else:
        print("❌ fact_bio_002 not found in production")

if __name__ == "__main__":
    asyncio.run(main())

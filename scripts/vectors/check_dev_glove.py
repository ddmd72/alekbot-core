#!/usr/bin/env python3
"""
Direct check for glove data in development_facts collection.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings

async def main():
    print("🔍 Direct check for glove data in development_facts...")

    # Load settings
    settings = load_settings()

    # Initialize services
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])

    # Check development facts collection directly
    dev_facts_col = db_client.collection('development_facts')

    print("🔍 Querying development facts for glove data...")

    # Get all facts for U_DMYTRO_CORE and search for glove data
    docs = dev_facts_col.where("owner_id", "==", "U_DMYTRO_CORE").where("is_current", "==", True).stream()

    found_glove_facts = []
    total_facts = 0
    async for doc in docs:
        total_facts += 1
        data = doc.to_dict()
        text = data.get('text', '')
        if 'glove' in text.lower() or 'перчатки' in text.lower():
            found_glove_facts.append({
                'id': doc.id,
                'text': text,
                'tags': data.get('tags', [])
            })

    print(f"📊 Total facts for U_DMYTRO_CORE in development: {total_facts}")

    if found_glove_facts:
        print(f"✅ Found {len(found_glove_facts)} glove facts in DEVELOPMENT:")
        for fact in found_glove_facts:
            print(f"  ID: {fact['id']}")
            print(f"  Text: {fact['text']}")
            print(f"  Tags: {fact['tags']}")
            print()
    else:
        print("❌ No glove facts found in DEVELOPMENT collection")

    # Check the specific document ID from production
    print("🔍 Checking specific document ID 'e58f31e6-ce42-4036-87e6-b11be4492b28' in development...")
    doc = await dev_facts_col.document("e58f31e6-ce42-4036-87e6-b11be4492b28").get()
    if doc.exists:
        data = doc.to_dict()
        print(f"✅ Found document: {data.get('text', '')}")
    else:
        print("❌ Document not found in development")

if __name__ == "__main__":
    asyncio.run(main())

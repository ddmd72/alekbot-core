#!/usr/bin/env python3
"""
Direct check for a single fact in the development_facts collection.

Usage: OWNER_ID=<uuid> FACT_ID=<uuid> FACT_KEYWORD=<word> python scripts/vectors/check_dev_fact.py
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings

async def main():
    print("🔍 Direct check for fact data in development_facts...")

    # Load settings
    settings = load_settings()

    # Initialize services
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])

    # Check development facts collection directly
    dev_facts_col = db_client.collection('development_facts')

    print(f"🔍 Querying development facts for keyword {keyword!r}...")

    owner_id = os.getenv("OWNER_ID") or "<owner-uuid>"
    keyword = (os.getenv("FACT_KEYWORD") or "").lower()

    # Get all facts for the owner and search for the keyword
    docs = dev_facts_col.where("owner_id", "==", owner_id).where("is_current", "==", True).stream()

    found_facts = []
    total_facts = 0
    async for doc in docs:
        total_facts += 1
        data = doc.to_dict()
        text = data.get('text', '')
        if keyword and keyword in text.lower():
            found_facts.append({
                'id': doc.id,
                'text': text,
                'tags': data.get('tags', [])
            })

    print(f"📊 Total facts for owner in development: {total_facts}")

    if found_facts:
        print(f"✅ Found {len(found_facts)} matching facts in DEVELOPMENT:")
        for fact in found_facts:
            print(f"  ID: {fact['id']}")
            print(f"  Text: {fact['text']}")
            print(f"  Tags: {fact['tags']}")
            print()
    else:
        print("❌ No matching facts found in DEVELOPMENT collection")

    # Check the specific document ID from production
    fact_id = os.getenv("FACT_ID") or "<fact-uuid>"
    print(f"🔍 Checking specific document ID {fact_id!r} in development...")
    doc = await dev_facts_col.document(fact_id).get()
    if doc.exists:
        data = doc.to_dict()
        print(f"✅ Found document: {data.get('text', '')}")
    else:
        print("❌ Document not found in development")

if __name__ == "__main__":
    asyncio.run(main())

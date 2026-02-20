#!/usr/bin/env python3
"""
Count documents with and without vectors in production.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings
from google.cloud import firestore

async def main():
    print("📊 Analyzing PRODUCTION vectors...")

    # Load settings
    settings = load_settings()

    # Initialize services
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])
    prod_facts_col = db_client.collection('facts')

    total = 0
    with_vector = 0
    without_vector = 0
    
    docs = prod_facts_col.where("owner_id", "==", "U_DMYTRO_CORE").where("is_current", "==", True).stream()
    
    async for doc in docs:
        total += 1
        data = doc.to_dict()
        if 'vector' in data and data['vector'] is not None:
            with_vector += 1
        else:
            without_vector += 1
            if without_vector <= 5:
                print(f"   Missing vector: {data.get('text', '')[:100]}...")

    print(f"\n📊 Results for PRODUCTION:")
    print(f"   Total active documents: {total}")
    print(f"   With vector: {with_vector}")
    print(f"   Without vector: {without_vector}")
    
    if total > 0:
        print(f"   Coverage: {with_vector/total*100:.1f}%")

if __name__ == "__main__":
    asyncio.run(main())

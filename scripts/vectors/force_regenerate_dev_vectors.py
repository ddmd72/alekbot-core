#!/usr/bin/env python3
"""
Force regenerate ALL vectors in development_facts using RETRIEVAL_DOCUMENT.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings
from src.services.embedding_service import EmbeddingService
from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector

async def main():
    print("🔄 Force regenerating ALL vectors in development_facts...")

    # Load settings
    settings = load_settings()

    # Initialize services
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])
    embedding = EmbeddingService(settings["GEMINI_API_KEY"])

    # Get development facts collection
    dev_facts_col = db_client.collection('development_facts')

    print("🔍 Fetching all documents from development_facts...")
    docs = []
    async for doc in dev_facts_col.stream():
        docs.append(doc)

    print(f"📊 Found {len(docs)} documents to process.")

    # Regenerate vectors
    fixed = 0
    for doc in docs:
        try:
            data = doc.to_dict()
            text = data.get('text', '')
            if not text:
                continue

            print(f"   -> Processing: {text[:60]}...")

            # Generate new vector with RETRIEVAL_DOCUMENT (now default)
            new_vector = await embedding.get_embedding(text, task_type="RETRIEVAL_DOCUMENT")
            
            # Update document
            await dev_facts_col.document(doc.id).update({
                'vector': Vector(new_vector)
            })

            fixed += 1
            if fixed % 10 == 0:
                print(f"      ✅ Updated {fixed}/{len(docs)} documents...")

        except Exception as e:
            print(f"      ❌ Failed for {doc.id}: {e}")

    print(f"\n🎉 Force regeneration complete!")
    print(f"   Updated: {fixed}/{len(docs)} documents")

if __name__ == "__main__":
    asyncio.run(main())

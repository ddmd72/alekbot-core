#!/usr/bin/env python3
"""
Regenerate missing vectors for documents in development_facts collection.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings
from src.services.embedding_service import EmbeddingService

async def main():
    print("🔄 Regenerating missing vectors in development_facts...")

    # Load settings
    settings = load_settings()

    # Initialize services
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])
    embedding = EmbeddingService(settings["GEMINI_API_KEY"])

    # Get development facts collection
    dev_facts_col = db_client.collection('development_facts')

    print("🔍 Finding documents with missing vectors...")
    docs_to_fix = []
    total_checked = 0

    async for doc in dev_facts_col.stream():
        total_checked += 1
        data = doc.to_dict()

        # Check if vector is missing or None
        has_vector = 'vector' in data
        vector_is_none = has_vector and data['vector'] is None
        vector_empty = has_vector and hasattr(data['vector'], '__len__') and len(data['vector']) == 0

        if not has_vector or vector_is_none or vector_empty:
            docs_to_fix.append({
                'id': doc.id,
                'text': data.get('text', ''),
                'current_vector': data.get('vector')
            })

    print(f"📊 Checked {total_checked} documents")
    print(f"🔧 Found {len(docs_to_fix)} documents needing vector regeneration")

    if not docs_to_fix:
        print("✅ All documents already have vectors!")
        return

    # Regenerate vectors
    print("\n🔄 Regenerating vectors...")
    fixed = 0

    for doc_info in docs_to_fix:
        try:
            print(f"   -> Regenerating for: {doc_info['text'][:60]}...")

            # Generate new vector
            new_vector = await embedding.get_embedding(doc_info['text'])
            print(f"      ✅ Generated vector (length: {len(new_vector)})")

            # Update document
            await dev_facts_col.document(doc_info['id']).update({
                'vector': new_vector
            })

            fixed += 1
            print(f"      ✅ Updated document {doc_info['id']}")

        except Exception as e:
            print(f"      ❌ Failed to regenerate vector for {doc_info['id']}: {e}")

    print(f"\n🎉 Vector regeneration complete!")
    print(f"   Fixed: {fixed}/{len(docs_to_fix)} documents")
    print(f"   Success rate: {fixed/len(docs_to_fix)*100:.1f}%")

    # Verification
    print("\n🔍 Verification: checking glove document...")
    glove_doc_id = "e58f31e6-ce42-4036-87e6-b11be4492b28"
    doc = await dev_facts_col.document(glove_doc_id).get()

    if doc.exists:
        data = doc.to_dict()
        has_vector = 'vector' in data
        vector_length = len(data['vector']) if has_vector and data['vector'] else 0
        print(f"   Glove document: vector={'✅ EXISTS' if has_vector and data['vector'] else '❌ MISSING'} (length: {vector_length})")

if __name__ == "__main__":
    asyncio.run(main())

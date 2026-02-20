#!/usr/bin/env python3
"""
Check if glove document has vector data.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings

async def main():
    print("🔍 Checking vector data for glove document...")

    # Load settings
    settings = load_settings()

    # Initialize services
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])

    # Check the glove document
    dev_facts_col = db_client.collection('development_facts')
    doc_id = "e58f31e6-ce42-4036-87e6-b11be4492b28"

    print(f"🔍 Checking document {doc_id}...")
    doc = await dev_facts_col.document(doc_id).get()

    if doc.exists:
        data = doc.to_dict()
        print(f"✅ Document exists")
        print(f"   Text: {data.get('text', '')[:100]}...")
        print(f"   Owner: {data.get('owner_id', '')}")
        print(f"   Current: {data.get('is_current', '')}")
        print(f"   Has vector: {'vector' in data}")
        if 'vector' in data:
            vector = data['vector']
            if hasattr(vector, '__len__'):
                print(f"   Vector length: {len(vector)}")
                print(f"   Vector sample: {vector[:5]}...")
            else:
                print(f"   Vector type: {type(vector)}")
        else:
            print("❌ NO VECTOR DATA!")
    else:
        print("❌ Document not found")

    # Also test a simple vector search query manually
    print("\n🔍 Testing vector search with known working query...")
    try:
        from src.services.embedding_service import EmbeddingService
        embedding = EmbeddingService(settings["GEMINI_API_KEY"])
        test_vector = await embedding.get_embedding("gloves size")
        print(f"✅ Generated test embedding, length: {len(test_vector)}")

        # Try vector search
        vector_query = dev_facts_col.where("owner_id", "==", "U_DMYTRO_CORE").where("is_current", "==", True).find_nearest(
            vector_field="vector",
            query_vector=test_vector,
            distance_measure="COSINE",
            limit=3
        )

        results = vector_query.stream()
        count = 0
        async for result in results:
            count += 1
            result_data = result.to_dict()
            print(f"   Result {count}: {result_data.get('text', '')[:80]}...")

        if count == 0:
            print("❌ Vector search returned no results")

    except Exception as e:
        print(f"❌ Vector search failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
Direct test of vector search for glove data.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings
from src.services.embedding_service import EmbeddingService

async def main():
    print("🔍 Testing vector search directly...")

    # Load settings
    settings = load_settings()

    # Initialize services
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])
    embedding = EmbeddingService(settings["GEMINI_API_KEY"])

    # Test queries
    queries = [
        "glove size",
        "размер перчаток",
        "My glove size is 9 or 10",
        "перчатки размер"
    ]

    for query in queries:
        print(f"\n🔍 Testing query: '{query}'")

        # Generate embedding
        query_vector = await embedding.get_embedding(query)
        print(f"   Generated embedding (length: {len(query_vector)})")

        # Direct vector search
        dev_facts_col = db_client.collection('development_facts')

        from google.cloud.firestore_v1.base_vector_query import DistanceMeasure

        vector_query = dev_facts_col.where("owner_id", "==", "U_DMYTRO_CORE").where("is_current", "==", True).find_nearest(
            vector_field="vector",
            query_vector=query_vector,
            distance_measure=DistanceMeasure.COSINE,
            limit=10  # Get more results to see if glove data is there
        )

        results = vector_query.stream()
        found_glove = False
        count = 0

        async for result in results:
            count += 1
            data = result.to_dict()
            text = data.get('text', '')

            # Check if this is the glove document
            if 'glove size is 9 or 10' in text.lower() or 'размер перчаток' in text.lower():
                found_glove = True
                print(f"   ✅ FOUND GLOVE DATA (rank {count}): {text[:100]}...")
            elif count <= 3:  # Show first 3 results
                print(f"   Result {count}: {text[:80]}...")

        if not found_glove:
            print("   ❌ Glove data NOT found in top 10 results")
        else:
            print("   ✅ Glove data found!")

        print(f"   Total results checked: {count}")

if __name__ == "__main__":
    asyncio.run(main())

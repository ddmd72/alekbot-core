#!/usr/bin/env python3
"""
Test vector search on PRODUCTION facts collection.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings
from src.services.embedding_service import EmbeddingService
from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

async def main():
    print("🔍 Testing PRODUCTION vector search...")

    # Load settings
    settings = load_settings()

    # Initialize services
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])
    embedding = EmbeddingService(settings["GEMINI_API_KEY"])

    # Test queries
    queries = [
        "какая марка моего авто",
        "размер перчаток",
        "Toyota Corolla",
        "glove size"
    ]

    prod_facts_col = db_client.collection('facts')

    for query in queries:
        print(f"\n🔍 Testing query: '{query}'")

        # Generate embedding
        query_vector = await embedding.get_embedding(query)
        print(f"   Generated embedding (length: {len(query_vector)})")

        # Direct vector search on PRODUCTION
        vector_query = prod_facts_col.where("owner_id", "==", "YOUR_USER_ID").where("is_current", "==", True).find_nearest(
            vector_field="vector",
            query_vector=Vector(query_vector),
            distance_measure=DistanceMeasure.COSINE,
            limit=5
        )

        results = vector_query.stream()
        count = 0
        found_relevant = False

        async for result in results:
            count += 1
            data = result.to_dict()
            text = data.get('text', '')
            
            # Check if relevant to car or gloves
            is_car = any(kw in text.lower() for kw in ['toyota', 'corolla', 'авто', 'машина', 'car'])
            is_glove = any(kw in text.lower() for kw in ['glove', 'перчат'])
            
            if is_car or is_glove:
                found_relevant = True
                print(f"   ✅ FOUND RELEVANT (rank {count}): {text[:100]}...")
            else:
                print(f"   Result {count}: {text[:80]}...")

        if not found_relevant:
            print("   ❌ No relevant data found in top 5 results")
        
        print(f"   Total results checked: {count}")

if __name__ == "__main__":
    asyncio.run(main())

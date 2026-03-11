#!/usr/bin/env python3
"""
Test vector search for glove size in development_facts.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings
from src.services.embedding_service import EmbeddingService
from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure

async def main():
    print("🔍 Testing GLOVE vector search in DEVELOPMENT...")

    # Load settings
    settings = load_settings()

    # Initialize services
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])
    embedding = EmbeddingService(settings["GEMINI_API_KEY"])

    # Get development facts collection
    dev_facts_col = db_client.collection('development_facts')

    queries = [
        "размер перчаток",
        "glove size",
        "what is my glove size",
        "какой у меня размер перчаток"
    ]

    for query_text in queries:
        print(f"\n🔍 Query: '{query_text}'")
        
        # Generate query vector (uses RETRIEVAL_QUERY by default now)
        query_vector = await embedding.get_embedding(query_text, task_type="RETRIEVAL_QUERY")
        
        vector_query = dev_facts_col.where("owner_id", "==", "YOUR_USER_ID").where("is_current", "==", True).find_nearest(
            vector_field="vector",
            query_vector=Vector(query_vector),
            distance_measure=DistanceMeasure.COSINE,
            limit=5,
            distance_result_field="vector_distance"
        )
        
        results = vector_query.stream()
        found = False
        count = 0
        async for result in results:
            count += 1
            data = result.to_dict()
            text = data.get('text', '')
            dist = data.get('vector_distance')
            
            if 'glove' in text.lower() or 'перчат' in text.lower():
                print(f"  ✅ FOUND (rank {count}, dist {dist:.4f}): {text[:100]}...")
                found = True
            else:
                print(f"  Result {count} (dist {dist:.4f}): {text[:80]}...")

        if not found:
            print(f"  ❌ Glove data NOT found in top 5 results")

if __name__ == "__main__":
    asyncio.run(main())

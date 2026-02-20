#!/usr/bin/env python3
"""
Debug production vectors and model consistency without numpy.
"""
import asyncio
import sys
import os
import math
sys.path.append('src')

from src.config.settings import load_settings
from src.services.embedding_service import EmbeddingService
from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector

def dot_product(v1, v2):
    return sum(x * y for x, y in zip(v1, v2))

def magnitude(v):
    return math.sqrt(sum(x * x for x in v))

def cosine_similarity(v1, v2):
    mag1 = magnitude(v1)
    mag2 = magnitude(v2)
    if mag1 == 0 or mag2 == 0:
        return 0
    return dot_product(v1, v2) / (mag1 * mag2)

async def main():
    print("🧪 Debugging PRODUCTION vectors...")

    # Load settings
    settings = load_settings()

    # Initialize services
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])
    embedding = EmbeddingService(settings["GEMINI_API_KEY"])

    # 1. Find the Mitsubishi document in production
    print("\n1. Fetching Mitsubishi document from production...")
    prod_facts_col = db_client.collection('facts')
    query = prod_facts_col.where("owner_id", "==", "U_DMYTRO_CORE").where("is_current", "==", True)
    docs = query.stream()
    
    target_doc = None
    async for doc in docs:
        data = doc.to_dict()
        if 'mitsubishi' in data.get('text', '').lower():
            target_doc = data
            target_id = doc.id
            break
    
    if not target_doc:
        print("❌ Could not find Mitsubishi document in production!")
        return

    print(f"✅ Found document: {target_id}")
    print(f"   Text: {target_doc['text'][:100]}...")
    
    stored_vector = target_doc.get('vector')
    if not stored_vector:
        print("❌ Document has NO vector in production!")
        return
    
    # Convert Vector object to list if necessary
    if hasattr(stored_vector, '__iter__'): # It's a Vector object or list
        stored_vector = list(stored_vector)
    elif isinstance(stored_vector, dict) and 'values' in stored_vector:
        stored_vector = stored_vector['values']

    print(f"   Stored vector length: {len(stored_vector)}")

    # 2. Generate new vector for the same text
    print("\n2. Generating new vector with current model (text-embedding-004)...")
    new_vector = await embedding.get_embedding(target_doc['text'])
    print(f"   New vector length: {len(new_vector)}")

    # 3. Compare vectors
    similarity = cosine_similarity(stored_vector, new_vector)
    print(f"\n3. Comparison Result:")
    print(f"   Cosine Similarity: {similarity:.4f}")
    
    if similarity > 0.95:
        print("   ✅ Vectors are consistent. Model is likely the same.")
    else:
        print("   ❌ VECTORS ARE DIFFERENT! Model mismatch detected.")
        print("   This is why vector search is failing.")

    # 4. Test search with distance
    print("\n4. Testing search with distance metadata...")
    from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
    
    query_text = "Mitsubishi Colt"
    query_vector = await embedding.get_embedding(query_text)
    
    vector_query = prod_facts_col.where("owner_id", "==", "U_DMYTRO_CORE").where("is_current", "==", True).find_nearest(
        vector_field="vector",
        query_vector=Vector(query_vector),
        distance_measure=DistanceMeasure.COSINE,
        limit=5,
        distance_result_field="vector_distance"
    )
    
    results = vector_query.stream()
    async for result in results:
        data = result.to_dict()
        dist = data.get('vector_distance')
        text = data.get('text', '')
        print(f"   Distance: {dist:.4f} | Text: {text[:80]}...")

if __name__ == "__main__":
    asyncio.run(main())

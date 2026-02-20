#!/usr/bin/env python3
"""
Check if car data exists and works with vector search.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings

async def main():
    print("🚗 Checking car data in collections...")

    # Load settings
    settings = load_settings()

    # Check production facts for car data
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])

    # Production
    prod_facts_col = db_client.collection('facts')
    docs = prod_facts_col.where("owner_id", "==", "U_DMYTRO_CORE").where("is_current", "==", True).stream()

    prod_car_facts = []
    async for doc in docs:
        data = doc.to_dict()
        text = data.get('text', '')
        if any(keyword in text.lower() for keyword in ['mitsubishi', 'vw', 'passat', 'colt', 'авто', 'машина', 'car']):
            prod_car_facts.append({
                'id': doc.id,
                'text': text,
                'tags': data.get('tags', [])
            })

    print(f"📊 PRODUCTION - Found {len(prod_car_facts)} car facts:")
    for fact in prod_car_facts:
        print(f"  ID: {fact['id']}")
        print(f"  Text: {fact['text']}")
        print(f"  Tags: {fact['tags']}")
        print()

    # Development
    dev_facts_col = db_client.collection('development_facts')
    docs = dev_facts_col.where("owner_id", "==", "U_DMYTRO_CORE").where("is_current", "==", True).stream()

    dev_car_facts = []
    async for doc in docs:
        data = doc.to_dict()
        text = data.get('text', '')
        if any(keyword in text.lower() for keyword in ['mitsubishi', 'vw', 'passat', 'colt', 'авто', 'машина', 'car']):
            dev_car_facts.append({
                'id': doc.id,
                'text': text,
                'tags': data.get('tags', []),
                'has_vector': 'vector' in data and data['vector'] is not None
            })

    print(f"📊 DEVELOPMENT - Found {len(dev_car_facts)} car facts:")
    for fact in dev_car_facts:
        print(f"  ID: {fact['id']}")
        print(f"  Text: {fact['text']}")
        print(f"  Tags: {fact['tags']}")
        print(f"  Vector: {'✅' if fact['has_vector'] else '❌'}")
        print()

    if not dev_car_facts:
        print("❌ No car data found in development!")
        return

    # Test vector search
    print("🔍 Testing vector search for car queries...")

    from src.services.embedding_service import EmbeddingService
    from google.cloud.firestore_v1.base_vector_query import DistanceMeasure

    embedding = EmbeddingService(settings["GEMINI_API_KEY"])

    queries = [
        "какая марка моего авто",
        "what car do I drive",
        "Mitsubishi Colt",
        "VW Passat"
    ]

    for query in queries:
        print(f"\n🔍 Query: '{query}'")
        query_vector = await embedding.get_embedding(query)

        vector_query = dev_facts_col.where("owner_id", "==", "U_DMYTRO_CORE").where("is_current", "==", True).find_nearest(
            vector_field="vector",
            query_vector=query_vector,
            distance_measure=DistanceMeasure.COSINE,
            limit=5
        )

        results = vector_query.stream()
        found_car = False
        count = 0

        async for result in results:
            count += 1
            data = result.to_dict()
            text = data.get('text', '')

            if any(keyword in text.lower() for keyword in ['mitsubishi', 'vw', 'passat', 'colt']):
                found_car = True
                print(f"  ✅ FOUND CAR DATA (rank {count}): {text}")
            elif count <= 2:
                print(f"  Result {count}: {text[:60]}...")

        if not found_car:
            print("  ❌ Car data NOT found in top 5 results")
        print(f"  Total results: {count}")

if __name__ == "__main__":
    asyncio.run(main())

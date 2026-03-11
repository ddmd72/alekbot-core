#!/usr/bin/env python3
"""
Quick script to check if glove size data exists in development_facts collection.
"""
import asyncio
import sys
import os
sys.path.append('src')

from src.config.settings import load_settings
from src.adapters.firestore_repo import FirestoreFactRepository
from src.services.embedding_service import EmbeddingService
from src.tools.memory_search_tool import MemorySearchTool

async def main():
    print("🔍 Checking glove data in development_facts...")

    # Load settings
    settings = load_settings()
    env_config = settings["ENVIRONMENT_CONFIG"]
    print(f"📊 Environment: {settings['APP_ENV']}")
    print(f"📊 Collection prefix: {env_config.firestore_collection_prefix}")

    # Initialize services
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=settings["GOOGLE_CLOUD_PROJECT"])
    repo = FirestoreFactRepository(db_client, env_config)
    embedding = EmbeddingService(settings["GEMINI_API_KEY"])

    # Create search tool
    search_tool = MemorySearchTool(repo, embedding, "YOUR_USER_ID")

    # Test search
    queries = [
        "gloves",
        "перчатки",
        "размер перчаток",
        "glove size"
    ]

    for query in queries:
        print(f"\n🔍 Searching for: '{query}'")
        try:
            results = await search_tool.execute(query=query)
            print(f"✅ Found {len(results)} results:")
            for i, result in enumerate(results[:3]):  # Show first 3
                print(f"  [{i+1}] {result[:100]}...")
        except Exception as e:
            print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())

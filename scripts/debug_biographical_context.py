#!/usr/bin/env python3
"""
Debug script to check biographical context for a user.
Checks both cache and actual facts in Firestore.
"""

import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.adapters.firestore_repo import FirestoreFactRepository

async def debug_biographical_context(user_id: str):
    """Check biographical context cache and actual facts."""
    print(f"\n{'='*70}")
    print(f"🔍 DEBUGGING BIOGRAPHICAL CONTEXT FOR USER: {user_id}")
    print(f"{'='*70}\n")
    
    # Load config
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    print(f"Environment: {env_config.env.value}")
    print(f"Prefix: {env_config.firestore_collection_prefix}")
    
    # Create Firestore client
    db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    # Create repository
    repo = FirestoreFactRepository(db_client, env_config)
    await repo.initialize()
    
    print(f"\n{'='*70}")
    print(f"1. CHECKING CACHE (user_context collection)")
    print(f"{'='*70}\n")
    
    # Check cache document
    prefix = env_config.firestore_collection_prefix
    oauth_suffix = "_oauth" if env_config.use_oauth_collections else ""
    user_context_col = db_client.collection(f"{prefix}user_context{oauth_suffix}")
    
    cache_doc = await user_context_col.document(user_id).get()
    
    if cache_doc.exists:
        data = cache_doc.to_dict()
        facts = data.get("biographical_facts", [])
        print(f"✅ Cache document EXISTS")
        print(f"   Facts count: {data.get('facts_count', 0)}")
        print(f"   Last updated: {data.get('last_updated')}")
        print(f"   Version: {data.get('version')}")
        print(f"   Actual array length: {len(facts)}")
        
        if facts:
            print(f"\n   First 3 facts:")
            for i, fact in enumerate(facts[:3]):
                print(f"   {i+1}. {fact.get('text', 'NO TEXT')[:60]}...")
        else:
            print(f"\n   ⚠️  biographical_facts array is EMPTY!")
    else:
        print(f"❌ Cache document DOES NOT EXIST")
    
    print(f"\n{'='*70}")
    print(f"2. CHECKING ACTUAL FACTS (facts collection)")
    print(f"{'='*70}\n")
    
    # Check actual facts with account_id
    facts_col = db_client.collection(env_config.fact_collection_name)
    
    # Try account_id first
    query = facts_col.where("account_id", "==", user_id).where("is_current", "==", True).limit(10)
    docs = await query.get()
    
    print(f"Query 1 (account_id): Found {len(docs)} facts")
    if docs:
        for i, doc in enumerate(docs[:3]):
            data = doc.to_dict()
            print(f"   {i+1}. {data.get('text', 'NO TEXT')[:60]}...")
            print(f"       Tags: {data.get('tags', [])}")
            print(f"       Type: {data.get('type')}")
    
    # Try owner_id (legacy)
    query = facts_col.where("owner_id", "==", user_id).where("is_current", "==", True).limit(10)
    docs = await query.get()
    
    print(f"\nQuery 2 (owner_id - legacy): Found {len(docs)} facts")
    if docs:
        for i, doc in enumerate(docs[:3]):
            data = doc.to_dict()
            print(f"   {i+1}. {data.get('text', 'NO TEXT')[:60]}...")
            print(f"       Tags: {data.get('tags', [])}")
            print(f"       Type: {data.get('type')}")
    
    print(f"\n{'='*70}")
    print(f"3. TESTING get_biographical_context_cached()")
    print(f"{'='*70}\n")
    
    # Test the actual method
    bio_facts = await repo.get_biographical_context_cached(user_id, limit=100)
    
    print(f"Returned: {len(bio_facts)} facts")
    if bio_facts:
        print(f"\nFirst 3:")
        for i, fact in enumerate(bio_facts[:3]):
            print(f"   {i+1}. {fact.get('text', 'NO TEXT')[:60]}...")
    else:
        print(f"⚠️  Method returned EMPTY LIST!")
    
    print(f"\n{'='*70}")
    print(f"✅ DEBUG COMPLETE")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    user_id = os.getenv("DEV_USER_ID") or "os.getenv("USER_ID", "DEMO_USER")"
    asyncio.run(debug_biographical_context(user_id))

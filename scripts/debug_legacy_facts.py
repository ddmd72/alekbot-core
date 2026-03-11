"""Debug script to check legacy facts in Firestore."""
import asyncio
import os
import sys

from google.cloud.firestore import AsyncClient
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig

ACCOUNT_ID = os.getenv("ACCOUNT_ID")
if not ACCOUNT_ID:
    print("Error: ACCOUNT_ID environment variable is required")
    sys.exit(1)

async def main():
    settings = load_settings()
    env_config = EnvironmentConfig()
    db = AsyncClient(
        project=settings.get("GCP_PROJECT_ID"),
        database=env_config.firestore_database_id
    )

    facts_col = db.collection(env_config.domain_facts_collection)

    # Query 1: Count total facts for account
    query1 = facts_col.where("account_id", "==", ACCOUNT_ID).limit(100)
    docs1 = await query1.get()
    print(f"📊 Total facts for account: {len(docs1)}")
    
    # Check first few facts
    for i, doc in enumerate(docs1[:5]):
        data = doc.to_dict()
        has_domain = "domain" in data and data["domain"] is not None
        state = data.get("state", "N/A")
        print(f"   [{i+1}] domain={data.get('domain', 'MISSING')}, state={state}, text={data.get('text', '')[:40]}...")
    
    # Query 2: Count facts without domain field
    # Firestore doesn't support "field not exists" directly, so let's get all and count
    all_facts = []
    async for doc in facts_col.where("account_id", "==", ACCOUNT_ID).stream():
        data = doc.to_dict()
        data['id'] = doc.id
        all_facts.append(data)
    
    print(f"\n📊 Total facts in DB: {len(all_facts)}")
    
    # Count facts without domain
    no_domain = [f for f in all_facts if "domain" not in f or f["domain"] is None]
    print(f"📊 Facts without domain: {len(no_domain)}")
    
    # Count facts by state
    from collections import Counter
    states = Counter(f.get("state", "MISSING") for f in all_facts)
    print(f"\n📊 Facts by state:")
    for state, count in states.items():
        print(f"   {state}: {count}")
    
    # Show some facts without domain
    if no_domain:
        print(f"\n📋 Sample legacy facts (no domain):")
        for i, fact in enumerate(no_domain[:5]):
            print(f"   [{i+1}] state={fact.get('state', 'N/A')}, text={fact.get('text', '')[:60]}...")
    else:
        print("\n✅ All facts have domain field!")
        print("\n🔍 Let's check for other criteria...")
        
        # Maybe they're looking for facts with state != CURRENT?
        non_current = [f for f in all_facts if f.get("state") not in ["CURRENT", "current"]]
        print(f"📊 Facts with state != CURRENT: {len(non_current)}")
        
        if non_current:
            print(f"\n📋 Sample non-current facts:")
            for i, fact in enumerate(non_current[:5]):
                print(f"   [{i+1}] state={fact.get('state')}, domain={fact.get('domain')}, text={fact.get('text', '')[:60]}...")

if __name__ == "__main__":
    asyncio.run(main())

import asyncio
from google.cloud import firestore
from src.config.settings import load_settings

async def check_vectors():
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    prefix = env_config.firestore_collection_prefix
    
    print(f"🔍 Checking for facts with vector: null in {prefix}facts...")
    
    facts_col = db.collection(f"{prefix}facts")
    
    # We can't query directly for vector == null effectively without an index
    # So we'll just scan the last 100 docs
    docs = await facts_col.order_by("created_at", direction=firestore.Query.DESCENDING).limit(100).get()
    
    null_vectors = []
    user_facts_checked = 0
    system_facts_checked = 0
    
    for doc in docs:
        d = doc.to_dict()
        owner = d.get("owner_id", "unknown")
        if owner == "SYSTEM":
            system_facts_checked += 1
        else:
            user_facts_checked += 1
            
        if d.get("vector") is None:
            null_vectors.append({
                "id": doc.id,
                "owner": owner,
                "lineage": d.get("lineage_id"),
                "text": d.get("text", "")[:50] + "..."
            })
            
    print(f"📊 Scan results (last 100 docs):")
    print(f"   - User facts checked: {user_facts_checked}")
    print(f"   - System facts checked: {system_facts_checked}")
    print(f"   - Total facts with vector=null: {len(null_vectors)}")
    
    if null_vectors:
        print("\n❌ List of documents with null vectors:")
        for v in null_vectors:
            print(f"   • [{v['owner']}] ID: {v['id']} | Lineage: {v['lineage']} | {v['text']}")
    else:
        print("\n✅ All checked user facts have vectors!")

if __name__ == "__main__":
    asyncio.run(check_vectors())

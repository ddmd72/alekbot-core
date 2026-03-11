import asyncio
import time
from datetime import datetime
from google.cloud import firestore
from src.config.settings import load_settings

async def inspect_data():
    print("🔍 [Firestore Inspector] Starting...")
    
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    prefix = env_config.firestore_collection_prefix
    
    print(f"🟢 Environment: {env_config.env.value.upper()}")
    print(f"📋 Project: {config['GOOGLE_CLOUD_PROJECT']}")
    print("-" * 50)

    # 1. Inspect Consolidation Queue
    print(f"\n📦 CONSOLIDATION QUEUE (Collection: {prefix}consolidation_queue)")
    queue_col = db.collection(f"{prefix}consolidation_queue")
    # Order by created_at DESC
    query = queue_col.order_by("created_at", direction=firestore.Query.DESCENDING).limit(10)
    
    try:
        docs = await query.get()
        if not docs:
            print("   (Empty)")
        for doc in docs:
            d = doc.to_dict()
            status_icon = "✅" if d.get("status") == "completed" else "⏳" if d.get("status") == "processing" else "❌"
            created = datetime.fromtimestamp(d.get("created_at", 0)).strftime("%Y-%m-%d %H:%M:%S")
            processed = datetime.fromtimestamp(d.get("processed_at", 0)).strftime("%H:%M:%S") if d.get("processed_at") else "N/A"
            
            print(f"   {status_icon} Batch: {doc.id[:12]}... | Status: {d.get('status')} | Msgs: {len(d.get('messages', []))}")
            print(f"      Created: {created} | Processed: {processed} | Facts Extracted: {d.get('facts_extracted', 0)}")
            if d.get("last_error"):
                print(f"      Error: {d.get('last_error')}")
    except Exception as e:
        print(f"   ⚠️ Error fetching queue: {e} (Maybe index is missing?)")
        # Fallback without ordering
        docs = await queue_col.limit(5).get()
        for doc in docs:
            print(f"   • Batch: {doc.id[:12]}... Status: {doc.to_dict().get('status')}")

    # 2. Inspect Facts
    print(f"\n💡 RECENT FACTS (Collection: {prefix}facts)")
    facts_col = db.collection(f"{prefix}facts")
    query = facts_col.order_by("created_at", direction=firestore.Query.DESCENDING).limit(5)
    
    try:
        docs = await query.get()
        if not docs:
            print("   (Empty)")
        for doc in docs:
            d = doc.to_dict()
            print(f"\n   ✨ DOCUMENT: {doc.id}")
            import json
            
            # Remove vector from display to keep it clean
            if "vector" in d:
                from google.cloud.firestore_v1.vector import Vector
                if isinstance(d["vector"], Vector):
                    # Simply convert to string representation or generic placeholder
                    d["vector"] = "<Vector>"
                elif d["vector"] is None:
                    d["vector"] = "<None>"
                else:
                    d["vector"] = f"<List len={len(d['vector'])}>"
            
            # Handle datetime objects for json.dumps
            def json_serial(obj):
                if isinstance(obj, datetime):
                    return obj.isoformat()
                raise TypeError ("Type %s not serializable" % type(obj))
            
            print(json.dumps(d, indent=6, default=json_serial, ensure_ascii=False))
            print("-" * 20)
    except Exception as e:
        print(f"   ⚠️ Error fetching facts: {e}")

    # 3. Inspect Sessions
    print(f"\n💬 ACTIVE SESSIONS (Collection: {prefix}sessions)")
    sessions_col = db.collection(f"{prefix}sessions")
    query = sessions_col.order_by("last_activity", direction=firestore.Query.DESCENDING).limit(5)
    
    try:
        docs = await query.get()
        if not docs:
            print("   (Empty)")
        for doc in docs:
            d = doc.to_dict()
            last_act = datetime.fromtimestamp(d.get("last_activity", 0)).strftime("%Y-%m-%d %H:%M:%S")
            msg_count = len(d.get("history", []))
            
            print(f"   💬 Session: {doc.id[:12]}... | Msgs: {msg_count} | Last Activity: {last_act}")
            print(f"      Owner: {d.get('owner_id', 'unknown')[:8]}...")
    except Exception as e:
        print(f"   ⚠️ Error fetching sessions: {e}")

    print("\n" + "=" * 50)
    print("✅ Inspection complete.")

if __name__ == "__main__":
    asyncio.run(inspect_data())

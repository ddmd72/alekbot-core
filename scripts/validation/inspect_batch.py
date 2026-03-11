import asyncio
import os
import sys
import json
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from google.cloud import firestore
from src.config.settings import load_settings

async def inspect_batch(batch_id: str):
    """
    Inspect a specific consolidation batch in Firestore.
    """
    print("=" * 80)
    print(f"🔍 INSPECTING BATCH: {batch_id}")
    print("=" * 80)
    
    # Setup
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    db = firestore.AsyncClient(project=config.get("GOOGLE_CLOUD_PROJECT"))
    
    # Collection name
    collection_name = f"{env_config.firestore_collection_prefix}consolidation_queue"
    print(f"📂 Collection: {collection_name}")
    
    # Get document
    doc_ref = db.collection(collection_name).document(batch_id)
    doc = await doc_ref.get()
    
    if not doc.exists:
        print(f"❌ Batch {batch_id} NOT FOUND!")
        return
        
    data = doc.to_dict()
    
    print("\n📊 Batch Metadata:")
    print(f"   • Status: {data.get('status')}")
    print(f"   • Attempts: {data.get('attempts')}")
    print(f"   • Last Error: {data.get('last_error')}")
    print(f"   • Created At: {data.get('created_at')}")
    print(f"   • Processed At: {data.get('processed_at')}")
    
    # Messages
    msgs = data.get('messages', [])
    print(f"\n💬 Messages ({len(msgs)}):")
    for i, msg in enumerate(msgs[:3]):
        role = msg.get('role')
        text = msg.get('parts', [{}])[0].get('text', '')[:50]
        print(f"   {i+1}. {role}: {text}...")
    if len(msgs) > 3:
        print(f"   ... ({len(msgs)-3} more)")
        
    # Context
    ctx = data.get('biographical_context', [])
    print(f"\n📚 Biographical Context ({len(ctx)} facts):")
    
    if ctx:
        # Check size
        ctx_json = json.dumps(ctx)
        size_kb = len(ctx_json.encode('utf-8')) / 1024
        print(f"   • Size: {size_kb:.2f} KB")
        
        # Sample facts
        print("\n   Sample Facts:")
        for i, fact in enumerate(ctx[:5]):
            text = fact.get('text', '')[:70]
            tags = ", ".join(fact.get('tags', [])[:3])
            print(f"   {i+1}. {text}... [Tags: {tags}]")
            
    # Raw JSON dump to file for detailed inspection
    output_file = f"batch_{batch_id}_dump.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    
    print(f"\n💾 Full dump saved to: {output_file}")
    print("=" * 80)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python inspect_batch.py <batch_id>")
        # Default for testing
        batch_id = "batch_c917ccbd6593"
    else:
        batch_id = sys.argv[1]
        
    asyncio.run(inspect_batch(batch_id))

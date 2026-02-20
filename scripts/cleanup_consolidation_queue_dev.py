import asyncio
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from google.cloud import firestore
from src.config.settings import load_settings

async def delete_batch(batch_id: str):
    """
    Deletes a specific batch from the consolidation queue.
    """
    print("=" * 80)
    print(f"🗑️ DELETING BATCH: {batch_id}")
    print("=" * 80)
    
    # Setup
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    if env_config.is_production:
        print("❌ ERROR: This script cannot be run in a production environment.")
        return
        
    db = firestore.AsyncClient(project=config.get("GOOGLE_CLOUD_PROJECT"))
    
    # Collection name
    collection_name = f"{env_config.firestore_collection_prefix}consolidation_queue"
    print(f"📂 Collection: {collection_name}")
    
    # Get document ref and delete
    doc_ref = db.collection(collection_name).document(batch_id)
    
    # Check if exists before deleting
    if not (await doc_ref.get()).exists:
        print(f"✅ Batch {batch_id} already deleted or does not exist.")
        return
        
    await doc_ref.delete()
    print(f"✅ Batch {batch_id} successfully deleted.")
    print("=" * 80)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python delete_batch.py <batch_id>")
    else:
        batch_id_to_delete = sys.argv[1]
        asyncio.run(delete_batch(batch_id_to_delete))

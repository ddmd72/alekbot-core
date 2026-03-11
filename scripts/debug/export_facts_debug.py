import asyncio
import logging
import argparse
import sys
import os
import yaml
from datetime import datetime
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def export_facts_yaml(account_id: str, limit: int = 20):
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    if env_config.use_emulator:
        db = firestore.AsyncClient(project="emulator-project")
    else:
        db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])

    collection_name = env_config.domain_facts_collection
    logger.info(f"📂 Reading from: {collection_name}")
    logger.info(f"🔍 Searching account_id: {account_id}")

    collection = db.collection(collection_name)
    
    # Query facts
    query = collection.where(filter=FieldFilter("account_id", "==", account_id)).limit(limit)
    docs = await query.get()

    if not docs:
        logger.warning("❌ No facts found!")
        return

    logger.info(f"✅ Found {len(docs)} facts. Exporting...")

    facts_list = []
    for doc in docs:
        data = doc.to_dict()
        
        # Serialize specific fields
        fact_export = {
            "id": doc.id,
            "account_id": data.get("account_id"),
            "created_by_user_id": data.get("created_by_user_id"),
            "owner_id": data.get("owner_id"), # Legacy check
            "is_current": data.get("is_current"),
            "text": data.get("text"),
            "has_vector": "vector" in data and data["vector"] is not None,
            "vector_preview": str(data["vector"][:3]) if "vector" in data and data["vector"] else "None",
            "created_at": str(data.get("created_at")),
            "tags": data.get("tags", [])
        }
        facts_list.append(fact_export)

    # Save to YAML
    filename = f"facts_export_{account_id[:8]}.yaml"
    with open(filename, "w", encoding="utf-8") as f:
        yaml.dump(facts_list, f, allow_unicode=True, sort_keys=False)

    logger.info(f"💾 Saved to {filename}")
    
    # Print preview
    print("\n--- Facts Preview ---")
    print(yaml.dump(facts_list[:3], allow_unicode=True, sort_keys=False))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export facts for an account to YAML")
    parser.add_argument("account_id", type=str, help="Account ID to export facts for")
    args = parser.parse_args()

    asyncio.run(export_facts_yaml(args.account_id))

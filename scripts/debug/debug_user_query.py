import asyncio
import logging
import argparse
import sys
import os
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def debug_user_query(platform_id: str):
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    if env_config.use_emulator:
        db = firestore.AsyncClient(project="emulator-project")
    else:
        db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])

    collection_name = env_config.domain_users_collection
    logger.info(f"📂 Reading from: {collection_name}")
    logger.info(f"🔍 Querying platform_identities.slack == {platform_id}")

    collection = db.collection(collection_name)
    query = collection.where(filter=FieldFilter("platform_identities.slack", "==", platform_id)).limit(5)
    
    docs = await query.get()

    if not docs:
        logger.warning("❌ No user found by platform ID!")
        return

    logger.info(f"✅ Found {len(docs)} matching documents")

    for doc in docs:
        data = doc.to_dict()
        logger.info(f"📄 Document ID: {doc.id}")
        logger.info(f"   Email: {data.get('email')} (Type: {type(data.get('email'))})")
        logger.info(f"   Platform ID: {data.get('platform_identities', {}).get('slack')}")
        logger.info(f"   Is Active: {data.get('is_active')}")
        
        # Check raw serialization
        import json
        try:
            # Helper to serialize datetimes
            def serializer(obj):
                if hasattr(obj, 'isoformat'):
                    return obj.isoformat()
                return str(obj)
                
            print(json.dumps(data, default=serializer, indent=2))
        except Exception as e:
            logger.error(f"Serialization error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Debug user query by platform ID")
    parser.add_argument("platform_id", type=str, help="Slack User ID (e.g. U123456)")
    args = parser.parse_args()

    asyncio.run(debug_user_query(args.platform_id))

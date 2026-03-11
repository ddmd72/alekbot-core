import asyncio
import logging
import argparse
import sys
import os
import json
from datetime import datetime
from google.cloud import firestore

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def raw_user_dump(user_id: str):
    """
    Read Firestore document directly without any Pydantic serialization.
    """
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    if env_config.use_emulator:
        db = firestore.AsyncClient(project="emulator-project")
    else:
        # Force correct database
        db = firestore.AsyncClient(
            project=config["GOOGLE_CLOUD_PROJECT"],
            database="us-production"
        )

    # --- DIAGNOSTIC INFO ---
    logger.info("="*40)
    logger.info(f"🌍 PROJECT ID: {db.project}")
    if env_config.use_emulator:
        logger.info(f"🏠 EMULATOR HOST: {os.getenv('FIRESTORE_EMULATOR_HOST')}")
    else:
        logger.info(f"☁️  CLOUD MODE")
    logger.info("="*40)
    # -----------------------

    collection_name = env_config.domain_users_collection
    logger.info(f"📂 RAW Reading from: {collection_name}")
    logger.info(f"🔍 Searching user_id: {user_id}")

    doc_ref = db.collection(collection_name).document(user_id)
    doc = await doc_ref.get()

    if not doc.exists:
        logger.error(f"❌ User {user_id} not found!")
        return

    data = doc.to_dict()
    
    logger.info("-" * 40)
    logger.info(f"EMAIL FIELD: {data.get('email')} (Type: {type(data.get('email'))})")
    logger.info("-" * 40)
    
    # Helper to serialize datetimes
    def serializer(obj):
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return str(obj)
        
    print(json.dumps(data, default=serializer, indent=2))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Raw dump of user document")
    parser.add_argument("user_id", type=str, help="User ID to dump")
    args = parser.parse_args()

    asyncio.run(raw_user_dump(args.user_id))

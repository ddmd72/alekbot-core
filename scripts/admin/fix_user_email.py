import asyncio
import logging
import argparse
import sys
import os
from google.cloud import firestore

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def fix_user_email(user_id: str, email: str = None):
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    if env_config.use_emulator:
        db = firestore.AsyncClient(project="emulator-project")
    else:
        db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])

    collection_name = env_config.domain_users_collection
    logger.info(f"📂 Reading from: {collection_name}")
    logger.info(f"🔍 Searching user_id: {user_id}")

    doc_ref = db.collection(collection_name).document(user_id)
    doc = await doc_ref.get()

    if not doc.exists:
        logger.error(f"❌ User {user_id} not found!")
        return

    data = doc.to_dict()
    current_email = data.get("email")
    logger.info(f"👤 Current email: {current_email}")

    if not email:
        if current_email:
            logger.info("✅ Email exists, no action needed unless --update is used.")
        else:
            logger.warning("⚠️ Email is MISSING!")
        return

    # Update email
    await doc_ref.update({"email": email})
    logger.info(f"✅ Updated email to: {email}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix missing email in UserProfile")
    parser.add_argument("user_id", type=str, help="User ID to fix")
    parser.add_argument("--email", type=str, help="Email to set")
    args = parser.parse_args()

    asyncio.run(fix_user_email(args.user_id, args.email))

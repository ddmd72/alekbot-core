import argparse
import asyncio
import os
import sys
from google.cloud import firestore

# Add project root to path
sys.path.append(os.path.join(os.path.dirname(__file__), "../../"))

from src.config.environment import EnvironmentConfig
from src.config.settings import load_settings
from src.domain.user import UserProfile


async def migrate_llm_providers(env: str, dry_run: bool) -> None:
    os.environ["APP_ENV"] = env

    config = load_settings()
    env_config = EnvironmentConfig()
    project_id = config["GOOGLE_CLOUD_PROJECT"]
    db = firestore.AsyncClient(project=project_id)

    prefix = env_config.firestore_collection_prefix
    users_col = db.collection(f"{prefix}users")

    updated = 0
    async for doc in users_col.stream():
        data = doc.to_dict()
        user = UserProfile(**data)

        changed = False
        if not getattr(user.config, "light_llm_provider", None):
            user.config.light_llm_provider = user.config.smart_llm_provider
            changed = True
        if not getattr(user.config, "smart_llm_provider", None):
            user.config.smart_llm_provider = user.config.light_llm_provider
            changed = True

        if changed:
            updated += 1
            if not dry_run:
                await users_col.document(user.user_id).set(user.model_dump())

    print(f"✅ Migration completed. Updated users: {updated}")
    if dry_run:
        print("(dry-run mode: no writes performed)")


def parse_args():
    parser = argparse.ArgumentParser(description="Migrate users to new light/smart LLM provider fields")
    parser.add_argument("--env", default="production", choices=["development", "production", "test"])
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(migrate_llm_providers(args.env, args.dry_run))
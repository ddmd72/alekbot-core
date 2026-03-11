import argparse
import asyncio
import os
import sys
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

# Add project root to path
sys.path.append(os.path.join(os.path.dirname(__file__), "../../"))

from src.config.environment import EnvironmentConfig
from src.config.settings import load_settings
from src.domain.user import UserProfile


async def update_user_config(
    env: str,
    user_id: str | None,
    slack_id: str | None,
    full_model: str | None,
    light_model: str | None,
    smart_model: str | None,
    llm_provider: str | None,
    light_llm_provider: str | None,
    smart_llm_provider: str | None,
):
    os.environ["APP_ENV"] = env

    config = load_settings()
    env_config = EnvironmentConfig()
    project_id = config["GOOGLE_CLOUD_PROJECT"]
    db = firestore.AsyncClient(project=project_id)

    prefix = env_config.firestore_collection_prefix
    users_col = db.collection(f"{prefix}users")

    doc_ref = None

    if user_id:
        doc_ref = users_col.document(user_id)
        doc = await doc_ref.get()
        if not doc.exists:
            raise ValueError(f"User {user_id} not found in {prefix}users")
        data = doc.to_dict()
    elif slack_id:
        field_path = "platform_identities.slack"
        query = users_col.where(filter=FieldFilter(field_path, "==", slack_id)).limit(1)
        docs = query.stream()
        data = None
        async for doc in docs:
            data = doc.to_dict()
            doc_ref = users_col.document(data["user_id"])
            break
        if not data:
            raise ValueError(f"No user found with Slack ID {slack_id} in {prefix}users")
    else:
        raise ValueError("Provide --user-id or --slack-id")

    user = UserProfile(**data)

    if llm_provider:
        user.config.smart_llm_provider = llm_provider
        user.config.light_llm_provider = llm_provider
    if light_llm_provider:
        user.config.light_llm_provider = light_llm_provider
    if smart_llm_provider:
        user.config.smart_llm_provider = smart_llm_provider
    if full_model:
        user.config.full_model = full_model
    if light_model:
        user.config.light_model = light_model
    if smart_model:
        user.config.smart_model = smart_model

    await doc_ref.set(user.model_dump())

    print("✅ Updated user configuration")
    print(f"   User ID: {user.user_id}")
    print(f"   Slack ID: {user.platform_identities.get('slack')}")
    print(f"   light_llm_provider: {user.config.light_llm_provider}")
    print(f"   smart_llm_provider: {user.config.smart_llm_provider}")
    print(f"   full_model: {user.config.full_model}")
    print(f"   light_model: {user.config.light_model}")
    print(f"   smart_model: {user.config.smart_model}")


def parse_args():
    parser = argparse.ArgumentParser(description="Update user configuration in Firestore")
    parser.add_argument("--env", default="production", choices=["development", "production", "test"])
    parser.add_argument("--user-id", help="User ID to update")
    parser.add_argument("--slack-id", help="Slack user ID to update")
    parser.add_argument("--llm-provider", help="Set both light+smart providers (gemini|anthropic|openai)")
    parser.add_argument("--light-llm-provider", help="Set light_llm_provider (gemini|anthropic|openai)")
    parser.add_argument("--smart-llm-provider", help="Set smart_llm_provider (gemini|anthropic|openai)")
    parser.add_argument("--full-model", help="Set full_model")
    parser.add_argument("--light-model", help="Set light_model")
    parser.add_argument("--smart-model", help="Set smart_model")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        update_user_config(
            args.env,
            args.user_id,
            args.slack_id,
            args.full_model,
            args.light_model,
            args.smart_model,
            args.llm_provider,
            args.light_llm_provider,
            args.smart_llm_provider,
        )
    )

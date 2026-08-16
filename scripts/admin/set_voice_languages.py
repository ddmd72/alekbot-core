"""Set the languages a user may speak in voice messages.

Mirrors PUT /api/user/voice-languages for when the Cabinet UI is not available.
Writes only `config.voice_languages` — a targeted field update, so nothing else on the
user document is round-tripped through the model.

    python scripts/admin/set_voice_languages.py --languages ru uk en
    python scripts/admin/set_voice_languages.py --show
"""
import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from google.cloud import firestore

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# Before argparse defaults are evaluated — load_settings() would load .env too late.
load_dotenv()

from src.config.settings import load_settings  # noqa: E402
from src.domain.language import normalize_voice_languages  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def main(user_id: str, languages: list[str] | None, show: bool) -> int:
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]

    db = firestore.AsyncClient(
        project=config["GOOGLE_CLOUD_PROJECT"],
        database=os.getenv("FIRESTORE_DATABASE", "us-production"),
    )
    collection = env_config.domain_users_collection
    doc_ref = db.collection(collection).document(user_id)

    doc = await doc_ref.get()
    if not doc.exists:
        logger.error(f"❌ User {user_id[:8]}… not found in {collection}")
        return 1

    current = (doc.to_dict().get("config") or {}).get("voice_languages")
    logger.info(f"📂 {collection} / {user_id[:8]}…")
    logger.info(f"🎙 Current voice_languages: {current}")

    if show:
        return 0

    codes = normalize_voice_languages(languages or [])
    await doc_ref.update({"config.voice_languages": codes or None})
    logger.info(f"✅ Set voice_languages = {codes or None} (first code is primary)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-id",
        default=os.getenv("DEV_USER_ID"),
        help="target user (defaults to DEV_USER_ID from .env)",
    )
    parser.add_argument(
        "--languages",
        nargs="*",
        default=None,
        help="ISO-639-1 codes, ordered — first is primary. Empty clears the setting.",
    )
    parser.add_argument("--show", action="store_true", help="print the current value and exit")
    args = parser.parse_args()

    if not args.user_id:
        parser.error("--user-id is required (or set DEV_USER_ID in .env)")
    if args.languages is None and not args.show:
        parser.error("pass --languages or --show")

    sys.exit(asyncio.run(main(args.user_id, args.languages, args.show)))

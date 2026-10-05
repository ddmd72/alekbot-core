# scripts/skills/seed_custom_skill.py
"""Write one custom skill for a user (Agent Skills delivery A — before chat authoring exists).

Runs the same checks as `$skill save` will (format, size, security reject) and the same
transactional write. The skill text is personal: keep it in gitignored scripts/memory/.

    python scripts/skills/seed_custom_skill.py --user <USER_ID> --account <ACCOUNT_ID> \
        --file scripts/memory/skills/flight-status.md
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import firestore

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
load_dotenv()

from src.adapters.firestore_skill_repository import FirestoreSkillRepository  # noqa: E402
from src.adapters.security.composite_adapter import CompositeAdapter  # noqa: E402
from src.adapters.security.regex_adapter import RegexSecurityAdapter  # noqa: E402
from src.config.settings import load_settings  # noqa: E402
from src.services.skill_service import SkillService  # noqa: E402
from src.utils.skill_md import parse_skill_md  # noqa: E402


async def main(user_id: str, account_id: str, path: Path) -> int:
    skill = parse_skill_md(path.read_text(encoding="utf-8"))
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]  # resolves the collection prefix — never hardcode it
    db = firestore.AsyncClient(
        project=config["GOOGLE_CLOUD_PROJECT"],
        database=os.getenv("FIRESTORE_DATABASE", "us-production"),
    )
    service = SkillService(
        repository=FirestoreSkillRepository(db, env_config),
        security_port=CompositeAdapter(adapters=[RegexSecurityAdapter()], strategy="worst_case"),
    )
    version = await service.save(user_id, account_id, skill)
    print(f"✅ {skill.name} v{version} saved to {env_config.firestore_collection_prefix}skills")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Seed one custom skill")
    ap.add_argument("--user", required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--file", required=True, type=Path)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.user, a.account, a.file)))

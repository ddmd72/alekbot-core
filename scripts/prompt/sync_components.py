"""
Sync prompt component files to Firestore.

Usage:
  # Dry-run all system + agents to dev
  python scripts/prompt/sync_components.py --env development --level all --dry-run

  # Upload system components only
  python scripts/prompt/sync_components.py --env development --level system

  # Upload single agent overrides
  python scripts/prompt/sync_components.py --env development --level agent --agent smart

  # Upload account overrides (SESSION_26)
  python scripts/prompt/sync_components.py --env development --level account --account-id <acc_id>

  # Upload user overrides
  python scripts/prompt/sync_components.py --env development --level user --user-id <user_id>
"""

import argparse
import asyncio
import os
from pathlib import Path
from datetime import timezone
import sys
from typing import Dict, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore
import yaml

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.utils.logger import logger


BASE_DIR = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = BASE_DIR / "ai_templates"
COMPONENTS_ROOT = TEMPLATE_ROOT / "components"
MANIFEST_PATH = TEMPLATE_ROOT / "manifest.yaml"


class ComponentSync:
    def __init__(self, env: str, dry_run: bool = False):
        self.env = env
        self.dry_run = dry_run
        self.config = load_settings()
        os.environ["APP_ENV"] = env
        self.env_config = EnvironmentConfig()

        self.manifest = self._load_manifest()
        collection = self.manifest["environments"][env]["collection"]

        self.db = firestore.AsyncClient(project=self.config["GOOGLE_CLOUD_PROJECT"])
        self.collection_name = collection
        self.collection = self.db.collection(self.collection_name)

    def _load_manifest(self) -> Dict:
        if not MANIFEST_PATH.exists():
            raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}")
        return yaml.safe_load(MANIFEST_PATH.read_text())

    def _component_meta(self, component_id: str) -> Dict:
        if component_id not in self.manifest["components"]:
            raise KeyError(f"Component '{component_id}' not found in manifest.yaml")
        return self.manifest["components"][component_id]

    async def sync_system(self):
        system_dir = COMPONENTS_ROOT / "system"
        await self._sync_directory(system_dir, owner_type="SYSTEM", owner_value=None)

    async def sync_agent(self, agent_type: str):
        agent_dir = COMPONENTS_ROOT / "agent" / agent_type
        if not agent_dir.exists():
            raise FileNotFoundError(f"Agent directory not found: {agent_dir}")
        await self._sync_directory(agent_dir, owner_type="AGENT", owner_value=agent_type)

    async def sync_account(self, account_id: str):
        """
        Sync account-level component overrides.

        SESSION_26: Added for 4-level prompt resolution (ACCOUNT level).

        Args:
            account_id: Account identifier (billing account)
        """
        account_dir = COMPONENTS_ROOT / "account" / account_id
        if not account_dir.exists():
            raise FileNotFoundError(f"Account directory not found: {account_dir}")
        await self._sync_directory(account_dir, owner_type="ACCOUNT", owner_value=account_id)

    async def sync_user(self, user_id: str):
        user_dir = COMPONENTS_ROOT / "user" / user_id
        if not user_dir.exists():
            raise FileNotFoundError(f"User directory not found: {user_dir}")
        await self._sync_directory(user_dir, owner_type="USER", owner_value=user_id)

    async def _sync_directory(self, directory: Path, owner_type: str, owner_value: Optional[str]):
        files = [p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")]
        if not files:
            logger.warning(f"No component files found in {directory}")
            return

        for file_path in files:
            if file_path.suffix == ".exclude":
                component_id = file_path.stem
                content = ""
                is_enabled = False
            else:
                component_id = file_path.stem
                content = file_path.read_text().rstrip() + "\n"
                is_enabled = True

            meta = self._component_meta(component_id)
            doc_data = {
                "component_id": component_id,
                "owner_type": owner_type,
                "owner_value": owner_value,
                "is_enabled": is_enabled,
                "text": content,
                "scope": meta["scope"],
                "order": meta["order"],
                "version": "1.0",
                "description": meta.get("description", ""),
                "created_by": "sync_components.py",
                "updated_at": firestore.SERVER_TIMESTAMP,
                "created_at": firestore.SERVER_TIMESTAMP,
            }

            existing = await self._find_existing(component_id, owner_type, owner_value)
            label = f"{owner_type}/{owner_value}" if owner_value else owner_type
            action = "UPDATE" if existing else "CREATE"

            if self.dry_run:
                logger.info(f"DRY-RUN {action} {label} {component_id} (enabled={is_enabled})")
                continue

            if existing:
                await existing.reference.set(doc_data)
            else:
                await self.collection.document().set(doc_data)

            logger.info(f"✅ {action} {label} {component_id} (enabled={is_enabled})")

    async def _find_existing(self, component_id: str, owner_type: str, owner_value: Optional[str]):
        query = self.collection.where(
            filter=firestore.FieldFilter("component_id", "==", component_id)
        ).where(
            filter=firestore.FieldFilter("owner_type", "==", owner_type)
        )

        query = query.where(
            filter=firestore.FieldFilter("owner_value", "==", owner_value)
        )

        docs = [doc async for doc in query.limit(1).stream()]
        return docs[0] if docs else None


async def main():
    parser = argparse.ArgumentParser(description="Sync prompt component files to Firestore")
    parser.add_argument("--env", choices=["development", "production"], default="development")
    parser.add_argument(
        "--level",
        choices=["system", "agent", "account", "user", "all"],  # SESSION_26: Added "account"
        default="all"
    )
    parser.add_argument("--agent", help="Agent type for --level agent")
    parser.add_argument("--account-id", help="Account ID for --level account (SESSION_26)")
    parser.add_argument("--user-id", help="User id for --level user")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing")

    args = parser.parse_args()

    syncer = ComponentSync(env=args.env, dry_run=args.dry_run)

    if args.level == "system":
        await syncer.sync_system()
    elif args.level == "agent":
        if not args.agent:
            raise SystemExit("--agent is required for --level agent")
        await syncer.sync_agent(args.agent)
    elif args.level == "account":
        if not args.account_id:
            raise SystemExit("--account-id is required for --level account")
        await syncer.sync_account(args.account_id)
    elif args.level == "user":
        if not args.user_id:
            raise SystemExit("--user-id is required for --level user")
        await syncer.sync_user(args.user_id)
    elif args.level == "all":
        await syncer.sync_system()
        for agent in syncer.manifest.get("agents", []):
            agent_dir = COMPONENTS_ROOT / "agent" / agent
            if agent_dir.exists():
                await syncer.sync_agent(agent)


if __name__ == "__main__":
    asyncio.run(main())

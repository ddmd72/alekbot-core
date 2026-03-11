"""
Export Legacy Facts - Simple Text List

Usage:
    python scripts/migration/export_legacy_facts.py \\
        --account-id ACCOUNT_ID \\
        --output legacy_facts.txt
"""

import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.adapters.firestore_repo import FirestoreFactRepository
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.services.biographical_context_service import BiographicalContextService
from src.services.configuration_service import ConfigurationService
from src.utils.logger import logger


async def export_legacy_facts(account_id: str, output_path: str):
    """Export legacy facts to text file."""
    
    logger.info("🔧 Initializing...")
    
    settings = load_settings()
    env_config = EnvironmentConfig()
    
    # Initialize Firestore
    from google.cloud.firestore import AsyncClient
    db = AsyncClient(
        project=settings.get("GCP_PROJECT_ID"),
        database=env_config.firestore_database_id
    )
    
    # Initialize embedding service
    embedding_service = GeminiEmbeddingAdapter(api_key=settings.get("GEMINI_API_KEY"))
    
    # Initialize repository
    config_service = ConfigurationService()
    biographical_context_service = BiographicalContextService(
        repository=None,
        config_service=config_service,
        account_repo=None
    )
    
    repo = FirestoreFactRepository(
        db_client=db,
        env_config=env_config,
        embedding_service=embedding_service,
        biographical_context_service=biographical_context_service
    )
    biographical_context_service._repository = repo
    await repo.initialize()
    
    # Fetch all legacy facts
    logger.info(f"📥 Fetching legacy facts for account {account_id[:12]}...")
    legacy_facts = await repo.get_legacy_facts(account_id=account_id, limit=1000)
    
    if not legacy_facts:
        logger.info("✅ No legacy facts found!")
        return
    
    logger.info(f"📊 Found {len(legacy_facts)} legacy facts")
    
    # Write to file
    output_file = Path(output_path)
    with output_file.open('w', encoding='utf-8') as f:
        for i, fact in enumerate(legacy_facts, 1):
            f.write(f"{i}. {fact.text}\n")
    
    logger.info(f"✅ Exported {len(legacy_facts)} facts to {output_path}")
    
    # Also create numbered list without dots for easy copy-paste
    simple_path = output_file.stem + "_simple.txt"
    with open(simple_path, 'w', encoding='utf-8') as f:
        for fact in legacy_facts:
            f.write(f"{fact.text}\n")
    
    logger.info(f"✅ Also created simple list: {simple_path}")


async def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Export legacy facts to text file"
    )
    parser.add_argument("--account-id", required=True, help="Account ID")
    parser.add_argument("--output", default="legacy_facts.txt", help="Output file path")
    
    args = parser.parse_args()
    
    await export_legacy_facts(args.account_id, args.output)


if __name__ == "__main__":
    asyncio.run(main())

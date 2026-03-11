"""
Export ALL Facts (not just legacy)

Usage:
    python scripts/migration/export_all_facts.py \\
        --account-id ACCOUNT_ID \\
        --output all_facts.txt
"""

import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.utils.logger import logger


async def export_all_facts(account_id: str, output_path: str):
    """Export ALL facts to text file."""
    
    logger.info("🔧 Initializing...")
    
    settings = load_settings()
    env_config = EnvironmentConfig()
    
    # Initialize Firestore
    from google.cloud.firestore import AsyncClient
    db = AsyncClient(
        project=settings.get("GCP_PROJECT_ID"),
        database=env_config.firestore_database_id
    )
    
    # Determine collection name (use env_config method)
    collection_name = env_config.domain_facts_collection
    
    logger.info(f"📥 Fetching ALL facts from {collection_name}...")
    
    # Simple query by account_id only (no is_current filter to avoid index issues)
    query = db.collection(collection_name).where("account_id", "==", account_id)
    
    docs = query.stream()
    
    facts = []
    async for doc in docs:
        data = doc.to_dict()
        facts.append({
            "id": doc.id,
            "text": data.get("text", ""),
            "domain": data.get("domain", "N/A"),
            "temporal_class": data.get("temporal_class", "N/A"),
            "state": data.get("state", "N/A"),
            "tags": data.get("tags", []),
            "created_at": data.get("created_at")
        })
    
    if not facts:
        logger.info("✅ No facts found!")
        return
    
    logger.info(f"📊 Found {len(facts)} facts")
    
    # Write detailed version (with metadata)
    output_file = Path(output_path)
    with output_file.open('w', encoding='utf-8') as f:
        for i, fact in enumerate(facts, 1):
            f.write(f"{i}. {fact['text']}\n")
            f.write(f"   [Domain: {fact['domain']}, Temporal: {fact['temporal_class']}, State: {fact['state']}, Tags: {', '.join(fact['tags'])}]\n\n")
    
    logger.info(f"✅ Exported {len(facts)} facts (with metadata) to {output_path}")
    
    # Also create simple text-only list
    simple_path = output_file.stem + "_simple.txt"
    with open(simple_path, 'w', encoding='utf-8') as f:
        for fact in facts:
            f.write(f"{fact['text']}\n")
    
    logger.info(f"✅ Also created simple list: {simple_path}")
    
    # Create stats summary
    stats_path = output_file.stem + "_stats.txt"
    with open(stats_path, 'w', encoding='utf-8') as f:
        f.write(f"Total Facts: {len(facts)}\n\n")
        
        # Domain breakdown
        domains = {}
        for fact in facts:
            domain = fact['domain']
            domains[domain] = domains.get(domain, 0) + 1
        
        f.write("Domain Breakdown:\n")
        for domain, count in sorted(domains.items(), key=lambda x: -x[1]):
            f.write(f"  {domain}: {count}\n")
        
        f.write("\n")
        
        # Temporal breakdown
        temporal = {}
        for fact in facts:
            temp = fact['temporal_class']
            temporal[temp] = temporal.get(temp, 0) + 1
        
        f.write("Temporal Class Breakdown:\n")
        for temp, count in sorted(temporal.items(), key=lambda x: -x[1]):
            f.write(f"  {temp}: {count}\n")
        
        f.write("\n")
        
        # State breakdown
        states = {}
        for fact in facts:
            state = fact['state']
            states[state] = states.get(state, 0) + 1
        
        f.write("State Breakdown:\n")
        for state, count in sorted(states.items(), key=lambda x: -x[1]):
            f.write(f"  {state}: {count}\n")
    
    logger.info(f"✅ Created stats summary: {stats_path}")


async def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Export ALL facts to text file"
    )
    parser.add_argument("--account-id", required=True, help="Account ID")
    parser.add_argument("--output", default="all_facts.txt", help="Output file path")
    
    args = parser.parse_args()
    
    await export_all_facts(args.account_id, args.output)


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
Export facts for specific account from dev and prod collections.
"""
import asyncio
import os
from google.cloud import firestore
from datetime import datetime

ACCOUNT_ID = os.environ.get("DEV_ACCOUNT_ID") or f"account-{os.environ['DEV_USER_ID']}"
DEV_COLLECTION = "development_domain_facts_v2"
PROD_COLLECTION = "domain_facts_v2"
OUTPUT_FILE = "reports/account_facts_export.md"


async def export_facts():
    """Export facts from dev and prod collections."""
    # Initialize Firestore client (us-production database)
    db = firestore.AsyncClient(database='us-production')
    
    all_facts = []
    
    # Fetch from dev collection
    print(f"📥 Fetching from {DEV_COLLECTION}...")
    dev_query = db.collection(DEV_COLLECTION).where("account_id", "==", ACCOUNT_ID)
    dev_docs = dev_query.stream()
    
    dev_count = 0
    async for doc in dev_docs:
        data = doc.to_dict()
        all_facts.append({
            "type": data.get("type", "UNKNOWN"),
            "text": data.get("text", ""),
            "valid_from": data.get("valid_from"),
            "source": "dev"
        })
        dev_count += 1
    
    print(f"✅ Dev: {dev_count} facts")
    
    # Fetch from prod collection
    print(f"📥 Fetching from {PROD_COLLECTION}...")
    prod_query = db.collection(PROD_COLLECTION).where("account_id", "==", ACCOUNT_ID)
    prod_docs = prod_query.stream()
    
    prod_count = 0
    async for doc in prod_docs:
        data = doc.to_dict()
        all_facts.append({
            "type": data.get("type", "UNKNOWN"),
            "text": data.get("text", ""),
            "valid_from": data.get("valid_from"),
            "source": "prod"
        })
        prod_count += 1
    
    print(f"✅ Prod: {prod_count} facts")
    
    # Sort by text (alphabetically)
    all_facts.sort(key=lambda f: f["text"].lower())
    
    # Generate Markdown
    print(f"📝 Generating Markdown...")
    lines = [
        f"# Account Facts Export",
        f"",
        f"**Account ID:** `{ACCOUNT_ID}`  ",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Total Facts:** {len(all_facts)} (Dev: {dev_count}, Prod: {prod_count})",
        f"",
        f"---",
        f""
    ]
    
    for fact in all_facts:
        # Format valid_from
        valid_from_str = ""
        if fact["valid_from"]:
            if hasattr(fact["valid_from"], "isoformat"):
                valid_from_str = fact["valid_from"].isoformat()
            else:
                valid_from_str = str(fact["valid_from"])
        
        # Format: - **[TYPE]** text (valid_from: YYYY-MM-DD)
        line = f"- **[{fact['type']}]** {fact['text']}"
        if valid_from_str:
            line += f" _(valid_from: {valid_from_str})_"
        
        lines.append(line)
    
    # Write to file
    content = "\n".join(lines)
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    
    print(f"✅ Exported to {OUTPUT_FILE}")
    print(f"📊 Total: {len(all_facts)} facts")


if __name__ == "__main__":
    asyncio.run(export_facts())

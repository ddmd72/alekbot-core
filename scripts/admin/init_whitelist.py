"""
Initialize Whitelist in Firestore.

Creates initial whitelist configuration document.
Run once after deployment or when resetting whitelist.

Usage:
    python scripts/admin/init_whitelist.py [--env dev|prod]
"""
import os
import asyncio
import argparse
from google.cloud import firestore

# Populate via environment variables before running.
# Example: ADMIN_EMAILS="you@example.com,other@example.com" ADMIN_DOMAIN="example.com"
_admin_emails = [e.strip() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]
_admin_domain = os.getenv("ADMIN_DOMAIN", "")

# Initial whitelist configuration
INITIAL_WHITELIST = {
    "allowed_emails": _admin_emails,
    "allowed_domains": [_admin_domain] if _admin_domain else [],
    "updated_at": firestore.SERVER_TIMESTAMP
}


async def init_whitelist(env: str = "dev"):
    """Initialize whitelist in Firestore using ADR-006 semantic naming."""
    db = firestore.Client()
    
    # ADR-006: Semantic Collection Naming
    # Dev: development_domain_whitelist_v1
    # Prod: domain_whitelist_v1
    prefix = "development_" if env == "dev" else ""
    collection_name = f"{prefix}domain_whitelist_v1"
    doc_id = "config"
    
    print(f"📂 Initializing whitelist in collection: {collection_name}")
    print(f"📄 Document ID: {doc_id}")
    print(f"\n📋 Initial configuration:")
    print(f"   Emails: {INITIAL_WHITELIST['allowed_emails']}")
    print(f"   Domains: {INITIAL_WHITELIST['allowed_domains']}")
    
    # Check if document already exists
    doc_ref = db.collection(collection_name).document(doc_id)
    doc = doc_ref.get()
    
    if doc.exists:
        print(f"\n⚠️  Whitelist document already exists!")
        data = doc.to_dict()
        print(f"   Current emails: {data.get('allowed_emails', [])}")
        print(f"   Current domains: {data.get('allowed_domains', [])}")
        
        response = input("\n❓ Overwrite existing whitelist? (yes/no): ")
        if response.lower() != "yes":
            print("❌ Aborted. No changes made.")
            return
    
    # Create/update whitelist document
    doc_ref.set(INITIAL_WHITELIST)
    
    print(f"\n✅ Whitelist initialized successfully!")
    print(f"   Collection: {collection_name}")
    print(f"   Document: {doc_id}")
    print(f"\n🔍 Verify in Firestore Console:")
    print(f"   https://console.firebase.google.com/project/_/firestore/data/{collection_name}/{doc_id}")


def main():
    parser = argparse.ArgumentParser(description="Initialize Firestore whitelist")
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default="dev",
        help="Environment (dev or prod)"
    )
    
    args = parser.parse_args()
    
    print(f"🚀 Initializing whitelist for environment: {args.env}")
    print(f"=" * 60)
    
    asyncio.run(init_whitelist(args.env))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Diagnostic script to check for duplicate users by email.

Usage:
    python scripts/admin/check_duplicate_users.py [--email EMAIL]

Examples:
    # Check all users for duplicates
    python scripts/admin/check_duplicate_users.py

    # Check specific email
    python scripts/admin/check_duplicate_users.py --email dmytro_es@ddmd13.com
"""
import asyncio
import argparse
from google.cloud import firestore
from collections import defaultdict

from src.config.settings import load_settings


async def check_duplicates(email_filter: str = None):
    """Check for duplicate users by email."""
    
    # Load config
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    # Initialize Firestore
    if env_config.use_emulator:
        print(f"🏠 Using Firestore EMULATOR at {env_config.get_emulator_host()}")
        db = firestore.AsyncClient(project="emulator-project")
    else:
        print(f"☁️ Using Firestore CLOUD ({env_config.env.value})")
        db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    # Get users collection
    collection_name = env_config.domain_users_collection
    print(f"📂 Collection: {collection_name}\n")
    
    users_col = db.collection(collection_name)
    
    # Fetch all users
    if email_filter:
        query = users_col.where("email", "==", email_filter)
        print(f"🔍 Filtering by email: {email_filter}\n")
    else:
        query = users_col
        print("🔍 Fetching ALL users...\n")
    
    docs = query.stream()
    
    # Group by email
    users_by_email = defaultdict(list)
    total_users = 0
    
    async for doc in docs:
        total_users += 1
        user_data = doc.to_dict()
        email = user_data.get("email", "NO_EMAIL")
        
        users_by_email[email].append({
            "user_id": doc.id,
            "email": email,
            "display_name": user_data.get("display_name"),
            "external_user_id": user_data.get("external_user_id"),
            "platform_identities": user_data.get("platform_identities", {}),
            "account_id": user_data.get("account_id"),
            "created_at": user_data.get("created_at"),
        })
    
    print(f"📊 Total users: {total_users}\n")
    print("=" * 80)
    
    # Check for duplicates
    duplicates_found = False
    
    for email, users in users_by_email.items():
        if len(users) > 1:
            duplicates_found = True
            print(f"\n❌ DUPLICATE FOUND - Email: {email} ({len(users)} users)")
            print("-" * 80)
            
            for i, user in enumerate(users, 1):
                print(f"\n  User #{i}:")
                print(f"    user_id:          {user['user_id']}")
                print(f"    display_name:     {user['display_name']}")
                print(f"    external_user_id: {user['external_user_id']}")
                print(f"    account_id:       {user['account_id']}")
                print(f"    platform_ids:     {user['platform_identities']}")
                print(f"    created_at:       {user['created_at']}")
        elif email_filter:
            # Show single user when filtering by email
            user = users[0]
            print(f"\n✅ Single user found for: {email}")
            print("-" * 80)
            print(f"  user_id:          {user['user_id']}")
            print(f"  display_name:     {user['display_name']}")
            print(f"  external_user_id: {user['external_user_id']}")
            print(f"  account_id:       {user['account_id']}")
            print(f"  platform_ids:     {user['platform_identities']}")
            print(f"  created_at:       {user['created_at']}")
    
    print("\n" + "=" * 80)
    
    if not duplicates_found:
        print("\n✅ No duplicates found!")
    else:
        print("\n⚠️ Duplicates detected! Review above.")
    
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Check for duplicate users by email"
    )
    parser.add_argument(
        "--email",
        type=str,
        help="Filter by specific email address"
    )
    
    args = parser.parse_args()
    
    asyncio.run(check_duplicates(email_filter=args.email))


if __name__ == "__main__":
    main()

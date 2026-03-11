"""
Create user-specific component override.

Allows users to customize individual prompt components.

Session: 23+ (Prompt Component Architecture - User Customization)
RFC: docs/architecture/rfcs/PROMPT_COMPONENT_ARCHITECTURE_RFC.md

Usage:
    # Override humor_engine for specific user
    python scripts/prompt/create_user_override.py \
        --user-id abc123 \
        --component-id humor_engine \
        --scope class.Alek.properties \
        --order 30 \
        --text "humor_engine { style: 'sarcastic', frequency: 'frequent' }" \
        --env development
    
    # Interactive mode
    python scripts/prompt/create_user_override.py --interactive
"""

import asyncio
import argparse
import sys
import os
from typing import Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.domain.prompt import ComponentScope
from src.utils.logger import logger


class UserOverrideCreator:
    """Create user-specific component overrides."""
    
    def __init__(self, env: str):
        self.env = env
        self.config = load_settings()
        self.env_config = EnvironmentConfig.from_env(env)
        
        # Initialize Firestore
        self.db = firestore.AsyncClient(project=self.config["GOOGLE_CLOUD_PROJECT"])
        self.collection_name = f"{self.env_config.firestore_collection_prefix}facts"
        self.collection = self.db.collection(self.collection_name)
    
    async def create_override(
        self,
        user_id: str,
        component_id: str,
        scope: str,
        order: int,
        text: str
    ):
        """Create or update user override."""
        lineage_id = f"prompt_component_{component_id}"
        
        # Validate scope
        try:
            ComponentScope(scope)
        except ValueError:
            logger.error(f"❌ Invalid scope: {scope}")
            logger.info(f"Valid scopes: {[s.value for s in ComponentScope]}")
            return False
        
        # Check if override already exists
        existing = await self._find_existing(user_id, lineage_id)
        
        doc_data = {
            "lineage_id": lineage_id,
            "owner_id": user_id,
            "status": "active",
            "text": text.strip(),
            "metadata": {
                "component_type": "groovy_block",
                "scope": scope,
                "order": order,
                "version": "1.0",
                "is_user_override": True
            },
            "updated_at": firestore.SERVER_TIMESTAMP
        }
        
        if existing:
            # Update existing override
            await existing.reference.update(doc_data)
            logger.info(f"✅ Updated user override: {component_id} for user {user_id[:8]}")
        else:
            # Create new override
            doc_data["created_at"] = firestore.SERVER_TIMESTAMP
            doc_ref = self.collection.document()
            await doc_ref.set(doc_data)
            logger.info(f"✅ Created user override: {component_id} for user {user_id[:8]}")
        
        return True
    
    async def delete_override(self, user_id: str, component_id: str):
        """Delete user override."""
        lineage_id = f"prompt_component_{component_id}"
        existing = await self._find_existing(user_id, lineage_id)
        
        if existing:
            await existing.reference.update({
                "status": "inactive",
                "updated_at": firestore.SERVER_TIMESTAMP
            })
            logger.info(f"🗑️ Deleted user override: {component_id} for user {user_id[:8]}")
            return True
        else:
            logger.warning(f"⚠️ Override not found: {component_id} for user {user_id[:8]}")
            return False
    
    async def list_overrides(self, user_id: str):
        """List all overrides for user."""
        query = (
            self.collection
            .where(filter=firestore.FieldFilter("owner_id", "==", user_id))
            .where(filter=firestore.FieldFilter("status", "==", "active"))
            .where(filter=firestore.FieldFilter("metadata.component_type", "==", "groovy_block"))
        )
        
        overrides = []
        async for doc in query.stream():
            data = doc.to_dict()
            lineage_id = data.get("lineage_id", "")
            if lineage_id.startswith("prompt_component_"):
                component_id = lineage_id.replace("prompt_component_", "")
                metadata = data.get("metadata", {})
                overrides.append({
                    "id": component_id,
                    "scope": metadata.get("scope"),
                    "order": metadata.get("order"),
                    "text_preview": data.get("text", "")[:100] + "..."
                })
        
        if overrides:
            logger.info(f"📦 Found {len(overrides)} overrides for user {user_id[:8]}:")
            for override in overrides:
                logger.info(f"   - {override['id']} ({override['scope']}, order={override['order']})")
        else:
            logger.info(f"ℹ️  No overrides found for user {user_id[:8]}")
        
        return overrides
    
    async def _find_existing(self, user_id: str, lineage_id: str):
        """Find existing override."""
        query = (
            self.collection
            .where(filter=firestore.FieldFilter("owner_id", "==", user_id))
            .where(filter=firestore.FieldFilter("lineage_id", "==", lineage_id))
            .where(filter=firestore.FieldFilter("status", "==", "active"))
            .limit(1)
        )
        
        docs = [doc async for doc in query.stream()]
        return docs[0] if docs else None


# =============================================================================
# EXAMPLE OVERRIDES (for reference)
# =============================================================================

EXAMPLE_OVERRIDES = {
    "humor_engine": {
        "scope": "class.Alek.properties",
        "order": 30,
        "text": """humor_engine {
    style: 'sarcastic'
    frequency: 'frequent'
    cultural_context: 'internet_memes'
    
    note: 'User prefers more edgy humor'
}"""
    },
    "archetype": {
        "scope": "class.Alek.properties",
        "order": 20,
        "text": """archetype = 'technical_expert'"""
    },
    "custom_policy": {
        "scope": "class.Alek.policies",
        "order": 55,
        "text": """@critical rule User_Preference() {
    instruction: 'Always provide code examples when explaining technical concepts'
    priority: 2
}"""
    }
}


async def interactive_mode(creator: UserOverrideCreator):
    """Interactive mode for creating overrides."""
    print("\n" + "="*60)
    print("🎨 INTERACTIVE USER OVERRIDE CREATOR")
    print("="*60)
    
    # Get user ID
    user_id = input("\n📝 Enter user ID: ").strip()
    if not user_id:
        print("❌ User ID required")
        return
    
    # List existing overrides
    print(f"\n📦 Checking existing overrides for {user_id[:8]}...")
    await creator.list_overrides(user_id)
    
    # Action menu
    print("\n🎯 What would you like to do?")
    print("1. Create new override")
    print("2. Delete existing override")
    print("3. Use example override")
    print("4. Exit")
    
    choice = input("\nChoice (1-4): ").strip()
    
    if choice == "1":
        # Create new override
        print("\n📝 Enter component details:")
        component_id = input("Component ID (e.g., humor_engine): ").strip()
        
        print(f"\n📋 Available scopes:")
        for i, scope in enumerate(ComponentScope, 1):
            print(f"{i}. {scope.value}")
        
        scope_choice = input("Scope number or value: ").strip()
        try:
            scope_num = int(scope_choice)
            scope = list(ComponentScope)[scope_num - 1].value
        except (ValueError, IndexError):
            scope = scope_choice
        
        order = int(input("Order (number, e.g., 30): ").strip())
        
        print("\n📄 Enter component text (end with empty line):")
        lines = []
        while True:
            line = input()
            if not line:
                break
            lines.append(line)
        text = "\n".join(lines)
        
        # Confirm
        print(f"\n✅ Will create override:")
        print(f"   User: {user_id[:8]}")
        print(f"   Component: {component_id}")
        print(f"   Scope: {scope}")
        print(f"   Order: {order}")
        print(f"   Text: {len(text)} characters")
        
        confirm = input("\nProceed? (yes/no): ").strip().lower()
        if confirm == "yes":
            await creator.create_override(user_id, component_id, scope, order, text)
        else:
            print("❌ Cancelled")
    
    elif choice == "2":
        # Delete override
        component_id = input("\n🗑️ Component ID to delete: ").strip()
        confirm = input(f"Delete {component_id} for user {user_id[:8]}? (yes/no): ").strip().lower()
        if confirm == "yes":
            await creator.delete_override(user_id, component_id)
        else:
            print("❌ Cancelled")
    
    elif choice == "3":
        # Use example
        print("\n📚 Available examples:")
        for i, (name, example) in enumerate(EXAMPLE_OVERRIDES.items(), 1):
            print(f"{i}. {name} - {example['scope']}")
        
        example_choice = input("\nExample number: ").strip()
        try:
            example_num = int(example_choice)
            example_name = list(EXAMPLE_OVERRIDES.keys())[example_num - 1]
            example = EXAMPLE_OVERRIDES[example_name]
            
            print(f"\n✅ Using example: {example_name}")
            await creator.create_override(
                user_id,
                example_name,
                example["scope"],
                example["order"],
                example["text"]
            )
        except (ValueError, IndexError):
            print("❌ Invalid choice")
    
    else:
        print("👋 Goodbye!")


async def main():
    parser = argparse.ArgumentParser(description="Create user-specific component overrides")
    parser.add_argument("--user-id", help="User UUID")
    parser.add_argument("--component-id", help="Component identifier")
    parser.add_argument("--scope", help="Component scope (e.g., class.Alek.properties)")
    parser.add_argument("--order", type=int, help="Component order")
    parser.add_argument("--text", help="Component Groovy text")
    parser.add_argument(
        "--env",
        choices=["development", "production"],
        default="development",
        help="Environment (default: development)"
    )
    parser.add_argument("--interactive", action="store_true", help="Interactive mode")
    parser.add_argument("--list", action="store_true", help="List user's overrides")
    parser.add_argument("--delete", action="store_true", help="Delete override")
    
    args = parser.parse_args()
    
    creator = UserOverrideCreator(env=args.env)
    
    if args.interactive:
        await interactive_mode(creator)
    elif args.list:
        if not args.user_id:
            print("❌ --user-id required for --list")
            return
        await creator.list_overrides(args.user_id)
    elif args.delete:
        if not args.user_id or not args.component_id:
            print("❌ --user-id and --component-id required for --delete")
            return
        await creator.delete_override(args.user_id, args.component_id)
    else:
        # Create override
        if not all([args.user_id, args.component_id, args.scope, args.order, args.text]):
            print("❌ Required: --user-id, --component-id, --scope, --order, --text")
            print("   Or use --interactive mode")
            return
        
        await creator.create_override(
            args.user_id,
            args.component_id,
            args.scope,
            args.order,
            args.text
        )


if __name__ == "__main__":
    asyncio.run(main())

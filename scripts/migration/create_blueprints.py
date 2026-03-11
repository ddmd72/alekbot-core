"""
Create Universal Blueprint for Prompt Design System v3 Migration.

This script generates and uploads universal_agent_v1 blueprint to Firestore.

CRITICAL: There is ONE universal blueprint containing ALL possible slots.
Agent profiles determine which slots to use.

Usage:
    python scripts/migration/create_blueprints.py --dry-run  # Preview only
    python scripts/migration/create_blueprints.py --upload   # Upload to Firestore

Phase: 5.3 (Migration - Blueprint Creation)
Date: 2026-02-02
Updated: 2026-02-02 (Migrated to universal blueprint architecture)
"""

import asyncio
from typing import List
from google.cloud import firestore
import os
import sys

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.slot import BlueprintClass, OwnerType
from src.domain.prompt_v3.token import TokenId, TokenCategory


# ============================================================================
# Blueprint Definitions
# ============================================================================

def create_universal_agent_v1_blueprint() -> Blueprint:
    """Create universal_agent_v1 blueprint with ALL 19 placeholders.
    
    Template source: memory/.frozen/universal_blueprint.groovy
    
    All 19 placeholders:
    - 5 personality: ARCHETYPE, VIBE, VOICE, HUMOR_ENGINE, MOTTO_DEFAULT
    - 1 behavior: BEHAVIOR_GUIDE_RANEVSKAYA_MODE  
    - 1 few_shot: FEW_SHOT_EXAMPLES_DEFAULT
    - 1 cognitive: COGNITIVE_PROCESS
    - 6 policies: POLICY_OUTPUT_LANGUAGE, POLICY_PRIVACY, POLICY_NO_OPEN_LOOPS,
                  POLICY_ANTI_GUARDIAN, POLICY_WITTY_ACCENTUATION, POLICY_ALIGN_WITH_ANCHORS
    - 2 protocols: PROTOCOL_SEARCH_MEMORY, PROTOCOL_WEB_SEARCH
    - 1 output: OUTPUT_FORMAT
    - 2 directives: DIRECTIVE_SLACK_FORMATTING, DIRECTIVE_BREVITY
    """

    classes = {
        # ========== PERSONALITY SLOTS (5) ==========
        "ARCHETYPE": BlueprintClass(
            allowed_token_categories={TokenCategory("archetype")},
            overridable_by={OwnerType.USER, OwnerType.ACCOUNT},
            default_token=TokenId("ARCHETYPE_INTELLECTUAL_SNIPER")
        ),
        "VIBE": BlueprintClass(
            allowed_token_categories={TokenCategory("vibe")},
            overridable_by={OwnerType.USER, OwnerType.ACCOUNT},
            default_token=TokenId("VIBE_BATTLE_WEARY")
        ),
        "VOICE": BlueprintClass(
            allowed_token_categories={TokenCategory("voice")},
            overridable_by={OwnerType.USER, OwnerType.ACCOUNT},
            default_token=TokenId("VOICE_APHORISTIC")
        ),
        "HUMOR_ENGINE": BlueprintClass(
            allowed_token_categories={TokenCategory("humor_engine")},
            overridable_by={OwnerType.USER, OwnerType.ACCOUNT},
            default_token=TokenId("HUMOR_PRESET_RANEVSKAYA")
        ),
        "MOTTO_DEFAULT": BlueprintClass(
            allowed_token_categories={TokenCategory("motto")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("MOTTO_DEFAULT")
        ),
        
        # ========== BEHAVIOR GUIDE (1) ==========
        "BEHAVIOR_GUIDE_RANEVSKAYA_MODE": BlueprintClass(
            allowed_token_categories={TokenCategory("behavior_guide")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("BEHAVIOR_GUIDE_RANEVSKAYA_MODE")
        ),
        
        # ========== FEW SHOT EXAMPLES (1) ==========
        "FEW_SHOT_EXAMPLES_DEFAULT": BlueprintClass(
            allowed_token_categories={TokenCategory("few_shot_examples")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("FEW_SHOT_EXAMPLES_DEFAULT")
        ),
        
        # ========== COGNITIVE PROCESS (1) ==========
        "COGNITIVE_PROCESS": BlueprintClass(
            allowed_token_categories={TokenCategory("cognitive_process")},
            overridable_by={OwnerType.SYSTEM, OwnerType.AGENT},
            default_token=TokenId("COGNITIVE_PROCESS_SMART")
        ),
        
        # ========== POLICIES (6) ==========
        "POLICY_OUTPUT_LANGUAGE": BlueprintClass(
            allowed_token_categories={TokenCategory("policy")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("POLICY_OUTPUT_LANGUAGE")
        ),
        "POLICY_PRIVACY": BlueprintClass(
            allowed_token_categories={TokenCategory("policy")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("POLICY_PRIVACY")
        ),
        "POLICY_NO_OPEN_LOOPS": BlueprintClass(
            allowed_token_categories={TokenCategory("policy")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("POLICY_NO_OPEN_LOOPS")
        ),
        "POLICY_ANTI_GUARDIAN": BlueprintClass(
            allowed_token_categories={TokenCategory("policy")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("POLICY_ANTI_GUARDIAN")
        ),
        "POLICY_WITTY_ACCENTUATION": BlueprintClass(
            allowed_token_categories={TokenCategory("policy")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("POLICY_WITTY_ACCENTUATION")
        ),
        "POLICY_ALIGN_WITH_ANCHORS": BlueprintClass(
            allowed_token_categories={TokenCategory("policy")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("POLICY_ALIGN_WITH_ANCHORS")
        ),
        
        # ========== PROTOCOLS (2) ==========
        "PROTOCOL_SEARCH_MEMORY": BlueprintClass(
            allowed_token_categories={TokenCategory("protocol")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("PROTOCOL_SEARCH_MEMORY")
        ),
        "PROTOCOL_WEB_SEARCH": BlueprintClass(
            allowed_token_categories={TokenCategory("protocol")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("PROTOCOL_WEB_SEARCH")
        ),
        
        # ========== OUTPUT FORMAT (1) ==========
        "OUTPUT_FORMAT": BlueprintClass(
            allowed_token_categories={TokenCategory("output_format")},
            overridable_by={OwnerType.AGENT, OwnerType.ACCOUNT},
            default_token=TokenId("OUTPUT_FORMAT_STANDARD")
        ),
        
        # ========== DIRECTIVES (2) ==========
        "DIRECTIVE_SLACK_FORMATTING": BlueprintClass(
            allowed_token_categories={TokenCategory("final_directive")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("DIRECTIVE_SLACK_FORMATTING")
        ),
        "DIRECTIVE_BREVITY": BlueprintClass(
            allowed_token_categories={TokenCategory("final_directive")},
            overridable_by={OwnerType.SYSTEM},
            default_token=TokenId("DIRECTIVE_BREVITY")
        ),
    }

    # Read template from memory/.frozen/universal_blueprint.groovy
    import pathlib
    template_path = pathlib.Path(__file__).parent / "universal_blueprint.groovy"
    if not template_path.exists():
        # Fallback to memory/.frozen/
        template_path = pathlib.Path(__file__).parent.parent.parent / "memory" / ".frozen" / "universal_blueprint.groovy"
    
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    blueprint = Blueprint(
        id="universal_agent_v1",
        classes=classes,
        template=template
    )

    # Validate blueprint
    blueprint.validate()

    return blueprint


async def upload_blueprints_to_firestore(blueprints: List[Blueprint], collection_name: str):
    """Upload blueprints to Firestore."""
    db = firestore.Client()

    print(f"\n📤 Uploading {len(blueprints)} blueprints to Firestore collection: {collection_name}")

    for blueprint in blueprints:
        doc_ref = db.collection(collection_name).document(blueprint.id)

        # Serialize classes
        classes_data = {}
        for class_name, class_schema in blueprint.classes.items():
            classes_data[class_name] = {
                "allowed_token_categories": [
                    str(cat) for cat in class_schema.allowed_token_categories
                ],
                "overridable_by": [owner.value for owner in class_schema.overridable_by],
                "default_token": str(class_schema.default_token)
            }

        data = {
            "blueprint_id": blueprint.id,
            "template": blueprint.template,
            "classes": classes_data,
            "created_at": firestore.SERVER_TIMESTAMP
        }

        doc_ref.set(data)
        print(f"  ✅ Uploaded: {blueprint.id}")

    print(f"\n✅ Upload complete! {len(blueprints)} blueprints in {collection_name}")


async def main():
    """Main execution."""
    import argparse

    parser = argparse.ArgumentParser(description="Create and upload blueprints for Prompt v3")
    parser.add_argument("--dry-run", action="store_true", help="Preview blueprints without uploading")
    parser.add_argument("--upload", action="store_true", help="Upload blueprints to Firestore")
    parser.add_argument("--collection", default="dev_prompt_blueprints_v3", help="Firestore collection name")

    args = parser.parse_args()

    if not args.dry_run and not args.upload:
        parser.error("Must specify either --dry-run or --upload")

    print("=" * 80)
    print("🚀 Prompt Design System v3 - Universal Blueprint Creation")
    print("=" * 80)

    # Create ONE universal blueprint
    print("\n🔧 Creating universal blueprint...")
    blueprint = create_universal_agent_v1_blueprint()
    print(f"  ✅ {blueprint.id} ({len(blueprint.classes)} slots)")

    # Print summary
    print("\n" + "=" * 80)
    print("📊 Blueprint Summary")
    print("=" * 80)
    
    print(f"\nBlueprint: {blueprint.id}")
    print(f"  Total slots: {len(blueprint.classes)}")
    print(f"  Personality slots: 5 (HUMOR_ENGINE, ARCHETYPE, VOICE, RESPONSE_STYLE, VIBE)")
    print(f"  Cognitive slot: 1 (COGNITIVE_PROCESS)")
    print(f"  Output slot: 1 (OUTPUT_FORMAT)")
    print(f"\n  Slot definitions:")
    for class_name, class_schema in blueprint.classes.items():
        print(
            f"    - {class_name}: default={class_schema.default_token}, "
            f"overridable_by={[o.value for o in class_schema.overridable_by]}"
        )
    print(f"\n  Template size: {len(blueprint.template)} chars")
    
    blueprints = [blueprint]

    # Upload if requested
    if args.upload:
        await upload_blueprints_to_firestore(blueprints, args.collection)
    else:
        print(f"\n🔍 DRY RUN - No upload performed")
        print(f"   To upload, run: python {__file__} --upload --collection {args.collection}")


if __name__ == "__main__":
    asyncio.run(main())

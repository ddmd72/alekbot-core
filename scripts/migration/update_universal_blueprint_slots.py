#!/usr/bin/env python3
"""
Update universal_agent_v1 blueprint with full classes definition.

This adds ALL 19 classes that are referenced in the template.
"""

import asyncio
from google.cloud import firestore


async def update_universal_blueprint():
    """Add full classes definition to universal_agent_v1."""
    
    db = firestore.AsyncClient(project='gen-lang-client-0554950952')
    
    # Read template from file
    with open('scripts/migration/universal_blueprint.groovy', 'r') as f:
        template = f.read()
    
    # Define ALL classes with defaults and allowed token categories
    classes = {
        "FEW_SHOT_EXAMPLES_DEFAULT": {
            "allowed_token_categories": ["few_shot_examples"],
            "overridable_by": ["SYSTEM"],
            "default_token": "FEW_SHOT_EXAMPLES_DEFAULT"
        },
        "ARCHETYPE": {
            "allowed_token_categories": ["archetype"],
            "overridable_by": ["USER", "ACCOUNT"],
            "default_token": "ARCHETYPE_INTELLECTUAL_SNIPER"
        },
        "VIBE": {
            "allowed_token_categories": ["vibe"],
            "overridable_by": ["USER", "ACCOUNT"],
            "default_token": "VIBE_BATTLE_WEARY"
        },
        "MOTTO_DEFAULT": {
            "allowed_token_categories": ["motto"],
            "overridable_by": ["USER", "ACCOUNT"],
            "default_token": "MOTTO_DEFAULT"
        },
        "VOICE": {
            "allowed_token_categories": ["voice"],
            "overridable_by": ["USER", "ACCOUNT"],
            "default_token": "VOICE_APHORISTIC"
        },
        "BEHAVIOR_GUIDE_RANEVSKAYA_MODE": {
            "allowed_token_categories": ["behavior_guide"],
            "overridable_by": ["SYSTEM"],
            "default_token": "BEHAVIOR_GUIDE_RANEVSKAYA_MODE"
        },
        "HUMOR_ENGINE": {
            "allowed_token_categories": ["humor_engine"],
            "overridable_by": ["USER", "ACCOUNT"],
            "default_token": "HUMOR_PRESET_RANEVSKAYA"
        },
        "COGNITIVE_PROCESS": {
            "allowed_token_categories": ["cognitive_process"],
            "overridable_by": ["SYSTEM"],
            "default_token": "COGNITIVE_PROCESS_SMART"
        },
        "POLICY_OUTPUT_LANGUAGE": {
            "allowed_token_categories": ["policy"],
            "overridable_by": ["SYSTEM"],
            "default_token": "POLICY_OUTPUT_LANGUAGE"
        },
        "POLICY_PRIVACY": {
            "allowed_token_categories": ["policy"],
            "overridable_by": ["SYSTEM"],
            "default_token": "POLICY_PRIVACY"
        },
        "POLICY_NO_OPEN_LOOPS": {
            "allowed_token_categories": ["policy"],
            "overridable_by": ["SYSTEM"],
            "default_token": "POLICY_NO_OPEN_LOOPS"
        },
        "POLICY_ANTI_GUARDIAN": {
            "allowed_token_categories": ["policy"],
            "overridable_by": ["SYSTEM"],
            "default_token": "POLICY_ANTI_GUARDIAN"
        },
        "POLICY_WITTY_ACCENTUATION": {
            "allowed_token_categories": ["policy"],
            "overridable_by": ["SYSTEM"],
            "default_token": "POLICY_WITTY_ACCENTUATION"
        },
        "POLICY_ALIGN_WITH_ANCHORS": {
            "allowed_token_categories": ["policy"],
            "overridable_by": ["SYSTEM"],
            "default_token": "POLICY_ALIGN_WITH_ANCHORS"
        },
        "PROTOCOL_SEARCH_MEMORY": {
            "allowed_token_categories": ["protocol"],
            "overridable_by": ["SYSTEM"],
            "default_token": "PROTOCOL_SEARCH_MEMORY"
        },
        "PROTOCOL_WEB_SEARCH": {
            "allowed_token_categories": ["protocol"],
            "overridable_by": ["SYSTEM"],
            "default_token": "PROTOCOL_WEB_SEARCH"
        },
        "OUTPUT_FORMAT": {
            "allowed_token_categories": ["output_format"],
            "overridable_by": ["SYSTEM"],
            "default_token": "OUTPUT_FORMAT_STANDARD"
        },
        "DIRECTIVE_SLACK_FORMATTING": {
            "allowed_token_categories": ["final_directive"],
            "overridable_by": ["SYSTEM"],
            "default_token": "DIRECTIVE_SLACK_FORMATTING"
        },
        "DIRECTIVE_BREVITY": {
            "allowed_token_categories": ["final_directive"],
            "overridable_by": ["SYSTEM"],
            "default_token": "DIRECTIVE_BREVITY"
        },
    }
    
    # Update blueprint in Firestore
    doc_ref = db.collection('dev_prompt_blueprints').document('universal_agent_v1')
    await doc_ref.set({
        'blueprint_id': 'universal_agent_v1',
        'template': template,
        'classes': classes,
        'metadata': {
            'version': '1.2',
            'updated_at': firestore.SERVER_TIMESTAMP,
            'changes': 'Added full classes definition (19 classes)'
        }
    })
    
    print(f'✅ Updated universal_agent_v1 blueprint')
    print(f'   Template: {len(template)} chars')
    print(f'   Classes: {len(classes)} defined')
    print()
    print('Classes:')
    for class_name, class_data in classes.items():
        print(
            f'  - {class_name}: default={class_data["default_token"]}, '
            f'categories={class_data["allowed_token_categories"]}'
        )


if __name__ == "__main__":
    asyncio.run(update_universal_blueprint())

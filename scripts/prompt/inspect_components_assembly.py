"""
Inspect component-based prompt assembly.

Tests the NEW architecture: loads components from Firestore
and assembles them using PromptComponentService.

Session: 23 (Prompt Component Architecture Implementation)
Usage:
    python scripts/prompt/inspect_components_assembly.py --user-id <uuid>
"""

import asyncio
import argparse
import sys
import os
from datetime import datetime
from typing import Optional

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.services.prompt_component_service import PromptComponentService
from src.adapters.firestore_prompt_repository import FirestorePromptComponentRepository
from src.adapters.groovy_prompt_assembler import GroovyPromptAssembler
from src.domain.prompt import TEMPLATE_LIGHT, TEMPLATE_FULL


async def inspect_components_assembly(
    user_id: str,
    template_name: str = "light"
):
    print(f"\n🧩 INITIALIZING COMPONENT-BASED ASSEMBLY INSPECTION")
    print(f"User ID: {user_id}")
    print(f"Template: {template_name}")
    
    # 1. Setup Infrastructure
    config = load_settings()
    env_config = EnvironmentConfig()
    
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    # 2. Initialize PromptComponentService with real adapters
    collection_name = f"{env_config.firestore_collection_prefix}facts"
    repository = FirestorePromptComponentRepository(db_client, collection_name)
    assembler = GroovyPromptAssembler()
    service = PromptComponentService(
        repository=repository,
        assembler=assembler,
        cache_ttl=300  # 5 minutes
    )
    
    # 3. Select template
    template = TEMPLATE_LIGHT if template_name == "light" else TEMPLATE_FULL
    
    # 4. Assemble prompt
    print("🔨 Assembling prompt from components...")
    start_time = datetime.now()
    
    try:
        system_prompt = await service.get_assembled_prompt(template, user_id=user_id)
        
        assembly_time = (datetime.now() - start_time).total_seconds()
        
        # 5. Get component statistics
        components = await service.get_components_for_user(user_id, scope=None)
        default_count = sum(1 for c in components if not c.is_user_override)
        override_count = sum(1 for c in components if c.is_user_override)
        
        # 6. Get cache stats
        cache_stats = service.get_cache_stats()
        
        # 7. Generate report
        now = datetime.now()
        date_part = now.strftime("%Y-%m-%d")
        time_part = now.strftime("%H%M%S")
        user_short = user_id[:4]
        report_path = f"reports/prompt/{date_part}-components-{user_short}-{time_part}.md"
        os.makedirs("reports/prompt", exist_ok=True)
        
        # 8. Write report with metadata
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("# Component-Based Prompt Assembly Report\n\n")
            f.write(f"**Generated:** {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"**User ID:** {user_id}\n")
            f.write(f"**Template:** {template.name}\n")
            f.write(f"**Assembly Time:** {assembly_time:.3f}s\n\n")
            
            f.write("## Component Statistics\n\n")
            f.write(f"- **Total Components:** {len(components)}\n")
            f.write(f"- **Default Components:** {default_count}\n")
            f.write(f"- **User Overrides:** {override_count}\n\n")
            
            f.write("## Component List\n\n")
            for comp in sorted(components, key=lambda c: (c.scope.value, c.order)):
                override_marker = " [OVERRIDE]" if comp.is_user_override else ""
                f.write(f"- `{comp.id}` ({comp.scope.value}, order={comp.order}){override_marker}\n")
            
            f.write(f"\n## Cache Statistics\n\n")
            f.write(f"- **Total Entries:** {cache_stats['total_entries']}\n")
            f.write(f"- **Expired Entries:** {cache_stats['expired_entries']}\n")
            f.write(f"- **Cache TTL:** {cache_stats['cache_ttl_seconds']}s\n")
            f.write(f"- **Hit Ratio (estimate):** {cache_stats['cache_hit_ratio_estimate']}\n\n")
            
            f.write("## Assembled Prompt\n\n")
            f.write("```groovy\n")
            f.write(system_prompt)
            f.write("\n```\n")
        
        print(f"\n✨ DONE! Component-based prompt saved to: {report_path}")
        print(f"\n📊 Statistics:")
        print(f"  - Assembly time: {assembly_time:.3f}s")
        print(f"  - Total components: {len(components)} ({default_count} default + {override_count} overrides)")
        print(f"  - Prompt length: {len(system_prompt)} chars")
        print(f"  - Cache entries: {cache_stats['total_entries']}")
        
        print("\n--- PREVIEW (first 1000 chars) ---")
        print(system_prompt[:1000] + "..." if len(system_prompt) > 1000 else system_prompt)
        
    except Exception as e:
        print(f"\n❌ Error during assembly: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect Component-Based Prompt Assembly")
    parser.add_argument("--user-id", help="User UUID (defaults to PROD_USER_ID/DEV_USER_ID from env)")
    parser.add_argument("--template", choices=["light", "full"], default="light", 
                       help="Template to use (default: light)")

    args = parser.parse_args()
    user_id = args.user_id or os.getenv("PROD_USER_ID") or os.getenv("DEV_USER_ID")
    if not user_id:
        raise ValueError("USER_ID required: provide --user-id or set PROD_USER_ID/DEV_USER_ID in .env")

    asyncio.run(inspect_components_assembly(
        user_id=user_id,
        template_name=args.template
    ))

import asyncio
import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import firestore
from src.adapters.firestore_repo import FirestoreFactRepository
from src.services.prompt_builder import PromptBuilder
from src.services.brain_service import BrainService
from src.config.environment import EnvironmentConfig

async def main():
    print("⏳ Initializing components...")
    
    # 1. Initialize Firestore client and repository
    env_config = EnvironmentConfig()
    db_client = firestore.AsyncClient()
    repo = FirestoreFactRepository(db_client, env_config)
    
    # 2. Initialize PromptBuilder
    prompt_builder = PromptBuilder(repo)
    await prompt_builder.preload_components()
    
    # 3. Build Components
    print("⏳ Fetching prompt components...")
    components = await prompt_builder.build_system_prompt(mode="full")
    
    # 4. Format Prompt using BrainService logic
    # We instantiate BrainService with None for unused dependencies just to access the formatting method
    brain = BrainService(
        config={}, 
        repository=repo, 
        embedding_service=None, 
        llm_service=None,
        user_repo=None,
        quota_service=None,
        user_id="SYSTEM",
        account_id=None,
        prompt_builder=prompt_builder
    )
    
    full_prompt = brain._format_full_prompt(components)
    
    # 5. Output
    reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "prompt")
    os.makedirs(reports_dir, exist_ok=True)
    output_file = os.path.join(reports_dir, "debug_prompt_output.groovy")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(full_prompt)

    print(f"\n✅ System Prompt generated successfully!")
    print(f"📂 Saved to: {output_file}")
    print(f"📊 Size: {len(full_prompt)} characters")
    print("-" * 80)
    print(full_prompt)
    print("-" * 80)

if __name__ == "__main__":
    asyncio.run(main())

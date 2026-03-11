import asyncio
import difflib
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import firestore
from src.adapters.firestore_repo import FirestoreFactRepository
from src.config.environment import EnvironmentConfig
from src.services.prompt_builder import PromptBuilder
from src.services.brain_service import BrainService


async def _build_prompt(app_env: str, output_path: Path) -> str:
    os.environ["APP_ENV"] = app_env

    env_config = EnvironmentConfig()
    db_client = firestore.AsyncClient()
    repo = FirestoreFactRepository(db_client, env_config)

    prompt_builder = PromptBuilder(repo)
    await prompt_builder.preload_components()
    components = await prompt_builder.build_system_prompt(mode="full")

    brain = BrainService(
        config={},
        repository=repo,
        embedding_service=None,
        llm_service=None,
        prompt_builder=prompt_builder,
    )

    full_prompt = brain._format_full_prompt(components)
    output_path.write_text(full_prompt, encoding="utf-8")

    return full_prompt


async def main():
    print("🔍 Generating DEV and PROD prompt snapshots...")

    reports_dir = Path(__file__).resolve().parents[1] / "reports" / "prompt"
    reports_dir.mkdir(parents=True, exist_ok=True)
    dev_path = reports_dir / "debug_prompt_dev.groovy"
    prod_path = reports_dir / "debug_prompt_prod.groovy"
    report_path = reports_dir / "comparison_report.txt"

    dev_prompt = await _build_prompt("development", dev_path)
    prod_prompt = await _build_prompt("production", prod_path)

    diff = difflib.unified_diff(
        dev_prompt.splitlines(),
        prod_prompt.splitlines(),
        fromfile="DEV",
        tofile="PROD",
        lineterm="",
    )

    diff_text = "\n".join(diff)
    report_path.write_text(diff_text or "No differences found.", encoding="utf-8")

    if diff_text:
        print("⚠️ Differences detected between DEV and PROD prompts.")
        print(f"📄 Diff report saved to: {report_path}")
    else:
        print("✅ DEV and PROD prompts are identical.")
        print(f"📄 Diff report saved to: {report_path}")

    print(f"📂 DEV prompt: {dev_path}")
    print(f"📂 PROD prompt: {prod_path}")


if __name__ == "__main__":
    asyncio.run(main())

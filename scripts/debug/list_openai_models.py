"""
List OpenAI models via API to verify actual model IDs.
Usage: python scripts/debug/list_openai_models.py [--filter gpt-5]
"""
import asyncio
import os
import sys
import argparse
from dotenv import load_dotenv

load_dotenv()

import openai


async def main(filter_prefix: str | None) -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set in environment")
        sys.exit(1)

    client = openai.AsyncOpenAI(api_key=api_key)
    models = await client.models.list()

    rows = sorted(
        (m for m in models.data if not filter_prefix or m.id.startswith(filter_prefix)),
        key=lambda m: m.id,
    )

    if not rows:
        print(f"No models found matching prefix '{filter_prefix}'")
        return

    print(f"{'Model ID':<50} {'Owner':<20} {'Created'}")
    print("-" * 85)
    for m in rows:
        from datetime import datetime, timezone
        created = datetime.fromtimestamp(m.created, tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"{m.id:<50} {m.owned_by:<20} {created}")

    print(f"\nTotal: {len(rows)} models")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", default=None, help="Filter by model ID prefix (e.g. gpt-5)")
    args = parser.parse_args()
    asyncio.run(main(args.filter))

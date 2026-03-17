"""Quick test: fetch one Unsplash photo URL by keyword."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


async def main():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

    from src.adapters.unsplash_adapter import UnsplashAdapter

    key = os.getenv("UNSPLASH_ACCESS_KEY")
    if not key:
        print("UNSPLASH_ACCESS_KEY not set")
        return

    query = sys.argv[1] if len(sys.argv) > 1 else "mountains,fog"
    adapter = UnsplashAdapter(key)
    results = await adapter.search(query, count=1)

    if not results:
        print("No results")
        return

    r = results[0]
    print(f"URL:          {r.url}")
    print(f"Photographer: {r.photographer}")
    print(f"Profile:      {r.photographer_url}")


asyncio.run(main())

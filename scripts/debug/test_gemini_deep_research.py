"""
Quick smoke test for Gemini Deep Research API (interactions endpoint).
Usage: python scripts/debug/test_gemini_deep_research.py
"""
import asyncio
import time
from google import genai
from src.config.settings import load_settings

MODEL = "deep-research-pro-preview-12-2025"
QUERY = "What is the capital of France? Answer briefly."


async def main():
    settings = load_settings()
    api_key = settings.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set")
        return

    print(f"API key: {api_key[:8]}...")
    client = genai.Client(api_key=api_key)

    print(f"Submitting deep research job (model={MODEL})...")
    try:
        loop = asyncio.get_event_loop()
        interaction = await loop.run_in_executor(
            None,
            lambda: client.interactions.create(
                input=QUERY,
                agent=MODEL,
                background=True,
            ),
        )
        print(f"Job created: id={interaction.id}, status={interaction.status}")
    except Exception as e:
        print(f"ERROR on create: {e}")
        return

    print("Polling for up to 60s...")
    for i in range(12):
        await asyncio.sleep(5)
        try:
            interaction = await loop.run_in_executor(
                None,
                lambda: client.interactions.get(interaction.id),
            )
            print(f"  [{i+1}] status={interaction.status}")
            if interaction.status in ("completed", "failed"):
                if interaction.status == "completed":
                    text = getattr(interaction, "response", None) or ""
                    print(f"Result: {str(text)[:200]}")
                else:
                    print(f"Failed: {interaction}")
                break
        except Exception as e:
            print(f"  [{i+1}] ERROR on poll: {e}")
            break


asyncio.run(main())

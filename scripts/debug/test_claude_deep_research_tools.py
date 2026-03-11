"""
Quick smoke test for ClaudeDeepResearchRunnerAgent tool configuration.
Sends a minimal request to verify no 400 errors from tool definitions.
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from anthropic import AsyncAnthropic

NATIVE_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209",  "name": "web_fetch"},
]

MODEL = "claude-sonnet-4-6"


async def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ ANTHROPIC_API_KEY not set")
        sys.exit(1)

    client = AsyncAnthropic(api_key=api_key)

    print(f"Testing tools: {[t['name'] for t in NATIVE_TOOLS]}")
    print(f"Model: {MODEL}")
    print("Sending minimal request (1 turn, simple query)...\n")

    try:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": "What is 2+2? Answer briefly."}],
            tools=NATIVE_TOOLS,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            temperature=1.0,
        ) as stream:
            response = await stream.get_final_message()

        print(f"✅ OK — stop_reason={response.stop_reason} blocks={len(response.content)}")
        for block in response.content:
            t = getattr(block, "type", None)
            if t == "text":
                print(f"  [text] {block.text[:200]}")
            else:
                print(f"  [{t}]")

    except Exception as e:
        print(f"❌ FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

"""
Probe: what OpenAI native web_search actually counts as input/output.

Runs one Responses API call with the native web_search tool and dumps the full
usage breakdown so you can see, empirically, the token mechanics before committing
to an expensive pro-model custom deep-research loop.

NOTE: the per-search tool FEE is billed separately and does NOT appear in
response.usage — check it on the OpenAI pricing page. This probe only shows the
TOKEN layer (input incl. cached / search-result content, output incl. reasoning).

Usage:
  python scripts/validation/probe_openai_websearch_usage.py            # gpt-5-mini (cheap)
  python scripts/validation/probe_openai_websearch_usage.py gpt-5.5-pro  # the real thing (pricey)
"""
import os
import sys

import openai
import truststore
from dotenv import load_dotenv

from src.domain.billing import calculate_cost

truststore.inject_into_ssl()  # trust the OS keychain (e.g. Charles CA) before any TLS client is built
load_dotenv()

MODEL = sys.argv[1] if len(sys.argv) > 1 else "gpt-5-mini"
PROMPT = (
    "Research the 3 most significant developments in EU AI regulation in the last "
    "month. Use web search, check multiple sources, and give a cited summary."
)


def main() -> None:
    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    print(f"model={MODEL}\nprompt={PROMPT!r}\n--- calling (native web_search) ---")

    r = client.responses.create(
        model=MODEL,
        input=PROMPT,
        tools=[{"type": "web_search"}],
    )

    u = r.usage
    inp = getattr(u, "input_tokens", 0) or 0
    out = getattr(u, "output_tokens", 0) or 0
    itd = getattr(u, "input_tokens_details", None)
    otd = getattr(u, "output_tokens_details", None)
    cached = getattr(itd, "cached_tokens", 0) or 0 if itd else 0
    reasoning = getattr(otd, "reasoning_tokens", 0) or 0 if otd else 0

    types = [getattr(it, "type", "?") for it in r.output]
    n_search = sum(1 for t in types if t == "web_search_call")

    print("\n=== TOKEN LAYER (response.usage) ===")
    print(f"  input_tokens (TOTAL, incl. cached + search content): {inp:,}")
    print(f"    └ cached_tokens (billed 0.1x):                     {cached:,}")
    print(f"    └ uncached input (full rate):                      {inp - cached:,}")
    print(f"  output_tokens (TOTAL, incl. reasoning):              {out:,}")
    print(f"    └ reasoning_tokens (pro models: the big one):      {reasoning:,}")
    print(f"  web_search_call items in output:                     {n_search}")
    print(f"  output item types: {types}")

    # Token-only cost via the repo's pricing table (does NOT include per-search fee).
    cost = calculate_cost(
        model=MODEL,
        prompt_tokens=inp - cached,
        completion_tokens=out,
        cache_read_tokens=cached,
    )
    print(f"\n  token-only cost (NO per-search fee): ${cost:.4f}")
    print("  ⚠ add the web_search per-call fee separately (see OpenAI pricing).")
    print(f"  ⚠ {n_search} search call(s) this run — multiply by the per-1k fee.")


if __name__ == "__main__":
    main()

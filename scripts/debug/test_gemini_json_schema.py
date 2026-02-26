"""
Diagnostic script: isolate why gemini-flash-lite returns empty responses.

Tests progressively more complex configurations to pinpoint the failure.

Usage:
    python scripts/debug/test_gemini_json_schema.py
"""

import asyncio
import os
import sys

# Load .env
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from dotenv import load_dotenv
load_dotenv()

from google import genai
from google.genai import types

API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = "gemini-flash-lite-latest"

SYSTEM_INSTRUCTION = """You are a JSON key extractor. Given a SEARCH_REQUEST, return a JSON object with:
- keywords: array of 3-5 search terms
- primary_query: main search phrase
- alternative_query: alternative phrasing
- domains: array of 1-2 domain strings from ["biographical","possession","health","location","work","network","preference"]

Output JSON only."""

USER_MESSAGE = 'SEARCH_REQUEST "What cars do I own?"'

SCHEMA_DICT_UPPERCASE = {
    "type": "OBJECT",
    "properties": {
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}, "minItems": 3, "maxItems": 5},
        "primary_query": {"type": "STRING"},
        "alternative_query": {"type": "STRING"},
        "domains": {"type": "ARRAY", "items": {"type": "STRING"}, "maxItems": 2},
    },
    "required": ["keywords", "primary_query", "alternative_query", "domains"],
}

SCHEMA_DICT_LOWERCASE = {
    "type": "object",
    "properties": {
        "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 5},
        "primary_query": {"type": "string"},
        "alternative_query": {"type": "string"},
        "domains": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
    },
    "required": ["keywords", "primary_query", "alternative_query", "domains"],
}


async def call(label: str, **config_kwargs):
    client = genai.Client(api_key=API_KEY)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.0,
        max_output_tokens=200,
        safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
        ],
        **config_kwargs,
    )
    contents = [types.Content(role="user", parts=[types.Part(text=USER_MESSAGE)])]
    try:
        response = await client.aio.models.generate_content(model=MODEL, contents=contents, config=config)
        candidate = response.candidates[0] if response.candidates else None
        if candidate and candidate.content and candidate.content.parts:
            text = "".join(p.text for p in candidate.content.parts if p.text)
            finish = getattr(candidate, "finish_reason", "?")
            print(f"  ✅ [{label}] finish={finish} text={text[:120]!r}")
        else:
            finish = getattr(candidate, "finish_reason", "?") if candidate else "NO_CANDIDATE"
            print(f"  ❌ [{label}] EMPTY — finish_reason={finish}")
    except Exception as e:
        print(f"  💥 [{label}] EXCEPTION: {e}")


async def main():
    print(f"Model: {MODEL}\n")

    print("=== Test 1: plain text (no schema, no mime type) ===")
    await call("plain_text")

    print("\n=== Test 2: response_mime_type only (no schema) ===")
    await call("mime_only", response_mime_type="application/json")

    print("\n=== Test 3: response_schema uppercase (old Gemini style) ===")
    await call("schema_uppercase", response_mime_type="application/json", response_schema=SCHEMA_DICT_UPPERCASE)

    print("\n=== Test 4: response_schema lowercase (JSON Schema style) ===")
    await call("schema_lowercase", response_mime_type="application/json", response_schema=SCHEMA_DICT_LOWERCASE)

    print("\n=== Test 5: response_json_schema lowercase ===")
    await call("json_schema_lowercase", response_mime_type="application/json", response_json_schema=SCHEMA_DICT_LOWERCASE)

    print("\n=== Test 6: Test 5 + AFC disabled explicitly ===")
    await call(
        "json_schema_afc_disabled",
        response_mime_type="application/json",
        response_json_schema=SCHEMA_DICT_LOWERCASE,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    print("\n=== Test 7: Test 5 with actual MemorySearch system prompt (incl. CACHE_BOUNDARY) ===")
    # Load the actual failing prompt
    import glob
    prompt_files = sorted(glob.glob("debug_prompts/memory_search_prompt_*.txt"), reverse=True)
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        # Extract system instruction section
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()
            client2 = genai.Client(api_key=API_KEY)
            config2 = types.GenerateContentConfig(
                system_instruction=sys_instr,
                temperature=0.0,
                max_output_tokens=200,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
                response_json_schema=SCHEMA_DICT_LOWERCASE,
            )
            contents2 = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]
            try:
                response2 = await client2.aio.models.generate_content(model=MODEL, contents=contents2, config=config2)
                candidate2 = response2.candidates[0] if response2.candidates else None
                if candidate2 and candidate2.content and candidate2.content.parts:
                    text2 = "".join(p.text for p in candidate2.content.parts if p.text)
                    finish2 = getattr(candidate2, "finish_reason", "?")
                    print(f"  ✅ [real_prompt] finish={finish2} text={text2[:120]!r}")
                else:
                    finish2 = getattr(candidate2, "finish_reason", "?") if candidate2 else "NO_CANDIDATE"
                    print(f"  ❌ [real_prompt] EMPTY — finish_reason={finish2}")
            except Exception as e:
                print(f"  💥 [real_prompt] EXCEPTION: {e}")
        else:
            print("  ⚠️  Could not parse prompt file structure")
    else:
        print("  ⚠️  No memory_search_prompt files found")

    # Narrow down the cause within the real prompt
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()

            # Split at CACHE_BOUNDARY
            from src.ports.llm_service import PROMPT_CACHE_BOUNDARY
            if PROMPT_CACHE_BOUNDARY in sys_instr:
                groovy_part = sys_instr.split(PROMPT_CACHE_BOUNDARY)[0].strip()
                boundary_part = sys_instr.split(PROMPT_CACHE_BOUNDARY)[1].strip()
            else:
                groovy_part = sys_instr
                boundary_part = ""

            print("\n=== Test 8: Groovy prompt ONLY (no CACHE_BOUNDARY) ===")
            client3 = genai.Client(api_key=API_KEY)
            config3 = types.GenerateContentConfig(
                system_instruction=groovy_part,
                temperature=0.0,
                max_output_tokens=200,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
                response_json_schema=SCHEMA_DICT_LOWERCASE,
            )
            contents3 = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]
            try:
                r3 = await client3.aio.models.generate_content(model=MODEL, contents=contents3, config=config3)
                c3 = r3.candidates[0] if r3.candidates else None
                if c3 and c3.content and c3.content.parts:
                    t3 = "".join(p.text for p in c3.content.parts if p.text)
                    print(f"  ✅ [groovy_only] finish={getattr(c3,'finish_reason','?')} text={t3[:120]!r}")
                else:
                    print(f"  ❌ [groovy_only] EMPTY — finish_reason={getattr(c3,'finish_reason','?') if c3 else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [groovy_only] EXCEPTION: {e}")

            if boundary_part:
                print(f"\n=== Test 9: CACHE_BOUNDARY part only ===")
                print(f"  Boundary content: {boundary_part[:100]!r}")
                client4 = genai.Client(api_key=API_KEY)
                config4 = types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION + "\n\n" + PROMPT_CACHE_BOUNDARY + "\n" + boundary_part,
                    temperature=0.0,
                    max_output_tokens=200,
                    safety_settings=[
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ],
                    response_mime_type="application/json",
                    response_json_schema=SCHEMA_DICT_LOWERCASE,
                )
                try:
                    r4 = await client4.aio.models.generate_content(model=MODEL, contents=contents3, config=config4)
                    c4 = r4.candidates[0] if r4.candidates else None
                    if c4 and c4.content and c4.content.parts:
                        t4 = "".join(p.text for p in c4.content.parts if p.text)
                        print(f"  ✅ [simple+boundary] finish={getattr(c4,'finish_reason','?')} text={t4[:120]!r}")
                    else:
                        print(f"  ❌ [simple+boundary] EMPTY — finish_reason={getattr(c4,'finish_reason','?') if c4 else 'NO_CANDIDATE'}")
                except Exception as e:
                    print(f"  💥 [simple+boundary] EXCEPTION: {e}")

    # Test Groovy prompt combinations
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()
            from src.ports.llm_service import PROMPT_CACHE_BOUNDARY
            groovy_only = sys_instr.split(PROMPT_CACHE_BOUNDARY)[0].strip() if PROMPT_CACHE_BOUNDARY in sys_instr else sys_instr
            contents_real = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]

            print("\n=== Test 10: Groovy prompt + mime_type ONLY (no schema) ===")
            client5 = genai.Client(api_key=API_KEY)
            config5 = types.GenerateContentConfig(
                system_instruction=groovy_only,
                temperature=0.0,
                max_output_tokens=300,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
            )
            try:
                r5 = await client5.aio.models.generate_content(model=MODEL, contents=contents_real, config=config5)
                c5 = r5.candidates[0] if r5.candidates else None
                if c5 and c5.content and c5.content.parts:
                    t5 = "".join(p.text for p in c5.content.parts if p.text)
                    print(f"  ✅ [groovy+mime_only] finish={getattr(c5,'finish_reason','?')} text={t5[:200]!r}")
                else:
                    print(f"  ❌ [groovy+mime_only] EMPTY — finish_reason={getattr(c5,'finish_reason','?') if c5 else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [groovy+mime_only] EXCEPTION: {e}")

            print("\n=== Test 11: Groovy prompt + NO json mode at all ===")
            client6 = genai.Client(api_key=API_KEY)
            config6 = types.GenerateContentConfig(
                system_instruction=groovy_only,
                temperature=0.0,
                max_output_tokens=300,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
            )
            try:
                r6 = await client6.aio.models.generate_content(model=MODEL, contents=contents_real, config=config6)
                c6 = r6.candidates[0] if r6.candidates else None
                if c6 and c6.content and c6.content.parts:
                    t6 = "".join(p.text for p in c6.content.parts if p.text)
                    print(f"  ✅ [groovy+no_json] finish={getattr(c6,'finish_reason','?')} text={t6[:200]!r}")
                else:
                    print(f"  ❌ [groovy+no_json] EMPTY — finish_reason={getattr(c6,'finish_reason','?') if c6 else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [groovy+no_json] EXCEPTION: {e}")

    # Test 12: exact production config after fix (Groovy + CACHE_BOUNDARY + mime_type only)
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()
            contents_real = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]

            print("\n=== Test 12: FIXED — real prompt (Groovy + CACHE_BOUNDARY stripped by adapter) + mime_type ONLY ===")
            from src.ports.llm_service import PROMPT_CACHE_BOUNDARY
            sys_instr_stripped = sys_instr.replace(PROMPT_CACHE_BOUNDARY, "").strip()
            client12 = genai.Client(api_key=API_KEY)
            config12 = types.GenerateContentConfig(
                system_instruction=sys_instr_stripped,
                temperature=0.0,
                max_output_tokens=200,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
            )
            try:
                r12 = await client12.aio.models.generate_content(model=MODEL, contents=contents_real, config=config12)
                c12 = r12.candidates[0] if r12.candidates else None
                if c12 and c12.content and c12.content.parts:
                    t12 = "".join(p.text for p in c12.content.parts if p.text)
                    print(f"  ✅ [fixed_real] finish={getattr(c12,'finish_reason','?')}")
                    print(f"  Full response: {t12}")
                else:
                    print(f"  ❌ [fixed_real] EMPTY — finish_reason={getattr(c12,'finish_reason','?') if c12 else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [fixed_real] EXCEPTION: {e}")

    # Test 13: real prompt with CACHE_BOUNDARY marker stripped (keep content after it)
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()
            from src.ports.llm_service import PROMPT_CACHE_BOUNDARY
            contents_real = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]

            # Strip the marker but keep both parts
            stripped_instr = sys_instr.replace(PROMPT_CACHE_BOUNDARY, "").strip()

            print("\n=== Test 13: real prompt with CACHE_BOUNDARY marker removed (content kept) ===")
            client13 = genai.Client(api_key=API_KEY)
            config13 = types.GenerateContentConfig(
                system_instruction=stripped_instr,
                temperature=0.0,
                max_output_tokens=200,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
            )
            try:
                r13 = await client13.aio.models.generate_content(model=MODEL, contents=contents_real, config=config13)
                c13 = r13.candidates[0] if r13.candidates else None
                if c13 and c13.content and c13.content.parts:
                    t13 = "".join(p.text for p in c13.content.parts if p.text)
                    print(f"  ✅ [stripped_boundary] finish={getattr(c13,'finish_reason','?')}")
                    print(f"  Full response: {t13}")
                else:
                    print(f"  ❌ [stripped_boundary] EMPTY — finish_reason={getattr(c13,'finish_reason','?') if c13 else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [stripped_boundary] EXCEPTION: {e}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())

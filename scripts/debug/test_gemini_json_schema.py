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

    # Test 14: Groovy + CACHE_BOUNDARY stripped + response_schema UPPERCASE (pre-fa8b7c7 path)
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()
            from src.ports.llm_service import PROMPT_CACHE_BOUNDARY
            stripped = sys_instr.replace(PROMPT_CACHE_BOUNDARY, "").strip()
            contents_real = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]

            print("\n=== Test 14: Groovy + boundary stripped + response_schema UPPERCASE ===")
            client14 = genai.Client(api_key=API_KEY)
            config14 = types.GenerateContentConfig(
                system_instruction=stripped,
                temperature=0.0,
                max_output_tokens=200,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
                response_schema=SCHEMA_DICT_UPPERCASE,
            )
            try:
                r14 = await client14.aio.models.generate_content(model=MODEL, contents=contents_real, config=config14)
                c14 = r14.candidates[0] if r14.candidates else None
                if c14 and c14.content and c14.content.parts:
                    t14 = "".join(p.text for p in c14.content.parts if p.text)
                    print(f"  ✅ [groovy+stripped+schema_upper] finish={getattr(c14,'finish_reason','?')}")
                    print(f"  Full response: {t14}")
                else:
                    print(f"  ❌ [groovy+stripped+schema_upper] EMPTY — finish_reason={getattr(c14,'finish_reason','?') if c14 else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [groovy+stripped+schema_upper] EXCEPTION: {e}")

    # Tests 15-16: isolate whether current_date_time block breaks Groovy+mime_type
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()
            from src.ports.llm_service import PROMPT_CACHE_BOUNDARY
            groovy_only = sys_instr.split(PROMPT_CACHE_BOUNDARY)[0].strip() if PROMPT_CACHE_BOUNDARY in sys_instr else sys_instr
            after_boundary = sys_instr.split(PROMPT_CACHE_BOUNDARY)[1].strip() if PROMPT_CACHE_BOUNDARY in sys_instr else ""
            contents_real = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]

            print("\n=== Test 15: Groovy only (no boundary, no current_date_time) + mime_type ===")
            client15 = genai.Client(api_key=API_KEY)
            config15 = types.GenerateContentConfig(
                system_instruction=groovy_only,
                temperature=0.0, max_output_tokens=200,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
            )
            try:
                r15 = await client15.aio.models.generate_content(model=MODEL, contents=contents_real, config=config15)
                c15 = r15.candidates[0] if r15.candidates else None
                if c15 and c15.content and c15.content.parts:
                    t15 = "".join(p.text for p in c15.content.parts if p.text)
                    print(f"  ✅ [groovy_only_mime] text={t15[:100]!r}")
                else:
                    print(f"  ❌ [groovy_only_mime] EMPTY — finish_reason={getattr(c15,'finish_reason','?') if c15 else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [groovy_only_mime] EXCEPTION: {e}")

            if after_boundary:
                print(f"\n=== Test 16: Groovy + current_date_time block (no marker) + mime_type ===")
                print(f"  After-boundary content: {after_boundary[:80]!r}")
                combined = groovy_only + "\n\n" + after_boundary
                client16 = genai.Client(api_key=API_KEY)
                config16 = types.GenerateContentConfig(
                    system_instruction=combined,
                    temperature=0.0, max_output_tokens=200,
                    safety_settings=[
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ],
                    response_mime_type="application/json",
                )
                try:
                    r16 = await client16.aio.models.generate_content(model=MODEL, contents=contents_real, config=config16)
                    c16 = r16.candidates[0] if r16.candidates else None
                    if c16 and c16.content and c16.content.parts:
                        t16 = "".join(p.text for p in c16.content.parts if p.text)
                        print(f"  ✅ [groovy+datetime_mime] text={t16[:100]!r}")
                    else:
                        print(f"  ❌ [groovy+datetime_mime] EMPTY — finish_reason={getattr(c16,'finish_reason','?') if c16 else 'NO_CANDIDATE'}")
                except Exception as e:
                    print(f"  💥 [groovy+datetime_mime] EXCEPTION: {e}")

    # Test 17: datetime injected INSIDE Groovy class (before closing })
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()
            from src.ports.llm_service import PROMPT_CACHE_BOUNDARY
            groovy_only = sys_instr.split(PROMPT_CACHE_BOUNDARY)[0].strip() if PROMPT_CACHE_BOUNDARY in sys_instr else sys_instr
            after_boundary = sys_instr.split(PROMPT_CACHE_BOUNDARY)[1].strip() if PROMPT_CACHE_BOUNDARY in sys_instr else ""
            contents_real = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]

            # Inject datetime INSIDE the class: before the last closing }
            if after_boundary and groovy_only.endswith("}"):
                injected = groovy_only[:-1].rstrip() + "\n\n" + after_boundary + "\n\n}"
            else:
                injected = groovy_only

            print("\n=== Test 17: datetime injected INSIDE Groovy class (before closing }) + mime_type ===")
            print(f"  Injected tail: ...{injected[-120:]!r}")
            client17 = genai.Client(api_key=API_KEY)
            config17 = types.GenerateContentConfig(
                system_instruction=injected,
                temperature=0.0, max_output_tokens=200,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
            )
            try:
                r17 = await client17.aio.models.generate_content(model=MODEL, contents=contents_real, config=config17)
                c17 = r17.candidates[0] if r17.candidates else None
                if c17 and c17.content and c17.content.parts:
                    t17 = "".join(p.text for p in c17.content.parts if p.text)
                    print(f"  ✅ [datetime_inside_class] finish={getattr(c17,'finish_reason','?')}")
                    print(f"  Full response: {t17}")
                else:
                    print(f"  ❌ [datetime_inside_class] EMPTY — finish_reason={getattr(c17,'finish_reason','?') if c17 else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [datetime_inside_class] EXCEPTION: {e}")

    # Test 18: CACHE_BOUNDARY + datetime injected INSIDE class before closing }
    # This is the target production structure
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()
            from src.ports.llm_service import PROMPT_CACHE_BOUNDARY
            groovy_only = sys_instr.split(PROMPT_CACHE_BOUNDARY)[0].strip() if PROMPT_CACHE_BOUNDARY in sys_instr else sys_instr
            after_boundary = sys_instr.split(PROMPT_CACHE_BOUNDARY)[1].strip() if PROMPT_CACHE_BOUNDARY in sys_instr else ""
            contents_real = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]

            # Inject CACHE_BOUNDARY + datetime INSIDE class before closing }
            if after_boundary and groovy_only.endswith("}"):
                injected = groovy_only[:-1].rstrip() + "\n\n" + PROMPT_CACHE_BOUNDARY + "\n" + after_boundary + "\n\n}"
            else:
                injected = groovy_only

            print("\n=== Test 18: CACHE_BOUNDARY + datetime INSIDE class (before closing }) + mime_type ===")
            print(f"  Injected tail: ...{injected[-150:]!r}")
            client18 = genai.Client(api_key=API_KEY)
            config18 = types.GenerateContentConfig(
                system_instruction=injected,
                temperature=0.0, max_output_tokens=200,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
                response_schema=SCHEMA_DICT_UPPERCASE,
            )
            try:
                r18 = await client18.aio.models.generate_content(model=MODEL, contents=contents_real, config=config18)
                c18 = r18.candidates[0] if r18.candidates else None
                if c18 and c18.content and c18.content.parts:
                    t18 = "".join(p.text for p in c18.content.parts if p.text)
                    print(f"  ✅ [boundary_datetime_inside] finish={getattr(c18,'finish_reason','?')}")
                    print(f"  Full response: {t18}")
                else:
                    print(f"  ❌ [boundary_datetime_inside] EMPTY — finish_reason={getattr(c18,'finish_reason','?') if c18 else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [boundary_datetime_inside] EXCEPTION: {e}")

    # Test 18b: same as 18 but mime_type only (no schema) — isolate schema vs marker
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()
            from src.ports.llm_service import PROMPT_CACHE_BOUNDARY
            groovy_only = sys_instr.split(PROMPT_CACHE_BOUNDARY)[0].strip() if PROMPT_CACHE_BOUNDARY in sys_instr else sys_instr
            after_boundary = sys_instr.split(PROMPT_CACHE_BOUNDARY)[1].strip() if PROMPT_CACHE_BOUNDARY in sys_instr else ""
            contents_real = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]
            if after_boundary and groovy_only.endswith("}"):
                injected = groovy_only[:-1].rstrip() + "\n\n" + PROMPT_CACHE_BOUNDARY + "\n" + after_boundary + "\n\n}"
            else:
                injected = groovy_only

            print("\n=== Test 18b: CACHE_BOUNDARY + datetime INSIDE class + mime_type ONLY (no schema) ===")
            client18b = genai.Client(api_key=API_KEY)
            config18b = types.GenerateContentConfig(
                system_instruction=injected,
                temperature=0.0, max_output_tokens=200,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
            )
            try:
                r18b = await client18b.aio.models.generate_content(model=MODEL, contents=contents_real, config=config18b)
                c18b = r18b.candidates[0] if r18b.candidates else None
                if c18b and c18b.content and c18b.content.parts:
                    t18b = "".join(p.text for p in c18b.content.parts if p.text)
                    print(f"  ✅ [boundary_inside_mime_only] finish={getattr(c18b,'finish_reason','?')}")
                    print(f"  Full response: {t18b}")
                else:
                    print(f"  ❌ [boundary_inside_mime_only] EMPTY — finish_reason={getattr(c18b,'finish_reason','?') if c18b else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [boundary_inside_mime_only] EXCEPTION: {e}")

    # Test 19: MemorySearch Groovy + schema + real trailing content
    #          but with longer user message (like RouterAgent gets)
    #          Hypothesis: Flash Lite behaves differently with minimal vs rich context
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()
            contents_real = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]
            # Simulate router-style rich context: add fake conversation history before the search request
            rich_user_msg = (
                "user: [Feb 26, 14:10 UTC] Привіт, як справи?\n"
                "model: Добре, готовий допомагати.\n"
                "user: [Feb 26, 14:15 UTC] Розкажи про мою роботу\n"
                "model: Ти працюєш QA Lead на TravelBank.\n"
                f"user: [Feb 26, 14:45 UTC] {user_msg_line}"
            )
            contents_rich = [types.Content(role="user", parts=[types.Part(text=rich_user_msg)])]

            print("\n=== Test 19: MemorySearch Groovy + trailing datetime + schema + RICH user context ===")
            client19 = genai.Client(api_key=API_KEY)
            config19 = types.GenerateContentConfig(
                system_instruction=sys_instr,
                temperature=0.0, max_output_tokens=200,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
                response_schema=SCHEMA_DICT_UPPERCASE,
            )
            try:
                r19 = await client19.aio.models.generate_content(model=MODEL, contents=contents_rich, config=config19)
                c19 = r19.candidates[0] if r19.candidates else None
                if c19 and c19.content and c19.content.parts:
                    t19 = "".join(p.text for p in c19.content.parts if p.text)
                    print(f"  ✅ [groovy+schema+rich_context] finish={getattr(c19,'finish_reason','?')}")
                    print(f"  Full response: {t19}")
                else:
                    print(f"  ❌ [groovy+schema+rich_context] EMPTY — finish_reason={getattr(c19,'finish_reason','?') if c19 else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [groovy+schema+rich_context] EXCEPTION: {e}")

    # Tests 20a-20d: how much context is needed to unblock schema+Groovy+trailing datetime?
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()

            async def test_ctx(label, user_msg):
                client = genai.Client(api_key=API_KEY)
                cfg = types.GenerateContentConfig(
                    system_instruction=sys_instr,
                    temperature=0.0, max_output_tokens=200,
                    safety_settings=[
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ],
                    response_mime_type="application/json",
                    response_schema=SCHEMA_DICT_UPPERCASE,
                )
                r = await client.aio.models.generate_content(
                    model=MODEL,
                    contents=[types.Content(role="user", parts=[types.Part(text=user_msg)])],
                    config=cfg,
                )
                c = r.candidates[0] if r.candidates else None
                if c and c.content and c.content.parts:
                    t = "".join(p.text for p in c.content.parts if p.text)
                    print(f"  ✅ [{label}] {t[:80]!r}")
                else:
                    print(f"  ❌ [{label}] EMPTY")

            print("\n=== Tests 20a-20d: minimum context to unblock (no assembly changes) ===")
            # 20a: bare search request (current behavior)
            await test_ctx("20a_bare", user_msg_line)
            # 20b: one prior exchange
            await test_ctx("20b_1turn", f"user: Привіт\nmodel: Привіт!\nuser: {user_msg_line}")
            # 20c: just a system-hint line before the request
            await test_ctx("20c_hint", f"context: user is asking about work projects\n{user_msg_line}")
            # 20d: minimal — just repeat the request twice
            await test_ctx("20d_repeat", f"{user_msg_line}\n{user_msg_line}")
            # 20e: 3 turns with timestamps (exact Test 19 format) — reproducibility check
            rich3 = (
                "user: [Feb 26, 14:10 UTC] Привіт, як справи?\n"
                "model: Добре, готовий допомагати.\n"
                "user: [Feb 26, 14:15 UTC] Розкажи про мою роботу\n"
                "model: Ти працюєш QA Lead на TravelBank.\n"
                f"user: [Feb 26, 14:45 UTC] {user_msg_line}"
            )
            await test_ctx("20e_3turns", rich3)
            # 20f: 2 turns with timestamps
            rich2 = (
                "user: [Feb 26, 14:15 UTC] Розкажи про мою роботу\n"
                "model: Ти працюєш QA Lead на TravelBank.\n"
                f"user: [Feb 26, 14:45 UTC] {user_msg_line}"
            )
            await test_ctx("20f_2turns", rich2)

    # Test 21: biographical_context injected INSIDE Groovy class (before closing })
    # current_date_time stays TRAILING (no assembly change) — does bio context unblock schema?
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()
            user_msg_line = content.split("=== PROMPT ===")[1].strip()
            from src.ports.llm_service import PROMPT_CACHE_BOUNDARY
            groovy_only = sys_instr.split(PROMPT_CACHE_BOUNDARY)[0].strip() if PROMPT_CACHE_BOUNDARY in sys_instr else sys_instr
            after_boundary = sys_instr.split(PROMPT_CACHE_BOUNDARY)[1].strip() if PROMPT_CACHE_BOUNDARY in sys_instr else ""

            BIO_SNIPPET = """\
knowledge_base {
    biographical_context: '''
**Biographical**
- Full name: Dmytro Deleur (born June 13, 1972). Lives in Valencia, Spain.
- Ukrainian citizen, originally from Kyiv.

**Work**
- QA Lead Manager at TravelBank (iOS/web/mobile apps). Also maintains Test Cases internal project.

**Network**
- Wife: Olena. Sons: Dmytro (Chicago) and Nazar (Angers). Brother: Aleksey. Mother: Valentina.
    '''
}"""

            # Bio AFTER class closing }, BEFORE CACHE_BOUNDARY — like quick_response_prompt
            full_with_bio = groovy_only + "\n\n" + BIO_SNIPPET + "\n\n" + PROMPT_CACHE_BOUNDARY + "\n" + after_boundary if after_boundary else groovy_only + "\n\n" + BIO_SNIPPET

            contents_real = [types.Content(role="user", parts=[types.Part(text=user_msg_line)])]

            print("\n=== Test 21: bio AFTER class + datetime TRAILING (no assembly change) + schema ===")
            print(f"  Structure tail: ...{full_with_bio[-200:]!r}")
            client21 = genai.Client(api_key=API_KEY)
            config21 = types.GenerateContentConfig(
                system_instruction=full_with_bio,
                temperature=0.0, max_output_tokens=200,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ],
                response_mime_type="application/json",
                response_schema=SCHEMA_DICT_UPPERCASE,
            )
            try:
                r21 = await client21.aio.models.generate_content(model=MODEL, contents=contents_real, config=config21)
                c21 = r21.candidates[0] if r21.candidates else None
                if c21 and c21.content and c21.content.parts:
                    t21 = "".join(p.text for p in c21.content.parts if p.text)
                    print(f"  ✅ [bio_inside+datetime_trailing] finish={getattr(c21,'finish_reason','?')}")
                    print(f"  Full response: {t21}")
                else:
                    print(f"  ❌ [bio_inside+datetime_trailing] EMPTY — finish_reason={getattr(c21,'finish_reason','?') if c21 else 'NO_CANDIDATE'}")
            except Exception as e:
                print(f"  💥 [bio_inside+datetime_trailing] EXCEPTION: {e}")

    # Test 22: original short user message instead of expanded SEARCH_REQUEST
    if prompt_files:
        with open(prompt_files[0]) as f:
            content = f.read()
        if "=== SYSTEM INSTRUCTION ===" in content and "=== PROMPT ===" in content:
            sys_instr = content.split("=== SYSTEM INSTRUCTION ===")[1].split("=== PROMPT ===")[0].strip()

            short_messages = [
                ("22a_short_ru",  "а поищи все по моим рабочим проектам"),
                ("22b_short_en",  "search my work projects"),
                ("22c_wrapped",   'SEARCH_REQUEST "work projects"'),
                ("22d_full",      'SEARCH_REQUEST "Work projects: TravelBank iOS web mobile apps, Test Cases internal project, QA Lead Manager role, professional tasks"'),
            ]

            print("\n=== Tests 22a-d: short/natural user message vs expanded SEARCH_REQUEST ===")
            for label, msg in short_messages:
                client22 = genai.Client(api_key=API_KEY)
                cfg22 = types.GenerateContentConfig(
                    system_instruction=sys_instr,
                    temperature=0.0, max_output_tokens=200,
                    safety_settings=[
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ],
                    response_mime_type="application/json",
                    response_schema=SCHEMA_DICT_UPPERCASE,
                )
                r22 = await client22.aio.models.generate_content(
                    model=MODEL,
                    contents=[types.Content(role="user", parts=[types.Part(text=msg)])],
                    config=cfg22,
                )
                c22 = r22.candidates[0] if r22.candidates else None
                if c22 and c22.content and c22.content.parts:
                    t22 = "".join(p.text for p in c22.content.parts if p.text)
                    print(f"  ✅ [{label}] {msg[:50]!r} → {t22[:80]!r}")
                else:
                    print(f"  ❌ [{label}] {msg[:50]!r} → EMPTY")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())

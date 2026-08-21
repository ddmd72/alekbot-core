#!/usr/bin/env python3
"""
HtmlPageGenerator — one-pass test of an UNPUSHED prompt patch
================================================================
Runs the REAL HtmlPageGeneratorAgent (real PromptBuilder, real provider adapter,
real cost accounting) against arbitrary content, but with the COGNITIVE_PROCESS_HTML_PAGE
token patched in-memory to the version currently sitting in
prompts_snapshot/tokens/system/COGNITIVE_PROCESS_HTML_PAGE.groovy — WITHOUT touching
Firestore. `snapshot_upload.py --apply` is human-only by design; this script exists so
a prompt change can be judged on real output before anyone types that confirmation.

Mechanism: fetch the real live prompt via agent.prompt_builder.build_for_agent(), then
apply the exact same string substitutions that were made to the local .groovy mirror
(hardcoded below, one tuple per edit). Every substitution is asserted to match exactly
once against the LIVE text — if Firestore drifted since the local edit was made, this
fails loudly instead of silently testing against stale content.

Unlike ab_grok.py, this script does NOT call agent.execute() end-to-end — execute()
discards the visible design-brief declaration (step_5b_declare) after using it to cut
the HTML preamble. This script replicates execute()'s request construction, calls the
same _call_llm_recitation_aware() site, and prints/saves the brief text separately so
the emotion + lateral-move reasoning can actually be read, not just the resulting HTML.

Usage:
    python scripts/html_page/test_design_patch.py --text-file /path/to/content.txt
    python scripts/html_page/test_design_patch.py --text "inline content..."
    python scripts/html_page/test_design_patch.py --text-file content.txt --provider gemini
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv

load_dotenv()

from google.cloud import firestore

from src.composition.service_container import ServiceContainer
from src.composition.user_agent_factory import UserAgentFactory
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.config.settings import load_settings
from src.domain.billing import calculate_cost
from src.domain.complexity_settings import ComplexitySettings
from src.domain.llm import Message, MessagePart, PROMPT_CACHE_BOUNDARY
from src.agents.html_page_generator_agent import _resolve_unsplash_placeholders
from src.domain.user import PerformanceTier
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.ports.llm_port import LLMRequest

OUT_DIR = Path(__file__).parent.parent / "memory"

_TIERS = {
    "eco": PerformanceTier.ECO,
    "balanced": PerformanceTier.BALANCED,
    "performance": PerformanceTier.PERFORMANCE,
    "ultra": PerformanceTier.ULTRA,
}

# Exact (old, new) substrings from the three Edit calls applied to
# prompts_snapshot/tokens/system/COGNITIVE_PROCESS_HTML_PAGE.groovy today.
# Keep in sync with that file — this is what actually gets tested.
_PATCH = [
    (
        '    content: [\n'
        '        "Never use Lorem Ipsum. Write realistic, compelling, domain-specific copy.",\n'
        '        "Invent plausible statistics, company names, and testimonials if needed."\n'
        '    ]\n'
        '}',
        '    content: [\n'
        '        "Never use Lorem Ipsum. Write realistic, compelling, domain-specific copy.",\n'
        '        "Invent plausible statistics, company names, and testimonials if needed."\n'
        '    ]\n'
        '\n'
        '    avoid_ai_cliches: [\n'
        '        "Warm cream (#F4F1EA) background with a serif display face and terracotta accent.",\n'
        '        "Near-black background with a single acid-green or vermilion accent as the only color note.",\n'
        '        "A purple-to-blue gradient hero on an otherwise white page.",\n'
        '        "Inter or Space Grotesk as the default \'safe\' typeface when nothing else is specified.",\n'
        '        "Emoji used as section markers or bullet replacements.",\n'
        '        "Everything centered; no asymmetry anywhere on the page.",\n'
        '        "rounded-lg applied uniformly to every card, button, and container.",\n'
        '        "A colored accent bar/rail running down the side of every card.",\n'
        '        "Numbered markers (01/02/03) used as decoration, not because the content is an actual sequence."\n'
        '    ]\n'
        '}',
    ),
    (
        '    step_2_narrow {',
        '    step_1b_emotion {\n'
        '        action: """\n'
        '            Identify the dominant emotion(s) actually present in the provided content\n'
        '            itself — not the emotion generically expected of its category. A funeral home\n'
        '            site and a rescue-shelter adoption page are both "healthcare/wellness," but one\n'
        '            asks for solemnity, the other for hope. Name it in one or two words (e.g.\n'
        '            "urgent confidence," "quiet reverence," "playful defiance").\n'
        '        """\n'
        '    }\n'
        '\n'
        '    step_2_narrow {',
    ),
    (
        '            (3) Visual language: color system, typography, spacing rhythm.\n'
        '            (4) Two or three signature design patterns from the benchmark you will implement.\n'
        '        """\n'
        '    }\n'
        '\n'
        '    step_5_audit {\n'
        '        action: """\n'
        '            Read your brief as a senior designer at your chosen benchmark.\n'
        '            Name ONE decision that is generic — something that could belong to any site.\n'
        '            Rewrite it so it is unmistakably native to this benchmark\'s aesthetic.\n'
        '        """\n'
        '    }\n'
        '\n'
        '    step_5b_declare {\n'
        '        action: """\n'
        '            Before writing any HTML, output your decisions from steps 1–5 as plain visible\n'
        '            text — 3 to 5 sentences, concrete and specific, never generic: the content\n'
        '            classification, the ONE benchmark you named, and the core visual decisions\n'
        '            (color system, typography, one signature layout pattern you\'re borrowing).\n'
        '\n'
        '            This is the only text you output outside the HTML document.',
        '            (3) Visual language: color system, typography, spacing rhythm — each choice\n'
        '                must serve the emotion named in step_1b, not just the benchmark\'s default\n'
        '                palette.\n'
        '            (4) Two or three signature design patterns from the benchmark you will implement.\n'
        '        """\n'
        '    }\n'
        '\n'
        '    step_4b_lateral {\n'
        '        action: """\n'
        '            Name ONE layout or interaction decision that is genuinely unconventional for\n'
        '            this benchmark — something its own team would be unlikely to ship, chosen\n'
        '            because it serves THIS content\'s emotion (step_1b) better than the safe\n'
        '            version would. It must change how a section reads, scrolls, or is discovered —\n'
        '            not decoration. If you cannot justify it against the emotion, discard it and\n'
        '            pick another.\n'
        '        """\n'
        '    }\n'
        '\n'
        '    step_5_audit {\n'
        '        action: """\n'
        '            Read your brief as a senior designer at your chosen benchmark.\n'
        '            Name ONE decision that is generic — something that could belong to any site.\n'
        '            Rewrite it so it is unmistakably native to this benchmark\'s aesthetic.\n'
        '\n'
        '            Then check two more things:\n'
        '            — Does the layout still read as the emotion named in step_1b, or did it get\n'
        '              lost under the benchmark\'s default mood? If lost, adjust color/type/spacing\n'
        '              until it reads again.\n'
        '            — Does the brief land on any pattern in TechnicalGuardrails.avoid_ai_cliches?\n'
        '              If so, replace it with a choice this benchmark\'s team would actually make.\n'
        '        """\n'
        '    }\n'
        '\n'
        '    step_5b_declare {\n'
        '        action: """\n'
        '            Before writing any HTML, output your decisions from steps 1–5b as plain visible\n'
        '            text — 4 to 6 sentences, concrete and specific, never generic: the content\n'
        '            classification, the emotion named in step_1b, the ONE benchmark you named, the\n'
        '            core visual decisions (color system, typography, one signature layout pattern\n'
        '            you\'re borrowing), and the one deliberately unconventional decision from\n'
        '            step_4b plus why it serves the emotion.\n'
        '\n'
        '            This is the only text you output outside the HTML document.',
    ),
]

# --- Variant: SITREP --- adds a 5th domain option for (B) Document/report, so a
# grim daily briefing isn't squeezed between only editorial_journalism/data_media.
_PATCH_SITREP = [
    (
        '        healthcare_wellness: "One Medical (human-centred clinical) · Calm (stillness as product) · Headspace (science-backed warmth)"\n'
        '    }\n'
        '}',
        '        healthcare_wellness: "One Medical (human-centred clinical) · Calm (stillness as product) · Headspace (science-backed warmth)"\n'
        '        intelligence_briefing: "Bloomberg Terminal (amber-on-black monospace, data over prose) · Declassified cable (classification stamp, redaction bars, typewriter mono) · Stratfor geopolitical brief (threat-mapped analysis for a narrow readership) · NATO SITREP (Zulu time, terse abbreviation, grid references)"\n'
        '    }\n'
        '}',
    ),
    (
        '            (B) Document / report   → editorial_journalism, data_media, education_learning, corporate_fintech\n',
        '            (B) Document / report   → editorial_journalism, data_media, education_learning, corporate_fintech, intelligence_briefing\n',
    ),
]

# --- Variant: NODOMAIN --- removes the fixed benchmark catalogue entirely; the model
# grounds palette/type/layout in the subject itself instead of imitating a named brand
# (the artifact-design principle: "ground it in the subject", not benchmark-imitation).
_PATCH_NODOMAIN = [
    (
        'class HtmlPageDesigner {\n'
        '    identity: "Senior frontend designer and engineer with a deep understanding of diverse industry aesthetics."\n'
        '    framing: """\n'
        '        You do not apply generic "good design" rules that make every page look the same.\n'
        '        Your strength is stylistic variety. A SaaS site, a luxury fashion brand, and an experimental portfolio\n'
        '        require fundamentally different approaches to layout, typography, and color.\n'
        '        Use the provided benchmarks as deep inspiration for the *vibe* and *quality*, but feel free to create\n'
        '        unique interpretations.\n'
        '\n'
        '        The standard for every page you generate: would a senior designer at your chosen\n'
        '        benchmark recognize it as native to their work?\n'
        '    """\n'
        '    produces: "A single, complete, self-contained HTML document that feels like a top-tier production page in its specific domain."\n'
        '}',
        'class HtmlPageDesigner {\n'
        '    identity: "Senior frontend designer and engineer with a deep understanding of diverse industry aesthetics."\n'
        '    framing: """\n'
        '        You do not apply generic "good design" rules that make every page look the same.\n'
        '        Your strength is stylistic variety. A SaaS site, a luxury fashion brand, and an experimental portfolio\n'
        '        require fundamentally different approaches to layout, typography, and color.\n'
        '        Do not reach for a known brand\'s identity as a shortcut — derive the visual language\n'
        '        from this subject\'s own world, so no two subjects in the same category look alike.\n'
        '\n'
        '        The standard for every page you generate: would someone who has spent years inside\n'
        '        this subject\'s world recognize it as native, not generic?\n'
        '    """\n'
        '    produces: "A single, complete, self-contained HTML document that feels like a top-tier production page in its specific domain."\n'
        '}',
    ),
    (
        '    step_2_narrow {\n'
        '        action: """\n'
        '            Based on your content type, only these domains are valid candidates.\n'
        '            You MUST pick from this list — do not consider others.\n'
        '            (A) Marketing / brand   → saas_productivity, corporate_fintech, consumer_tech, healthcare_wellness\n'
        '            (B) Document / report   → editorial_journalism, data_media, education_learning, corporate_fintech\n'
        '            (C) Creative / personal → cv_portfolio, photography_art, fashion_luxury, architecture, experimental\n'
        '            (D) Event / experience  → event_conference, restaurant_hospitality, fine_art_museum\n'
        '            (E) Commerce            → ecommerce_retail, consumer_tech, fashion_luxury\n'
        '        """\n'
        '    }\n'
        '\n'
        '    step_3_pick {\n'
        '        action: """\n'
        '            Score this content on three axes:\n'
        '            — Tone:    formal ←————→ casual\n'
        '            — Density: flowing narrative ←————→ structured data (tables, numbered sections, comparisons, stats)\n'
        '            — Mood:    light  ←————→ dark\n'
        '\n'
        '            Find the benchmark from your candidate domains whose aesthetic personality\n'
        '            best matches this score profile. Reason through each benchmark\'s character —\n'
        '            there is no lookup table.\n'
        '\n'
        '            Name ONE specific benchmark site (e.g. "The Atlantic", not "editorial_journalism").\n'
        '            From this point, design as a member of that site\'s team. Inhabit the full aesthetic.\n'
        '        """\n'
        '    }',
        '    step_2_ground {\n'
        '        action: """\n'
        '            Do not reach for an existing brand, publication, or product to imitate.\n'
        '            Ground every visual decision in the SUBJECT of this content itself — its own\n'
        '            materials, instruments, textures, and vernacular — not in a borrowed identity.\n'
        '\n'
        '            Score this content on three axes:\n'
        '            — Tone:    formal ←————→ casual\n'
        '            — Density: flowing narrative ←————→ structured data (tables, numbered sections, comparisons, stats)\n'
        '            — Mood:    light  ←————→ dark\n'
        '\n'
        '            Name, in your own words, the specific visual world this subject belongs to —\n'
        '            not a company or publication name (e.g. "a field naturalist\'s specimen log,"\n'
        '            not "Nothing (transparent, cult following)"). This becomes your frame of\n'
        '            reference for every decision from here on.\n'
        '        """\n'
        '    }',
    ),
    (
        '            (3) Visual language: color system, typography, spacing rhythm — each choice\n'
        '                must serve the emotion named in step_1b, not just the benchmark\'s default\n'
        '                palette.\n'
        '            (4) Two or three signature design patterns from the benchmark you will implement.',
        '            (3) Visual language: color system, typography, spacing rhythm — each choice\n'
        '                must serve the emotion named in step_1b, derived from the subject, not a\n'
        '                default palette.\n'
        '            (4) Two or three signature design patterns drawn from that visual world you will implement.',
    ),
    (
        '            Speak from inside the benchmark\'s aesthetic — not about it.\n'
        '            Cover:\n'
        '            (1) Navigation and orientation: how does this benchmark let users know where they\n'
        '                are and move through content? Describe the specific navigation system you will build.',
        '            Speak from inside the visual world you named — not about it.\n'
        '            Cover:\n'
        '            (1) Navigation and orientation: how does this visual world let people know where\n'
        '                they are and move through it? Describe the specific navigation system you will build.',
    ),
    (
        '            Name ONE layout or interaction decision that is genuinely unconventional for\n'
        '            this benchmark — something its own team would be unlikely to ship, chosen\n'
        '            because it serves THIS content\'s emotion (step_1b) better than the safe',
        '            Name ONE layout or interaction decision that is genuinely unconventional —\n'
        '            something a safe, generic version of this page would never do, chosen because\n'
        '            it serves THIS content\'s emotion (step_1b) better than the safe',
    ),
    (
        '            Read your brief as a senior designer at your chosen benchmark.\n'
        '            Name ONE decision that is generic — something that could belong to any site.\n'
        '            Rewrite it so it is unmistakably native to this benchmark\'s aesthetic.\n'
        '\n'
        '            Then check two more things:\n'
        '            — Does the layout still read as the emotion named in step_1b, or did it get\n'
        '              lost under the benchmark\'s default mood? If lost, adjust color/type/spacing\n'
        '              until it reads again.\n'
        '            — Does the brief land on any pattern in TechnicalGuardrails.avoid_ai_cliches?\n'
        '              If so, replace it with a choice this benchmark\'s team would actually make.',
        '            Read your brief with fresh eyes.\n'
        '            Name ONE decision that is generic — something that could belong to any page\n'
        '            of this type, regardless of subject. Rewrite it so it is unmistakably native\n'
        '            to THIS subject\'s visual world.\n'
        '\n'
        '            Then check two more things:\n'
        '            — Does the layout still read as the emotion named in step_1b, or did it get\n'
        '              lost under a generic default mood? If lost, adjust color/type/spacing\n'
        '              until it reads again.\n'
        '            — Does the brief land on any pattern in TechnicalGuardrails.avoid_ai_cliches?\n'
        '              If so, replace it with a choice that genuinely belongs to this subject.',
    ),
    (
        '            classification, the emotion named in step_1b, the ONE benchmark you named, the\n'
        '            core visual decisions (color system, typography, one signature layout pattern\n'
        '            you\'re borrowing), and the one deliberately unconventional decision from',
        '            classification, the emotion named in step_1b, the visual world you named, the\n'
        '            core visual decisions (color system, typography, one signature layout pattern),\n'
        '            and the one deliberately unconventional decision from',
    ),
]

_DOCTYPE_RE = re.compile(r"<!DOCTYPE\s+html", re.IGNORECASE)

# The live prompt wraps the token body in `class HtmlPageAgent extends Agent {
# cognitive_process { ... } }` (per the blueprint's class_order), which re-indents
# every line of the token by two extra nesting levels vs. the flat .groovy mirror.
_LIVE_INDENT_DELTA = 8


def _reindent(text: str, delta: int) -> str:
    pad = " " * delta
    return "\n".join(pad + line if line.strip() else line for line in text.split("\n"))


_VARIANTS = {
    "base": [],
    "sitrep": _PATCH_SITREP,
    "nodomain": _PATCH_NODOMAIN,
}


def apply_patch(live_prompt: str, variant: str = "base") -> str:
    patch_list = _PATCH + _VARIANTS[variant]
    patched = live_prompt
    for i, (old_raw, new_raw) in enumerate(patch_list, 1):
        old = _reindent(old_raw, _LIVE_INDENT_DELTA)
        new = _reindent(new_raw, _LIVE_INDENT_DELTA)
        count = patched.count(old)
        if count != 1:
            raise SystemExit(
                f"PATCH {i}/{len(patch_list)} (variant={variant}) matched {count} times in "
                f"the LIVE prompt (expected 1) — either Firestore drifted since the local "
                f".groovy edit, or an earlier patch in the chain already changed this text. "
                f"Re-check prompts_snapshot/tokens/system/COGNITIVE_PROCESS_HTML_PAGE.groovy "
                f"vs the live token before trusting this test."
            )
        patched = patched.replace(old, new)
    return patched


def pin_provider(agent, container, config, provider_name: str, tier: PerformanceTier) -> str:
    ctx = container.context_builder.resolve_for_task(
        "html_page", config, ComplexitySettings(tier=tier, provider_override=provider_name),
    )
    ctx.fallback_provider = None
    ctx.fallback_provider_name = None
    agent._llm = ctx.provider
    agent.model_name = ctx.model_name
    agent._agent_execution_context = ctx
    return ctx.model_name


async def main(query: str, provider: str, tier_name: str, user_id: str, account_id: str, variant: str = "base") -> None:
    tier = _TIERS[tier_name]
    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    config_settings = load_settings()
    env_config = config_settings["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    coordinator = AgentCoordinator()
    container = ServiceContainer(config=config_settings, db_client=db, env_config=env_config, account_repo=account_repo)

    profile = await user_repo.get_user(user_id)
    if not profile:
        print(f"ERROR: user {user_id} not found.")
        return

    factory = UserAgentFactory(
        config=config_settings, env_config=env_config, coordinator=coordinator,
        user_repo=user_repo, account_repo=account_repo, **container.agent_services(),
    )
    print("Creating agents...")
    await factory.ensure_agents_for_user(user_id)
    await factory.create_agent_on_demand("html_page", user_id)
    agent = coordinator.get_agent(f"html_page_generator_agent_{user_id}")
    if agent is None:
        print("ERROR: html_page agent not created.")
        return

    model = pin_provider(agent, container, profile.config, provider, tier)

    # Fetch the REAL live prompt, then splice in the local (unpushed) patch.
    raw_prompt = await agent.prompt_builder.build_for_agent(
        account_id=account_id, agent_type="html_page", user_id=agent.user_id, include_biographical=False,
    )
    (OUT_DIR / "_debug_raw_prompt.txt").write_text(raw_prompt, encoding="utf-8")
    patched_prompt = apply_patch(raw_prompt, variant=variant)
    groovy_instructions = patched_prompt.split(PROMPT_CACHE_BOUNDARY)[0].strip()
    system_instruction = f"REQUEST\n\n{query}\n\n---\n\n{groovy_instructions}"

    request = LLMRequest(
        model_name=agent.model_name,
        system_instruction=system_instruction,
        messages=[Message(role="user", parts=[MessagePart(text="Generate the HTML page for the request in the REQUEST block above.")])],
        temperature=agent.TEMPERATURE,
        max_tokens=agent.MAX_TOKENS,
        thinking=agent.THINKING_EFFORT or None,
        timeout=agent.REQUEST_TIMEOUT_S,
    )

    print(f"\n{'='*70}\n  provider: {provider} @ {tier_name} -> {model}\n  input   : {len(query)} chars (PATCHED prompt, not pushed to Firestore)\n{'='*70}\n  running...", flush=True)
    started = time.perf_counter()
    response = await agent._call_llm_recitation_aware(request)
    elapsed = time.perf_counter() - started

    text = (response.text or "").strip()
    match = _DOCTYPE_RE.search(text)
    if match:
        brief = text[:match.start()].strip()
        html = text[match.start():]
    else:
        brief = "(no preamble found before <!DOCTYPE html> — check output)"
        html = text

    um = response.usage_metadata
    cost = calculate_cost(
        model=model,
        prompt_tokens=(um.prompt_tokens if um else 0) or 0,
        completion_tokens=(um.completion_tokens if um else 0) or 0,
        cache_read_tokens=(getattr(um, "cache_read_tokens", 0) if um else 0) or 0,
    )

    if agent._image_search:
        html = await _resolve_unsplash_placeholders(html, agent._image_search)
    else:
        print("  ⚠️  agent._image_search is None — Unsplash placeholders left unresolved "
              "(check UNSPLASH_ACCESS_KEY in .env)")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"html_page_patch_test_{stamp}.html"
    out.write_text(html, encoding="utf-8")

    print(f"\n{'='*70}\n  DECLARED DESIGN BRIEF (step_5b_declare output):\n{'='*70}\n{brief}\n")
    print(f"{'='*70}\n  wall time: {elapsed:.1f}s   cost: ${cost:.4f}   html: {len(html):,} chars")
    print(f"  saved to : {out}\n{'='*70}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="Inline content to design a page for")
    ap.add_argument("--text-file", help="Path to a file with the content")
    ap.add_argument("--provider", default="grok")
    ap.add_argument("--tier", default="performance", choices=list(_TIERS))
    ap.add_argument("--variant", default="base", choices=list(_VARIANTS))
    ap.add_argument("--user-id", default=os.getenv("DEV_USER_ID"))
    ap.add_argument("--account-id", default=os.getenv("DEV_ACCOUNT_ID"))
    args = ap.parse_args()

    if not args.text and not args.text_file:
        print("ERROR: pass --text or --text-file")
        sys.exit(1)
    if not args.user_id or not args.account_id:
        print("ERROR: DEV_USER_ID / DEV_ACCOUNT_ID missing (.env or --user-id/--account-id)")
        sys.exit(1)

    query = args.text if args.text else Path(args.text_file).read_text(encoding="utf-8")
    asyncio.run(main(query, args.provider, args.tier, args.user_id, args.account_id, args.variant))

"""
POC: single LLM call → HTML+CSS → Puppeteer → PDF.

No JSON spec, no planner/generator split. One Gemini call formats content as HTML.
Pass document text via stdin or --file flag; falls back to DEFAULT_QUERY.

Usage:
    # With document text from file:
    python scripts/debug/test_pdf_direct_html.py --file /path/to/document.txt

    # With text piped via stdin:
    cat document.txt | python scripts/debug/test_pdf_direct_html.py

    # With a topic query (LLM writes content too):
    python scripts/debug/test_pdf_direct_html.py "your query"

    # Override model:
    python scripts/debug/test_pdf_direct_html.py --file doc.txt --model gemini-flash-latest

    # Web viewport mode (no PDF framing):
    python scripts/debug/test_pdf_direct_html.py --web

    # Run all styles and compare:
    python scripts/debug/test_pdf_direct_html.py --batch
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from dotenv import load_dotenv
load_dotenv()

from google import genai
from google.genai import types

from src.adapters.node_puppeteer_runner import NodePuppeteerRunner
from src.ports.puppeteer_runner_port import PuppeteerRunnerError


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_INPUT_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memory", "document.txt")
MODEL = "gemini-pro-latest"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memory")


# ---------------------------------------------------------------------------
# Style catalogue (batch mode)
# ---------------------------------------------------------------------------

STYLES = {
    # --- original set ---
    "apple_keynote":    "Use the visual language of Apple Human Interface Guidelines (print/keynote variant), adapted for A4 PDF. Follow their rules for typography (large, confident scale), whitespace (extremely generous), rounded corners (8–12px), minimal chrome, and bold visual moments. No decorative clutter.",
    "notion":           "Use the visual language of Notion, adapted for A4 PDF. Follow their rules for typography (clean sans-serif, tight hierarchy), whitespace (soft and airy), color (neutral grays with subtle blue accents), and section dividers (hairline rules). Long-form reading comfort above all.",
    "linear_changelog": "Use the visual language of Linear's changelog and product design, adapted for A4 PDF. Follow their rules for typography (sharp, monochrome-first), spacing (tight and information-dense), color (black/white + one accent), and section structure (bold label + content blocks).",
    "stripe_report":    "Use the visual language of Stripe Annual Report, adapted for A4 PDF. Follow their rules for editorial layout (strong color blocks as section openers), large pull statistics, polished professional typography, and high-contrast section headers.",
    "pitch":            "Use the visual language of Pitch.com documents, adapted for A4 PDF. Follow their rules for bold section openers, card-based content layout, expressive use of color per section, and presentation-document hybrid structure.",
    "mckinsey_bcg":     "Use the visual language of McKinsey/BCG consulting reports, adapted for A4 PDF. Follow their rules for authoritative structure, clear callout and insight boxes, data-forward layout, professional neutrals with one accent color, and tight executive summary blocks.",
    "stripe_docs":      "Use the visual language of Stripe Documentation, adapted for A4 PDF. Follow their rules for clean sans-serif typography, precise content hierarchy, generous internal padding on callout blocks (info/warning/tip), and pastel accent fills for block types.",
    # --- new additions ---
    "govuk":            "Use the visual language of the Gov.uk Design System, adapted for A4 PDF. Follow their rules for ultimate readability: bold black headings, generous line spacing (1.8+), no decorative elements, high-contrast body text, structured warning and info panels, and typography that any reader can parse instantly.",
    "economist":        "Use the visual language of The Economist magazine, adapted for A4 PDF. Follow their rules for dense editorial layout, classic serif headings with a strong red accent, pull quotes as first-class content, clean data tables without excessive borders, and long-form readability with tight but comfortable line spacing.",
    "ibm_carbon":       "Use the visual language of IBM Carbon Design System, adapted for A4 PDF. Follow their rules for industrial-grade grid discipline, monospaced data presentation, 1px rule separators between sections, cool neutral palette, and structured B2B analytical layout.",
    "tufte":            "Use the visual language of Edward Tufte's information design principles, adapted for A4 PDF. Follow their rules for scientific minimalism: wide margins for sidenotes, no chartjunk, borderless tables with ruled lines only where needed, inline data integrated with prose, and letting content hierarchy emerge from typography alone.",
    "material3":        "Use the visual language of Google Material Design 3, adapted for A4 PDF. Follow their rules for card-based content containers, bold color surface layers (tonal fills), strong typographic scale (Display → Body), and clear visual separation between content zones via elevation and fill.",
}


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_PROMPT_BASE = """\
You are a senior product designer and front-end engineer specialising in screen-optimised PDF documents.

The output will be saved as a PDF and read on screen — not printed on paper.
This changes everything: you can use rich color, gradients, large type, generous spacing, and visual effects
that would be wasteful on paper but make screen reading a pleasure.

You receive information — topics, analysis, data, arguments.
This is a design brief, not a formatting request. Think: what is the best possible reading experience for this content?
Invent the layout from scratch. Use whatever patterns fit — hero section, sidebar, card grid,
timeline, stat callouts, pull quotes, color-coded sections. Mix and match freely.

First, read the content and select the most appropriate design language from this catalogue:
  apple_keynote   → presentations, executive summaries, luxury/brand, imaged materials
  economist       → news analysis, geopolitics, editorial long-reads
  govuk           → legal documents, official reports, regulations, instructions
  mckinsey_bcg    → business analysis, strategic reports, consulting
  stripe_report   → financial reports, annual reports, company metrics
  tufte           → academic papers, scientific analysis, research
  stripe_docs     → technical documentation, API references, developer guides
  ibm_carbon      → enterprise analytics, B2B reports, data-heavy documents
  notion          → general documents, knowledge base, how-to guides
  material3       → modern app-style, mobile-first, product docs
  pitch           → pitch decks, investor materials, startup documents
  linear_changelog→ release notes, product updates, changelogs
Apply the chosen design language faithfully — its typography rules, spacing, color coding, and layout patterns.
{design_language}

Design principles:
- Screen readability first: font size minimum 15px body, 20–36px headings. Line height ≥ 1.7. Max line length 70ch.
- Rich color: choose a deliberate color scheme (2–3 colors). Use fills, gradients, colored section headers.
- Visual effects are welcome: box shadows, rounded corners, colored borders, background tints, highlight bands.
- Emojis as visual anchors: use them sparingly but effectively for section markers, callouts, key points.
- Generous spacing: padding and margins should feel comfortable, not cramped.
- Every section must feel intentionally designed — not just text with a heading.
- CSS infographics where appropriate: timelines, step flows, comparison tables, stat blocks — built from pure HTML/CSS, no images or external resources.
- Page density: content should fill pages naturally — no large blank areas at page bottoms.
- Preserve ALL content verbatim — every sentence must appear somewhere, nothing omitted.

Technical rules (non-negotiable):
- Output ONLY the HTML document. No explanations, no markdown fences.
- Start with <!DOCTYPE html> and end with </html>.
- <meta charset="UTF-8"> required.
- All CSS in <style> tags. No external resources, no CDN fonts.
- @page {{ size: A4 portrait; margin: 15mm; }}
- -webkit-print-color-adjust: exact; print-color-adjust: exact.
- body {{ width: 794px; margin: 0 auto; box-sizing: border-box; }}
- Page break rules (mandatory):
    break-inside: avoid  on  .section, table, tr, .callout, h2, h3, h4, li, blockquote
    break-after:  avoid  on  h1, h2, h3, h4
    NEVER use break-before: page or page-break-before: always
- Mobile reading experience (mandatory): add @media (max-width: 600px) block.
    Think: someone reading this on a phone while commuting — scrolling, not paginating.
    Design for that: the mobile version should feel like a native mobile reading app (Notion Mobile, well-designed article).
    Specifically: body width 100%, padding 5vw; font-size 17px, line-height 1.85;
    all multi-column and sidebar layouts collapse to single column;
    section headers become full-width, visually prominent scroll anchors;
    callout blocks and pull quotes span full width;
    tables get overflow-x: auto so they scroll horizontally if needed;
    no decorative elements that require hover to make sense.
"""

def _build_prompt(style_key: str = "") -> str:
    if style_key and style_key in STYLES:
        override = f"\nYou MUST use the {style_key} design language — do not choose another."
    else:
        override = ""
    return _PROMPT_BASE.format(design_language=override)


# Default: auto style selection
SYSTEM_PROMPT_FORMAT = _build_prompt()


# Web prompt base — mobile-first, no PDF framing, style-aware.
_PROMPT_BASE_WEB = """\
You are a senior product designer and front-end engineer.
Design language: {design_language}

You receive information — topics, analysis, data, arguments.
This is a design brief, not a formatting request. Think: what is the best possible web page for this content?
Invent the layout from scratch. Use whatever patterns fit — hero section, sticky nav, card grid,
timeline, stat highlights, callout panels, pull quotes. Mix and match freely.

This page will be read on screens of all sizes. Mobile is the primary target.

Design principles:
- Mobile-first: design primarily for a phone screen (390px wide). Desktop (794px) is secondary.
- Strong typographic hierarchy — size, weight, color to guide the eye while scrolling.
- Generous whitespace, color blocks, borders, background fills to separate content zones.
- Every section must feel intentionally designed, not just text with a heading.
- CSS infographics where appropriate: timelines, step flows, comparison tables — pure HTML/CSS only.
- Visual restructuring encouraged: reorder, add callouts, pull quotes, TOC — whatever serves the reader.
- Preserve ALL content verbatim — every sentence must appear somewhere, nothing omitted.

Technical rules (non-negotiable):
- Output ONLY the HTML document. No explanations, no markdown fences.
- Start with <!DOCTYPE html> and end with </html>.
- <meta charset="UTF-8"> and <meta name="viewport" content="width=device-width, initial-scale=1"> required.
- All CSS in <style> tags. No external resources, no CDN fonts.
- Default styles target mobile (max-width 390px body, padding 5vw).
- @media (min-width: 600px) block for tablet/desktop: body max-width 794px, centered.
- font-size 17px body, line-height 1.85, max-width 70ch for prose blocks.
- Tables: overflow-x auto, scroll horizontally on mobile.
- No hover-only interactions.
"""

def _build_web_prompt(style_key: str) -> str:
    design_language = STYLES.get(style_key, STYLES["apple_keynote"])
    return _PROMPT_BASE_WEB.format(design_language=design_language)


# Default fallback prompts
SYSTEM_PROMPT_FORMAT_WEB = _build_web_prompt("apple_keynote")

# System prompt when no document text is given — LLM writes content too.
SYSTEM_PROMPT_GENERATE = """\
You are a professional document designer and writer.
Your task: produce a complete, self-contained HTML+CSS document based on the request below.
The document will be rendered to PDF via Puppeteer (Headless Chrome).

Rules:
- Output ONLY the HTML document. No explanations, no markdown fences.
- Start with <!DOCTYPE html> and end with </html>.
- <meta charset="UTF-8"> required.
- @page CSS rule: size A4 portrait; margin: 15mm.
- All CSS in <style> tags. No external resources, no CDN fonts.
- print-color-adjust: exact; -webkit-print-color-adjust: exact on relevant elements.
- Page break rules (mandatory — do not omit):
    break-inside: avoid  on  .section, table, tr, .callout, h2, h3, h4, li, blockquote
    break-after:  avoid  on  h1, h2, h3, h4
- Write professional, accurate content. No filler text.
- Design to match the document character: formal/legal → serif (Georgia); business → sans-serif (Arial).
- Clear visual hierarchy. One accent color. Generous whitespace. Print-ready quality.
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(
    query: str,
    model: str,
    is_format_mode: bool,
    web_mode: bool = False,
    style_key: str = "apple_keynote",
    output_suffix: str = "",
) -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    if is_format_mode:
        system_prompt = _build_web_prompt(style_key) if web_mode else _build_prompt(style_key)
    else:
        system_prompt = SYSTEM_PROMPT_GENERATE

    label = (f"web/{style_key}" if web_mode else style_key) + (", format" if is_format_mode else ", generate")
    print(f"\n{'='*60}")
    print(f"Style  : {label}")
    print(f"Model  : {model}")
    print(f"Input  : {query[:80]}{'...' if len(query) > 80 else ''}")
    print()

    # ---- LLM call ----------------------------------------------------------
    print("Step 1: LLM generating HTML...")
    t0 = time.time()

    if is_format_mode:
        user_message = f"Format the following document content as HTML+CSS. Preserve ALL content exactly as provided — do not rewrite, summarise, or invent anything:\n\n<document>\n{query}\n</document>"
    else:
        user_message = query

    response = client.models.generate_content(
        model=model,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.7,
            max_output_tokens=16_000,
        ),
    )

    html_code = response.text or ""
    # Strip accidental markdown fences
    if "```" in html_code:
        if html_code.startswith("```"):
            html_code = html_code.split("```", 2)[-1]
            if html_code.startswith("html\n"):
                html_code = html_code[5:]
        html_code = html_code.rsplit("```", 1)[0].strip()

    llm_ms = int((time.time() - t0) * 1000)
    tokens = response.usage_metadata.total_token_count if response.usage_metadata else "?"
    print(f"  Done: {llm_ms}ms, {tokens} tokens, {len(html_code)} HTML chars")

    if not html_code.strip().lower().startswith("<!"):
        print(f"  WARNING: unexpected output start — first 200 chars:")
        print(f"  {html_code[:200]!r}")

    # ---- Save HTML ---------------------------------------------------------
    stem = f"pdf_poc_{output_suffix}" if output_suffix else "pdf_poc_output"
    html_path = os.path.join(OUTPUT_DIR, f"{stem}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_code)
    print(f"  HTML → {html_path}")

    # ---- Puppeteer ---------------------------------------------------------
    print("Step 2: Puppeteer rendering PDF...")
    t1 = time.time()

    runner = NodePuppeteerRunner()
    try:
        pdf_bytes = await runner.run(html_code, timeout=60)
    except PuppeteerRunnerError as e:
        print(f"  ERROR: {e}")
        return

    puppeteer_ms = int((time.time() - t1) * 1000)
    pdf_path = os.path.join(OUTPUT_DIR, f"{stem}.pdf")
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)

    total_ms = int((time.time() - t0) * 1000)
    print(f"  Done: {puppeteer_ms}ms, {len(pdf_bytes)} PDF bytes")
    print(f"  PDF  → {pdf_path}")
    print(f"Total  : {total_ms / 1000:.1f}s  |  LLM {llm_ms}ms  |  Puppeteer {puppeteer_ms}ms")
    print(f"open {pdf_path}")


async def batch(query: str, model: str) -> None:
    failed = []
    for idx, style_key in enumerate(STYLES):
        if idx > 0:
            print(f"\nPausing 5s before next style...")
            await asyncio.sleep(5)
        try:
            await main(query, model, is_format_mode=True, style_key=style_key, output_suffix=style_key)
        except Exception as e:
            print(f"  FAILED ({style_key}): {e}")
            failed.append(style_key)
    if failed:
        print(f"\nFailed styles: {', '.join(failed)}")


if __name__ == "__main__":
    flags: dict = {}
    positional = []
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and "=" in a:
            k, v = a.lstrip("-").split("=", 1)
            flags[k] = v
        elif a.startswith("--"):
            k = a.lstrip("-")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                flags[k] = args[i + 1]
                i += 1
            else:
                flags[k] = True
        else:
            positional.append(a)
        i += 1

    model = flags.get("model") or MODEL
    web_mode = "web" in flags
    is_format_mode = False

    if "file" in flags:
        with open(flags["file"], encoding="utf-8") as f:
            query = f.read()
        is_format_mode = True
    elif not sys.stdin.isatty():
        query = sys.stdin.read()
        is_format_mode = True
    elif positional:
        query = positional[0]
    elif os.path.isfile(DEFAULT_INPUT_FILE):
        with open(DEFAULT_INPUT_FILE, encoding="utf-8") as f:
            query = f.read()
        is_format_mode = True
        print(f"Input file: {DEFAULT_INPUT_FILE}")
    else:
        print(f"ERROR: no input provided and default file not found: {DEFAULT_INPUT_FILE}")
        sys.exit(1)

    if "batch" in flags:
        asyncio.run(batch(query, model))
    else:
        style_key = flags.get("style", "")
        suffix = f"web_{style_key or 'auto'}" if web_mode else (style_key or "auto")

        asyncio.run(main(query, model, is_format_mode, web_mode=web_mode, style_key=style_key, output_suffix=suffix))

"""
Generate Prompt Builder v4 upload files from downloaded v3 Firestore documents.

Reads: firestore_utils/downloads/*.json
Writes: firestore_utils/uploads/v4_*.json (blueprints, profiles, split tokens)

Run:
    python scripts/migration/generate_v4_uploads.py

Then upload each collection manually:
    python firestore_utils/upload.py development_domain_prompt_blueprints_v3 <doc_id> --format json
    python firestore_utils/upload.py development_domain_prompt_profiles_v3 <doc_id> --format json
    python firestore_utils/upload.py development_domain_prompt_tokens_v3_system <doc_id> --format json
"""

import json
import re
import textwrap
from pathlib import Path

DOWNLOADS = Path("firestore_utils/downloads")
UPLOADS = Path("firestore_utils/uploads")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load(filename: str) -> dict:
    return json.loads((DOWNLOADS / filename).read_text(encoding="utf-8"))


def write_upload(doc_id: str, data: dict) -> None:
    path = UPLOADS / f"{doc_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {path}")


def strip_section_wrapper(content: str, section_name: str) -> str:
    """
    Strip the outermost `section_name { ... }` wrapper from content.
    Dedents the inner content by 4 spaces.
    Handles any nesting depth inside.

    Example:
        cognitive_process {
            instruction: "..."
            steps: [...]
        }
    → strips to:
        instruction: "..."
        steps: [...]
    """
    # Find the opening line
    lines = content.split("\n")
    start_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == f"{section_name} {{" or stripped.startswith(f"{section_name} {{"):
            start_idx = i
            break

    if start_idx is None:
        raise ValueError(f"Section '{section_name}' not found in content")

    # Find the matching closing brace
    depth = 0
    end_idx = None
    for i, line in enumerate(lines[start_idx:], start=start_idx):
        depth += line.count("{")
        depth -= line.count("}")
        if depth == 0 and i > start_idx:
            end_idx = i
            break

    if end_idx is None:
        raise ValueError(f"No matching closing brace for '{section_name}'")

    # Extract inner lines, normalize indentation to 0
    inner_lines = lines[start_idx + 1: end_idx]
    min_indent = None
    for line in inner_lines:
        if line.strip():
            spaces = len(line) - len(line.lstrip())
            if min_indent is None or spaces < min_indent:
                min_indent = spaces
    if min_indent is None:
        min_indent = 4
    dedented = []
    for line in inner_lines:
        if line.strip():
            dedented.append(line[min_indent:])
        else:
            dedented.append("")

    return "\n".join(dedented) + "\n"


def extract_section(content: str, section_name: str) -> str:
    """
    Extract the inner content of `section_name { ... }` from a multi-section body.
    The section name may appear at any indentation level.
    Returns the inner content dedented by 4 spaces.
    """
    lines = content.split("\n")
    start_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == f"{section_name} {{" or re.match(rf"\s*{re.escape(section_name)}\s*\{{", line):
            start_idx = i
            break

    if start_idx is None:
        raise ValueError(f"Section '{section_name}' not found")

    depth = 0
    end_idx = None
    for i, line in enumerate(lines[start_idx:], start=start_idx):
        depth += line.count("{")
        depth -= line.count("}")
        if depth == 0 and i > start_idx:
            end_idx = i
            break

    if end_idx is None:
        raise ValueError(f"No closing brace for '{section_name}'")

    inner_lines = lines[start_idx + 1: end_idx]
    # Detect minimum indentation of non-empty lines to normalize to 0
    min_indent = None
    for line in inner_lines:
        if line.strip():
            spaces = len(line) - len(line.lstrip())
            if min_indent is None or spaces < min_indent:
                min_indent = spaces
    if min_indent is None:
        min_indent = 4
    dedented = []
    for line in inner_lines:
        if line.strip():
            dedented.append(line[min_indent:])
        else:
            dedented.append("")

    return "\n".join(dedented) + "\n"


def extract_top_level_blocks(content: str, block_names: list[str]) -> str:
    """
    Extract multiple named top-level blocks from content, preserving order.
    Returns them concatenated, dedented by 4 spaces.
    """
    result_blocks = []
    for name in block_names:
        lines = content.split("\n")
        start_idx = None
        for i, line in enumerate(lines):
            if re.match(rf"\s*{re.escape(name)}\s*[\{{:]", line):
                start_idx = i
                break
        if start_idx is None:
            continue

        # Check if it's a property (name: value) or a block (name { ... })
        stripped = lines[start_idx].strip()
        if stripped.startswith(f"{name}:"):
            # It's a property, not a block — find until next blank or next block
            result_lines = [lines[start_idx].strip()]
            j = start_idx + 1
            while j < len(lines) and lines[j].strip() and not re.match(r"\s+\w+\s*[\{:]", lines[j]):
                result_lines.append(lines[j].strip())
                j += 1
            result_blocks.append("\n".join(result_lines))
            continue

        # It's a block — find matching close
        depth = 0
        end_idx = None
        for i, line in enumerate(lines[start_idx:], start=start_idx):
            depth += line.count("{")
            depth -= line.count("}")
            if depth == 0 and i > start_idx:
                end_idx = i
                break

        if end_idx is None:
            continue

        block_lines = lines[start_idx: end_idx + 1]
        dedented = []
        for line in block_lines:
            if line.startswith("    "):
                dedented.append(line[4:])
            else:
                dedented.append(line)
        result_blocks.append("\n".join(dedented))

    return "\n\n".join(result_blocks) + "\n"


# ---------------------------------------------------------------------------
# 1. Blueprints
# ---------------------------------------------------------------------------

def generate_blueprints():
    print("\n=== BLUEPRINTS ===")

    blueprints = [
        {
            "blueprint_id": "universal_agent_v1",
            "outer_class": "Alek extends Agent",
            "class_order": [
                "properties",
                "cognitive_process",
                "policies",
                "protocols",
                "few_shot_examples",
                "output_format",
                "final_directives",
            ],
        },
        {
            "blueprint_id": "router_agent_v1",
            "outer_class": "RouterAgent extends Agent",
            "class_order": [
                "identity",
                "knowledge_base",
                "policies",
                "cognitive_process",
                "conflict_resolution",
                "output_format",
                "examples",
            ],
        },
        {
            "blueprint_id": "websearch_agent_v1",
            "outer_class": "WebSearchAgent extends Agent",
            "class_order": ["properties", "cognitive_process", "output_format", "execution"],
        },
        {
            "blueprint_id": "websearch_light_agent_v1",
            "outer_class": "WebSearchLightAgent extends Agent",
            "class_order": ["properties", "cognitive_process", "output_format", "execution"],
        },
        {
            "blueprint_id": "consolidation_agent_v1",
            "outer_class": "ConsolidationAgent extends Agent",
            "class_order": [
                "taxonomy",
                "cognitive_process",
                "tools",
                "examples",
                "policies",
                "output_specification",
            ],
        },
        {
            "blueprint_id": "memorysearch_agent_v1",
            "outer_class": "MemorySearchAgent extends Agent",
            "class_order": [
                "identity",
                "cognitive_process",
                "examples",
                "anti_patterns",
                "output_format",
            ],
        },
    ]

    for bp in blueprints:
        write_upload(bp["blueprint_id"], bp)


# ---------------------------------------------------------------------------
# 2. Agent profiles
# ---------------------------------------------------------------------------

def generate_profiles():
    print("\n=== PROFILES ===")

    # quick — user-facing, override-friendly
    write_upload("universal_agent_v1_quick", {
        "blueprint_id": "universal_agent_v1",
        "agent_id": "quick",
        "tokens": {
            "COGNITIVE_PROCESS_QUICK":              {"order": 10},
            "ARCHETYPE_INTELLECTUAL_SNIPER":        {"order": 20, "non_overridable": True},
            "VIBE_BATTLE_WEARY":                    {"order": 30, "non_overridable": True},
            "VOICE_APHORISTIC":                     {"order": 40},
            "BEHAVIOR_GUIDE_RANEVSKAYA_MODE":       {"order": 50},
            "HUMOR_PRESET_RANEVSKAYA":              {"order": 60},
            "RESPONSE_CONCISE":                     {"order": 70},
            "MOTTO_DEFAULT":                        {"order": 80},
            "FEW_SHOT_EXAMPLES_RANEVSKAYA_ZHVANETSKY": {"order": 90, "non_overridable": True},
            "OUTPUT_FORMAT_JSON":                   {"order": 100},
            "PROTOCOL_SEARCH_MEMORY":               {"order": 200},
            "PROTOCOL_QUICK_AGENT_SELECTION":       {"order": 210, "non_overridable": True},
            "POLICY_OUTPUT_LANGUAGE":               {"order": 300, "non_overridable": True},
            "POLICY_PRIVACY":                       {"order": 310},
            "POLICY_NO_OPEN_LOOPS":                 {"order": 320},
            "POLICY_ANTI_GUARDIAN":                 {"order": 330},
            "POLICY_WITTY_ACCENTUATION":            {"order": 340},
            "POLICY_ALIGN_WITH_ANCHORS":            {"order": 350},
            "DIRECTIVE_SLACK_FORMATTING":           {"order": 400},
            "DIRECTIVE_BREVITY":                    {"order": 410},
        },
    })

    # smart — user-facing, override-friendly
    write_upload("universal_agent_v1_smart", {
        "blueprint_id": "universal_agent_v1",
        "agent_id": "smart",
        "tokens": {
            "COGNITIVE_PROCESS_SMART":              {"order": 10},
            "ARCHETYPE_INTELLECTUAL_SNIPER":        {"order": 20},
            "VIBE_BATTLE_WEARY":                    {"order": 30},
            "VOICE_APHORISTIC":                     {"order": 40},
            "BEHAVIOR_GUIDE_RANEVSKAYA_MODE":       {"order": 50},
            "HUMOR_PRESET_RANEVSKAYA":              {"order": 60},
            "RESPONSE_CONCISE":                     {"order": 70},
            "MOTTO_DEFAULT":                        {"order": 80},
            "FEW_SHOT_EXAMPLES_RANEVSKAYA_ZHVANETSKY": {"order": 90, "non_overridable": True},
            "OUTPUT_FORMAT_JSON":                   {"order": 100, "non_overridable": True},
            "PROTOCOL_SEARCH_MEMORY":               {"order": 200},
            "PROTOCOL_WEB_SEARCH":                  {"order": 210},
            "PROTOCOL_SMART_AGENT_SELECTION":       {"order": 220, "non_overridable": True},
            "POLICY_OUTPUT_LANGUAGE":               {"order": 300, "non_overridable": True},
            "POLICY_PRIVACY":                       {"order": 310},
            "POLICY_NO_OPEN_LOOPS":                 {"order": 320},
            "POLICY_ANTI_GUARDIAN":                 {"order": 330},
            "POLICY_WITTY_ACCENTUATION":            {"order": 340},
            "POLICY_ALIGN_WITH_ANCHORS":            {"order": 350},
            "DIRECTIVE_SLACK_FORMATTING":           {"order": 400},
            "DIRECTIVE_BREVITY":                    {"order": 410},
        },
    })

    # router — internal, fully locked
    write_upload("router_agent_v1_router", {
        "blueprint_id": "router_agent_v1",
        "agent_id": "router",
        "tokens": {
            "ROUTER_IDENTITY":           {"order": 10, "non_overridable": True},
            "ROUTER_KNOWLEDGE_BASE":     {"order": 20, "non_overridable": True},
            "ROUTER_POLICIES":           {"order": 30, "non_overridable": True},
            "ROUTER_COGNITIVE_PROCESS":  {"order": 40, "non_overridable": True},
            "ROUTER_CONFLICT_RESOLUTION": {"order": 50, "non_overridable": True},
            "ROUTER_OUTPUT_FORMAT":      {"order": 60, "non_overridable": True},
            "ROUTER_EXAMPLES":           {"order": 70, "non_overridable": True},
        },
    })

    # websearch — internal, fully locked
    write_upload("websearch_agent_v1_websearch", {
        "blueprint_id": "websearch_agent_v1",
        "agent_id": "websearch",
        "tokens": {
            "WEBSEARCH_PROPERTIES":        {"order": 10, "non_overridable": True},
            "WEBSEARCH_COGNITIVE_PROCESS": {"order": 20, "non_overridable": True},
            "WEBSEARCH_OUTPUT_FORMAT":     {"order": 30, "non_overridable": True},
            "WEBSEARCH_EXECUTION":         {"order": 40, "non_overridable": True},
        },
    })

    # websearch_light — internal, fully locked
    write_upload("websearch_light_agent_v1_websearch_light", {
        "blueprint_id": "websearch_light_agent_v1",
        "agent_id": "websearch_light",
        "tokens": {
            "WEBSEARCH_LIGHT_PROPERTIES":        {"order": 10, "non_overridable": True},
            "WEBSEARCH_LIGHT_COGNITIVE_PROCESS": {"order": 20, "non_overridable": True},
            "WEBSEARCH_LIGHT_OUTPUT_FORMAT":     {"order": 30, "non_overridable": True},
            "WEBSEARCH_LIGHT_EXECUTION":         {"order": 40, "non_overridable": True},
        },
    })

    # consolidation — internal, fully locked
    write_upload("consolidation_agent_v1_consolidation", {
        "blueprint_id": "consolidation_agent_v1",
        "agent_id": "consolidation",
        "tokens": {
            "CONSOLIDATION_TAXONOMY":         {"order": 10, "non_overridable": True},
            "CONSOLIDATION_COGNITIVE_PROCESS": {"order": 20, "non_overridable": True},
            "CONSOLIDATION_TOOLS":            {"order": 30, "non_overridable": True},
            "CONSOLIDATION_EXAMPLES":         {"order": 40, "non_overridable": True},
            "CONSOLIDATION_POLICIES":         {"order": 50, "non_overridable": True},
            "CONSOLIDATION_OUTPUT_SPEC":      {"order": 60, "non_overridable": True},
        },
    })

    # memorysearch — internal, fully locked
    write_upload("memorysearch_agent_v1_memorysearch", {
        "blueprint_id": "memorysearch_agent_v1",
        "agent_id": "memorysearch",
        "tokens": {
            "MEMORYSEARCH_IDENTITY":           {"order": 10, "non_overridable": True},
            "MEMORYSEARCH_COGNITIVE_PROCESS":  {"order": 20, "non_overridable": True},
            "MEMORYSEARCH_EXAMPLES":           {"order": 30, "non_overridable": True},
            "MEMORYSEARCH_ANTI_PATTERNS":      {"order": 40, "non_overridable": True},
            "OUTPUT_FORMAT_MEMORY_SEARCH":     {"order": 50, "non_overridable": True},
        },
    })


# ---------------------------------------------------------------------------
# 3. Updated system tokens (fix class field, strip wrappers)
# ---------------------------------------------------------------------------

def generate_updated_system_tokens():
    print("\n=== UPDATED SYSTEM TOKENS ===")

    # COGNITIVE_PROCESS_QUICK — strip cognitive_process { } wrapper
    t = load("COGNITIVE_PROCESS_QUICK.json")
    t["content"] = strip_section_wrapper(t["content"], "cognitive_process")
    write_upload("COGNITIVE_PROCESS_QUICK", t)

    # COGNITIVE_PROCESS_SMART — strip cognitive_process { } wrapper
    t = load("COGNITIVE_PROCESS_SMART.json")
    t["content"] = strip_section_wrapper(t["content"], "cognitive_process")
    write_upload("COGNITIVE_PROCESS_SMART", t)

    # OUTPUT_FORMAT_STANDARD — strip output_format { } wrapper
    t = load("OUTPUT_FORMAT_STANDARD.json")
    t["content"] = strip_section_wrapper(t["content"], "output_format")
    write_upload("OUTPUT_FORMAT_STANDARD", t)

    # OUTPUT_FORMAT_WEATHER — strip output_format { } wrapper
    t = load("OUTPUT_FORMAT_WEATHER.json")
    t["content"] = strip_section_wrapper(t["content"], "output_format")
    write_upload("OUTPUT_FORMAT_WEATHER", t)

    # OUTPUT_FORMAT_MEMORY_SEARCH — strip output_format { } wrapper + fix class
    t = load("OUTPUT_FORMAT_MEMORY_SEARCH.json")
    t["class"] = "output_format"
    t["content"] = strip_section_wrapper(t["content"], "output_format")
    write_upload("OUTPUT_FORMAT_MEMORY_SEARCH", t)

    # FEW_SHOT_EXAMPLES_DEFAULT — fix class from "knowledge_base" to "few_shot_examples"
    t = load("FEW_SHOT_EXAMPLES_DEFAULT.json")
    t["class"] = "few_shot_examples"
    write_upload("FEW_SHOT_EXAMPLES_DEFAULT", t)

    # FEW_SHOT_EXAMPLES_RANEVSKAYA_ZHVANETSKY — fix class + strip FEW_SHOT_EXAMPLES { } wrapper
    t = load("FEW_SHOT_EXAMPLES_RANEVSKAYA_ZHVANETSKY.json")
    t["class"] = "few_shot_examples"
    t["content"] = strip_section_wrapper(t["content"], "FEW_SHOT_EXAMPLES")
    write_upload("FEW_SHOT_EXAMPLES_RANEVSKAYA_ZHVANETSKY", t)


# ---------------------------------------------------------------------------
# 4. Router token splits
# ---------------------------------------------------------------------------

def generate_router_tokens():
    print("\n=== ROUTER TOKENS (split from COGNITIVE_PROCESS_ROUTER) ===")
    t = load("COGNITIVE_PROCESS_ROUTER.json")
    content = t["content"]

    def router_token(token_id: str, class_: str, section: str, category: str = None) -> None:
        inner = extract_section(content, section)
        write_upload(token_id, {
            "token_id": token_id,
            "category": category or class_,
            "class": class_,
            "content": inner,
            "metadata": {
                "description": f"RouterAgent v4 — {section} section",
                "override_by": ["SYSTEM"],
                "source": "split from COGNITIVE_PROCESS_ROUTER v3",
            },
        })

    router_token("ROUTER_IDENTITY", "identity", "identity")
    router_token("ROUTER_KNOWLEDGE_BASE", "knowledge_base", "knowledge_base")
    router_token("ROUTER_POLICIES", "policies", "policies", "policy_set")
    router_token("ROUTER_COGNITIVE_PROCESS", "cognitive_process", "cognitive_process")
    router_token("ROUTER_CONFLICT_RESOLUTION", "conflict_resolution", "conflict_resolution")
    router_token("ROUTER_OUTPUT_FORMAT", "output_format", "output_format")
    router_token("ROUTER_EXAMPLES", "examples", "examples")


# ---------------------------------------------------------------------------
# 5. WebSearch token splits
# ---------------------------------------------------------------------------

def generate_websearch_tokens():
    print("\n=== WEBSEARCH TOKENS (split from COGNITIVE_PROCESS_WEBSEARCH) ===")
    t = load("COGNITIVE_PROCESS_WEBSEARCH.json")
    content = t["content"]

    def ws_token(token_id: str, class_: str, section: str, category: str = None) -> None:
        inner = extract_section(content, section)
        write_upload(token_id, {
            "token_id": token_id,
            "category": category or class_,
            "class": class_,
            "content": inner,
            "metadata": {
                "description": f"WebSearchAgent v4 — {section} section",
                "override_by": ["SYSTEM", "AGENT"],
                "source": "split from COGNITIVE_PROCESS_WEBSEARCH v3",
            },
        })

    ws_token("WEBSEARCH_PROPERTIES", "properties", "properties", "archetype")
    ws_token("WEBSEARCH_COGNITIVE_PROCESS", "cognitive_process", "cognitive_process")
    ws_token("WEBSEARCH_OUTPUT_FORMAT", "output_format", "output_format")
    ws_token("WEBSEARCH_EXECUTION", "execution", "execution")


# ---------------------------------------------------------------------------
# 6. WebSearchLight token splits
# ---------------------------------------------------------------------------

def generate_websearch_light_tokens():
    print("\n=== WEBSEARCH_LIGHT TOKENS (split from COGNITIVE_PROCESS_WEBSEARCH_LIGHT) ===")
    t = load("COGNITIVE_PROCESS_WEBSEARCH_LIGHT.json")
    content = t["content"]

    def wsl_token(token_id: str, class_: str, section: str, category: str = None) -> None:
        inner = extract_section(content, section)
        write_upload(token_id, {
            "token_id": token_id,
            "category": category or class_,
            "class": class_,
            "content": inner,
            "metadata": {
                "description": f"WebSearchLightAgent v4 — {section} section",
                "override_by": ["SYSTEM", "AGENT"],
                "source": "split from COGNITIVE_PROCESS_WEBSEARCH_LIGHT v3",
            },
        })

    wsl_token("WEBSEARCH_LIGHT_PROPERTIES", "properties", "properties", "archetype")
    wsl_token("WEBSEARCH_LIGHT_COGNITIVE_PROCESS", "cognitive_process", "cognitive_process")
    wsl_token("WEBSEARCH_LIGHT_OUTPUT_FORMAT", "output_format", "output_format")
    wsl_token("WEBSEARCH_LIGHT_EXECUTION", "execution", "execution")


# ---------------------------------------------------------------------------
# 7. MemorySearch token splits
# ---------------------------------------------------------------------------

def generate_memorysearch_tokens():
    print("\n=== MEMORYSEARCH TOKENS (split from COGNITIVE_PROCESS_MEMORY_SEARCH) ===")
    t = load("COGNITIVE_PROCESS_MEMORY_SEARCH.json")
    content = t["content"]

    def ms_token(token_id: str, class_: str, section: str, category: str = None) -> None:
        try:
            inner = extract_section(content, section)
        except ValueError:
            # anti_patterns is a top-level property, not a block
            inner = _extract_property_block(content, section)
        write_upload(token_id, {
            "token_id": token_id,
            "category": category or class_,
            "class": class_,
            "content": inner,
            "metadata": {
                "description": f"MemorySearchAgent v4 — {section} section",
                "override_by": ["SYSTEM", "AGENT"],
                "source": "split from COGNITIVE_PROCESS_MEMORY_SEARCH v3",
            },
        })

    ms_token("MEMORYSEARCH_IDENTITY", "identity", "identity")
    ms_token("MEMORYSEARCH_COGNITIVE_PROCESS", "cognitive_process", "cognitive_process")
    ms_token("MEMORYSEARCH_EXAMPLES", "examples", "examples")
    ms_token("MEMORYSEARCH_ANTI_PATTERNS", "anti_patterns", "anti_patterns")


def _extract_property_block(content: str, prop_name: str) -> str:
    """Extract a multi-line array property that is NOT wrapped in a block."""
    lines = content.split("\n")
    start_idx = None
    for i, line in enumerate(lines):
        if re.match(rf"\s*{re.escape(prop_name)}\s*:", line):
            start_idx = i
            break
    if start_idx is None:
        raise ValueError(f"Property '{prop_name}' not found")

    result = [lines[start_idx].strip()]
    # Collect until closing bracket
    depth = lines[start_idx].count("[") - lines[start_idx].count("]")
    j = start_idx + 1
    while j < len(lines) and depth > 0:
        result.append(lines[j].strip())
        depth += lines[j].count("[") - lines[j].count("]")
        j += 1

    return "\n".join(result) + "\n"


# ---------------------------------------------------------------------------
# 8. Consolidation token splits
# ---------------------------------------------------------------------------

def generate_consolidation_tokens():
    print("\n=== CONSOLIDATION TOKENS (split from COGNITIVE_PROCESS_CONSOLIDATION) ===")
    t = load("COGNITIVE_PROCESS_CONSOLIDATION.json")
    content = t["content"]

    def con_token(token_id: str, class_: str, section: str, category: str = None) -> None:
        inner = extract_section(content, section)
        write_upload(token_id, {
            "token_id": token_id,
            "category": category or class_,
            "class": class_,
            "content": inner,
            "metadata": {
                "description": f"ConsolidationAgent v4 — {section} section",
                "override_by": ["AGENT"],
                "source": "split from COGNITIVE_PROCESS_CONSOLIDATION v3",
            },
        })

    # Taxonomy: merge all taxonomy-related top-level blocks into one token.
    # These are: opening comment + fact_taxonomy + tags_vs_metadata + negative_constraints
    #            + conflict_resolution + decision_heuristics + quality_rules
    taxonomy_blocks = _extract_taxonomy_section(content)
    write_upload("CONSOLIDATION_TAXONOMY", {
        "token_id": "CONSOLIDATION_TAXONOMY",
        "category": "taxonomy",
        "class": "taxonomy",
        "content": taxonomy_blocks,
        "metadata": {
            "description": "ConsolidationAgent v4 — fact taxonomy and reference data",
            "override_by": ["AGENT"],
            "source": "split from COGNITIVE_PROCESS_CONSOLIDATION v3",
        },
    })

    con_token("CONSOLIDATION_COGNITIVE_PROCESS", "cognitive_process", "cognitive_process")
    con_token("CONSOLIDATION_TOOLS", "tools", "tools")
    con_token("CONSOLIDATION_EXAMPLES", "examples", "examples")
    con_token("CONSOLIDATION_POLICIES", "policies", "policies", "policy_set")
    con_token("CONSOLIDATION_OUTPUT_SPEC", "output_specification", "output_specification", "output_format")


def _extract_taxonomy_section(content: str) -> str:
    """
    Extract everything from the opening comment through quality_rules:
    - The /** ... */ comment block
    - fact_taxonomy { }
    - tags_vs_metadata { }
    - negative_constraints { }
    - conflict_resolution { }
    - decision_heuristics { }
    - quality_rules: [...]
    """
    lines = content.split("\n")

    # Find end of taxonomy section: stop just before "cognitive_process {"
    end_idx = None
    for i, line in enumerate(lines):
        if re.match(r"\s*cognitive_process\s*\{", line):
            end_idx = i
            break

    if end_idx is None:
        raise ValueError("cognitive_process section not found in consolidation content")

    # Take everything before cognitive_process, dedented by 4 spaces
    taxonomy_lines = lines[:end_idx]
    # Strip trailing blank lines
    while taxonomy_lines and not taxonomy_lines[-1].strip():
        taxonomy_lines.pop()

    dedented = []
    for line in taxonomy_lines:
        if line.startswith("    "):
            dedented.append(line[4:])
        else:
            dedented.append(line)

    return "\n".join(dedented) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Generating Prompt Builder v4 upload files...")
    print(f"Reading from: {DOWNLOADS.resolve()}")
    print(f"Writing to:   {UPLOADS.resolve()}")

    generate_blueprints()
    generate_profiles()
    generate_updated_system_tokens()
    generate_router_tokens()
    generate_websearch_tokens()
    generate_websearch_light_tokens()
    generate_memorysearch_tokens()
    generate_consolidation_tokens()

    print("\n✅ Done. Review the generated files in firestore_utils/uploads/")
    print("\nUpload order:")
    print("  1. Blueprints  → development_domain_prompt_blueprints_v3")
    print("  2. Profiles    → development_domain_prompt_profiles_v3")
    print("  3. Sys tokens  → development_domain_prompt_tokens_v3_system")
    print("  4. User tokens → development_domain_prompt_tokens_v3_user (unchanged)")

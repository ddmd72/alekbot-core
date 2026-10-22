"""Small readers for the H4 vocabulary tables (TEST_COVERAGE_SYSTEMIC_RFC §3 H4): profile, blueprint, tokens and the
few shapes a token states a vocabulary in (a keyed block, a line naming a field, a Groovy `@tool`). Reads `prompts_snapshot/`
only; no Firestore, no src imports."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "prompts_snapshot"

_spec = importlib.util.spec_from_file_location("snapshot_serializer", ROOT / "firestore_utils" / "snapshot_serializer.py")
ser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ser)


def token_path(token_id):
    return SNAPSHOT / "tokens" / "system" / f"{token_id}.groovy"


def load_profile(agent_id):
    """The profile production renders for the agent (doc id = agent_id)."""
    return ser.doc_from_yaml((SNAPSHOT / "profiles" / f"{agent_id}.yaml").read_text(encoding="utf-8"))


def load_blueprint(blueprint_id):
    return ser.doc_from_yaml((SNAPSHOT / "blueprints" / f"{blueprint_id}.yaml").read_text(encoding="utf-8"))


def load_tokens(profile):
    """{token_id: (class, body)} for the tokens the profile lists; a missing token file raises FileNotFoundError."""
    out = {}
    for token_id in profile["tokens"]:
        doc = ser.token_from_file(token_path(token_id).read_text(encoding="utf-8"))
        out[token_id] = (doc["class"], doc["content"])
    return out


def _scan(body, keep_strings):
    """Drop comments; replace string literals by "" (or keep them), so braces and keys are read from structure only."""
    out, i, n = [], 0, len(body)
    while i < n:
        two = body[i:i + 2]
        if two == "/*":
            j = body.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif two == "//":
            j = body.find("\n", i)
            i = n if j < 0 else j
        elif body.startswith("'''", i) or body.startswith('"""', i):
            q = body[i:i + 3]
            j = body.find(q, i + 3)
            j = n if j < 0 else j + 3
            out.append(body[i:j] if keep_strings else '""')
            i = j
        elif body[i] == '"':
            j = i + 1
            while j < n and body[j] != '"':
                j += 2 if body[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(body[i:j] if keep_strings else '""')
            i = j
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def _block_lines(clean, start):
    """Lines inside the `{` that opens at clean[start], with the brace depth each line starts at (1 = directly inside)."""
    depth, rows = 0, []
    for line in clean[start:].split("\n"):
        if depth >= 1:
            rows.append((depth, line))
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            break
    return rows


def block_keys(body, block_name):
    """ALLCAPS keys directly inside the `block_name: {` / `block_name {` block."""
    clean = _scan(body, keep_strings=False)
    m = re.search(rf"\b{re.escape(block_name)}\s*:?\s*\{{", clean)
    assert m, f"no block {block_name!r}"
    keys = []
    for depth, line in _block_lines(clean, m.end() - 1):
        k = re.match(r"\s*([A-Z][A-Z_]*)\s*:", line)
        if depth == 1 and k:
            keys.append(k.group(1))
    return keys


def field_line_words(body, field):
    """ALLCAPS words on the lines that name the field (`context_priority`, `Context Priority`, `"context_priority":`)."""
    name = re.escape(field).replace("_", "[_ ]")
    words = set()
    for line in _scan(body, keep_strings=True).split("\n"):
        if re.search(rf"\b{name}\b", line, re.IGNORECASE):
            words |= set(re.findall(r"\b[A-Z][A-Z_]{2,}\b", line))
    return words


def groovy_tools(body):
    """{name: {"signature": [param names], "params": {key, ...}, "nested": {key: {subkey, ...}}}} for each `@tool`."""
    clean = _scan(body, keep_strings=False)
    tools = {}
    for m in re.finditer(r"@tool\s+(\w+)\(([^)]*)\)\s*\{", clean):
        rows = _block_lines(clean, m.end() - 1)
        tool = {"signature": [p.split(":")[0].strip() for p in m.group(2).split(",") if p.strip()],
                "params": set(), "nested": {}}
        in_params, nested_key = False, None
        for depth, line in rows:
            if depth == 1:
                in_params = bool(re.match(r"\s*parameters\s*:\s*\{", line))
            elif in_params and depth == 2:
                k = re.match(r"\s*(\w+)\s*:\s*(\{)?", line)
                if k:
                    tool["params"].add(k.group(1))
                    nested_key = k.group(1) if k.group(2) else None
                    if nested_key:
                        tool["nested"][nested_key] = set()
            elif in_params and depth == 3 and nested_key:
                k = re.match(r"\s*(\w+)\s*:", line)
                if k:
                    tool["nested"][nested_key].add(k.group(1))
        tools[m.group(1)] = tool
    return tools

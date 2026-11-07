"""Spec drift: re-fetch the providers' published specs and diff the parts our adapters use against a committed baseline.

TEST_COVERAGE_SYSTEMIC_RFC §4a "Contracts checked against reality", mechanism 1, built before P17 (the cheap part of
P18). P17's `contracts/providers/<provider>/openapi.yaml` subsets do not exist yet, so the baseline is this module's
own extract: `contracts/spec_drift/<source>.json`, one file per entry of `SOURCES`, written by `--update` and never
by hand. When P17 lands, the drift run diffs the re-extracted subsets against `contracts/providers/` instead.

What an extract holds, per kind of spec (documentation-only keys are dropped, set-like lists are sorted):
- `openapi`: each watched operation (`"<method> <path>"`) and every component its `$ref`s reach, transitively;
- `discovery` (Google): each watched method (by its `id`) and every schema its `$ref`s reach, transitively;
- `telegram` (the community Bot API spec): each watched method's and type's fields (name, types, required); a type's
  own field types are not followed (a `Message` reaches almost every type of the API).
The upstream version (`info.version`, `revision`, the Bot API version) is recorded as `meta`: reported, never drift.

The run never fails because something drifted: the verdict is the report (`--out-md`, `--out-json`) and the check-run
annotations (`--annotations`), read by the monthly `provider-contract-review` routine. Exit 1 only when no source could
be fetched (the run did not happen); a single failed source is an `error` row in the report.

Usage:
    python -m scripts.contracts.spec_drift [--source NAME] --out-md spec_drift.md --out-json spec_drift.json
    python -m scripts.contracts.spec_drift --annotations spec_drift.json
    python -m scripts.contracts.spec_drift --update [--source NAME]     # regenerate the baseline (accept the drift)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = REPO_ROOT / "contracts" / "spec_drift"


@dataclass(frozen=True)
class Source:
    """One published spec and the parts of it our adapters use."""

    name: str                 # the baseline file stem
    kind: str                 # "openapi" | "discovery" | "telegram"
    url: str
    watch: tuple[str, ...]    # openapi "post /responses"; discovery a method id; telegram "method:sendMessage" / "type:File"
    used_by: str              # where our code calls it, for the reader


_RAW = "https://raw.githubusercontent.com"

SOURCES: tuple[Source, ...] = (
    Source(
        "openai", "openapi", f"{_RAW}/openai/openai-openapi/master/openapi.yaml",
        ("post /responses", "get /responses/{response_id}", "post /audio/transcriptions"),
        "src/adapters/openai_adapter.py, openai_deep_research_adapter.py, openai_transcription_adapter.py",
    ),
    Source(
        "twilio_api_v2010", "openapi", f"{_RAW}/twilio/twilio-oai/main/spec/json/twilio_api_v2010.json",
        ("post /2010-04-01/Accounts/{AccountSid}/Calls.json",),
        "src/adapters/twilio_telephony_adapter.py",
    ),
    Source(
        "twilio_verify_v2", "openapi", f"{_RAW}/twilio/twilio-oai/main/spec/json/twilio_verify_v2.json",
        ("post /v2/Services/{ServiceSid}/Verifications", "post /v2/Services/{ServiceSid}/VerificationCheck"),
        "src/web/user_cabinet_app.py (phone OTP)",
    ),
    Source(
        "gmail", "discovery", "https://www.googleapis.com/discovery/v1/apis/gmail/v1/rest",
        ("gmail.users.messages.list", "gmail.users.messages.get", "gmail.users.messages.attachments.get"),
        "src/adapters/gmail_provider_adapter.py",
    ),
    Source(
        "gemini", "discovery", "https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta",
        (
            "generativelanguage.models.generateContent",
            "generativelanguage.models.embedContent",
            "generativelanguage.models.batchGenerateContent",
            "generativelanguage.batches.get",
            "generativelanguage.batches.list",
            "generativelanguage.media.upload",
            "generativelanguage.files.delete",
        ),
        "src/adapters/gemini_adapter.py, gemini_batch_adapter.py, gemini_embedding_adapter.py (google-genai SDK)",
    ),
    Source(
        "telegram", "telegram", f"{_RAW}/PaulSonOfLars/telegram-bot-api-spec/main/api.json",
        (
            "method:sendMessage", "method:editMessageText", "method:sendDocument", "method:sendPhoto",
            "method:getFile", "type:Update", "type:Message", "type:File",
        ),
        "src/adapters/telegram/ (python-telegram-bot)",
    ),
)

# Keys that only document: dropped everywhere. Vendor extensions of the OpenAI generator are documentation too.
_DOC_KEYS = frozenset({
    "description", "summary", "title", "example", "examples", "externalDocs", "enumDescriptions", "href",
})
_DOC_PREFIXES = ("x-oai", "x-stainless")
# Lists whose order carries no meaning: sorted, so a reordering is not drift.
_SET_KEYS = frozenset({"required", "enum", "parameterOrder", "scopes", "types", "returns"})

ANNOTATION_LIMIT = 4000   # the runner truncates a message at 4096 characters after decoding
MAX_WARNINGS = 9          # 10 annotations of a kind per step; one notice goes first
_VALUE_CHARS = 160


# --------------------------------------------------------------------------------------------------- normalisation
def normalize(node: Any) -> Any:
    """The node without documentation keys, with set-like lists sorted. Pure; returns a new structure."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _DOC_KEYS or key.startswith(_DOC_PREFIXES):
                continue
            value = normalize(value)
            if key in _SET_KEYS and isinstance(value, list) and all(isinstance(v, (str, int, float)) for v in value):
                value = sorted(value, key=lambda v: (type(v).__name__, v))
            out[key] = value
        return out
    if isinstance(node, list):
        return [normalize(v) for v in node]
    return node


def _refs(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.add(ref)
        for value in node.values():
            _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)


def _pointer(spec: dict, ref: str) -> Any:
    if not ref.startswith("#/"):
        raise KeyError(ref)
    node: Any = spec
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _closure(start: Any, resolve: Callable[[str], Any]) -> dict[str, Any]:
    """Every `$ref` reachable from `start`, resolved; a cycle terminates, an unresolvable ref maps to None."""
    found: dict[str, Any] = {}
    todo: set[str] = set()
    _refs(start, todo)
    while todo:
        ref = todo.pop()
        if ref in found:
            continue
        try:
            target = resolve(ref)
        except (KeyError, TypeError):
            found[ref] = None
            continue
        found[ref] = normalize(target)
        _refs(target, todo)
    return found


# ------------------------------------------------------------------------------------------------------ extraction
def _extract_openapi(source: Source, spec: dict) -> dict:
    operations: dict[str, Any] = {}
    paths = spec.get("paths") or {}
    for key in source.watch:
        method, _, path = key.partition(" ")
        item = paths.get(path) or {}
        op = item.get(method)
        if op is None:
            continue
        shared = item.get("parameters")  # path-level parameters apply to every operation of the path
        operations[key] = normalize({**op, "pathParameters": shared} if shared else op)
    components = _closure(list(operations.values()), lambda ref: _pointer(spec, ref))
    meta = {"openapi": spec.get("openapi"), "version": (spec.get("info") or {}).get("version")}
    return {"meta": meta, "items": operations, "components": dict(sorted(components.items()))}


def _discovery_methods(node: dict) -> Iterable[dict]:
    for method in (node.get("methods") or {}).values():
        yield method
    for child in (node.get("resources") or {}).values():
        yield from _discovery_methods(child)


def _extract_discovery(source: Source, spec: dict) -> dict:
    by_id = {m.get("id"): m for m in _discovery_methods(spec)}
    methods = {key: normalize(by_id[key]) for key in source.watch if key in by_id}
    schemas = spec.get("schemas") or {}
    components = _closure(list(methods.values()), lambda ref: schemas[ref])
    meta = {"revision": spec.get("revision"), "version": spec.get("version")}
    return {"meta": meta, "items": methods, "components": dict(sorted(components.items()))}


def _extract_telegram(source: Source, spec: dict) -> dict:
    items: dict[str, Any] = {}
    for key in source.watch:
        kind, _, name = key.partition(":")
        entry = (spec.get("methods" if kind == "method" else "types") or {}).get(name)
        if entry is None:
            continue
        reduced = {"fields": [{k: f.get(k) for k in ("name", "types", "required")} for f in entry.get("fields") or []]}
        if kind == "method":
            reduced["returns"] = entry.get("returns")
        if entry.get("subtypes"):
            reduced["subtypes"] = entry.get("subtypes")
        items[key] = normalize(reduced)
    meta = {"version": spec.get("version"), "release_date": spec.get("release_date")}
    return {"meta": meta, "items": items, "components": {}}


_EXTRACTORS = {"openapi": _extract_openapi, "discovery": _extract_discovery, "telegram": _extract_telegram}


def extract(source: Source, spec: dict) -> dict:
    """The watched part of `spec`: `{"meta", "items", "components"}`. A watched item absent upstream is absent here."""
    return _EXTRACTORS[source.kind](source, spec)


def parse(raw: bytes) -> dict:
    """JSON, or YAML (OpenAI publishes YAML; the C loader when present: the file is several MB)."""
    text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        return yaml.load(text, Loader=loader)  # a safe loader


# ------------------------------------------------------------------------------------------------------------ diff
@dataclass(frozen=True)
class Change:
    path: str
    kind: str          # "added" | "removed" | "changed"
    old: Any = None
    new: Any = None

    def line(self) -> str:
        if self.kind == "added":
            return f"+ {self.path}: {_short(self.new)}"
        if self.kind == "removed":
            return f"- {self.path}: {_short(self.old)}"
        return f"~ {self.path}: {_short(self.old)} -> {_short(self.new)}"


def _short(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return text if len(text) <= _VALUE_CHARS else text[: _VALUE_CHARS - 1] + "…"


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _named(items: list) -> dict | None:
    """A list of dicts that all carry a unique `name` (parameters, Telegram fields) keyed by it, else None."""
    if not items or not all(isinstance(i, dict) and isinstance(i.get("name"), str) for i in items):
        return None
    keyed = {i["name"]: i for i in items}
    return keyed if len(keyed) == len(items) else None


def diff(old: Any, new: Any, path: str = "") -> list[Change]:
    """Structural difference. Dicts by key; lists of named dicts by name; other lists as multisets (order-free)."""
    if isinstance(old, dict) and isinstance(new, dict):
        changes: list[Change] = []
        for key in sorted(set(old) | set(new)):
            sub = f"{path}.{key}" if path else str(key)
            if key not in new:
                changes.append(Change(sub, "removed", old=old[key]))
            elif key not in old:
                changes.append(Change(sub, "added", new=new[key]))
            else:
                changes.extend(diff(old[key], new[key], sub))
        return changes
    if isinstance(old, list) and isinstance(new, list):
        old_named, new_named = _named(old), _named(new)
        if old_named is not None and new_named is not None:
            return diff(old_named, new_named, path)
        old_c, new_c = [_canon(v) for v in old], [_canon(v) for v in new]
        removed = [v for v, c in zip(old, old_c) if old_c.count(c) > new_c.count(c)]
        added = [v for v, c in zip(new, new_c) if new_c.count(c) > old_c.count(c)]
        return [Change(f"{path}[]", "removed", old=v) for v in _dedupe(removed)] + \
               [Change(f"{path}[]", "added", new=v) for v in _dedupe(added)]
    if _canon(old) != _canon(new):
        return [Change(path, "changed", old=old, new=new)]
    return []


def _dedupe(values: list) -> list:
    seen: set[str] = set()
    out = []
    for v in values:
        c = _canon(v)
        if c not in seen:
            seen.add(c)
            out.append(v)
    return out


# ------------------------------------------------------------------------------------------------------------- run
@dataclass
class Result:
    source: str
    status: str                       # "same" | "drift" | "error" | "no-baseline"
    changes: list[Change] = field(default_factory=list)
    error: str = ""
    baseline_retrieved: str = ""
    meta: dict = field(default_factory=dict)
    baseline_meta: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "source": self.source, "status": self.status, "error": self.error,
            "baseline_retrieved": self.baseline_retrieved, "meta": self.meta, "baseline_meta": self.baseline_meta,
            "counts": {k: sum(1 for c in self.changes if c.kind == k) for k in ("removed", "changed", "added")},
            "changes": [{"path": c.path, "kind": c.kind, "old": c.old, "new": c.new} for c in self.changes],
        }


Fetch = Callable[[str], bytes]


def fetch_url(url: str, attempts: int = 3, timeout_s: int = 120) -> bytes:
    """GET a public spec (no credentials). Retries a transient failure twice."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "alekbot-spec-drift"})
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # fixed https URLs
                return response.read()
        except Exception as exc:  # reported as the source's error, never swallowed
            last = exc
            if attempt + 1 < attempts:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


def baseline_document(source: Source, extracted: dict, retrieved: str) -> dict:
    return {
        "source": source.name, "kind": source.kind, "url": source.url, "retrieved": retrieved,
        "watch": list(source.watch), "meta": extracted["meta"],
        "items": extracted["items"], "components": extracted["components"],
    }


def _comparable(doc: dict) -> dict:
    return {"watch": doc.get("watch"), "items": doc.get("items"), "components": doc.get("components")}


def check(source: Source, fetch: Fetch, baseline_dir: Path) -> Result:
    """Fetch, extract and diff one source against its baseline file."""
    try:
        extracted = extract(source, parse(fetch(source.url)))
    except Exception as exc:  # a broken source is a row in the report, not a crash of the run
        return Result(source.name, "error", error=f"{type(exc).__name__}: {exc}"[:500])
    current = baseline_document(source, extracted, retrieved="")
    path = baseline_dir / f"{source.name}.json"
    if not path.exists():
        return Result(source.name, "no-baseline", meta=extracted["meta"])
    base = json.loads(path.read_text(encoding="utf-8"))
    changes = diff(_comparable(base), _comparable(current))
    return Result(
        source.name, "drift" if changes else "same", changes=changes, baseline_retrieved=base.get("retrieved", ""),
        meta=extracted["meta"], baseline_meta=base.get("meta") or {},
    )


def update(source: Source, fetch: Fetch, baseline_dir: Path, today: str) -> Path:
    """Write the baseline of one source. Refuses when a watched item is absent upstream (a typo or a removal)."""
    extracted = extract(source, parse(fetch(source.url)))
    missing = [key for key in source.watch if key not in extracted["items"]]
    if missing:
        raise ValueError(f"{source.name}: watched items absent upstream: {', '.join(missing)}")
    baseline_dir.mkdir(parents=True, exist_ok=True)
    path = baseline_dir / f"{source.name}.json"
    text = json.dumps(baseline_document(source, extracted, today), indent=1, sort_keys=True, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------------------------- reports
def render_md(results: list[Result], sources: Iterable[Source] = SOURCES) -> str:
    used_by = {s.name: s.used_by for s in sources}
    lines = ["# Spec drift", "", "| source | status | removed | changed | added | baseline | upstream |", "|---|---|---|---|---|---|---|"]
    for r in results:
        counts = r.to_json()["counts"]
        upstream = ", ".join(f"{k} {v}" for k, v in r.meta.items() if v) or "-"
        lines.append(
            f"| {r.source} | {r.status} | {counts['removed']} | {counts['changed']} | {counts['added']} "
            f"| {r.baseline_retrieved or '-'} | {upstream} |"
        )
    for r in results:
        if r.status == "error":
            lines += ["", f"## {r.source}: error", "", f"`{r.error}`"]
        elif r.status == "drift":
            lines += ["", f"## {r.source}", "", f"Used by: {used_by.get(r.source, '-')}", "", "```"]
            lines += [c.line() for c in sorted(r.changes, key=_order)]
            lines.append("```")
    return "\n".join(lines) + "\n"


def _order(change: Change) -> tuple[int, str]:
    return ({"removed": 0, "changed": 1, "added": 2}[change.kind], change.path)


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotations(report: dict) -> list[str]:
    """Workflow commands: one notice with every source's status, one warning per drifted or failed source (≤ 9)."""
    rows = report.get("results") or []
    summary = "; ".join(
        f"{r['source']} {r['status']}"
        + (f" (-{r['counts']['removed']} ~{r['counts']['changed']} +{r['counts']['added']})" if r["status"] == "drift" else "")
        for r in rows
    )
    out = [f"::notice title=Spec drift verdict::{_escape(summary or 'no sources')}"]
    flagged = [r for r in rows if r["status"] in ("drift", "error", "no-baseline")]
    for r in flagged[:MAX_WARNINGS]:
        if r["status"] == "drift":
            body = [Change(c["path"], c["kind"], c["old"], c["new"]).line() for c in r["changes"]]
            body.sort(key=lambda line: ("-~+".index(line[0]), line))
            head = f"baseline {r['baseline_retrieved']}"
        else:
            body, head = [r["error"] or r["status"]], r["status"]
        message, used = [head], len(head)
        for i, line in enumerate(body):
            cost = len(_escape(line)) + 3
            if used + cost > ANNOTATION_LIMIT - 60:
                message.append(f"… {len(body) - i} more (run the script for the full list)")
                break
            message.append(line)
            used += cost
        out.append(f"::warning title=Spec drift {r['source']}::" + _escape("\n".join(message)))
    return out


# ------------------------------------------------------------------------------------------------------------- CLI
def _select(name: str) -> list[Source]:
    if not name:
        return list(SOURCES)
    chosen = [s for s in SOURCES if s.name == name]
    if not chosen:
        raise SystemExit(f"unknown source {name!r}; known: {', '.join(s.name for s in SOURCES)}")
    return chosen


def main(argv: list[str] | None = None, fetch: Fetch = fetch_url, baseline_dir: Path = BASELINE_DIR) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="", help="one source name (empty = all)")
    parser.add_argument("--update", action="store_true", help="regenerate the baseline file(s)")
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--annotations", type=Path, help="print workflow annotations from an --out-json report")
    args = parser.parse_args(argv)

    if args.annotations:
        for line in annotations(json.loads(args.annotations.read_text(encoding="utf-8"))):
            print(line)
        return 0

    sources = _select(args.source)
    if args.update:
        today = _dt.date.today().isoformat()
        failed = 0
        for source in sources:
            try:
                print(f"wrote {update(source, fetch, baseline_dir, today)}")
            except Exception as exc:  # reported per source; the exit code carries it
                print(f"{source.name}: not updated: {type(exc).__name__}: {exc}", file=sys.stderr)
                failed += 1
        return 2 if failed else 0

    results = [check(source, fetch, baseline_dir) for source in sources]
    report = {"results": [r.to_json() for r in results]}
    markdown = render_md(results, sources)
    if args.out_json:
        args.out_json.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.out_md:
        args.out_md.write_text(markdown, encoding="utf-8")
    if not (args.out_json or args.out_md):
        print(markdown)
    if results and all(r.status == "error" for r in results):
        print("::error::no source could be fetched: the run did not happen")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

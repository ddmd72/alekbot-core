"""
The owner's stored texts that name the drive's old intents (DRIVE_SPECIALIST_RFC §10 step 6, §13 step 5): the pure
half of `drive_intent_text_scan.py`, unit-tested without Firestore or GCS.

Before the cut, Smart's stored instructions may name an intent or field Smart will no longer have: the owner's custom
skills, unsaved skill drafts, reminder
instructions, standing directives, workspace files and prompt overrides. After the cut such a text costs one rejected call that Smart
self-corrects; rewritten intent-free ("find it on the drive", "save it to the drive") it works with the old code and
the new, so the rewrite can land first and survives a revert. The rollback scan (§13 step 5) looks for the new intent
in texts written after the deploy.

Public interface:
    scan_item(item, include_new=False) -> [Hit]       one stored text
    scan(items, include_new=False) -> [Hit]
    render_report(scanned, hits, include_new) -> str   markdown; names stores, keys and lines (personal: scripts/memory)
    backup_payload(items, hits) -> dict                every item that hit, its full text and version
    skill_markdown(name, description, body) -> str     a SKILL.md that `parse_skill_md` reads back
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

import yaml

OLD_INTENTS = ("search_files_in_drive", "save_file_to_drive", "list_files_in_drive", "open_file_from_drive",
               "move_file_in_drive", "create_folder_in_drive", "update_file_in_drive", "delete_file_from_drive")
NEW_INTENT = "work_with_drive_files"
# Fields only the drive intents had: a hit wherever they appear.
DRIVE_FIELDS = ("drive_query", "drive_folder", "drive_kind", "drive_date_from", "drive_date_to", "drive_deleted")
# Field names other intents or plain prose use too: a hit only near a drive intent (§10 step 6).
GENERIC_FIELDS = ("original", "append_text", "source_ref", "new_name", "folder", "text", "file_ref")
# A skill that says "load drive-search first" fails after the cut: the system skill is gone (§10 step 4).
SKILL_NAMES = ("drive-search",)
NEAR_LINES = 2
STORES = ("skill", "skill_file", "skill_draft", "reminder", "directive", "workspace", "prompt_override")


def _word(name: str) -> "re.Pattern[str]":
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")


_OLD = [(n, _word(n)) for n in OLD_INTENTS]
_NEW = (NEW_INTENT, _word(NEW_INTENT))
_DRIVE_FIELDS = [(n, _word(n)) for n in DRIVE_FIELDS]
_GENERIC = [(n, _word(n)) for n in GENERIC_FIELDS]
_SKILLS = [(n, _word(n)) for n in SKILL_NAMES]


@dataclass(frozen=True)
class ScanItem:
    """One stored text: `store` is one of STORES, `key` names it within the store (a skill's name and version, a
    reminder's id, a workspace path, an override's owner and token id)."""
    store: str
    key: str
    text: str
    version: Optional[int] = None


@dataclass(frozen=True)
class Hit:
    store: str
    key: str
    line_no: int     # 1-based
    pattern: str
    line: str


def scan_item(item: ScanItem, include_new: bool = False) -> List[Hit]:
    lines = item.text.splitlines()
    intents = list(_OLD) + ([_NEW] if include_new else [])
    intent_lines = {i for i, line in enumerate(lines) if any(rx.search(line) for _, rx in intents)}
    near = {j for i in intent_lines for j in range(i - NEAR_LINES, i + NEAR_LINES + 1)}
    hits: List[Hit] = []
    for i, line in enumerate(lines):
        named = [n for n, rx in intents + _DRIVE_FIELDS + _SKILLS if rx.search(line)]
        if i in near:
            named += [n for n, rx in _GENERIC if rx.search(line)]
        hits += [Hit(item.store, item.key, i + 1, n, line.strip()[:300]) for n in named]
    return hits


def scan(items: Iterable[ScanItem], include_new: bool = False) -> List[Hit]:
    return [hit for item in items for hit in scan_item(item, include_new)]


def render_report(scanned: Mapping[str, int], hits: Sequence[Hit], include_new: bool) -> str:
    mode = "rollback (texts naming work_with_drive_files too)" if include_new else "before the cut"
    lines = [f"# Drive intent text scan — {mode}", "",
             "Stored texts that name a drive intent, a drive-only field, a field next to a drive intent, or the "
             "drive-search skill. Rewrite each intent-free, as a new version where the store is versioned; the "
             "originals are in backup.json.", "",
             "| store | scanned | with hits |", "|---|---|---|"]
    with_hits = Counter(store for store, _ in {(h.store, h.key) for h in hits})
    for store in STORES:
        lines.append(f"| {store} | {scanned.get(store, 0)} | {with_hits.get(store, 0)} |")
    if not hits:
        lines += ["", "No hits: nothing to rewrite."]
        return "\n".join(lines) + "\n"
    by_item: Dict[tuple, List[Hit]] = {}
    for hit in hits:
        by_item.setdefault((hit.store, hit.key), []).append(hit)
    for (store, key), group in by_item.items():
        lines += ["", f"## {store}: {key}"]
        for line_no, pattern, line in sorted({(h.line_no, h.pattern, h.line) for h in group}):
            lines.append(f"- line {line_no}, `{pattern}`: {line}")
    return "\n".join(lines) + "\n"


def backup_payload(items: Sequence[ScanItem], hits: Sequence[Hit]) -> Dict[str, object]:
    keys = {(h.store, h.key) for h in hits}
    return {"items": [asdict(i) for i in items if (i.store, i.key) in keys], "hits": [asdict(h) for h in hits]}


def skill_markdown(name: str, description: str, body: str) -> str:
    front = yaml.safe_dump({"name": name, "description": description}, allow_unicode=True, sort_keys=False,
                           width=10_000).strip()
    return f"---\n{front}\n---\n{body}"

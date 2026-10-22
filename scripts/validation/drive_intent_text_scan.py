"""
Read-only scan of the owner's stored texts for the drive's old intents (DRIVE_SPECIALIST_RFC §10 step 6) — or, with
`--include-new`, for `work_with_drive_files` too (the rollback scan, §13 step 5). OWNER-run: it reads personal data
(Firestore: custom skills and their files, unexpired skill drafts not yet saved, reminders, standing directives,
prompt overrides; GCS: the workspace's `projects/`, `references/`, `scratch/` and `files/` text files), and writes
only under the gitignored scripts/memory/. Standing directives are read from the `agent_directive` domain and from
`preference` (the SUBJECT x SCOPE rule demotes situational agent rules there). **Only the owner (DEV_USER_ID) is
scanned**; another whitelisted user with custom skills, reminders or workspace files is not
covered. A `skill_file` hit is redrafted in chat (`draft_skill`, `$skill save`): `seed_custom_skill.py` keeps the current
files, and `--check-rewrites` does not read them.

Writes into `--out-dir` (default scripts/memory/drive_intent_scan_<stamp>/):
    report.md      every hit, by store and item (paths, skill names and lines: personal)
    backup.json    each item that hit, full text and version — the restore point of the rewrite
    skills/        every custom skill as `<name>.v<version>.md` (SKILL.md format, `seed_custom_skill.py` reads it)
                   and its files under `<name>.v<version>.files/` — the bodies the rewrite starts from

    venv/bin/python -m scripts.validation.drive_intent_text_scan [--include-new] [--out-dir scripts/memory/...]
    venv/bin/python -m scripts.validation.drive_intent_text_scan --check-rewrites scripts/memory/<scan>/rewrite
        (no Firestore: each rewritten `<name>.md` must parse as a SKILL.md, and it and each `others/*.txt` — the new
        text of a hit in another store — must name no old intent, drive-only field or drive-search; DIFF.md, the
        owner's side-by-side, is not checked)
Env: DEV_USER_ID, DEV_ACCOUNT_ID (.env), FIRESTORE_DATABASE (default us-production), the workspace bucket from
load_settings (GCS_WORKSPACE_BUCKET; without it the workspace is reported as not scanned).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from scripts.validation.drive_intent_text_scan_logic import (
    ScanItem, backup_payload, render_report, scan, skill_markdown)
from src.domain.entities import FactDomain
from src.domain.prompt_v3.slot import OwnerType
from src.domain.workspace import workspace_kind

_REPO = Path(__file__).resolve().parents[2]
_MEMORY = _REPO / "scripts" / "memory"
WORKSPACE_FOLDERS = ("projects/", "references/", "scratch/", "files/")
DIRECTIVE_DOMAINS = ((FactDomain.AGENT_DIRECTIVE, "directive"), (FactDomain.PREFERENCE, "preference"))


@dataclass
class ScanSources:
    """The read ports the scan needs; any of them may be None (that store is reported as not scanned)."""
    skills: Any = None          # SkillRepository: list_current, get_file
    drafts: Any = None          # DraftReader: list_drafts(user_id, now) -> [(code, Skill, {path: text})]
    notes: Any = None           # AgentNotePort: list_active_notes
    facts: Any = None           # FactRepository: get_active_facts_ordered
    workspace: Any = None       # WorkspaceStorePort: list, read
    profiles: Any = None        # AgentProfileRepository: get_override_tokens
    tokens: Any = None          # TokenRepository: get


async def collect(src: ScanSources, user_id: str, account_id: str, now: datetime
                  ) -> Tuple[List[ScanItem], Dict[str, Tuple[str, str, str]], List[str]]:
    """(items, skill exports {file stem: (name, description, body)}, stores not scanned)."""
    items: List[ScanItem] = []
    exports: Dict[str, Tuple[str, str, str]] = {}
    skipped: List[str] = []
    if src.skills is not None:
        for skill in await src.skills.list_current(user_id):
            key = f"{skill.name} v{skill.version}"
            items.append(ScanItem("skill", key, f"{skill.description}\n{skill.body}", skill.version))
            exports[f"{skill.name}.v{skill.version}"] = (skill.name, skill.description, skill.body)
            for f in skill.files:
                text = await src.skills.get_file(user_id, skill.name, f.sha256)
                if text is not None:
                    items.append(ScanItem("skill_file", f"{key}:{f.path}", text, skill.version))
                    exports[f"{skill.name}.v{skill.version}.files/{f.path}"] = ("", "", text)
    else:
        skipped += ["skill", "skill_file"]
    if src.drafts is not None:
        # A draft the owner has not saved yet holds a model-written skill: after `$skill save` it is a skill (§13 step 5).
        for code, draft, files in await src.drafts.list_drafts(user_id, now):
            key = f"{draft.name} draft {code}"
            items.append(ScanItem("skill_draft", key, f"{draft.description}\n{draft.body}"))
            items += [ScanItem("skill_draft", f"{key}:{path}", text) for path, text in sorted(files.items())]
    else:
        skipped.append("skill_draft")
    if src.notes is not None:
        for note in await src.notes.list_active_notes(user_id, now):
            text = "\n".join(t for t in (note.text, note.instruction, note.workspace_file or "") if t)
            items.append(ScanItem("reminder", note.note_id, text))
    else:
        skipped.append("reminder")
    if src.facts is not None:
        for domain, store in DIRECTIVE_DOMAINS:
            for fact in await src.facts.get_active_facts_ordered(account_id, domain=domain.value):
                items.append(ScanItem(store, fact.id, fact.text))
    else:
        skipped += ["directive", "preference"]
    if src.workspace is not None:
        for folder in WORKSPACE_FOLDERS:
            for entry in await src.workspace.list(user_id, folder):
                if workspace_kind(entry.path) != "text":
                    continue
                blob = await src.workspace.read(user_id, entry.path)
                items.append(ScanItem("workspace", entry.path, blob.data.decode("utf-8", errors="replace"),
                                      entry.generation))
    else:
        skipped.append("workspace")
    if src.profiles is not None and src.tokens is not None:
        for owner, owner_id in ((OwnerType.USER, user_id), (OwnerType.ACCOUNT, account_id)):
            for token_id in sorted(await src.profiles.get_override_tokens(owner, owner_id)):
                try:
                    token = await src.tokens.get(token_id)
                except KeyError:   # an override naming a token that no longer exists: reported, not scanned
                    skipped.append(f"prompt_override {owner.value}:{token_id} (token missing)")
                    continue
                items.append(ScanItem("prompt_override", f"{owner.value}:{token_id}", str(token.content)))
    else:
        skipped.append("prompt_override")
    return items, exports, skipped


REVIEW_FILE = "DIFF.md"   # the owner's side-by-side of old and new: it quotes the old lines, so it is never checked
OTHERS_DIR = "others"     # the new text of each hit outside the skills, one `<store>__<key>.txt` per stored item


def check_rewrites(folder: Path) -> List[str]:
    """Problems of the rewrites in `folder`: a `<name>.md` (SKILL.md format) unreadable or still naming what the scan
    looks for, or an `others/*.txt` still naming it. Empty = every rewrite is intent-free. Reads only local files."""
    from src.utils.skill_md import parse_skill_md

    problems: List[str] = []
    files = sorted(p for p in folder.glob("*.md") if p.name != REVIEW_FILE)
    others = sorted((folder / OTHERS_DIR).glob("*.txt"))
    if not files and not others:
        return [f"no rewritten skill (*.md) or other text ({OTHERS_DIR}/*.txt) in {folder}"]
    for path in others:
        for hit in scan([ScanItem("other", path.name, path.read_text(encoding="utf-8"))]):
            problems.append(f"{OTHERS_DIR}/{path.name}: line {hit.line_no} names `{hit.pattern}`: {hit.line}")
    for path in files:
        try:
            skill = parse_skill_md(path.read_text(encoding="utf-8"))
        except Exception as exc:   # noqa: BLE001 — any parse failure is the problem being reported
            problems.append(f"{path.name}: not a valid SKILL.md ({exc})")
            continue
        for hit in scan([ScanItem("skill", skill.name, f"{skill.description}\n{skill.body}")]):
            problems.append(f"{path.name}: line {hit.line_no} names `{hit.pattern}`: {hit.line}")
    return problems


def resolve_out(out: Optional[Path], stamp: str) -> Path:
    path = (out or _MEMORY / f"drive_intent_scan_{stamp}").resolve()
    if _MEMORY.resolve() not in path.parents:
        raise SystemExit(f"--out-dir must be under scripts/memory/ (gitignored): {path}")
    return path


def write_outputs(out_dir: Path, items: Sequence[ScanItem], exports: Dict[str, Tuple[str, str, str]],
                  skipped: Sequence[str], include_new: bool) -> Tuple[Path, int]:
    hits = scan(items, include_new=include_new)
    scanned: Dict[str, int] = {}
    for item in items:
        scanned[item.store] = scanned.get(item.store, 0) + 1
    out_dir.mkdir(parents=True, exist_ok=True)
    report = render_report(scanned, hits, include_new)
    if skipped:
        report += "\nNot scanned (no reader configured): " + ", ".join(skipped) + "\n"
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "backup.json").write_text(json.dumps(backup_payload(items, hits), ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    for stem, (name, description, body) in exports.items():
        path = out_dir / "skills" / (stem if ".files/" in stem else f"{stem}.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body if ".files/" in stem else skill_markdown(name, description, body), encoding="utf-8")
    return out_dir / "report.md", len(hits)


class _DraftReader:
    """The user's unexpired skill drafts (read-only): the draft collection has no list method on SkillRepository, so
    the codes come from one `user_id` query and each draft is read through the repository."""

    def __init__(self, collection: Any, skills: Any) -> None:
        self._collection = collection
        self._skills = skills

    async def list_drafts(self, user_id: str, now: datetime) -> List[Tuple[str, Any, Dict[str, str]]]:
        from google.cloud.firestore import FieldFilter

        out: List[Tuple[str, Any, Dict[str, str]]] = []
        async for snap in self._collection.where(filter=FieldFilter("user_id", "==", user_id)).stream():
            expires = (snap.to_dict() or {}).get("expires_at")
            if expires is not None and expires <= now:
                continue
            code = snap.id.split(":", 1)[1] if ":" in snap.id else snap.id
            draft = await self._skills.get_draft(user_id, code)
            if draft is not None:
                out.append((code, draft, await self._skills.get_draft_files(user_id, code)))
        return out


async def _sources() -> ScanSources:
    from google.cloud import firestore

    from src.adapters.firestore_account_repo import FirestoreAccountRepository
    from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
    from src.adapters.firestore_skill_repository import FirestoreSkillRepository
    from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
    from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
    from src.adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
    from src.adapters.security.composite_adapter import CompositeAdapter
    from src.adapters.security.regex_adapter import RegexSecurityAdapter
    from src.composition.service_container import ServiceContainer   # the fact repository's own wiring
    from src.config.settings import load_settings

    settings = load_settings()
    env = settings["ENVIRONMENT_CONFIG"]
    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env.account_collection_name)
    container = ServiceContainer(config=settings, db_client=db, env_config=env, account_repo=account_repo)
    bucket = settings.get("GCS_WORKSPACE_BUCKET") or ""
    skills = FirestoreSkillRepository(db, env)
    return ScanSources(
        skills=skills, drafts=_DraftReader(db.collection(env.skill_drafts_collection), skills),
        notes=FirestoreAgentNoteAdapter(db, env), facts=container.repository,
        workspace=GcsWorkspaceAdapter(bucket) if bucket else None,
        profiles=FirestoreAgentProfileRepository(db=db, profiles_collection=env.domain_prompt_profiles_collection,
                                                 overrides_collection=env.domain_prompt_overrides_collection),
        tokens=FirestoreTokenRepository(db=db, system_collection=f"{env.domain_prompt_tokens_collection}_system",
                                        user_collection=f"{env.domain_prompt_tokens_collection}_user",
                                        security_port=CompositeAdapter(adapters=[RegexSecurityAdapter()],
                                                                       strategy="worst_case")))


def _args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_intent_text_scan")
    parser.add_argument("--include-new", action="store_true",
                        help="also find work_with_drive_files (the rollback scan, DRIVE_SPECIALIST_RFC §13 step 5)")
    parser.add_argument("--out-dir", type=Path, default=None, help="under scripts/memory/ only")
    parser.add_argument("--check-rewrites", type=Path, default=None,
                        help="a folder of rewritten SKILL.md files to check (no Firestore read)")
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = resolve_out(args.out_dir, stamp)
    items, exports, skipped = await collect(await _sources(), os.environ["DEV_USER_ID"], os.environ["DEV_ACCOUNT_ID"],
                                            datetime.now(timezone.utc))
    report, n = write_outputs(out_dir, items, exports, skipped, args.include_new)
    print(f"{len(items)} texts scanned, {n} hits; report: {report}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    from dotenv import load_dotenv

    args = _args(argv)
    if args.check_rewrites is not None:
        problems = check_rewrites(args.check_rewrites)
        for problem in problems:
            print(problem)
        print("rewrites: intent-free" if not problems else f"rewrites: {len(problems)} problem(s)")
        return 1 if problems else 0
    load_dotenv(_REPO / ".env")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())

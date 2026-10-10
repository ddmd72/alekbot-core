#!/usr/bin/env python3
"""
Stage-1 dry run: do file-operation exchanges leak into long-term memory?
(docs/10_rfcs/USER_DRIVE_RFC.md §4.12)

Real Stage-1 consolidation, fact writes intercepted, on a SYNTHETIC batch of drive
exchanges plus one real fact. `--rule new` adds the file-operations line to
Trivial_Exclusions in the ASSEMBLED prompt only — Firestore is untouched.

A leak is any written fact that mentions a file location or operation — including a
mixed one ("lease ends Dec 2027, contract in Договоры/"), the likeliest leak.

    python scripts/consolidation/test_file_ops_exclusion_dryrun.py --rule both --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from google.cloud import firestore  # noqa: E402

from scripts.consolidation import test_stage1_classification_dryrun as base  # noqa: E402
from src.adapters.firestore_account_repo import FirestoreAccountRepository  # noqa: E402
from src.adapters.firestore_user_repo import FirestoreUserRepository  # noqa: E402
from src.composition.service_container import ServiceContainer  # noqa: E402
from src.composition.user_agent_factory import UserAgentFactory  # noqa: E402
from src.config.settings import load_settings  # noqa: E402
from src.infrastructure.agent_coordinator import AgentCoordinator  # noqa: E402

_LINE = ('"File operations: saving, opening, moving, renaming, deleting files or folders on the '
         'user\'s drive or in chat — the drive is the record of what exists and where"')
_EXCLUDE_RE = re.compile(r'("Temporary debugging state: [^"]*")(\s*\n\s*\])')


def patch_prompt(prompt: str):
    patched, n = _EXCLUDE_RE.subn(lambda m: f"{m.group(1)},\n                {_LINE}{m.group(2)}", prompt, count=1)
    return patched, bool(n)


def _m(role: str, text: str) -> Dict[str, Any]:
    return {"role": role, "text": text, "timestamp": time.time()}


BATCH = [
    _m("user", 'Запомни этот файл [File: "lease_2026.pdf" (1.2MB)]'),
    _m("model", "Saved [Drive: Inbox/lease_2026.pdf (1.2MB)] to Inbox."),
    _m("user", "Перенеси его в Договоры/Аренда"),
    _m("model", "Moved: Inbox/lease_2026.pdf → Договоры/Аренда/lease_2026.pdf."),
    _m("user", "Удали папку Встречи/2025"),
    _m("model", "Deleted folder Встречи/2025 (14 files)."),
    _m("user", "Кстати, аренда квартиры заканчивается в декабре 2027"),
    _m("model", "Noted: the lease ends in December 2027."),
]
_FILE_TERMS = re.compile(
    r"inbox|договор[ыи]/|аренда/|встречи/|lease_2026|\.pdf\b|\bdrive\b|диск|папк|folder|"
    r"\bsaved\b|\bmoved\b|\bdeleted\b|сохран|перен[её]с|удал", re.I)
_REAL_FACT = re.compile(r"2027")


def _op_text(op: Dict[str, Any]) -> str:
    return str(op.get("content") or json.dumps(op.get("updates") or {}, ensure_ascii=False))


async def _build_agent(user_id: str):
    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    cfg = load_settings()
    env = cfg["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env.account_collection_name)
    user_repo = FirestoreUserRepository(db, env, account_repo)
    container = ServiceContainer(config=cfg, db_client=db, env_config=env, account_repo=account_repo)
    factory = UserAgentFactory(config=cfg, env_config=env, coordinator=AgentCoordinator(),
                               user_repo=user_repo, account_repo=account_repo, **container.agent_services())
    agent = (await factory.ensure_agents_for_user(user_id)).get("consolidation_agent")
    if agent is None or agent._fact_management is None:
        raise SystemExit("consolidation_agent unavailable")

    async def _noop_async(*a, **k):
        pass

    agent._repo.refresh_biographical_context_cache = _noop_async
    if agent.prompt_builder:
        agent.prompt_builder.invalidate_biographical_cache = lambda *a, **k: None
    return agent


async def main(rule: str, runs: int) -> None:
    base.patch_prompt = patch_prompt
    user_id, account_id = os.environ["DEV_USER_ID"], os.environ["DEV_ACCOUNT_ID"]
    agent = await _build_agent(user_id)
    print(f"model: {agent.model_name}")
    for leg in (["baseline", "new"] if rule == "both" else [rule]):
        for i in range(runs):
            ops, elapsed, _, hit = await base.run_stage1(agent, BATCH, [], user_id, account_id, leg == "new")
            if leg == "new" and not hit:
                raise SystemExit("Trivial_Exclusions anchor NOT matched — fix _EXCLUDE_RE to the live token text.")
            texts = [_op_text(op) for op in ops if op.get("action") in ("CREATE", "UPDATE", "MERGE")]
            leaks = [t for t in texts if _FILE_TERMS.search(t)]
            clean_real = any(_REAL_FACT.search(t) and not _FILE_TERMS.search(t) for t in texts)
            print(f"[{leg} run {i + 1}] {elapsed:.0f}s ops={len(ops)} leaks={len(leaks)} "
                  f"real_fact_clean={clean_real}")
            for t in leaks:
                print(f"    LEAK: {t[:160]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", choices=["baseline", "new", "both"], default="both")
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    asyncio.run(main(a.rule, a.runs))

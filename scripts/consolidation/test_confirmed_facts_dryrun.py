#!/usr/bin/env python3
"""
WARNING: this calls the real consolidator model (paid) and writes to the live database (biographical cache
refresh, billing counters). Run it only with the owner's explicit approval.

Stage-1 dry run: do owner-confirmed facts survive consolidation?
(docs/10_rfcs/MEMORY_FACT_CONFIRMATION_RFC.md)

The confirmation feature appends the confirmed facts to session history as one user/model
pair. The consolidator's "echo trap" rule DISCARDs what the ASSISTANT stated and the USER
only confirmed. This runs the REAL Stage-1 consolidation (fact writes intercepted, nothing
is written) on SYNTHETIC batches and counts how many of the 3 facts are captured.

`--rule new` inserts ONE line (CONFIRMED_FACTS_LINE) into the ASSEMBLED prompt only —
Firestore is untouched. `--rule old` is the live prompt.

    python scripts/consolidation/test_confirmed_facts_dryrun.py --rule both --runs 3
    python scripts/consolidation/test_confirmed_facts_dryrun.py --rule new --scenario B --runs 5
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
from typing import Any, Dict, List, Tuple

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

# ─────────────────────────────────────────────────────────────────────────────
# The candidate line (copy this to the owner if it earns the write)
# ─────────────────────────────────────────────────────────────────────────────

CONFIRMED_FACTS_LINE = (
    "A USER turn that opens with '[System: the owner reviewed and confirmed these facts' is the "
    "owner's own statement of new information: extract each listed fact; the echo-trap rule does "
    "not apply to it."
)

# Anchors, in order of preference; the line is inserted as a new list element right after the
# matched element. Both are elements of a `[ "...", ... ]` list in CONSOLIDATION policies
# (COGNITIVE_PROCESS_CONSOLIDATION, rule Domain_Scope / rule Never_Store).
_ANCHORS = [
    # live text: "NEVER process ASSISTANT statements as facts unless USER confirms with NEW info",
    # (the repo snapshot says "NEW information." - tolerate both)
    re.compile(r'^([ \t]*)("NEVER process ASSISTANT statements as facts unless USER confirms '
               r'with NEW info[^"\n]*",?)[ \t]*$', re.M),
    re.compile(r'^([ \t]*)("Echo trap responses: ASSISTANT recalled fact[^"\n]*",?)[ \t]*$', re.M),
]


def patch_prompt(prompt: str) -> Tuple[str, bool]:
    """Insert CONFIRMED_FACTS_LINE after the first anchor that matches exactly once."""
    for rx in _ANCHORS:
        found = list(rx.finditer(prompt))
        if len(found) != 1:
            continue
        m = found[0]
        element = m.group(2) if m.group(2).endswith(",") else m.group(2) + ","
        text = f'{m.group(1)}{element}\n{m.group(1)}"{CONFIRMED_FACTS_LINE}",'
        return prompt[:m.start()] + text + prompt[m.end():], True
    return prompt, False


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic batches
# ─────────────────────────────────────────────────────────────────────────────

FACTS = [
    ("User's car insurance policy number ends in 8204 and renews on 2027-04-01.", "8204", "car-insurance.pdf"),
    ("User's bicycle frame number is WTU-0915-K.", "WTU-0915", "bike-invoice.pdf"),
    ("User's dentist is Dr. Marta Quillen at Clínica Ribeira.", "Quillen", "dental-card.pdf"),
]
FACT_NAMES = ["insurance", "bike", "dentist"]
FRAMES = ("sources", "plain")


def _m(role: str, text: str) -> Dict[str, Any]:
    return {"role": role, "text": text, "timestamp": time.time()}


def confirmed_pair(frame: str) -> List[Dict[str, Any]]:
    sources = ", ".join(dict.fromkeys(f[2] for f in FACTS))
    read_from = f", read from: {sources}" if frame == "sources" else ""
    body = "\n".join(f"- {f[0]}" for f in FACTS)
    return [
        _m("user", "[System: the owner reviewed and confirmed these facts about their life and the "
                   f"people in it{read_from}. Background consolidation adds them to "
                   "memory; search_memory cannot find them before that.]\n" + body),
        _m("model", f"Submitted {len(FACTS)} confirmed facts to memory."),
    ]


_FILES = ('[File: "car-insurance.pdf" (0.8MB)] [File: "bike-invoice.pdf" (0.5MB)] '
          '[File: "dental-card.pdf" (1.1MB)]')


def scenario(name: str, frame: str) -> List[Dict[str, Any]]:
    pair = confirmed_pair(frame)
    if name == "A":
        return [_m("user", f"Вот сканы документов, запомни данные оттуда {_FILES}"),
                _m("model", "Прочитал документы и подготовил факты для подтверждения.")] + pair
    if name == "B":
        return [_m("user", f"Что в этих документах? {_FILES}"),
                _m("model", "В документах: страховка авто — номер полиса заканчивается на 8204, "
                            "продление 2027-04-01; счёт на велосипед — номер рамы WTU-0915-K; "
                            "карта стоматолога — врач Marta Quillen, клиника Clínica Ribeira."),
                _m("user", "запомни это"),
                _m("model", "Подготовил эти факты для подтверждения.")] + pair
    return [_m("user", "Привет, как дела?"),
            _m("model", "Привет! Всё хорошо, чем могу помочь?")] + pair


_FILE_RE = re.compile(r"\.pdf\b|car-insurance|bike-invoice|dental-card", re.I)


def _op_text(op: Dict[str, Any]) -> str:
    return str(op.get("content") or json.dumps(op.get("updates") or {}, ensure_ascii=False))


def _op_domain(op: Dict[str, Any]) -> str:
    d = (op.get("metadata") or {}).get("domain") or (op.get("updates") or {}).get("domain") or "?"
    return str(d).lower()


# ─────────────────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────────────────

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


async def _assembled_prompt(agent, user_id: str, account_id: str) -> str:
    """Build the Stage-1 system prompt exactly as the agent does — no model call."""
    return await agent.prompt_builder.build_for_agent(
        agent_type="consolidation", user_id=user_id, account_id=account_id,
        routing_metadata=None, include_directives=False, biographical_facts=[], conversation_history=[])


async def main(rule: str, runs: int, scen: str, frame: str, check_only: bool,
               user_id: str, account_id: str) -> int:
    base.patch_prompt = patch_prompt
    agent = await _build_agent(user_id)
    print(f"model: {agent.model_name}  (fact writes intercepted; Firestore prompt untouched)")
    legs = ["old", "new"] if rule == "both" else [rule]
    # Validate the anchor against the live assembled prompt BEFORE any model call.
    if "new" in legs or check_only:
        _, ok = patch_prompt(await _assembled_prompt(agent, user_id, account_id))
        print(f"anchor check on live assembled prompt: {'OK' if ok else 'NOT MATCHED'}")
        if not ok:
            print("ERROR: anchor not matched exactly once in the live assembled prompt - fix _ANCHORS.")
            return 2
    if check_only:
        return 0
    names = ["A", "B", "C"] if scen == "all" else [scen]
    frames = list(FRAMES) if frame == "both" else [frame]
    stats: Dict[Tuple[str, str, str], List[Tuple[int, int, int]]] = {}
    failures = 0
    for sc in names:
        for leg in legs:
            for fr in frames:
                for i in range(runs):
                    tag = f"{sc}/{leg}/{fr} run {i + 1}"
                    try:
                        ops, elapsed, _, hit = await base.run_stage1(
                            agent, scenario(sc, fr), [], user_id, account_id, leg == "new")
                    except Exception as e:  # one failure must not skip the rest
                        failures += 1
                        print(f"[{tag}] FAILED: {type(e).__name__}: {e}")
                        continue
                    if leg == "new" and not hit:
                        failures += 1
                        print(f"[{tag}] FAILED: patch not applied")
                        continue
                    writes = [op for op in ops if op.get("action") in ("CREATE", "UPDATE", "MERGE")]
                    texts = [_op_text(op) for op in writes]
                    got = [FACT_NAMES[k] for k, (_, tok, _) in enumerate(FACTS)
                           if any(tok.lower() in t.lower() for t in texts)]
                    directives = [op for op in writes if "directive" in _op_domain(op)]
                    files = [t for t in texts if _FILE_RE.search(t)]
                    stats.setdefault((sc, leg, fr), []).append((len(got), len(directives), len(files)))
                    print(f"[{tag}] {elapsed:.0f}s ops={len(ops)} writes={len(writes)} "
                          f"captured={len(got)}/3 {got} directives={len(directives)} file_mentions={len(files)}")
                    for op in writes:
                        print(f"    {op.get('action'):6} domain={_op_domain(op):14} {_op_text(op)[:160]}")
                    for op in ops:
                        if op.get("action") == "DISCARD":
                            print(f"    DISCARD {str(op.get('reason', ''))[:150]}")
    print("\n=== summary (averages per scenario/rule/frame) ===")
    print(f"{'combo':<18}{'runs':>5}{'captured/3':>12}{'directives':>12}{'file_mentions':>15}")
    for (sc, leg, fr), v in stats.items():
        n = len(v)
        print(f"{sc + '/' + leg + '/' + fr:<18}{n:>5}{sum(x[0] for x in v) / n:>12.1f}"
              f"{sum(x[1] for x in v) / n:>12.1f}{sum(x[2] for x in v) / n:>15.1f}")
    if failures:
        print(f"\n{failures} run(s) failed (see FAILED lines)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Confirmed-facts survival through Stage-1 consolidation")
    ap.add_argument("--rule", choices=["old", "new", "both"], default="both")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--scenario", choices=["A", "B", "C", "all"], default="all")
    ap.add_argument("--frame", choices=["sources", "plain", "both"], default="both",
                    help="confirmed-pair frame: with the 'read from: ...' clause, without it, or both")
    ap.add_argument("--check-anchor", action="store_true",
                    help="assemble the live prompt, validate the --rule new anchor, exit (no model call)")
    ap.add_argument("--user-id", default=os.getenv("DEV_USER_ID"))
    ap.add_argument("--account-id", default=os.getenv("DEV_ACCOUNT_ID"))
    a = ap.parse_args()
    if not a.user_id or not a.account_id:
        print("ERROR: set DEV_USER_ID / DEV_ACCOUNT_ID in .env")
        sys.exit(1)
    sys.exit(asyncio.run(main(a.rule, a.runs, a.scenario, a.frame, a.check_anchor, a.user_id, a.account_id)))

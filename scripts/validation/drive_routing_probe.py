#!/usr/bin/env python3
"""
Routing probe for the drive (docs/10_rfcs/USER_DRIVE_RFC.md §7; DRIVE_SPECIALIST_RFC §4.1, §7.4).
Runs the REAL Smart on fixed phrasings; every delegation is captured and answered with
a stub, nothing executes. Two-turn scenarios seed the previous exchange into Smart's
loaded history. Each scenario runs --runs times.
Smart reaches the drive through one intent, `work_with_drive_files`, whose query must carry what the scenario
needs (the [File: …] label, the folder the user named, the ref from history). A call to one of the 8 removed drive
intents counts as a miss.

    NO_PROXY='*' python scripts/validation/drive_routing_probe.py --runs 3   # NO_PROXY: a local proxy breaks SDK TLS
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from google.cloud import firestore  # noqa: E402

from src.adapters.firestore_account_repo import FirestoreAccountRepository  # noqa: E402
from src.adapters.firestore_user_repo import FirestoreUserRepository  # noqa: E402
from src.composition.service_container import ServiceContainer  # noqa: E402
from src.composition.user_agent_factory import UserAgentFactory  # noqa: E402
from src.config.settings import load_settings  # noqa: E402
from src.domain.agent import AgentIntent, AgentMessage  # noqa: E402
from src.domain.llm import Message, MessagePart  # noqa: E402
from src.domain.request_context import RequestContext  # noqa: E402
from src.infrastructure.agent_coordinator import AgentCoordinator  # noqa: E402
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS  # noqa: E402
from src.infrastructure.agent_registry import AgentRegistry  # noqa: E402
from src.infrastructure.delegation_engine import DelegationEngine, ToolResult  # noqa: E402

LEASE = '[File: "lease_2026.pdf" (1.2MB)]'
DRIVE = "work_with_drive_files"
REMOVED_DRIVE_INTENTS = {
    "save_file_to_drive", "list_files_in_drive", "open_file_from_drive", "move_file_in_drive",
    "create_folder_in_drive", "update_file_in_drive", "delete_file_from_drive", "search_files_in_drive",
}


def _pair(user: str, model: str, drive_context: Any = None) -> List[Message]:
    """A stored exchange in production shape: refs live only in the `*_context` JSON block that
    ConversationHandler appends to the model turn (conversation_handler.py:1326-1340), never in the
    prose — the reply is not assumed to repeat any label."""
    model_text = model
    if drive_context is not None:
        model_text += "\n\n" + json.dumps({"drive_context": drive_context}, ensure_ascii=False, separators=(",", ":"))
    return [Message(role="user", parts=[MessagePart(text=user)]),
            Message(role="model", parts=[MessagePart(text=model_text)])]


_LISTED = [[{"path": "Договори/lease_2026.pdf", "ref": "drive:AAA"},
           {"path": "Договори/lease_2025.pdf", "ref": "drive:BBB"}]]

# `query_has`: words the commission's query must carry (casefolded); `query_lacks`: words it must not
SCENARIOS: List[Dict[str, Any]] = [
    {"text": f"Запам'ятай цей файл {LEASE}", "expect": DRIVE, "query_has": ["lease_2026.pdf"]},
    # the user's own words reach memory through consolidation: no memory tool, and no file saved to the drive
    {"text": "Запам'ятай, що оренда закінчується в грудні 2027", "expect": None, "forbid": DRIVE},
    {"text": f"Запам'ятай цей файл у папку Зустрічі {LEASE}", "expect": DRIVE, "query_has": ["lease_2026.pdf", "Зустрічі"]},
    {"seed": _pair(f"Відкрий {LEASE}", "Lease agreement for the flat, 12 months, ends Mar 2029."),
     "text": "Поклади його в папку Договори", "expect": DRIVE, "query_has": ["lease_2026.pdf", "Договори"]},
    {"seed": _pair("Що в мене на диску в папці Договори?", "У папці Договори два файли: договір оренди на 2026 і на 2025 рік.", _LISTED),
     "text": "Відкрий другий", "expect": DRIVE, "query_has": ["drive:BBB"], "query_lacks": ["drive:AAA"]},
]
_STUB = {"open_file": "[File: lease_2026.pdf]\nLease agreement…\n[/File: lease_2026.pdf]"}
CALLS: List[Dict[str, Any]] = []


def _install_capture() -> None:
    async def capture(self, tool_call, context, *args, **kwargs):
        a = tool_call.args or {}
        CALLS.append({"intent": a.get("intent", ""), "query": str(a.get("query", "")), "context": a.get("context") or {}})
        return ToolResult(name=tool_call.name, result_str=_STUB.get(a.get("intent", ""), "Done."))
    DelegationEngine.dispatch = capture


def judge_run(scenario: Dict[str, Any], calls: List[Dict[str, Any]]) -> bool:
    """One run's verdict: the expected intent was called (or none was expected), the forbidden one was not, no removed
    drive intent was called, and the drive commission's query carries the scenario's words and not its wrong ones."""
    intents = [c["intent"] for c in calls]
    if scenario["expect"] is not None and scenario["expect"] not in intents:
        return False
    if scenario.get("forbid") in intents or any(i in REMOVED_DRIVE_INTENTS for i in intents):
        return False
    queries = " ".join(c.get("query", "") for c in calls if c["intent"] == DRIVE).casefold()
    if any(w.casefold() not in queries for w in scenario.get("query_has", [])):
        return False
    return not any(w.casefold() in queries for w in scenario.get("query_lacks", []))


async def main(runs: int) -> int:
    user_id, account_id = os.environ["DEV_USER_ID"], os.environ["DEV_ACCOUNT_ID"]
    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    settings = load_settings()
    env_config = settings["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    container = ServiceContainer(config=settings, db_client=db, env_config=env_config, account_repo=account_repo)
    registry = AgentRegistry()
    for d in ALL_DESCRIPTORS:
        registry.register(d)
    factory = UserAgentFactory(config=settings, env_config=env_config, coordinator=AgentCoordinator(registry=registry),
                               user_repo=user_repo, account_repo=account_repo, **container.agent_services())
    smart = (await factory.ensure_agents_for_user(user_id))["smart_agent"]
    original_load = smart._load_conversation_context
    _install_capture()
    failures = 0
    passes: Dict[str, int] = {}
    for sc in SCENARIOS:
        seed = sc.get("seed", [])

        async def seeded(*args, _seed=seed, **kwargs):
            return list(_seed) + await original_load(*args, **kwargs)

        smart._load_conversation_context = seeded
        for run in range(runs):
            CALLS.clear()
            msg = AgentMessage.create(
                sender="drive_routing_probe", recipient=smart.agent_id, intent=AgentIntent.QUERY,
                payload={"text": sc["text"]},
                context={"user_id": user_id, "account_id": account_id,
                         "session_id": f"{user_id}:drive_routing_probe",
                         "current_message_parts": [MessagePart(text=sc["text"])]},
            )
            async with RequestContext(user_id=user_id, account_id=account_id):
                await smart.execute(msg)
            intents = [c["intent"] for c in CALLS]
            ok = judge_run(sc, CALLS)
            failures += 0 if ok else 1
            passes[sc["text"][:50]] = passes.get(sc["text"][:50], 0) + (1 if ok else 0)
            queries = [c["query"][:120] for c in CALLS if c["intent"] == DRIVE]
            print(f"{'OK ' if ok else 'BAD'} [{run + 1}] {sc['text'][:50]!r} → {intents} queries={queries}")
    smart._load_conversation_context = original_load
    for text, n in passes.items():
        print(f"PASS {n}/{runs}  {text!r}")
    print(f"\n{failures} failing run(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    sys.exit(asyncio.run(main(ap.parse_args().runs)))

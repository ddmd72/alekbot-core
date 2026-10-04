# scripts/skills/skill_trigger_eval.py
"""Agent Skills gate (RFC §10, delivery A): does Smart's model load a skill when it should?

Reproduces Smart's FIRST request: real system prompt (available_skills = the user's skills plus
decoys), real tool list, the user-turn anchor and timestamps, delegation temperature, response
schema and thinking — resolved for a task complexity the way production resolves it. One call per
case, nothing executed. Cases are personal → gitignored JSON:

    [{"prompt": "is IB3122 on time?", "expect": "flight-status"},
     {"prompt": "find me cheap flights to Kyiv", "expect": null}, ...]

    python scripts/skills/skill_trigger_eval.py --cases scripts/memory/skills/eval_cases.json \
        [--complexity info_search]          # small_talk | info_search | simple_analytics | deep_reasoning

A hit = use_skill(<expected>) anywhere in the first response, WITHOUT the terminal
deliver_response in the same batch (on Grok that means it answered before reading the skill).
Spends API budget. Pass bar (owner may change): recall >= 0.8, false loads <= 0.1.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from google.cloud import firestore  # noqa: E402

from src.adapters.firestore_account_repo import FirestoreAccountRepository  # noqa: E402
from src.adapters.firestore_user_repo import FirestoreUserRepository  # noqa: E402
from src.infrastructure.skill_tools import build_use_skill_tool_declaration  # noqa: E402
from src.composition.service_container import ServiceContainer  # noqa: E402
from src.composition.user_agent_factory import UserAgentFactory  # noqa: E402
from src.config.settings import load_settings  # noqa: E402
from src.domain.agent import AgentIntent, AgentMessage  # noqa: E402
from src.domain.request_context import RequestContext  # noqa: E402
from src.domain.skill import Skill, render_catalog  # noqa: E402
from src.infrastructure.agent_coordinator import AgentCoordinator  # noqa: E402
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS  # noqa: E402
from src.infrastructure.agent_registry import AgentRegistry  # noqa: E402
from src.ports.llm_port import LLMRequest, Message, MessagePart  # noqa: E402

_TERMINAL = "deliver_response"
_DECOYS = [
    Skill(name="weekly-vendor-report", description="Use when the owner asks for the weekly summary of supplier offers.", body="x"),
    Skill(name="trip-packing-list", description="Use when the owner asks what to pack for a trip or wants a packing checklist.", body="x"),
    Skill(name="invoice-triage", description="Use when the owner forwards an invoice or asks which bills are due.", body="x"),
]
_OUT_DIR = Path("scripts/memory/skills")


def _score(tool_calls, expect):
    names = [tc.name for tc in tool_calls]
    loaded = [(tc.args or {}).get("name") for tc in tool_calls if tc.name == "use_skill"]
    answered_in_batch = _TERMINAL in names
    hit = (expect in loaded) and not answered_in_batch
    return {"first_batch": names, "loaded": loaded, "answered_in_batch": answered_in_batch, "hit": hit}


async def main(cases_path: Path, complexity: str | None) -> None:
    user_id, account_id = os.environ["DEV_USER_ID"], os.environ["DEV_ACCOUNT_ID"]
    cases = json.loads(cases_path.read_text(encoding="utf-8"))

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

    # Resolve provider/model/thinking exactly as production does for this complexity.
    probe = AgentMessage.create(sender="skill_eval", recipient=smart.agent_id, intent=AgentIntent.QUERY,
                                payload={"text": ""},
                                context={"task_complexity": complexity} if complexity else {})
    eff = smart._resolve_effective(probe)

    skills = sorted([*await container.skill_service.list_skills(user_id), *_DECOYS], key=lambda s: s.name)
    async with RequestContext(user_id=user_id, account_id=account_id):
        system_prompt = await smart.prompt_builder.build_for_agent(
            agent_type="smart", user_id=user_id, account_id=account_id, kb_preamble=True,
            capabilities=eff.ctx.capabilities, skills_catalog=render_catalog(skills),
        )
    tools = smart._get_tool_declarations() + [build_use_skill_tool_declaration()]

    rows = []
    for case in cases:
        history = [Message(role="user", parts=[MessagePart(text=case["prompt"])])]
        history = smart._inject_user_turn_anchor(smart._inject_timestamps(history))
        request = LLMRequest(model_name=eff.ctx.model_name, system_instruction=system_prompt, tools=tools,
                             messages=history, temperature=smart.DELEGATION_TEMPERATURE,
                             response_schema=smart._RESPONSE_SCHEMA, thinking=eff.thinking_effort,
                             max_tokens=smart.MAX_TOKENS)
        response = await eff.ctx.provider.generate_content(request=request)
        row = {**case, **_score(response.tool_calls or [], case["expect"])}
        rows.append(row)
        print(f"  expect={case['expect']!s:16} hit={row['hit']!s:5} batch={row['first_batch']}  {case['prompt'][:60]}")

    positives = [r for r in rows if r["expect"]]
    negatives = [r for r in rows if not r["expect"]]
    recall = sum(r["hit"] for r in positives) / max(len(positives), 1)
    false_loads = sum(bool(r["loaded"]) for r in negatives) / max(len(negatives), 1)
    verdict = "PASS" if recall >= 0.8 and false_loads <= 0.1 else "FAIL"
    label = f"{eff.ctx.provider_name}:{eff.ctx.model_name}"
    print(f"\n{label} complexity={complexity or 'default'} recall={recall:.2f} ({len(positives)}) "
          f"false_loads={false_loads:.2f} ({len(negatives)}) → {verdict}")
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / f"trigger_eval_{eff.ctx.model_name}_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"model": label, "complexity": complexity, "recall": recall,
                               "false_loads": false_loads, "verdict": verdict, "rows": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Skill trigger eval")
    ap.add_argument("--cases", required=True, type=Path)
    ap.add_argument("--complexity", default=None,
                    choices=["small_talk", "info_search", "simple_analytics", "deep_reasoning"],
                    help="resolve Smart's provider/model/thinking as production does for this complexity")
    a = ap.parse_args()
    asyncio.run(main(a.cases, a.complexity))

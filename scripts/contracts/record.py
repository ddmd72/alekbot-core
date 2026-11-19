"""Record real provider exchanges for the contract tests: the regeneration command of every recorded fixture.

TEST_COVERAGE_SYSTEMIC_RFC §4a, P17 ("Rules revised": a recorded fixture is accepted only with a regeneration command;
never edited by hand). Each scenario runs through the REAL adapter and the REAL SDK; the SDK's HTTP transport is a
`RecordingTransport` that forwards every request to the provider and keeps the exchange. One file per scenario:
`tests/fixtures/providers/<provider>/<scenario>.json` = `_meta` (source, date, model, SDK version, outcome, cost, the
command that regenerates it) + `exchanges` (request and response of every HTTP call, in order; a streamed response is
kept as its server-sent events). Headers are scrubbed to an allowlist (no auth, request, organisation or project id);
bodies are kept. Prompts use only the invented universe (tests/CLAUDE.md "Invented cast").

Money (enforced here, not by the caller):
- before every HTTP call a pessimistic estimate (1 token per body byte priced as a cache write, the full output
  allowance, a search allowance on a grounding call) must fit under `--max-usd` with what was already spent, or the run
  stops (exit 4) before the call is sent;
- after every call the token legs (input / cache read / cache write / output) and their cost by
  `src/domain/billing.calculate_cost` are printed; a call with tokens whose cost is $0 means an unpriced model or a broken
  meter: the run stops (exit 3);
- `--dry-run` prints the plan and a pessimistic estimate per scenario; it builds each first request through the adapter
  with an in-process transport, so nothing reaches the network and no key is needed.

Usage (make contract-record PROVIDER=openai|anthropic [ONLY=<scenario>] [MAX_USD=1.0] [DRY_RUN=1] [ENV_FILE=<path>]):
    python -m scripts.contracts.record --provider anthropic --dry-run
    python -m scripts.contracts.record --provider openai --only plain_text --env-file ../alekbot-core/.env
Keys: OPENAI_API_KEY / ANTHROPIC_API_KEY from the environment (the Makefile exports `.env`) or `--env-file`.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from src.domain.billing import calculate_cost
from src.domain.exceptions import LLMError
from src.ports.llm_port import LLMRequest, LLMResponse, Message, MessagePart, PromptCacheConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "providers"
OPERATIONS = {"openai": ("POST", "/responses"), "anthropic": ("POST", "/v1/messages")}
KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}

# Pessimistic allowances for the estimate before a call (never billed as such; the real legs are).
GROUNDING_INPUT_ALLOWANCE = 60_000   # tokens a search or fetch can add to one call's input
SEARCHES_PER_CALL_ALLOWANCE = 5
DEFAULT_OUTPUT_ALLOWANCE = 16_000    # when a body names no output cap
# Web search is billed per search on top of tokens, by both providers ($10 per 1,000); billing.py has no such leg, so
# the recorder prices it here and prints it as its own leg.
SEARCH_USD = 0.01

# Headers kept in a recording; every other one (auth, request ids, organisation and project ids, rate limits, cookies,
# the SDK's machine fingerprint) is dropped.
REQUEST_HEADERS = frozenset({"content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta"})
RESPONSE_HEADERS = frozenset({"content-type"})


class BudgetExceeded(Exception):
    """The next call could take the run over --max-usd."""


class BrokenMeter(Exception):
    """A call used tokens and costs $0: the model is unpriced in billing.py, or the usage was not read."""


@dataclass(frozen=True)
class Scenario:
    """One recording: its steps run in order through one adapter; a step builds its request from the responses so far
    (a replay step sends the previous turn back as raw_content)."""

    name: str
    purpose: str                                              # the response kind, or the NOT_REACHABLE entry
    steps: tuple[Callable[[list[LLMResponse]], LLMRequest], ...]

    @property
    def model(self) -> str:
        return self.steps[0]([]).model_name


@dataclass
class Legs:
    """One call's billable usage, as disjoint legs (billing.calculate_cost's convention)."""

    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    searches: int = 0

    @property
    def tokens(self) -> int:
        return self.input + self.cache_read + self.cache_write + self.output


@dataclass
class Meter:
    """The money guard: a pessimistic check before each call, the real cost after it."""

    max_usd: float
    spent: float = 0.0
    calls: int = 0
    lines: list[str] = field(default_factory=list)

    def before(self, body: Any) -> float:
        estimate = estimate_call(body)
        if self.spent + estimate > self.max_usd:
            raise BudgetExceeded(f"the next call may cost up to ${estimate:.4f}; spent ${self.spent:.4f} of "
                                 f"${self.max_usd:.2f}: stopped before sending it")
        return estimate

    def after(self, provider: str, model: str, response: Mapping[str, Any]) -> dict[str, Any]:
        legs = usage_legs(provider, response)
        tokens_usd = calculate_cost(model, legs.input, legs.output, legs.cache_read, legs.cache_write)
        if legs.tokens and tokens_usd == 0:
            raise BrokenMeter(f"{model}: {legs.tokens} tokens cost $0 by billing.py (unpriced model or unread usage)")
        cost = tokens_usd + legs.searches * SEARCH_USD
        self.spent += cost
        self.calls += 1
        line = (f"call {self.calls}: model={model} input={legs.input} cache_read={legs.cache_read} "
                f"cache_write={legs.cache_write} output={legs.output} searches={legs.searches} -> "
                f"${tokens_usd:.6f} tokens + ${legs.searches * SEARCH_USD:.4f} search = ${cost:.6f} "
                f"(spent ${self.spent:.6f} of ${self.max_usd:.2f})")
        self.lines.append(line)
        print(line, flush=True)
        return {"input": legs.input, "cache_read": legs.cache_read, "cache_write": legs.cache_write,
                "output": legs.output, "searches": legs.searches, "usd": round(cost, 6)}


def main(argv: list[str] | None = None, *, upstream: httpx.AsyncBaseTransport | None = None,
         out_dir: Path = FIXTURES_DIR) -> int:
    """The CLI. `upstream` replaces the network for the recorder's own tests."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", required=True, choices=sorted(OPERATIONS))
    parser.add_argument("--only", default="", help="record this scenario only")
    parser.add_argument("--max-usd", type=float, default=1.0, help="hard stop for the run's total (default 1.0)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and the estimate; no network")
    parser.add_argument("--env-file", default="", help="a .env file to read the key from (the environment wins)")
    args = parser.parse_args(argv)
    scenarios = plan(args.provider, args.only)
    if args.dry_run:
        return asyncio.run(dry_run(args.provider, scenarios, args.max_usd))
    if args.env_file:
        _load_env_file(Path(args.env_file))
    key = os.environ.get(KEYS[args.provider], "")
    if not key:
        raise SystemExit(f"{KEYS[args.provider]} is not set (environment or --env-file)")
    return asyncio.run(record(args.provider, scenarios, key, Meter(args.max_usd), upstream=upstream, out_dir=out_dir))


# ------------------------------------------------------------------------------------------------------ the scenarios
def _user(text: str) -> Message:
    return Message(role="user", parts=[MessagePart(text=text)])


LOOKUP_TOOL = {
    "name": "lookup_value",
    "description": "Look a value up in the household notes by its key.",
    "parameters": {"type": "object", "properties": {"key": {"type": "string", "description": "the note's key"}},
                   "required": ["key"]},
}
FESTIVAL_SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "month": {"type": "string"}},
                   "required": ["city", "month"]}
# A static system prompt long enough for either provider to cache (over 4,096 tokens), made of invented-universe facts.
HANDBOOK = "Household notes of Alex Doe, 12 Oak Street, Springfield.\n" + "\n".join(
    f"Note {i}: the 2015 Skoda Fabia was serviced at 90,000 km; the weekend in Coimbra with Clara Moreira is Mar 6-8; "
    f"Jamie and Ana Doe go to Example School; Robin trains at the Iron Temple gym." for i in range(1, 161))
_FESTIVAL = "When is the Sao Joao festival celebrated in Porto? Answer in one sentence and cite your source."
_LOOKUP = "Use the lookup_value tool to find the date of the car service, then answer in one sentence."
_SERVICE = ("Alex Doe's 2015 Skoda Fabia had its oil changed at 10,000 km and was serviced at 90,000 km. "
            "How many km lie between the two? Think it through, then answer with the number only.")


def _req(model: str, prompt: str, **kw: Any) -> Callable[[list[LLMResponse]], LLMRequest]:
    kw.setdefault("max_tokens", 300)
    return lambda prev: LLMRequest(model_name=model, messages=[_user(prompt)], **kw)


def _replay(first: Callable[[list[LLMResponse]], LLMRequest], follow_up: str
            ) -> Callable[[list[LLMResponse]], LLMRequest]:
    """The next turn: the first request's messages, the first answer replayed as raw_content, then the tool results
    (`2026-11-05`, the car service date, for every call) or a short follow-up."""
    def step(prev: list[LLMResponse]) -> LLMRequest:
        request, answer = first([]), prev[0]
        model_turn = Message(role="model", raw_content=answer.raw_content, parts=[
            MessagePart(text=answer.text or ""), *(MessagePart(tool_call=c) for c in answer.tool_calls)])
        after = Message(role="user", parts=[
            MessagePart(tool_response={"name": c.name, "response": "2026-11-05", "tool_use_id": c.thought_signature})
            for c in answer.tool_calls]) if answer.tool_calls else _user(follow_up)
        return request.model_copy(update={"messages": [*request.messages, model_turn, after]})
    return step


def _common(model: str, *, thinking_effort: str) -> dict[str, Scenario]:
    """The response kinds both providers record, on the provider's cheapest model."""
    cached = _req(model, "Which city is the weekend trip in? One word.", system_instruction=HANDBOOK, max_tokens=50,
                  cache_config=PromptCacheConfig(enabled=True))
    tool_turn = _req(model, _LOOKUP, tools=[LOOKUP_TOOL], thinking=thinking_effort, max_tokens=2048)
    scenarios = [
        Scenario("plain_text", "a text answer, thinking off",
                 (_req(model, "Clara Moreira and Alex Doe spend Mar 6-8 in Coimbra. Which city? One word.",
                       max_tokens=50),)),
        Scenario("forced_tool_call", "a forced tool call",
                 (_req(model, _LOOKUP, tools=[LOOKUP_TOOL], force_tool_use=True, max_tokens=200),)),
        Scenario("thinking_on", "a reasoning / thinking block before the answer",
                 (_req(model, _SERVICE, thinking=thinking_effort, max_tokens=2048),)),
        Scenario("prompt_cache_write_then_read", "a cache write, then a cache read of the same prefix",
                 (cached, cached)),
        Scenario("grounding_citations", "web search with citations",
                 (_req(model, _FESTIVAL, use_grounding=True, max_tokens=1024),)),
        Scenario("structured_output", "a JSON answer to a response schema",
                 (_req(model, "Where and in which month is the Sao Joao festival held? From what you know.",
                       response_schema=FESTIVAL_SCHEMA, response_mime_type="application/json", max_tokens=200),)),
        Scenario("replay_thinking_and_tool_use",
                 "a tool call after thinking, replayed as raw_content with the tool result (request-side replay)",
                 (tool_turn, _replay(tool_turn, "Thanks."))),
    ]
    return {s.name: s for s in scenarios}


def _openai_scenarios() -> dict[str, Scenario]:
    return _common("gpt-6-luna", thinking_effort="low")


def _anthropic_scenarios() -> dict[str, Scenario]:
    out = _common("claude-haiku-5-5", thinking_effort="medium")
    # Haiku grounds through the legacy search tools on the plain stream path: its raw_content carries the server-tool
    # blocks, which the next turn replays.
    legacy = _req("claude-haiku-5-5", _FESTIVAL, use_grounding=True, max_tokens=1024)
    dynamic = _req("claude-sonnet-5-5",
                   "Fetch https://example.com, then count the words of its first paragraph with code. "
                   "Answer with the number and cite the page.", use_grounding=True, max_tokens=1024)
    extra = [
        Scenario("replay_server_tool_blocks",
                 "NOT_REACHABLE: server-tool blocks of a grounded answer replayed in the next turn",
                 (legacy, _replay(legacy, "Thanks. Now say it in five words."))),
        Scenario("grounding_dynamic_container",
                 "NOT_REACHABLE container: the dynamic search tools bring code execution; a pause_turn continuation "
                 "sends the container id and replays the code-execution blocks (a pause is the provider's choice)",
                 (dynamic,)),
    ]
    return {**out, **{s.name: s for s in extra}}


SCENARIOS: dict[str, Callable[[], dict[str, Scenario]]] = {"openai": _openai_scenarios,
                                                            "anthropic": _anthropic_scenarios}
# What the brief asks to record that no real call can produce, with the reason.
UNREACHABLE: dict[str, dict[str, str]] = {
    "openai": {},
    "anthropic": {
        "same_provider_fallback": "the adapter falls back to the next model only on a 529/5xx from the provider; a "
                                  "real call cannot provoke one (the request side covers it offline: the fallback "
                                  "shapes of claude_request_shapes.py)",
    },
}


def plan(provider: str, only: str = "") -> list[Scenario]:
    scenarios = SCENARIOS[provider]()
    if only:
        if only not in scenarios:
            raise SystemExit(f"no scenario {only!r} for {provider} (have {sorted(scenarios)})")
        return [scenarios[only]]
    return list(scenarios.values())


# -------------------------------------------------------------------------------------------------- money: estimate
def _is_grounding(body: Any) -> bool:
    tools = body.get("tools") if isinstance(body, dict) else None
    return any(isinstance(t, dict) and str(t.get("type", "")).startswith(("web_search", "web_fetch"))
               for t in tools or [])


def estimate_call(body: Any, *, size: int | None = None) -> float:
    """A pessimistic cost of one call: every body byte a token priced as a cache write (the dearest input leg), the
    whole output cap, and on a grounding call the search allowance."""
    model = body.get("model", "") if isinstance(body, dict) else ""
    if calculate_cost(model, 1_000_000, 0) == 0:
        raise BrokenMeter(f"model {model!r} is not priced in billing.py: no estimate, no call")
    raw = len(json.dumps(body).encode("utf-8")) if size is None else size
    output = body.get("max_output_tokens") or body.get("max_tokens") or DEFAULT_OUTPUT_ALLOWANCE
    grounding = _is_grounding(body)
    input_tokens = raw + (GROUNDING_INPUT_ALLOWANCE if grounding else 0)
    estimate = calculate_cost(model, 0, output, 0, input_tokens)
    estimate = max(estimate, calculate_cost(model, input_tokens, output))
    return estimate + (SEARCHES_PER_CALL_ALLOWANCE * SEARCH_USD if grounding else 0.0)


# ----------------------------------------------------------------------------------------------------- money: usage
def usage_legs(provider: str, response: Mapping[str, Any]) -> Legs:
    """The legs of one recorded response: a JSON body (`body`) or a stream (`events`)."""
    if provider == "openai":
        body = response.get("body") or {}
        usage = body.get("usage") or {}
        details = usage.get("input_tokens_details") or {}
        cached, written = details.get("cached_tokens") or 0, details.get("cache_write_tokens") or 0
        searches = sum(1 for item in body.get("output") or []
                       if item.get("type") == "web_search_call" and (item.get("action") or {}).get("type") == "search")
        return Legs(max((usage.get("input_tokens") or 0) - cached - written, 0), cached, written,
                    usage.get("output_tokens") or 0, searches)
    usage: dict[str, Any] = {}
    for event in response.get("events") or []:
        data = event.get("data") or {}
        if data.get("type") == "message_start":
            usage.update((data.get("message") or {}).get("usage") or {})
        elif data.get("type") == "message_delta":
            usage.update({k: v for k, v in (data.get("usage") or {}).items() if v is not None})
    if not usage and isinstance(response.get("body"), dict):
        usage = response["body"].get("usage") or {}
    return Legs(usage.get("input_tokens") or 0, usage.get("cache_read_input_tokens") or 0,
                usage.get("cache_creation_input_tokens") or 0, usage.get("output_tokens") or 0,
                (usage.get("server_tool_use") or {}).get("web_search_requests") or 0)


# ------------------------------------------------------------------------------------------------- the transport
def scrub_headers(headers: Mapping[str, str], keep: frozenset[str]) -> dict[str, str]:
    """Only the allowlisted headers, lower-cased and sorted."""
    return {k.lower(): v for k, v in sorted(headers.items(), key=lambda kv: kv[0].lower()) if k.lower() in keep}


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Server-sent events as `[{"event": name, "data": JSON}]` (data that is not JSON stays a string)."""
    events: list[dict[str, Any]] = []
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        name, data = None, []
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:"):].lstrip())
        if name is None and not data:
            continue
        payload = "\n".join(data)
        try:
            parsed: Any = json.loads(payload)
        except ValueError:
            parsed = payload
        events.append({"event": name, "data": parsed})
    return events


class RecordingTransport(httpx.AsyncBaseTransport):
    """Forwards each request upstream after the meter allows it, keeps the exchange, hands the SDK the same response."""

    def __init__(self, provider: str, meter: Meter, upstream: httpx.AsyncBaseTransport | None = None):
        self.provider = provider
        self.meter = meter
        self.upstream = upstream or httpx.AsyncHTTPTransport()
        self.exchanges: list[dict[str, Any]] = []
        # A money stop raised here reaches the adapter wrapped by the SDK (a connection error): kept to re-raise as is.
        self.stop: BudgetExceeded | BrokenMeter | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._handle(request)
        except (BudgetExceeded, BrokenMeter) as stop:
            self.stop = stop
            raise

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        estimate = self.meter.before(body)
        upstream = await self.upstream.handle_async_request(request)
        try:
            content = await upstream.aread()
        finally:
            await upstream.aclose()
        # `aread` decoded the body: drop the headers that describe the encoded one.
        headers = [(k, v) for k, v in upstream.headers.multi_items()
                   if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
        response = httpx.Response(upstream.status_code, headers=headers, content=content, request=request)
        recorded: dict[str, Any] = {"status": response.status_code,
                                    "headers": scrub_headers(response.headers, RESPONSE_HEADERS)}
        if "text/event-stream" in response.headers.get("content-type", ""):
            recorded["events"] = parse_sse(response.text)
        else:
            try:
                recorded["body"] = response.json()
            except ValueError:
                recorded["text"] = response.text
        exchange = {
            "request": {"method": request.method, "path": request.url.path,
                        "headers": scrub_headers(request.headers, REQUEST_HEADERS), "body": body},
            "response": recorded,
            "estimate_usd": round(estimate, 6),
        }
        model = body.get("model", "") if isinstance(body, dict) else ""
        exchange["cost"] = self.meter.after(self.provider, model, recorded)
        self.exchanges.append(exchange)
        return response


# ------------------------------------------------------------------------------------------------------ the runs
def build_adapter(provider: str, key: str, transport: httpx.AsyncBaseTransport) -> Any:
    """The production adapter with its SDK client on `transport` (no SDK retries: a retry is a second paid call)."""
    http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(300.0, connect=10.0))
    if provider == "openai":
        from openai import AsyncOpenAI

        from src.adapters.openai_adapter import OpenAIAdapter
        adapter: Any = OpenAIAdapter(api_key=key)
        adapter.client = AsyncOpenAI(api_key=key, max_retries=0, http_client=http)
        return adapter
    from anthropic import AsyncAnthropic

    from src.adapters.claude_adapter import ClaudeAdapter
    adapter = ClaudeAdapter(api_key=key)
    adapter.client = AsyncAnthropic(api_key=key, max_retries=0, http_client=http)
    return adapter


def _sdk_version(provider: str) -> str:
    module = __import__("openai" if provider == "openai" else "anthropic")
    return f"{module.__name__} {module.__version__}"


async def run_scenario(provider: str, scenario: Scenario, key: str, meter: Meter,
                       upstream: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Every step of the scenario through the real adapter; the recording document. An LLM error ends the scenario
    and is its recorded outcome (a provider's refusal is a finding, not a crash); a money stop propagates as itself,
    whatever the SDK wrapped it in."""
    transport = RecordingTransport(provider, meter, upstream)
    adapter = build_adapter(provider, key, transport)
    responses: list[LLMResponse] = []
    outcome = "ok"
    for step in scenario.steps:
        try:
            responses.append(await adapter.generate_content(step(responses)))
        except Exception as error:
            if transport.stop is not None:
                raise transport.stop from error
            if not isinstance(error, LLMError):
                raise
            outcome = f"{type(error).__name__}: {error}"
            break
    method, path = OPERATIONS[provider]
    return {
        "_meta": {
            "provider": provider, "operation": f"{method} {path}", "scenario": scenario.name,
            "purpose": scenario.purpose, "source": "recorded",
            "recorded_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": scenario.model, "sdk": _sdk_version(provider), "outcome": outcome,
            "cost_usd": round(sum(e["cost"]["usd"] for e in transport.exchanges), 6),
            "regenerate": f"make contract-record PROVIDER={provider} ONLY={scenario.name}",
        },
        "exchanges": transport.exchanges,
    }


def write_recording(provider: str, doc: Mapping[str, Any], out_dir: Path = FIXTURES_DIR) -> Path:
    path = out_dir / provider / f"{doc['_meta']['scenario']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


async def record(provider: str, scenarios: list[Scenario], key: str, meter: Meter, *,
                 upstream: httpx.AsyncBaseTransport | None = None, out_dir: Path = FIXTURES_DIR) -> int:
    """Record each scenario and write its file. Exit codes: 0 all ok, 1 a scenario ended in an LLM error (written),
    3 a broken meter, 4 the budget (the scenario in progress is not written)."""
    failed = 0
    for scenario in scenarios:
        print(f"[{scenario.name}] {scenario.purpose}", flush=True)
        try:
            doc = await run_scenario(provider, scenario, key, meter, upstream)
        except BrokenMeter as error:
            print(f"STOP (broken meter): {error}", flush=True)
            return 3
        except BudgetExceeded as error:
            print(f"STOP (budget): {error}", flush=True)
            return 4
        path = write_recording(provider, doc, out_dir)
        failed += doc["_meta"]["outcome"] != "ok"
        print(f"[{scenario.name}] {doc['_meta']['outcome']}; ${doc['_meta']['cost_usd']:.6f} -> {path}", flush=True)
    print(f"total ${meter.spent:.6f} over {meter.calls} calls", flush=True)
    return 1 if failed else 0


async def first_request_body(provider: str, scenario: Scenario) -> dict[str, Any]:
    """The body the scenario's first call would send, built by the real adapter and SDK on an in-process transport
    that answers 400 (no network, no key)."""
    seen: list[httpx.Request] = []

    def sink(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                   "message": "dry run"}})

    adapter = build_adapter(provider, "dry-run", httpx.MockTransport(sink))
    try:
        await adapter.generate_content(scenario.steps[0]([]))
    except LLMError:
        pass
    if not seen:
        raise RuntimeError(f"{scenario.name}: the adapter sent nothing")
    return json.loads(seen[0].content)


async def dry_run(provider: str, scenarios: list[Scenario], max_usd: float) -> int:
    """The plan and a pessimistic estimate per scenario: a later step is estimated as the first one with its body
    grown by the replayed history (step k: k+1 times the bytes), a grounding step as up to 3 calls (a paused turn)."""
    total = 0.0
    print(f"{provider}: {len(scenarios)} scenarios, cap ${max_usd:.2f}")
    for scenario in scenarios:
        body = await first_request_body(provider, scenario)
        size = len(json.dumps(body).encode("utf-8"))
        calls = 3 if _is_grounding(body) and provider == "anthropic" else 1
        estimate = sum(estimate_call(body, size=size * (k + 1)) * calls for k in range(len(scenario.steps)))
        total += estimate
        print(f"  {scenario.name:32} {body['model']:20} steps={len(scenario.steps)} up to ${estimate:.4f}  "
              f"({scenario.purpose})")
    for name, reason in UNREACHABLE[provider].items():
        print(f"  {name:32} not recorded: {reason}")
    print(f"pessimistic total ${total:.4f} (the cap stops the run at ${max_usd:.2f} either way)")
    return 0


def _load_env_file(path: Path) -> None:
    from dotenv import load_dotenv
    if not path.is_file():
        raise SystemExit(f"--env-file {path}: no such file")
    load_dotenv(path, override=False)


if __name__ == "__main__":
    sys.exit(main())

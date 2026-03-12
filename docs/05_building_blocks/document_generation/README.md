# Document Generation (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the two-agent pipeline for generating professional DOCX files from natural language
requests: **DocPlannerAgent** (layout planning) → **DocGeneratorAgent** (Node.js code generation
and execution) → async delivery via Slack.

### When to Read

- **For AI Agents:** Before modifying document generation logic, adding new document types, or
  changing the async delivery path.
- **For Developers:** When extending the runner adapter (e.g., Cloud Function), changing the spec
  format, or troubleshooting Slack file delivery.

### When to Update

This document MUST be updated when:

- [ ] DocPlannerAgent or DocGeneratorAgent execution logic changes.
- [ ] `DocxRunnerPort` interface or `NodeDocxRunner` implementation changes.
- [ ] The JSON layout spec schema changes.
- [ ] A new `DocxRunnerPort` implementation is added (Cloud Function, etc.).
- [ ] `AgentWorkerHandler._deliver_docx_result()` delivery path changes.
- [ ] `UserNotificationService.notify_file_bytes()` channel resolution logic changes.

### Cross-References

- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)
- **Agent Registry (ACP v2):** [../agent_registry/README.md](../agent_registry/README.md)
- **New Agent Playbook:** [../../how_to/NEW_AGENT_PLAYBOOK.md](../../how_to/NEW_AGENT_PLAYBOOK.md)

---

## 1. Overview

Document generation converts a natural language request ("Write a sales proposal for Acme Corp")
into a professionally formatted `.docx` file delivered directly in Slack.

**Why two agents:**

| Concern | Agent | Layer |
|---------|-------|-------|
| Understanding document structure, selecting sections, deciding layout | `DocPlannerAgent` | LLM reasoning (PERFORMANCE tier) |
| Writing executable Node.js + `docx` npm script, retrying on errors | `DocGeneratorAgent` | LLM code generation (PERFORMANCE tier) |

Separation keeps each LLM call focused. The planner reasons about *what* the document should
contain; the generator reasons about *how* to produce it as binary output. Neither knows about
Slack — delivery is handled by `AgentWorkerHandler`.

**Execution mode:** Both agents run as independent `ASYNC` Cloud Tasks. The orchestrator receives
an immediate ACK; the planner runs in the background and enqueues the generator as a second Cloud
Task (fire and forget); the DOCX file is uploaded to the user's channel when the generator
completes.

---

## 2. Architecture

```
User → Smart/Quick → coordinator.handle_delegation(CREATE_DOCUMENT, ASYNC)
                              │
                              └─ Cloud Task #1 enqueued (DocPlannerAgent)
                                        │
                              [background: /worker endpoint]
                                        │
                              AgentWorkerHandler.handle_task()
                                        │
                              DocPlannerAgent.execute()
                                  │
                                  ├─ LLM: natural language → JSON layout spec (single call)
                                  │
                                  └─ coordinator.handle_delegation(GENERATE_DOCX_CODE, ASYNC)
                                              │
                                              └─ Cloud Task #2 enqueued (DocGeneratorAgent)
                                                        │
                                              [background: /worker endpoint]
                                                        │
                                              AgentWorkerHandler.handle_task()
                                                        │
                                              DocGeneratorAgent.execute()
                                                        │
                                                        ├─ LLM: spec → Node.js script (tool-calling loop)
                                                        │
                                                        └─ DocxRunnerPort.run(js_code, raw_spec)
                                                              │
                                                        NodeDocxRunner (subprocess)
                                                              │
                                                        DOCX bytes → DeliveryItem("file_upload")
                                                              │
                                              AgentWorkerHandler._deliver_docx_result()
                                                        │
                                              UserNotificationService.notify_file_bytes()
                                                        │
                                              SlackMediaAdapter.upload_file(channel_id=D...)
```

**Hexagonal boundaries:**

- `DocxRunnerPort` — system boundary between application and OS subprocess (or future Cloud Function).
- `PlatformMediaPort` — system boundary between notification service and Slack file API.
- `NotificationStatePort` — reads stored channel info; no platform coupling.

---

## 3. DocPlannerAgent

**File:** `src/agents/doc_planner_agent.py`
**Intent:** `Intent.CREATE_DOCUMENT`
**ExecutionMode:** `ASYNC` (always runs as Cloud Task)
**Tier:** PERFORMANCE (Claude default, resolved by `AgentContextBuilder`)

### 3.1 can_handle

```python
message.intent in (AgentIntent.QUERY, AgentIntent.DELEGATE)
and bool(message.payload.get("query", ""))
```

Accepts both `QUERY` (coordinator's `_execute_sync` path, tests) and `DELEGATE`
(`AgentWorkerHandler` Cloud Task path). This is the only ASYNC specialist that requires `DELEGATE`
in its `can_handle` — all SYNC specialists only check `QUERY`.

### 3.2 Execution — Phase 1: Planning

Single LLM call with `response_mime_type="application/json"` and `_RESPONSE_SCHEMA` (enforces
`{status, task_summary, doc_spec}` envelope at Gemini level; silently ignored by Claude).
The system prompt is assembled via `PromptBuilder` profile `"doc_planner"`.

**Flow:**

1. LLM call → raw JSON text captured from `response.text`.
2. Raw text forwarded as-is to `DocGeneratorAgent` via `coordinator.handle_delegation(
   GENERATE_DOCX_CODE, raw, context)` — no parsing, no status check.
3. Generator enqueued as a separate ASYNC Cloud Task (fire and forget).
4. Planner returns `AgentResponse.success` immediately — does not wait for generation.

The planner never parses its own LLM output. If the LLM wraps JSON in a markdown fence, the
generator's LLM will handle it. This keeps the planner stateless and eliminates a full retry
cycle just for formatting.

### 3.3 JSON Layout Spec Schema

```json
{
  "status": "ready",
  "task_summary": "Short human-readable description of what will be created",
  "doc_spec": {
    "document_type": "report",
    "title": "Sales Report Q1 2026",
    ...
  }
}
```

`doc_spec` is an open object — structure is defined by the `doc_planner` Firestore prompt.
`DocGeneratorAgent` receives the full spec as a raw JSON string (may include markdown wrapper).

### 3.4 Failure Hooks

All failure paths call `_on_agent_error(exc, context_tag)` before returning:

| Path | Tag |
|------|-----|
| Empty query | `"empty_query"` |
| PromptBuilder failure | `"prompt_builder"` |

---

## 4. DocGeneratorAgent

**File:** `src/agents/doc_generator_agent.py`
**Intent:** `Intent.GENERATE_DOCX_CODE`
**ExecutionMode:** `ASYNC` (runs as an independent Cloud Task dispatched by DocPlannerAgent)
**Registration:** `internal=True` — never shown to LLM tool selection
**Tier:** PERFORMANCE (Claude default)

`payload["query"]` is the raw LLM text output from DocPlannerAgent. It is included verbatim in
the LLM user message and piped to the Node.js script's stdin.

### 4.1 Execution — Phase 2: Code Generation

Tool-calling loop (max `MAX_TURNS = 5`) with a single tool `generate_docx`:

```
Turn 1..N:
  LLM call (system prompt via PromptBuilder "doc_generator")
    │
    ├─ tool_calls = [ToolCall(name="generate_docx", args={"js_code": "..."})]
    │      │
    │      └─ DocxRunnerPort.run(js_code, raw_query, timeout)
    │              ├─ SUCCESS → capture bytes, send {status:"success", bytes_size:N} back to LLM
    │              └─ DocxRunnerError → send {status:"error", stderr:"..."} back to LLM → LLM retries
    │
    └─ no tool_calls (LLM text only)
           ├─ bytes already captured → break loop, return success
           └─ no bytes → DocGeneratorError → failure
```

**Node.js contract:** The LLM-written script must read the JSON spec from `process.stdin` and
write raw DOCX bytes to `process.stdout`. Any output to `stderr` is non-fatal and logged as debug.

### 4.2 Result

On success, `DocGeneratorAgent` returns:

```python
AgentResponse.success(
    result="docx_generated",
    delivery_items=[
        DeliveryItem(
            type="file_upload",
            data={
                "file_bytes_b64": "<base64-encoded DOCX>",
                "filename": "report-2026-03-12.docx",
                "title": "Sales Report Q1",
            },
        )
    ],
)
```

Filename is generated by `_make_filename(doc_spec)`:
`{document_type_lowercase}-{YYYY-MM-DD}.docx`.
`doc_spec` is extracted via best-effort `json.loads(raw_query)` — falls back to
`"document-{date}.docx"` if parsing fails (e.g., markdown wrapper present).

### 4.3 Failure Hooks

| Path | Hook called |
|------|-------------|
| Empty query | `_on_agent_error(ValueError, "empty_query")` |
| PromptBuilder failure | `_on_agent_error(exc, "prompt_builder")` |
| LLM no tool call | `_on_agent_error(DocGeneratorError, "docx_generation")` |
| MAX_TURNS exhausted | `_on_agent_error(RuntimeError, "docx_generation")` |

---

## 5. DocxRunnerPort — System Boundary

**File:** `src/ports/docx_runner_port.py`

```python
class DocxRunnerError(Exception): ...

class DocxRunnerPort(ABC):
    async def run(self, js_code: str, spec_json: str, timeout: int) -> bytes: ...
```

**Why a port:** Subprocess execution is a system boundary per the hexagonal rule:
"Port is justified when: 2+ implementations, testable substitution, system boundary."
Future implementations may include Cloud Run Jobs or a serverless Cloud Function for environments
where `node` is not available or for better isolation.

### 5.1 NodeDocxRunner (current implementation)

**File:** `src/adapters/node_docx_runner.py`

Writes `js_code` to a temp `.js` file inside `docx_generator/` (project root), launches
`node <tmp_file>` via `asyncio.create_subprocess_exec`, pipes `spec_json` to stdin, captures
stdout as raw DOCX bytes.

**Why `docx_generator/` directory:**
The `docx` npm library must be resolvable via `node_modules/`. Writing the temp file inside
`docx_generator/` ensures Node.js resolves the package correctly without global install.

**Error cases:**
- Non-zero exit code → `DocxRunnerError("exit code N\n<stderr>")`
- Timeout → kills process → `DocxRunnerError("timed out after Ns")`
- Empty stdout → `DocxRunnerError("stdout is empty")`

**Temp file cleanup:** `finally: os.unlink(tmp.name)` — guaranteed regardless of success or failure.

**Configuration:** `DOC_GENERATOR.node_timeout_s` from `agent_config.py` (default: 60s).

### 5.2 Future Implementations

| Implementation | When to use |
|---|---|
| `NodeDocxRunner` | Local `node` + npm. Current default. |
| `CloudFunctionDocxRunner` | Serverless; no node on the host; better sandbox isolation. |
| `CloudRunJobDocxRunner` | Heavy documents; longer timeout; dedicated CPU. |

All implementations raise `DocxRunnerError` on failure. `DocGeneratorAgent` catches only
`DocxRunnerError` — platform-specific exceptions stay inside the adapter.

---

## 6. Async Delivery Flow

### 6.1 Cloud Task Dispatch — Planner

When Smart or Quick delegates `CREATE_DOCUMENT`, `AgentCoordinator.handle_delegation()` sees
`ExecutionMode.ASYNC` in `DOC_PLANNER` descriptor and calls `_execute_async()`, which enqueues
Cloud Task #1 with `dispatch_deadline_s=720`:

```json
{
  "task_type": "agent_execution",
  "agent_id": "doc_planner_agent",
  "intent": "create_document",
  "query": "<original natural-language request>",
  "context": {"user_id": "...", "account_id": "...", "session_id": "..."}
}
```

The orchestrator returns an immediate ACK: `"Your document is being created. I'll send it when
it's ready."`.

### 6.2 Cloud Task Dispatch — Generator

Inside its Cloud Task, `DocPlannerAgent` calls `coordinator.handle_delegation(GENERATE_DOCX_CODE, raw)`.
Coordinator sees `ExecutionMode.ASYNC` in `DOC_GENERATOR` descriptor and enqueues Cloud Task #2
with `dispatch_deadline_s=720`. The planner returns immediately after enqueuing.

### 6.3 Worker Execution

`AgentWorkerHandler.handle_task()` at `/worker` endpoint handles both Cloud Tasks:

**Planner task:**
1. Reconstructs `AgentMessage` with `intent=AgentIntent.DELEGATE`.
2. Routes to `doc_planner_agent_{user_id}` via coordinator (explicit routing).
3. On `SUCCESS` → no delivery (planner returns no delivery_items).
4. On `FAILED/CANNOT_HANDLE` → `_notify_docx_failure(context, error)` → QuickAgent informs user.

**Generator task:**
1. Same flow; routes to `doc_generator_agent_{user_id}`.
2. On `SUCCESS` → calls `_deliver_docx_result(response, context)`.
3. On `FAILED/CANNOT_HANDLE` → `_notify_docx_failure(context, error)`.

Both `Intent.CREATE_DOCUMENT` and `Intent.GENERATE_DOCX_CODE` are routed to the same delivery
and failure handlers in `AgentWorkerHandler`.

### 6.4 DOCX File Delivery

`AgentWorkerHandler._deliver_docx_result()`:
1. Iterates `response.delivery_items` filtered by `type == "file_upload"`.
2. Decodes `file_bytes_b64` → raw bytes.
3. Calls `notification_service.notify_file_bytes(user_id, account_id, bytes, filename, title)`.

`UserNotificationService.notify_file_bytes()`:
1. Loads `channel_info` from `NotificationStatePort` — may be a Slack user ID (`U...`).
2. Creates `response_channel` via `NotificationChannelFactoryPort`.
3. **Channel ID resolution:** Sends `"📎"` placeholder via `response_channel.send_message()`.
   `SlackResponseChannel.send_message()` normalises `channel_id` from `U...` to `D...` (the real
   DM channel ID) by reading `response["channel"]` from the Slack API response and updating
   `self.channel_id`. This is the same established pattern used for `chat.update` in DMs.
4. Reads resolved `response_channel.channel_id` → passes it to `platform_media.upload_file()`.

**Why channel ID resolution is here (not in SlackMediaAdapter):**

`ConversationHandler` intentionally saves the Slack user ID (`U...`) instead of the DM channel
ID (`D...`) for DMs — this makes text notifications resilient to stale DM channels after bot
reinstall. `chat.postMessage` accepts user IDs; `files.completeUploadExternal` does not. The fix
lives in `notify_file_bytes()` because this is where the platform-agnostic service bridges to
the platform-specific file upload API, using an already-established platform pattern
(`send_message` → normalise → resolved channel).

---

## 7. Prompt Work

Both agents use Firestore prompt profiles. Blueprint and tokens must be uploaded before the agent
can produce structured output on Claude.

| Agent | `agent_type` | Blueprint | Profile doc ID |
|-------|-------------|-----------|----------------|
| DocPlannerAgent | `"doc_planner"` | `doc_planner_agent_v1` | `doc_planner` |
| DocGeneratorAgent | `"doc_generator"` | `doc_generator_agent_v1` | `doc_generator` |

**Required token classes in blueprints:**
- `cognitive_process` — identity, spec format rules, anti-patterns
- `output_format` — JSON envelope contract (critical for Claude; Gemini uses `response_schema` as backup)

Upload commands (human only, dev first):
```bash
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_DOC_PLANNER --format json
python firestore_utils/upload.py development_domain_prompt_blueprints_v3 doc_planner_agent_v1 --format json
python firestore_utils/upload.py development_domain_prompt_profiles_v3 doc_planner --format json
# repeat for doc_generator
```

---

## 8. Agent Configuration

Both agents read from `src/infrastructure/agent_config.py`:

| Parameter | DocPlannerAgent | DocGeneratorAgent |
|-----------|-----------------|-------------------|
| `temperature` | `1.0` (required when thinking is enabled) | `0.5` (balanced: code precision + rendering reasoning) |
| `max_tokens` | `16_000` | `16_000` |
| `timeout_ms` | `600_000` (10 min) | `600_000` (10 min) |
| `thinking_effort` | `"medium"` | `"medium"` |
| `node_timeout_s` | — | `60` |
| `dispatch_deadline_s` | `720` (600s + 2 min overhead) | `720` |

Temperature 1.0 on the planner is a hard Claude constraint when `thinking` is enabled.
Temperature 0.5 on the generator balances deterministic code output with the reasoning needed
for the pre-submission rendering simulation step.

---

## 9. Error Handling Summary

| Layer | Error | User experience |
|-------|-------|-----------------|
| `DocPlannerAgent`: PromptBuilder failure | Cannot assemble system prompt | `_notify_docx_failure()` → QuickAgent informs user |
| `DocGeneratorAgent`: Node.js always fails | Script has bugs LLM cannot fix in 5 turns | Same |
| `DocGeneratorAgent`: `DocxRunnerError` (timeout) | `node` binary absent or process hung | Same |
| `notify_file_bytes`: `send_message` fails | Slack API error before file upload | Error logged, silent failure (file not delivered) |
| `upload_file` fails | Slack file API error | Error logged, silent failure |

**Circuit breaker:** Both agents inherit `CircuitBreaker` from `BaseAgent`. 3 consecutive
failures open the circuit for 5 minutes — Cloud Tasks will retry the job (exponential backoff),
by which time the circuit may have recovered.

---

## 10. Code References

- `src/agents/doc_planner_agent.py` — Phase 1: planning, raw forward to generator, fire-and-forget
- `src/agents/doc_generator_agent.py` — Phase 2: tool-calling loop, `_make_filename`
- `src/ports/docx_runner_port.py` — `DocxRunnerPort` ABC + `DocxRunnerError`
- `src/adapters/node_docx_runner.py` — `NodeDocxRunner`: temp file, subprocess, timeout
- `src/infrastructure/agent_manifest.py` — `Intent.CREATE_DOCUMENT`, `Intent.GENERATE_DOCX_CODE`, `DOC_PLANNER`, `DOC_GENERATOR` descriptors
- `src/infrastructure/agent_config.py` — `DocPlannerAgentConfig`, `DocGeneratorAgentConfig`
- `src/infrastructure/agent_coordinator.py` — `_execute_async` receives `deadline_seconds` from descriptor
- `src/services/agent_context_builder.py` — `"doc_planner"` and `"doc_generator"` strategy entries
- `src/composition/user_agent_factory.py` — `NodeDocxRunner()` injected into `DocGeneratorAgent`; both agents registered and cached per user
- `src/handlers/agent_worker_handler.py` — `_deliver_docx_result()`, `_notify_docx_failure()`, handles both `CREATE_DOCUMENT` and `GENERATE_DOCX_CODE` intents
- `src/services/user_notification_service.py` — `notify_file_bytes()`, channel ID resolution via `send_message`
- `src/adapters/slack/response_channel.py:155-160` — `send_message` normalises `U...` → `D...`
- `src/adapters/slack/media_adapter.py` — `SlackMediaAdapter.upload_file()`
- `docx_generator/` — project root directory; `node_modules/docx` installed here
- `tests/unit/agents/test_doc_planner_agent.py` — 21 tests (can_handle, fire-and-forget delegation, failure paths, LLM call params)
- `tests/unit/agents/test_doc_generator_agent.py` — 19 tests (port mock, tool loop, failure paths, raw query forwarding)
- `tests/unit/adapters/test_slack_media_adapter.py` — upload_file contract tests

---

## 11. Status

**Status:** ✅ Production Ready
**Last Updated:** 2026-03-12

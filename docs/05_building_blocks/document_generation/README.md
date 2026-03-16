# Document Generation (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the pipelines for generating professional documents and web pages from natural language
requests:

- **DOCX pipeline:** DocPlannerAgent (layout planning) → DocGeneratorAgent (Node.js `docx` npm code generation) → async delivery via Slack.
- **PDF pipeline:** PdfGeneratorAgent (single LLM call: natural language → HTML+CSS + Puppeteer rendering) → GCS storage + async delivery via Slack.
- **HTML page pipeline:** HtmlPageGeneratorAgent (single LLM call: natural language → HTML+CSS+JS) → GCS public URL delivered as Slack link.

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
- [ ] PdfGeneratorAgent execution logic changes (single-LLM-call pipeline).
- [ ] `PuppeteerRunnerPort` interface or `NodePuppeteerRunner` implementation changes.
- [ ] `DocumentDeliveryService` GCS storage logic or key format changes.
- [ ] `pdf_generator/runner.js` stdin/stdout contract changes.
- [ ] HtmlPageGeneratorAgent execution logic changes.
- [ ] `COGNITIVE_PROCESS_HTML_PAGE` token rules change (output contract, mobile-first, CDN allowlist, OG tags).
- [ ] `GcsMediaAdapter._inject_noindex()` logic or scope changes.
- [ ] GCS bucket IAM policy changes (affects public URL accessibility).

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

`payload["query"]` is the raw LLM text output from DocPlannerAgent (JSON string). It is prepended
to the system prompt as a `document_spec { ... }` block (context before rules), and piped to the
Node.js script's stdin at execution time. The LLM user message is a fixed `"Generate."` string.

### 4.0 can_handle

```python
message.intent in (AgentIntent.QUERY, AgentIntent.DELEGATE)
and bool(message.payload.get("query"))
```

Accepts both `QUERY` (coordinator's direct call, tests) and `DELEGATE`
(`AgentWorkerHandler` Cloud Task path). Same pattern as `DocPlannerAgent`.

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

## 10. PDF Generation Pipeline

The PDF pipeline uses a **single-ASYNC-task** pattern: one Cloud Task, one LLM call. Unlike the
DOCX pipeline (two-phase planner → generator), PDF generation is handled by a single
`PdfGeneratorAgent` that accepts a natural-language request, writes a complete HTML+CSS document
in one shot, and renders it to PDF via Puppeteer. Both HTML and PDF are stored to GCS via
`DocumentDeliveryService` before delivery.

### 10.1 Architecture

```
User → Smart/Quick → coordinator.handle_delegation(CREATE_PDF, ASYNC)
                              │
                              └─ Cloud Task enqueued (PdfGeneratorAgent)
                                        │
                              [background: /worker endpoint]
                                        │
                              AgentWorkerHandler.handle_task()
                                        │
                              PdfGeneratorAgent.execute()
                                  │
                                  ├─ PromptBuilder → system prompt (agent_type="pdf_generator")
                                  │
                                  ├─ Single LLM call: natural language → complete HTML+CSS
                                  │   (auto-selects design language from embedded style catalogue)
                                  │
                                  ├─ _strip_markdown_fences(html)
                                  │
                                  ├─ PuppeteerRunnerPort.run(html)
                                  │        │
                                  │   NodePuppeteerRunner (node pdf_generator/runner.js)
                                  │        │
                                  │   PDF bytes
                                  │
                                  ├─ _extract_filename_from_html(html) → (slug, display_name)
                                  │   (reads <title> tag; falls back to "document")
                                  │
                                  └─ DeliveryItem("document") × 2
                                              │
                                  AgentWorkerHandler._deliver_item()
                                              │
                                  DocumentDeliveryService.store() → GCS: docs/{uuid}-{filename}
                                              │
                                  UserNotificationService.notify_document_link()  [HTML + PDF links]
                                  UserNotificationService.notify_file_bytes()     [PDF upload only]
```

**Hexagonal boundaries:**

- `PuppeteerRunnerPort` — system boundary between application and Node.js Puppeteer subprocess.
- `MediaStoragePort` — system boundary between `DocumentDeliveryService` and GCS.
- `PlatformMediaPort` — system boundary between notification service and Slack file API.

### 10.2 PdfGeneratorAgent

**File:** `src/agents/pdf_generator_agent.py`
**Intent:** `Intent.CREATE_PDF`
**ExecutionMode:** `ASYNC` (always runs as Cloud Task)
**Registration:** `internal=False` — exposed to LLM tool selection (Quick, Smart)
**Tier:** BALANCED (Gemini, `agent_type="pdf_generator"`)

Single LLM call → complete HTML+CSS document as raw text response. The system prompt embeds a
design language catalogue (12 styles: Apple Keynote, Economist, Gov.uk, McKinsey/BCG, Stripe,
Tufte, etc.) and instructs the LLM to auto-select the most appropriate style based on content
type. The model writes a fully self-contained HTML+CSS page optimised for Puppeteer rendering
(screen PDF, not print): rich color, generous whitespace, `@page` CSS, `break-inside: avoid` on
sections, mobile `@media` block for HTML readability.

**Output processing:**
1. `_strip_markdown_fences(html)` — removes accidental ` ```html ``` ` wrapping.
2. `PuppeteerRunnerPort.run(html_code)` → PDF bytes. Failure → `AgentResponse.failure`.
3. `_extract_filename_from_html(html)` — extracts `<title>` text: display name + sanitized slug.
   Fallback to `("document", "Document")` when `<title>` absent or empty.

**Delivery items returned on success:**

1. HTML source — `content_type="text/html"`, `file_upload=False` → stored to GCS only.
2. PDF binary — `content_type="application/pdf"`, `file_upload=True` → stored to GCS + uploaded to Slack.

**Known CSS rendering constraints:**
- CSS Paged Media margin boxes (`@top-left`, `@bottom-center`, etc.) are silently ignored by headless Chromium. Headers and footers must use CSS `position: fixed` elements or `@page` rules for page numbers only.
- `break-inside: avoid` is mandatory on sections, tables, and callouts to prevent bad page breaks.
- `NEVER use break-before: page` — causes blank pages in headless Chromium.

**Prompt:** PromptBuilder profile `pdf_generator`, blueprint `pdf_generator_agent_v1`.
Tokens: `PDF_GENERATOR_COGNITIVE_PROCESS`, `OUTPUT_FORMAT_PDF_GENERATOR`.

**Agent configuration** (from `agent_config.py`):

| Parameter | Value |
|-----------|-------|
| `temperature` | `0.5` |
| `max_tokens` | `64_000` (full HTML+CSS can be large) |
| `timeout_ms` | `600_000` (10 min) |
| `node_timeout_s` | `60` |

### 10.3 PuppeteerRunnerPort — System Boundary

**File:** `src/ports/puppeteer_runner_port.py`

```python
class PuppeteerRunnerError(Exception): ...

class PuppeteerRunnerPort(ABC):
    async def run(self, html: str, timeout: int) -> bytes: ...
```

**Why a port:** Subprocess execution is a system boundary. The interface is also testable in
isolation — `PdfGeneratorAgent` receives a mock port in unit tests without spawning Node.js.

**NodePuppeteerRunner** (`src/adapters/node_puppeteer_runner.py`): pipes `html` to stdin of
`node pdf_generator/runner.js`, captures raw PDF bytes from stdout. Error cases match
`NodeDocxRunner`: non-zero exit code, timeout, or empty stdout all raise `PuppeteerRunnerError`.

**Why `pdf_generator/` is separate from `docx_generator/`:** Each is an independent Node.js
project with its own `node_modules/`. `docx_generator/` has the `docx` npm library;
`pdf_generator/` has `puppeteer ^24.x`, which downloads its own bundled Chromium (~170 MB)
during `npm install`. Mixing them would couple two unrelated dependency trees.

**`pdf_generator/runner.js` contract:** Reads HTML from stdin, renders via Puppeteer headless
Chrome with `printBackground: true` and the page size/margins from the JSON spec, writes raw
PDF bytes to stdout, exits 0. No intermediate files — stdin → stdout pipeline.

### 10.4 DocumentDeliveryService

**File:** `src/services/document_delivery_service.py`

Stores document bytes to GCS via `MediaStoragePort`. Key format: `docs/{uuid4()}-{filename}`.
Returns a signed URL or public GCS URL depending on bucket configuration. Used by
`PdfGeneratorAgent` to persist both HTML and PDF before the delivery notification is sent.

This service is separate from `RichContentService` — it handles document storage only, with no
rendering or platform-specific upload logic.

### 10.5 Comparison: DOCX vs PDF Pipeline

| Concern | DOCX pipeline | PDF pipeline |
|---------|--------------|-------------|
| Phases | Two tasks (planner → generator) | Single task (one LLM call) |
| LLM output | Node.js script (docx npm API calls) | Complete HTML+CSS document |
| Renderer | Node.js process runs `docx` library | Puppeteer (headless Chromium) |
| Node.js project | `docx_generator/` (`docx` npm) | `pdf_generator/` (`puppeteer` npm) |
| Port | `DocxRunnerPort` | `PuppeteerRunnerPort` |
| Adapter | `NodeDocxRunner` | `NodePuppeteerRunner` |
| GCS storage | Not used (bytes returned inline) | `DocumentDeliveryService` (both HTML + PDF) |
| DeliveryItem type | `"file_upload"` | `"document"` (two items: HTML + PDF) |
| Filename source | JSON spec `filename` field | `<title>` tag extracted from HTML |
| Tier | PERFORMANCE | PERFORMANCE |
| Orchestrator visibility | `create_document` exposed to LLMs | `create_pdf` exposed to LLMs |

---

## 11. HTML Page Pipeline

### 11.1 Architecture Overview

```
User Request (natural language page description)
     │
     ▼
HtmlPageGeneratorAgent  [ASYNC Cloud Task, PERFORMANCE tier, Gemini]
     │  1. build_for_agent(account_id, "html_page", user_id)  → system_prompt
     │  2. Single LLM call  →  HTML+CSS+JS raw text response
     │  3. _strip_markdown_fences(html)
     │  4. _extract_filename_from_html(html)  →  (base_filename, display_name)
     │
     ▼
DeliveryItem("document")
     content_b64: HTML bytes
     content_type: text/html; charset=utf-8
     file_upload: False
     │
     ▼
AgentWorkerHandler._deliver_document_result()
     │
     ▼
DocumentDeliveryService  →  GCS public URL  (key: docs/{uuid}-{filename}.html)
     │
     ▼
UserNotificationService  →  Slack link message
```

### 11.2 HtmlPageGeneratorAgent

**File:** `src/agents/html_page_generator_agent.py`
**Intent:** `Intent.CREATE_HTML_PAGE`
**ExecutionMode:** `ASYNC` (always runs as Cloud Task)
**Registration:** `internal=False` — exposed to LLM tool selection (Quick, Smart)
**Tier:** PERFORMANCE (Gemini, `agent_type="html_page"`)

Single LLM call → complete HTML+CSS+JS document as raw text response. No Node.js subprocess —
HTML is the final artifact. The system prompt (assembled via PromptBuilder, profile `html_page`,
blueprint `html_page_agent_v1`) instructs the LLM to produce a production-grade, mobile-responsive
single-page layout targeting the visual quality of Stripe, Apple, Linear, or Vercel pages, with
self-contained CSS and vanilla JS.

**Output processing:**
1. `_strip_markdown_fences(html)` — removes accidental ` ```html ``` ` wrapping.
2. Validate non-empty HTML → `AgentResponse.failure` if empty.
3. `_extract_filename_from_html(html)` — extracts `<title>` text: display name + sanitized slug.
   Fallback to `("page", "Page")` when `<title>` absent or empty.

**Delivery item returned on success:**

1. HTML source — `content_type="text/html; charset=utf-8"`, `file_upload=False` → stored to GCS,
   public URL sent as a link in Slack. No binary render step, no Slack file upload.

**Prompt:** PromptBuilder profile `html_page`, blueprint `html_page_agent_v1`.
Token: `COGNITIVE_PROCESS_HTML_PAGE` (category: `cognitive_process`, `non_overridable=true`).

Design constraints enforced by prompt (`COGNITIVE_PROCESS_HTML_PAGE`):
- All CSS in a single `<style>` block; all JS in a single `<script>` block before `</body>`.
- No external resources except one Google Fonts `<link>` in `<head>`. Additional CDN libraries
  allowed only when genuinely required: Chart.js (data charts), Leaflet (maps), Alpine.js (UI state).
- Open Graph tags (`og:title`, `og:description`, `og:type`) required in `<head>` for Slack unfurl
  previews. `og:image` explicitly excluded — no image source available.
- Mobile-first: base styles target mobile, desktop in `@media (min-width: 768px)`. `width: 100vw`
  prohibited — causes iOS viewport overflow; `width: 100%` required instead.
- `IntersectionObserver` scroll-reveal animations + `@media (prefers-reduced-motion)` fallback.
- Page type routing: landing page, portfolio, dashboard preview, documentation, product showcase —
  each with a prescribed section set.

**Prompt philosophy (as of 2026-03-16):** Prescriptive design rules (typography scale,
color system variable names, layout pixel values, visual richness details) were intentionally
removed. The LLM produces higher-quality, more contextually appropriate designs with freedom
constrained only by technical contracts and mobile correctness. See § 11.4 for ADR.

**Agent configuration** (from `agent_config.py`):

| Parameter | Value |
|-----------|-------|
| `temperature` | `1.0` (high creativity for layout and design decisions) |
| `max_tokens` | `64_000` (full HTML+CSS+JS document) |
| `timeout_ms` | `600_000` (10 min) |

### 11.4 Architecture & Design Decisions (ADR, 2026-03-16)

#### ADR-1: Prompt minimalism — remove prescriptive design rules

**Decision:** Removed all aesthetic micro-constraints from `COGNITIVE_PROCESS_HTML_PAGE`:
typography scale (specific `clamp()` values, line heights, font weight counts), color system
variable name requirements, layout pixel values (1200px max-width, 80px section padding),
visual richness rules (border-radius ranges, box-shadow requirements, hover transform values).

**Rationale:** Prescriptive constraints caused two categories of problems:
1. **Layout bugs:** `max-width: 65ch` applied to `p`/`ul`/`ol` but not `h2`/`h3` created
   visual inconsistency (headings full-width, text constrained). Root cause: LLM interprets
   element-level rules literally, not holistically.
2. **Design regression:** Specific values constrain the LLM to one aesthetic. Removing them
   allows the model to select typography, spacing, and color appropriate to each content type.

**Retained:** Technical output contract (DOCTYPE, meta tags, single style/script block),
quality bar (`Select_Style` — Stripe/Apple/Linear/Vercel level), mobile-first structural rules,
animation pattern, section catalogue by page type.

**Result:** Immediate quality improvement observed — dark theme, multi-font pairing,
terminal blocks, contextual layout decisions — none of which the prescriptive prompt allowed.

---

#### ADR-2: OG tags in prompt, not in adapter

**Decision:** `og:title`, `og:description`, `og:type` added to `Output_Contract` as required
`<head>` elements. `og:image` explicitly excluded.

**Rationale:** Slack unfurl bots (not search engine crawlers) fetch pages to display previews.
They ignore `robots` meta tags — `noindex` and OG tags are fully compatible. The LLM generates
OG tags from content it has just written, making `og:description` semantically accurate.
`og:image` excluded because no image source is available; its absence does not degrade Slack
previews.

**Alternative considered:** Injecting OG tags in `GcsMediaAdapter` from `<title>` — rejected
because adapter would produce empty `og:description` and would need to parse HTML it shouldn't
own.

---

#### ADR-3: `noindex` injected in adapter, not in prompt

**Decision:** `GcsMediaAdapter._inject_noindex()` injects `<meta name="robots" content="noindex,
nofollow">` into `<head>` for all `text/html` uploads. Applied before `blob.upload_from_string`.

**Rationale:** Guaranteed injection regardless of LLM compliance. Prompt-level instructions
for technical meta tags are sometimes omitted. The adapter is the correct enforcement point
for infrastructure-level concerns (storage, indexing policy) that have nothing to do with
content generation. Covers both `DocumentDeliveryService` and `RichContentService` upload paths.

---

#### ADR-4: GCS bucket IAM — anonymous list access removed

**Decision:** `alek-media-dev` bucket IAM changed from `allUsers: roles/storage.objectViewer`
to `allUsers: roles/storage.legacyObjectReader`.

**Rationale:** `objectViewer` grants `storage.objects.list` — any party knowing the bucket name
could enumerate all objects and filenames. `legacyObjectReader` grants only `storage.objects.get`
— direct URLs remain publicly accessible (required for Slack link delivery) but enumeration is
blocked. Verified: anonymous `gsutil ls` → `401`; direct URL → `200`.

**Search engine indexing risk:** Low. Googlebot requires an external link to discover pages;
Slack channels are not indexed. `noindex` injection (ADR-3) provides defense in depth.
No signed URLs used — files have no expiry. Acceptable for dev bucket given UUID-based paths.

---

### 11.3 Comparison: DOCX vs PDF vs HTML Page

| Concern | DOCX pipeline | PDF pipeline | HTML page pipeline |
|---------|--------------|-------------|-------------------|
| Phases | Two tasks (planner → generator) | Single task | Single task |
| LLM output | Node.js script (docx npm API) | Complete HTML+CSS | Complete HTML+CSS+JS |
| Renderer | Node.js `docx` library | Puppeteer (headless Chromium) | None — HTML is final |
| Node.js project | `docx_generator/` | `pdf_generator/` | None |
| Port | `DocxRunnerPort` | `PuppeteerRunnerPort` | None |
| Adapter | `NodeDocxRunner` | `NodePuppeteerRunner` | None |
| GCS storage | Not used (bytes inline) | `DocumentDeliveryService` (HTML + PDF) | `DocumentDeliveryService` (HTML only) |
| DeliveryItem type | `"file_upload"` | `"document"` (two items) | `"document"` (one item) |
| Filename source | JSON spec `filename` field | `<title>` tag | `<title>` tag |
| Tier | PERFORMANCE | PERFORMANCE | PERFORMANCE |
| Slack delivery | File upload | Link + file upload (PDF) | Link only |

---

## 12. Code References

**DOCX pipeline:**
- `src/agents/doc_planner_agent.py` — Phase 1: planning, raw forward to generator, fire-and-forget
- `src/agents/doc_generator_agent.py` — Phase 2: tool-calling loop, `_make_filename`
- `src/ports/docx_runner_port.py` — `DocxRunnerPort` ABC + `DocxRunnerError`
- `src/adapters/node_docx_runner.py` — `NodeDocxRunner`: temp file, subprocess, timeout
- `docx_generator/` — project root directory; `node_modules/docx` installed here

**PDF pipeline:**
- `src/agents/pdf_generator_agent.py` — single LLM call, `_strip_markdown_fences`, Puppeteer rendering, `_extract_filename_from_html`, two DeliveryItems
- `src/ports/puppeteer_runner_port.py` — `PuppeteerRunnerPort` ABC + `PuppeteerRunnerError`
- `src/adapters/node_puppeteer_runner.py` — `NodePuppeteerRunner`: stdin HTML → stdout PDF bytes
- `src/services/document_delivery_service.py` — GCS storage via `MediaStoragePort` (key: `docs/{uuid}-{filename}`)
- `pdf_generator/runner.js` — Puppeteer wrapper: stdin → headless Chrome → stdout PDF bytes
- `pdf_generator/package.json` — `puppeteer ^24.x`

**HTML page pipeline:**
- `src/agents/html_page_generator_agent.py` — single LLM call, `_strip_markdown_fences`, `_extract_filename_from_html`, one DeliveryItem (HTML, GCS link)
- `src/services/document_delivery_service.py` — same service as PDF; stores HTML bytes to GCS
- `firestore_utils/uploads/COGNITIVE_PROCESS_HTML_PAGE.groovy` — system prompt source (design rules, mobile-first, animations, section catalogue)
- `firestore_utils/uploads/COGNITIVE_PROCESS_HTML_PAGE.json` — Firestore token upload file
- `firestore_utils/uploads/html_page_agent_v1.json` — Blueprint (`html_page_agent_v1`, class_order: [cognitive_process])
- `firestore_utils/uploads/html_page.json` — Agent profile (`agent_id: "html_page"`)
- `tests/unit/agents/test_html_page_generator_agent.py` — 44 tests (can_handle, execute paths, filename extraction, fence stripping, failure paths, LLM call params)

**Shared infrastructure:**
- `src/infrastructure/agent_manifest.py` — `Intent.CREATE_DOCUMENT`, `Intent.GENERATE_DOCX_CODE`, `Intent.CREATE_PDF`, `Intent.CREATE_HTML_PAGE` and corresponding descriptors
- `src/infrastructure/agent_config.py` — `DocPlannerAgentConfig`, `DocGeneratorAgentConfig`, `PdfGeneratorAgentConfig`, `HtmlPageGeneratorAgentConfig`
- `src/infrastructure/agent_coordinator.py` — `_execute_async` receives `deadline_seconds` from descriptor
- `src/services/agent_context_builder.py` — `"doc_planner"`, `"doc_generator"`, `"pdf_generator"`, `"html_page"` strategy entries
- `src/composition/user_agent_factory.py` — DOCX agents (planner + generator), PDF generator, and HTML page generator registered and cached per user
- `src/handlers/agent_worker_handler.py` — `_deliver_docx_result()` (DOCX intents), `_deliver_document_result()` (PDF + HTML page), `_notify_docx_failure()`, handles `CREATE_DOCUMENT`, `GENERATE_DOCX_CODE`, `CREATE_PDF`, `CREATE_HTML_PAGE`
- `src/services/user_notification_service.py` — `notify_file_bytes()`, channel ID resolution via `send_message`
- `src/adapters/slack/response_channel.py:155-160` — `send_message` normalises `U...` → `D...`
- `src/adapters/slack/media_adapter.py` — `SlackMediaAdapter.upload_file()`
- `tests/unit/agents/test_doc_planner_agent.py` — 21 tests (can_handle, fire-and-forget delegation, failure paths, LLM call params)
- `tests/unit/agents/test_doc_generator_agent.py` — 19 tests (port mock, tool loop, failure paths, raw query forwarding)
- `tests/unit/adapters/test_slack_media_adapter.py` — upload_file contract tests

---

## 13. Status

**Status:** ✅ Production Ready (DOCX, PDF) | ✅ Production Ready (HTML page)
**Last Updated:** 2026-03-16

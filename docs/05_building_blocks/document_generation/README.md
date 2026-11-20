<!-- living-doc
covers:
  src/agents/doc_generator_agent.py
  src/agents/doc_planner_agent.py
  src/agents/html_page_generator_agent.py
  src/agents/pdf_generator_agent.py
  src/ports/docx_runner_port.py
  src/ports/puppeteer_runner_port.py
  src/ports/image_search_port.py
  src/adapters/unsplash_adapter.py
  src/adapters/sandbox_client/sandbox_docx_runner.py
  src/adapters/sandbox_client/sandbox_puppeteer_runner.py
  src/services/document_delivery_service.py
  src/domain/docx_script_prelude.py
-->
# Document Generation

## Purpose

This block turns a chat request into a finished document, in three forms: a Word file (DOCX), a PDF, and a single-page
HTML page. The model writes the document's source (a Node.js script for DOCX, an HTML document for PDF and page), code
renders it, and the result goes back to the channel the request came from. All three run as background Cloud Tasks:
the orchestrator (Smart or Quick) delegates, the user is not kept waiting, and the file or link arrives later.

What it is not: it does not render anything itself (rendering is the render sandbox, see
[`sandbox_render_service.md`](../../04_solution_strategy/decisions/sandbox_render_service.md)), it does not own file
storage or link minting (see [file_storage](../file_storage/README.md)), and it does not write memory.

| Form | Intent (`Intent.*` in `src/infrastructure/agent_manifest.py`) | Agents | Output |
|---|---|---|---|
| DOCX | `CREATE_DOCUMENT`, then `GENERATE_DOCX_CODE` (internal) | `DocPlannerAgent`, `DocGeneratorAgent` | file uploaded to the chat |
| PDF | `CREATE_PDF` | `PdfGeneratorAgent` | HTML and PDF as capability links, PDF also uploaded |
| HTML page | `CREATE_HTML_PAGE` | `HtmlPageGeneratorAgent` | capability link |

DOCX has two agents so that each LLM call has one job: the planner decides what the document contains (JSON layout
spec), the generator decides how to build it (a script, repaired in a loop against real execution errors). PDF and page
are one LLM call each, because their source is the document itself.

## Owned data

Nothing in Firestore. Delivered files live in the private media bucket under `docs/{user_id}/{uuid}-{filename}` (or
`email_review/...`), written by `DocumentDeliveryService.store`; the bucket, capability tokens and link lifetime belong
to [file_storage](../file_storage/README.md) (private storage and capability access). Prompts are Firestore prompt tokens, blueprints and profiles
(`agent_type` `doc_planner`, `doc_generator`, `pdf_generator`, `html_page`; sources in `prompts_snapshot/`); there is no
inline fallback prompt: a `PromptBuilder` failure fails the task.

## Flows

### Dispatch (all forms)

1. The orchestrator calls `AgentCoordinator.handle_delegation` (`src/infrastructure/agent_coordinator.py`). All four
   descriptors are `ExecutionMode.ASYNC`; `_execute_async` enqueues one Cloud Task with the descriptor's
   `dispatch_deadline_s` and returns an acknowledgement to the orchestrator (the user-visible wording is the
   orchestrator's). `handle_delegation` prepends a `[Day DD Mon YYYY, HH:MM UTC]` stamp to the query.
2. `/worker` runs `AgentWorkerHandler.handle_task` (`src/handlers/agent_worker_handler.py`). It first calls
   `AgentCoordinator.prefetch_file_ref`, which turns `context.file_ref` into `file_content` (or `file_note` when the file
   cannot be read), then routes an `AgentIntent.DELEGATE` message to `{agent_id}_{user_id}`.
3. On success the handler delivers by intent; on a failed status it calls `_notify_docx_failure` (a `DOCUMENT_DELIVERY`
   system alert carrying the error text). The same failure path serves the image intents.

The three generators accept `file_content` as source text and fail with `file_note` as the user-visible reason when the
prefetch could not read the file; the note is never used as document text. Text only: a binary source arrives as
converted text.

### DOCX

1. `DocPlannerAgent.execute`: one LLM call with `response_schema` `{status, task_summary, doc_spec}` (enforced on Gemini;
   Claude relies on the output-format token). Extra string fields of the payload are appended to the query.
2. The planner forwards the raw text unparsed through `handle_delegation(GENERATE_DOCX_CODE)` (a second Cloud Task, fire
   and forget) and returns success at once. A fence or prose around the JSON is the generator's problem by design: the
   planner never retries over formatting.
3. `DocGeneratorAgent.execute` strips the delegation timestamp (`domain/delegation_timestamp.py::strip_delegation_timestamp`;
   it would break `JSON.parse` in the script), puts the spec in the system prompt as a `document_spec { ... }` block, and
   loops up to `MAX_TURNS` with one tool, `generate_docx(js_code)`. The script reads the spec from stdin and writes DOCX
   bytes to stdout.
4. `DocxRunnerPort.run` executes it (`SandboxDocxRunner` → sandbox `render.docx`). A `DocxRunnerError` (script error,
   timeout, empty output, `sandbox_lost`) goes back to the model as the tool result and the model repairs the script.
   `SandboxUnavailable` / `SandboxMisconfigured` do not: they are systemic, so the agent fails at once with a "try again
   later" error (unless a DOCX was already produced in this run, which is kept). Tests:
   `tests/unit/agents/test_doc_generator_sandbox_failures.py`.
5. On the first successful run the agent returns a `file_upload` delivery item (`file_bytes_b64`, `filename`, `title`).
   The filename is `_make_filename`: document type from the spec plus the date; it falls back to a generic name when the
   spec is not parseable JSON.
6. `AgentWorkerHandler._deliver_docx_result` decodes it and calls `UserNotificationService.notify_file_bytes`.

### PDF

`PdfGeneratorAgent.execute`: one LLM call returns a full HTML+CSS document (the system prompt embeds the design-language
catalogue and the print/CSS rules); `_strip_markdown_fences`; `PuppeteerRunnerPort.run` renders it (`SandboxPuppeteerRunner`
→ sandbox `render.pdf`; any failure, systemic or not, is a `PuppeteerRunnerError` and fails the task: there is no repair
loop). The filename and label come from the HTML `<title>` (`_extract_filename_from_html`). Two `document` items return:
the HTML (`file_upload` false) and the PDF (`file_upload` true).

### HTML page

`HtmlPageGeneratorAgent.execute`: one LLM call, no renderer. The user request is placed first and the prompt text last,
with the prompt-cache boundary and everything after it cut off. The prompt has the model state a design brief before the
document; `_extract_html_document` drops everything before the first `<!DOCTYPE html`, then fences are stripped. If an
`ImageSearchPort` is present, `_resolve_unsplash_placeholders` replaces the `source.unsplash.com` URLs the model wrote
with real photos (one search per distinct URL, in parallel, sized from the `WxH` in the path), and adds credit links
before `</body>`. One `document` item returns; its `storage_class` is taken from the run context (the daily email review
sets `email_review`, which picks the prefix, TTL and gating).

### Delivery of `document` items

`AgentWorkerHandler._deliver_document_result`, per item: `DocumentDeliveryService.store` (private object plus capability
link `/f/<token>`, `DeliveredDocument.key` for history), optional short link (`ShortLinkService`),
`UserNotificationService.notify_document_link`, and when `file_upload` is set also `notify_file_bytes`. An item that
fails is logged and skipped, so the others still go.

### Why `notify_file_bytes` posts a placeholder first (Slack)

The stored Slack channel may be a user id (`U...`); `chat.postMessage` accepts that, `files.completeUploadExternal` does
not. So `notify_file_bytes` first sends a one-emoji message through the response channel, whose
`SlackResponseChannel.send_message` replaces `channel_id` with the real DM id from the reply, and uploads to that id.
Other platforms use the stored id as it is; a platform with no media port returns False before posting anything.

## Invariants and traps

- **The planner never parses its output, and the generator receives the spec with no timestamp.** The delegation stamp
  is added to every delegated query, JSON included; dropping `strip_delegation_timestamp` breaks every DOCX silently.
- **Systemic render failures are not repair material.** `DocGeneratorAgent` must not feed `SandboxUnavailable` or
  `SandboxMisconfigured` to the model (the loop would burn turns on an outage). Enforced by
  `tests/unit/agents/test_doc_generator_sandbox_failures.py`.
- **Degraded mode.** With `SANDBOX_RENDER_URL` empty, `composition/sandbox_client_setup.py` logs an ERROR once and wires
  runners that always raise the "unavailable" error: DOCX and PDF fail, the HTML page still works. The variable is an
  optional knob read with `getenv`, not through `load_settings()`.
- **Model scripts are untrusted.** `SECURITY_PRELUDE` and `SAFE_ENV_KEYS` (`domain/docx_script_prelude.py`) are applied
  by the sandbox (`adapters/sandbox_engine/render_docx_child.py`): blocked modules (network, process, filesystem) and a
  reduced environment. Tested in `tests/unit/domain/test_docx_script_prelude.py`. The two Node projects (`docx_generator/`,
  `pdf_generator/render_html.js`) exist in the sandbox image only (`Dockerfile.sandbox`).
- **A script error is the model's to fix; a lost sandbox counts against it too.** `SandboxDocxRunner` maps `sandbox_lost`
  and an engine `bad_request` to `DocxRunnerError`.
- **Timeouts.** Each agent's `timeout_ms`, `request_timeout_s` and `node_timeout_s` are in `infrastructure/agent_config.py`
  (`DOC_PLANNER`, `DOC_GENERATOR`, `PDF_GENERATOR`, `HTML_PAGE_GENERATOR`); the descriptor's `dispatch_deadline_s` must
  stay above the agent timeout. The DOCX generator is a multi-turn loop and has no per-call request timeout.
- **No in-process retry.** All four agents set `RETRY_POLICY = NO_RETRY_POLICY`: a retry would repeat the whole
  generation; retry belongs to the Cloud Task.
- **Provider and tier per agent** are in `AgentProviderStrategy` (`services/agent_context_builder.py`) and
  `_DEFAULT_AGENT_TIERS` (`domain/user.py`), not here; the HTML page default provider is an owner decision
  ([`html_page_grok_default.md`](../../04_solution_strategy/decisions/html_page_grok_default.md)).
- **Headless Chromium ignores CSS Paged Media margin boxes** (`@top-left`, `@bottom-center`) and a forced `break-before`
  yields blank pages; the PDF prompt token encodes these rules. Prompt text ships by token upload, not by code deploy.
- **Unsplash is best effort.** `UnsplashAdapter.search` returns `[]` on any failure and a failed placeholder is left as the
  model wrote it; the page always ships. Placeholders from `images.unsplash.com` are not matched by the regex and get no
  credit. `UNSPLASH_ACCESS_KEY` is read in `composition/user_agent_factory.py`; without it the agent gets no image port.
- **`noindex` is injected by the storage adapter, not the prompt** (`adapters/gcs_media_adapter.py::_inject_noindex`, for
  every `text/html` upload), so it holds whatever the model wrote.
- **The delivered object is private.** Reachability is the capability link only; a bare storage URL is 403.
- **A silent delivery failure is possible.** Upload errors in `notify_file_bytes` and per-item errors in the handler are
  logged and swallowed; the user sees nothing. Only an agent failure produces the failure notice.

## Links

- [`DOCUMENT_DELIVERY_RFC.md`](../../10_rfcs/DOCUMENT_DELIVERY_RFC.md), [`DELIVERY_ITEMS_RFC.md`](../../10_rfcs/DELIVERY_ITEMS_RFC.md)
- [`sandbox_render_service.md`](../../04_solution_strategy/decisions/sandbox_render_service.md),
  [`worker_oidc_and_docx_sandbox.md`](../../04_solution_strategy/decisions/worker_oidc_and_docx_sandbox.md)
- [`private_file_storage_token_redirect.md`](../../04_solution_strategy/decisions/private_file_storage_token_redirect.md),
  [file_storage](../file_storage/README.md)
- [`html_page_grok_default.md`](../../04_solution_strategy/decisions/html_page_grok_default.md),
  [`html_page_fallback_context_wiring.md`](../../04_solution_strategy/decisions/html_page_fallback_context_wiring.md),
  [`html_page_subject_grounded_design.md`](../../04_solution_strategy/decisions/html_page_subject_grounded_design.md)

## Where to start reading

- `src/agents/doc_planner_agent.py::DocPlannerAgent.execute` and `src/agents/doc_generator_agent.py::DocGeneratorAgent.execute` (DOCX)
- `src/agents/pdf_generator_agent.py::PdfGeneratorAgent.execute`
- `src/agents/html_page_generator_agent.py::_resolve_unsplash_placeholders`
- `src/handlers/agent_worker_handler.py::AgentWorkerHandler._deliver_document_result`
- `src/services/user_notification_service.py::UserNotificationService.notify_file_bytes`
- `src/adapters/sandbox_client/sandbox_docx_runner.py::SandboxDocxRunner.run`

<!-- living-doc
covers:
  job_main.py
  src/agents/deep_research_agent.py
  src/agents/claude_deep_research_runner_agent.py
  src/ports/deep_research_port.py
  src/adapters/claude_deep_research_adapter.py
  src/ports/job_runner_port.py
  src/adapters/cloud_run_jobs_adapter.py
  src/services/deep_research_delivery.py
  src/services/deep_research_notice_sender.py
  src/domain/deep_research_notices.py
  scripts/debug/test_claude_deep_research_tools.py
-->
# Deep Research

## Purpose

Deep research is a long, expensive, autonomous investigation: the model searches and reads the web for tens of
minutes (the manifest says 80-160 searches, 5-60 minutes) and writes one cited report. This block starts such a run
from a chat turn, runs it **outside the main service**, and brings the report, or an honest failure notice, back to
the channel the request came from.

It is built around one fact: the work is far longer than a Cloud Run request or a Cloud Task can live. So the chat
side only **starts** a run and answers at once; the run itself is a **Cloud Run Job** that owns the whole loop and
ends by handing the finished text back to the main service as ordinary Cloud Tasks. The reasons the work is a Job
and the routine background work is not are in
[`cloud_tasks_vs_jobs.md`](../../04_solution_strategy/decisions/cloud_tasks_vs_jobs.md).

What it is not: it is not an `LLMPort` agent (it never goes through the provider/tier resolution of chat), it does not
write memory, and it does not render the report itself: the styled HTML page is made by the HTML page generator agent
(see [document_generation](../document_generation/README.md)). Whether Smart should start a run at all (asking what
is missing, confirming the brief) is prompt behaviour, not code: the manifest text for the intent says "only when the
user explicitly requested it and confirmed the brief".

## Owned data

The block has **no Firestore collection of its own**. What it leaves behind:

- **Raw outputs in the private media bucket** under `deep_research/{user_id}/`, one set per run, keyed by one UTC
  timestamp: the markdown (`-report.md`, or `-round1.md` and `-round2.md` when a second pass ran) and `-meta.json`
  (query, model, total tokens, rounds, result length). Written by `deliver_deep_research`. They are the history note's
  `open_file` refs; the bucket lifecycle deletes them (see the drive-copy decision), so they are not an archive.
- **Per-user settings** in `UserBotConfig`: `deep_research_second_pass` (the critic pass, Cabinet
  `/api/user/deep-research`) and `drive_copy_deep_research` (opt-in copy of the HTML report to the user's drive,
  [`deep_research_drive_copy.md`](../../04_solution_strategy/decisions/deep_research_drive_copy.md)). Also the
  per-agent tier (`_DEFAULT_AGENT_TIERS["deep_research"]`) and an optional provider pin in `agent_providers`.
- **Usage**: the job records the run's tokens and cost on the account (`FirestoreQuotaService.record_usage`), so the
  day's budget accounting includes research.
- **Prompt content**: when `DEBUG_PROMPTS` is on, both passes are written to the BigQuery prompt-content table
  (`BigQueryPromptContentAdapter.record_dr_result`), the only place where the first pass survives.

The run context is not stored anywhere: it travels as two environment overrides of one job execution, `JOB_QUERY` and
`JOB_CONTEXT_JSON` (user, account, original query, system prompt, model, job id, session id, second-pass flag, origin
platform).

## Flows

### Start (main service, seconds)

1. Smart delegates the `deep_research` intent (`src/infrastructure/agent_manifest.py::DEEP_RESEARCH_AGENT`; a SYNC
   specialist, so the turn waits only for the kick-off).
2. `src/composition/user_agent_factory.py::UserAgentFactory._build_deep_research` resolves the port through
   `AgentContextBuilder.resolve_async_context` and builds `DeepResearchAgent` with the user's tier and second-pass flag.
   A provider pin outside `AgentProviderStrategy`'s allowed list raises `UnsupportedProviderError`, the agent is not
   built and the request fails; it never falls back to Claude silently.
3. `DeepResearchAgent.execute` builds the system prompt (`PromptBuilder`), appends the language instruction to the brief
   and calls `DeepResearchPort.create_interaction`.
4. `ClaudeDeepResearchAdapter.create_interaction` maps tier to model (`MODEL_TIERS`; `CLAUDE_DEEP_RESEARCH_MODEL` pins
   every tier), mints the job id, and calls `JobRunnerPort.run_job`; `CloudRunJobsAdapter.run_job` posts the Cloud Run
   Jobs v2 `:run` request with the overrides and returns without waiting.
5. The agent answers `{"status": "started", "interaction_id": ...}`. `DelegationEngine` treats a `started` result as a
   deferred delivery, so an empty final text after it is not an empty answer. If the kick-off raises, the agent returns
   a failure to Smart, which tells the user.

The provider registry is wired in `src/composition/application_graph.py::_build_generation_registries`; the adapter is
registered only when the Anthropic key and the project are configured, otherwise the agent is simply absent.

### Run (the Job, `job_main.py::_run_job`)

1. Read the two overrides, build the Anthropic client (read timeout is deliberate: it guards the mid-stream stall bug),
   the task queue and the media store. The job has no Firestore wiring of the app graph: it builds only what it needs.
2. `ClaudeDeepResearchRunnerAgent` runs `_research_loop`: one streamed Claude session with the server-side `web_search`
   and `web_fetch` tools, adaptive thinking and high effort. The API runs the tools; the agent only resumes on
   `pause_turn` (re-sending the partial assistant content and the container id captured from the stream), retries
   `overloaded_error` with backoff, returns partial text on `max_tokens`, and raises on a `refusal` stop.
3. Optional second pass (`_build_critic_query`): a fresh session that gets the first report as **unverified leads** and
   must write a new report as if the notes did not exist. It runs only when both the per-user flag (context
   `second_pass`) and the environment gate `DEEP_RESEARCH_SECOND_PASS` allow it. A failed second pass keeps the first
   result.
4. In this order: capture both passes (`_capture_research_result`), record billing (`_record_billing`), then deliver.
   Billing and capture come first so a failed delivery never leaves expensive work uncounted or unsaved.
5. `deliver_deep_research` (see next flow). Exit code 0 on success; any failure reports and exits 1.

### Delivery (`src/services/deep_research_delivery.py::deliver_deep_research`)

1. Upload the markdown rounds and `-meta.json` (`_upload_round`). In the Job no notification service is passed, so the
   user is **not** sent the raw markdown links; they get only the page below.
2. Enqueue an `html_page_generator_agent` task (`create_html_page`, deep-reasoning complexity) whose context carries the
   origin channel and platform, and `DEEP_RESEARCH_REPORT_CONTEXT_KEY`. The task is named from the job id, so a retried
   create whose first attempt timed out after the task existed is a no-op; the create is retried only when named.
3. The page is built and sent by `AgentWorkerHandler._deliver_document_result`; because of the context key it then
   calls `_copy_deep_research_report`, the opt-in drive copy (best effort, bounded by
   `DEEP_RESEARCH_COPY_TIMEOUT_S`, never fails the delivery).
4. The function returns `False` when the enqueue failed; the Job then treats it as a delivery failure.

### Failure notice

The Job cannot talk to the user, so every failure path calls `job_main.py::_report_failure`, which enqueues the
`deep_research_failed` `/worker` task (reasons `agent_failed`, `empty_result`, `delivery_failed`), named by job id so a
retry never notifies twice. `DeepResearchFailedTask` (registered in `src/composition/worker_setup.py`) picks the text
from `src/domain/deep_research_notices.py` (the delivery failure says the research finished but the report could not
be prepared) and sends it through `DeepResearchNoticeSender.send`, a `notify` of kind `NotificationKind.DEEP_RESEARCH`
to the origin channel. It answers 5xx only when nothing was sent, so Cloud Tasks retries exactly the unsent case, and
it suppresses the agent's own transient retry for the same reason.

## Invariants and traps

- **No retry of paid work.** The runner has `NO_RETRY_POLICY`, the Job is deployed with zero retries, and only
  `overloaded_error` is retried inside the loop. A retry would redo and pay for 10-25 minutes of research.
- **The Job's task timeout is the only ceiling.** `job_main.py` builds the runner with `timeout_ms=None`; the 30-minute
  `ClaudeDeepResearchRunnerConfig.timeout_ms` applies only on a coordinator path. The timeout is set where the Job is
  deployed (`cloudbuild-dev.yaml`); a comment there still says one hour while the flag is five.
- **The `agent_execution` route for the runner is dead code.** `AgentWorkerHandler` still handles the intent
  `execute_deep_research_claude` (links, then the page), and `UserAgentFactory._build_claude_runner` still builds the
  runner, but nothing enqueues that task any more: the adapter starts the Job. Only the Job path is live; the handler
  branch is the one that would send round links.
- **Model gates are duplicated by design.** `ClaudeDeepResearchRunnerAgent._THINKING_MODELS` and
  `_NO_SAMPLING_MODELS` mirror the ones in `ClaudeAdapter` (an agent may not import an adapter, domain may not hold
  model names; the runner is whitelisted in `tests/unit/arch_tech_debt.py`). Change both together. New-generation
  models reject a non-default sampling parameter (none is sent) and get the larger output budget because their
  tokenizer is longer. A model matching neither set falls to a fixed-budget thinking config.
- **Billing prices input and output separately** (`_record_billing`); collapsing them bills a report-sized output at the
  input rate.
- **A failed prompt build does not stop the run.** `DeepResearchAgent.execute` logs a warning and proceeds with no
  system prompt, unlike the no-fallback-prompt rule of chat agents.
- **The Job needs the worker identity.** The delivery task and the failure task go to `/worker`, which checks a Google
  OIDC token; `_build_task_queue` requires `SERVICE_ACCOUNT_EMAIL`, else the report is never delivered.
- **The report returns to the channel it was asked in**: derived from the per-channel `session_id` plus
  `origin_platform` (`tests/unit/test_deep_research_origin_platform.py`).
- **Notification budget.** `NotificationKind.DEEP_RESEARCH` has its own SLA in `src/infrastructure/notification_sla.py`;
  it covers only the short notice, not the research
  ([`notification_sla_per_tier.md`](../../04_solution_strategy/decisions/notification_sla_per_tier.md)).
- **One provider.** Claude is the only registered deep-research backend; `allowed_providers` lists only it. The Gemini
  and OpenAI backends are removed, and `DEEP_RESEARCH_RFC.md` sections on them are history.
- `DeepResearchPort.get_status` exists on the port but is unused: delivery is pushed by the Job, never polled.
- `scripts/debug/test_claude_deep_research_tools.py` is a manual smoke check of the native tool definitions against the
  live API; it is not part of any suite.

## Links

- [`DEEP_RESEARCH_RFC.md`](../../10_rfcs/DEEP_RESEARCH_RFC.md): origin of the two-phase idea (prepare and confirm, then
  run); its Gemini polling and OpenAI webhook designs no longer exist.
- [`cloud_tasks_vs_jobs.md`](../../04_solution_strategy/decisions/cloud_tasks_vs_jobs.md): why a Job.
- [`gemini_deep_research_adapter_removal.md`](../../04_solution_strategy/decisions/gemini_deep_research_adapter_removal.md)
  and
  [`openai_deep_research_adapter_deferred.md`](../../04_solution_strategy/decisions/openai_deep_research_adapter_deferred.md):
  how the other backends went (the second one is also gone from the code).
- [`claude_ultra_tier_to_opus_4_8_plus_dr_gate_unification.md`](../../04_solution_strategy/decisions/claude_ultra_tier_to_opus_4_8_plus_dr_gate_unification.md):
  why the thinking gate is a substring match; the model it names for ULTRA is superseded by `MODEL_TIERS`.
- [`deep_research_drive_copy.md`](../../04_solution_strategy/decisions/deep_research_drive_copy.md) and
  [`notification_sla_per_tier.md`](../../04_solution_strategy/decisions/notification_sla_per_tier.md).
- Deploy units: [`docs/07_deployment/README.md`](../../07_deployment/README.md). Notification routine:
  [rich_content_protocol](../rich_content_protocol/README.md).

## Where to start reading

- `src/agents/deep_research_agent.py::DeepResearchAgent.execute` (the kick-off)
- `src/adapters/claude_deep_research_adapter.py::ClaudeDeepResearchAdapter.create_interaction` (tier to model, job start)
- `job_main.py::_run_job` (the run, in order)
- `src/agents/claude_deep_research_runner_agent.py::ClaudeDeepResearchRunnerAgent._research_loop`
- `src/services/deep_research_delivery.py::deliver_deep_research`
- `src/services/deep_research_failed_task.py::DeepResearchFailedTask` (the failure path, with `DeepResearchNoticeSender`)

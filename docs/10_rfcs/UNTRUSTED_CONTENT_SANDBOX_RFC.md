# RFC: Untrusted-content sandbox service — one place where hostile bytes are processed, holding nothing

**Status:** Proposed (rev 3; the owner resolved every open question on 2026-10-20, §5 and §10; S1 built on `feat/sandbox-s1`, behind `SANDBOX_RENDER`, not deployed and not accepted; see `decisions/sandbox_render_service.md`). *O* items are owner decisions, *P* items are author proposals the owner did not change.
**Date:** 2026-10-20
**Owner decisions (2026-10-19, "we need a proper sandbox with a limited scope"):**
- **O1. One separate Cloud Run deploy unit** whose own service account holds **no IAM role** and which is given **no secret**.
- **O2. It executes every piece of untrusted processing** the product does: document parsing for the drive, conversion of
  chat uploads, media decoding for transcripts, and the runners of model-written code and model-written HTML (§1).
- **O3. The drive parser stays a reasoned residual until it moves** (`decisions/drive_parser_sandbox_resource_isolation.md`
  names this service as its trigger's answer); this RFC is that answer, widened to the other consumers.
- **O4. Transcripts for a second user wait for this service** (USER_DRIVE_INDEXING_RFC §27.12, OQ-T3).

**Prerequisite (P0, O8):** the per-runtime service-account work (each runtime on its own identity with only the roles it needs) is a stated
prerequisite: drive ops are enabled only after the indexer has its own identity; chat and render ops only after the main service has its own. See §4.4.

Related: `USER_DRIVE_INDEXING_RFC.md` §9, §24.3, §24.14, §27.4; `decisions/drive_parser_sandbox_resource_isolation.md`;
`decisions/worker_oidc_and_docx_sandbox.md`; `LONG_RUNNING_TURNS_RFC.md` (style, not dependency).

## 0. In one paragraph

Everything the product does with bytes it did not write (a file on a drive, a file sent in chat, a recording, a page of
HTML, a script a model wrote) runs today inside a runtime that also holds credentials: the main service, the drive
indexer. Each has a local guard (child process, resource limits, an unprivileged user, a module hook, a request filter),
and each guard bounds **resources** or **privileges inside one container**. This RFC moves that work behind a **service
boundary**: a request carries bytes in and gets text, structured data or bytes out. The service has no credentials, sees one
request at a time, identifies no one, and exits after use. The callers keep their existing ports; each gains an HTTP
adapter next to the in-process one. The in-process adapters stay as the service's own engine and as the local-development
and test implementation. The work is cut into one spike and five slices plus a close-out, one caller family at a time, each with a switch
back. **Design rule that applies to every op:** the engine (parser, markitdown, ffmpeg, the Node DOCX script, Chromium) always runs in a **fresh child
process under an unprivileged uid**; the server process only moves the payload to a file and spawns the child (§4.6).

## 1. What is in scope, and where it runs today

| # | Consumer | Input it receives | Runs today | Port today |
|---|----------|-------------------|------------|------------|
| C1 | Drive document parsing (PDF, DOCX, PPTX, XLSX/XLS, ODF, EPUB, HTML, RTF, MSG, EML, text) | a file from a user's drive | indexer, child process under an unprivileged user with rlimits (`src/adapters/subprocess_content_extractor.py:113`, `:154`, `:223`; `src/adapters/drive_parser/child.py`, `readers.py`) | `ContentExtractorPort.extract` (`src/ports/content_extractor_port.py`); the PDF page ops (`pdf_page_count`, `cut_pdf_window`) are **not** on the port: they reach the reader through `PdfSplitter` (`src/composition/drive_reader_runner.py:73-78`) |
| C2 | Chat-upload conversion; on-demand reading of a drive file in chat | a file attached in Slack/Telegram; a drive file opened by `open_file` | main service, **in process**, a thread (`src/utils/file_conversion.py:207`; five call sites in three modules: `src/services/file_conversion_service.py:137`, `:178` and `:294`, `src/services/email_search_service.py:244`, `src/handlers/conversation_handler.py:920`) | none: a free function |
| C3 | Audio/video decoding for transcripts (`ffmpeg`, `ffprobe`) | a recording on a drive (§27.4: a short-lived download URL) | not built (planned in the indexer's parser child); `ffprobe` on raw bytes already runs in the main service (`src/utils/video_probe.py:21`) | `MediaAudioPort` (proposed in §27.4), none for the probe |
| C4 | DOCX generation | a Node script written by a model | main service, Node subprocess with a module hook and a withheld environment (`src/adapters/node_docx_runner.py:25-62`; wired at `src/composition/user_agent_factory.py:730`) | `DocxRunnerPort.run(js_code, spec_json, timeout) -> bytes` (`src/ports/docx_runner_port.py`) |
| C5 | HTML to PDF | HTML written by a model | main service, Node + Puppeteer subprocess, request filter in `pdf_generator/runner.js:18-56` (`src/adapters/node_puppeteer_runner.py`) | `PuppeteerRunnerPort.run(html, timeout) -> bytes` |
| C6 | HTML to PNG (rich-content widgets) | HTML written by a model | main service, one shared headless Chromium (`src/adapters/playwright_html_renderer.py:110-123`; wired `main.py:797-800`; used `src/services/rich_content_service.py:134`) | `HtmlRendererPort.render(html, width) -> bytes` plus `start`/`stop` |

The five converter call sites above are the full list found by grep (the e-mail attachment path is one of them); the markitdown builder
(`src/utils/document_hardening.py:38`) has no other user. HTML cleaning of e-mail bodies with BeautifulSoup in the Gmail adapter
(`src/adapters/gmail_provider_adapter.py`) stays in the main service **by decision** (O10): it is markup text cleaning of mail the user's own provider
delivered, not a decoder of attachments; revisit if its input class changes.

**What stays outside the sandbox, by rule.**
- **LLM API calls.** The readers and triage agents, transcription, the profiler: they need keys and they are the callers' job. The sandbox returns
  bytes (a PDF window, an Opus window); the caller sends them to a model.
- **Every call to a platform API** with the user's credentials (Graph, Slack, Telegram, Gmail) and every store (Firestore, GCS). The caller fetches the
  bytes, the sandbox never fetches on a user's behalf. The one exception is the media op's pre-authenticated URL (§4.5), which carries no credential of ours.
- **Decisions.** Which kind a file is, whether it is admitted, what a status means for the record: caller code (`drive_index_policy`, `outcome_step`).
- **Untrusted-data handling after the fact.** What the sandbox returns is still untrusted text: the nonce wrapper, the `untrusted` protocol line and
  every existing treatment are unchanged. The sandbox contains the *processing*, it does not make the *content* safe.

## 2. Goals and non-goals

**Goals**
- A flaw in any parser, decoder, renderer or interpreter reaches **no credential, no store and no other request's data**.
- One mechanism, one contract, one place to harden, for all of C1-C6.
- Callers change at an adapter and a composition line. No agent, handler or domain rule changes meaning.
- The same code runs in-process in development and tests, behind the same ports.

**Non-goals**
- Defending the platform itself, or a caller against its own bugs. The sandbox is a containment layer, not a proof.
- Content moderation, malware scanning, or prompt-injection defence (separate mechanisms).
- Multi-tenancy of the sandbox's *outputs*. Output goes back to the one caller that sent the request.
- Replacing the in-process guards. They stay as depth (§4.6); the claim moves to the boundary.

## 3. Threat model (neutral)

Untrusted bytes arrive from four places: a drive (any user's), a chat (any whitelisted user's), a recording, and a model
that read any of the above. Their author is outside our control. The sandbox assumes nothing about them except that they
are **well-typed enough to reach a decoder** and that the decoder, its libraries, the interpreter or the browser may
misbehave: spend memory or CPU out of proportion, hang, crash, or, in the worst case, run attacker-chosen code.

What a misbehaving component can reach is exactly what its host holds. The sandbox's job is to make that set **empty**:

| Invariant | Meaning | Enforced by |
|-----------|---------|-------------|
| I1 Holds nothing | no secret in the environment or mounted; the runtime identity has no role on any project resource | deploy has no `--set-secrets`; zero-role identity; startup self-check (§4.3) |
| I2 One request's data | at most one request is processed at a time and no instance serves a second request after an abnormal one | concurrency 1; recycle policy (§4.6) |
| I3 Identifies no one | no user, account, item or file name reaches it, only an opaque correlation id | contract (§4.7); a test pins the field set |
| I4 Bounded | CPU, memory, wall time, input size, output size are capped per op | §4.8 and rlimits inside the container |
| I5 Not a source of truth | callers treat output as untrusted, exactly as before | unchanged downstream treatment |
| I6 Stateless | nothing persists between requests: no cache, no file, no database | recycle; per-request directory removed |
| I7 Engines never share the server's uid | the process that accepts requests is never the process that touches untrusted bytes | child under an unprivileged uid for every op (§4.6) |

Out of scope on purpose: an attacker who already controls a caller, or the cloud platform.

## 4. Design

### 4.1 A service, not a job (P1)

A Cloud Run **service** answering a synchronous request.
- A job takes no request body: input would travel through a bucket or the environment. A bucket needs a role for the job's identity (breaks O1), an
  environment override needs `run.jobs.runWithOverrides` on the caller (the power the research job's launcher already needs, and a heavy one to hand out again).
- A job's start is slower and its result is not returned to the caller; the callers want a result in the call (chat conversion waits for it).
- A service gives the properties we want for free: per-request billing, concurrency 1, an invoker role per caller, and an instance that can end
  itself after a request.

### 4.2 Two services, one repository (O10)

| Service | Ops | Image content | Resources (initial) | Request timeout |
|---------|-----|---------------|---------------------|-----------------|
| **sandbox** (light) | C1, C2, C3 (incl. `ffprobe` of chat videos) | Python, the parser libraries, `ffmpeg`/`ffprobe`. No Node, no browser | 2 GiB, 2 vCPU (ffmpeg used ~1.07 CPU-s per audio minute, §27.2) | 900 s (backstop; the in-app wall is shorter) |
| **sandbox-render** | C4, C5, C6 | Python shell, Node, Puppeteer, Playwright Chromium | 2 GiB, 2 vCPU | 300 s |

Why two: the render image carries ~0.5 GB of browser and runs code a model wrote, the light image carries decoders and runs bytes someone sent;
they differ in size, cold start, and in what a compromise of either would be worth to the other. Why not three: a separate media service buys a
different network policy only (§4.5), which the owner has accepted as is (O7).
Cost of two: one more deploy unit for a solo developer. Both services are part of the plan; the slice order (§7) starts with the render service.

### 4.3 Identity and the "nothing" the service holds

- Each service runs as its **own service account with no project role and no resource binding**. (Not even logging: the logger writes
  structured lines to stdout, which the platform collects, `src/utils/logger.py:85-90`.) Image pull and deploy are done by platform agents and the deployer, not by the runtime identity.
- **No secret:** the Cloud Build step has no `--set-secrets`. A unit test reads `cloudbuild-dev.yaml` and fails if a sandbox step carries one (the pattern of
  `tests/unit/test_cloudbuild_indexer_model_secrets.py`). The environment is a fixed allowlist (`APP_ENV`, `PORT`, limits, `SANDBOX_*` knobs).
- **Startup self-check:** the process refuses to start if its environment holds a name matching `KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL` outside the allowlist, or a
  `GOOGLE_APPLICATION_CREDENTIALS`. Cheap, and it catches a deploy mistake before traffic.
- **Image contents** (build test, S1): the sandbox images copy an explicit allowlist of paths, never the repository root; no environment file or key file is in the image;
  the Firestore, Secret Manager and storage client libraries are not importable inside it (the test imports each and expects failure).
- **The honest claim.** A compromised engine has the reach of **an anonymous internet client plus a Google identity that holds no grants**. The metadata server will
  still mint tokens for that identity; they authenticate a principal that holds nothing. It can call public endpoints, and any endpoint whose policy admits "any
  authenticated Google principal". Two checks keep that reach small (S0, repeated at each deploy): (1) no policy in the project binds `allAuthenticatedUsers`, and `allUsers`
  appears only on the **intended public-invoker set**, written down in §8; (2) a token of the zero-role identity gets 403 on every enabled API that bills or answers on a
  project's behalf (generative, AI, maps and similar). If (2) shows any such API answering, the no-NAT egress of §4.5 moves forward for the non-media ops, because the network is then the remaining limit.
- No outbound credential exists for the sandbox to use; "authentication of the sandbox to anything" is nothing, by construction.

### 4.4 Who may call it

- **Prerequisite on identities (P0, O8).** Caller sets must not overlap and must not include a runtime that merely shares an identity with the rest of the project. Drive ops
  are enabled only after the indexer runs as its own identity; chat and render ops only after the main service does. Until then the invoker binding would admit every
  service on the shared identity. **Startup check:** each sandbox service refuses to start if the caller sets of two op families overlap, or if a caller set is empty on Cloud Run.
- **Platform authentication.** The services are deployed `--no-allow-unauthenticated`. Each caller identity holds `roles/run.invoker` **on the sandbox service
  resource** only. The caller sends its ID token for the sandbox URL in `Authorization: Bearer` (never `X-Serverless-Authorization`),
  fetched from the metadata server as the relay does toward the main service (`relay_main.py:105-116`). Deploy tests pin `--no-allow-unauthenticated`, forbid
  `--no-invoker-iam-check`, and forbid any public-invoker binding on a sandbox service.
- **In-app verification, in v1.** The app **verifies the token signature itself** (reusing `src/web/worker_oidc_verifier.py`, extended to accept a set of caller
  emails per op rather than one: S-Δ9), checks `email_verified`, and **pins the audience** to the service's own URL. The platform check is not the only gate: a mistaken binding does not silently admit a caller.
- **Per-op allowlist.** The verified `email` is mapped to the ops it may call (indexer: `drive.*`, `media.*`; main: `chat.*`, `render.*`). Values live in the environment, not in the repository.
- **Not `--ingress=internal`:** a call to a `run.app` URL counts as internal only through the caller's VPC, which would force VPC egress on the callers. Spike item S0-e.
- **Fail closed** on Cloud Run when the email map is empty (`worker_oidc_gate` precedent, `src/web/indexer_worker_app.py:41-65`); open when `K_SERVICE` is unset.

### 4.5 Network egress (O7)

| Op family | Needs the network? |
|-----------|--------------------|
| `drive.*`, `chat.*`, `render.*` (input travels in the request) | no |
| `media.*` (input is a pre-authenticated download URL, §27.4) | yes, to the storage host named in the URL |

- **v1: the platform's default egress.** For every op except `media.*` the service makes no outbound call, and a test asserts its code opens none.
- **The media op reads through a loopback range proxy inside the sandbox, not through ffmpeg's own network stack.** The URL comes from the drive provider through a trusted caller (not
  from the file), but ffmpeg is a large surface and should not open sockets. A small **Python fetcher** (a helper under its own unprivileged uid, never the engine child) takes the
  URL and serves ranges on `127.0.0.1` only: `https` only; the host must match a suffix allowlist at a **dot boundary** (`.example.com`, never `example.com.evil`); **no IP literals**;
  **no redirects**; DNS resolved **per request** and **never pinned by address** (provider addresses may change; the allowlist is by name suffix only, O7); a size and rate cap. ffmpeg reads only `http://127.0.0.1:<port>/…` with
  `-protocol_whitelist http,tcp`, `-format_whitelist` (the demuxers of §27.4), `-codec_whitelist` (audio decoders only), `-max_alloc`, `-threads 1`, `-fs`, `-nostdin`, `-vn -sn -dn`, `-map 0:a:0`.
  **The URL is redacted from every log line** and never stored; the request log shows host class and byte counts only.
- **Why that is acceptable first:** the service holds no credential and sees one request's data (I1, I2), so egress lets an attacker send out *the document it
  already sent in*. The residual that matters is reaching other services by their public addresses; those are public by design and authenticate by themselves.
- **Hardening, not v1 (S6; earlier for non-media ops if S0-c requires it):** deploy with Direct VPC egress on all traffic, a subnet with no NAT and a deny-all egress rule, so `drive.*`/`chat.*`/`render.*`
  have **no route** out (Google APIs reach through Private Google Access, which they do not use). `media.*` then needs its own service with NAT and a name allowlist (not planned; the owner accepted the media op's egress through the in-sandbox proxy, O7). Effort: about half a day of network setup, no running cost without NAT. Recommended **after** S3 lands, as it changes nothing in the contract.

### 4.6 Every engine in a child under its own uid; one request, one instance

- **Rule (applies to all ops, S1 included).** The server process (it holds the listener and the verified identity) **never parses, decodes, renders or executes
  anything from a payload**. For each request it streams the body to a file in a per-request directory, then spawns a **fresh child** for the engine (parser, markitdown, ffmpeg, the
  Node script, Chromium) under a **separate unprivileged uid**, with no supplementary groups, as the drive parser already does (`subprocess_content_extractor.py:223`, `child.py`).
  Limits applied in the child before the engine loads: `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE`, `RLIMIT_CORE` 0, **`RLIMIT_NPROC`** and **`RLIMIT_NOFILE`**; the wall clock kills the whole
  process group. Different ops use different uids where cheap. The result goes back through a file the child writes, which the
  server reads and **validates as an envelope only** (§4.7); the server never interprets binary output.
- **`concurrency=1`, `max-instances=N`** (§11). One request per container at a time.
- **Recycle (`SANDBOX_RECYCLE`, O5):** `always` (default): the instance serves **one** request and ends; `never` (local only). **`always` applies to every op, chat uploads and PNG widgets included (O5)**; a policy `abnormal` is not offered in production.
  The recycling is done by a **supervisor that never touches untrusted bytes** (a separate process from the one reading payload files): the server finishes the response, then the supervisor
  stops the instance. An instance holding a request does not accept a second one.
- **Draining is visible to the caller.** Before the last response byte the server marks the connection as ending (HTTP/2 `GOAWAY`, or `Connection: close`). A request that reaches an
  instance already draining is answered with a distinct **503 `recycling`**, which callers retry at once **without charging the file** (§4.9). Spike S0-b measures back-to-back bursts and the
  rate of `recycling`. The added 1-3 s per request is accepted by the owner (O5); real latency is measured after deploy and reported.
- **Workdirs** live on a **size-limited in-memory volume** (Cloud Run second generation), removed in `finally` and by the recycle; each engine uid owns only its own directory.
- Cost: a cold start per request, ~1-3 s for the light image (S0-b).

### 4.7 The call contract

One route family, versioned: `POST /v1/{op}`.

- **Request.** The **raw body is the payload** (streamed straight to the workdir file); the metadata travels in one header, `X-Sandbox-Meta` (base64url JSON, ≤ 8 KiB), not in a multipart
  frame the server would have to parse. `meta` always has `request_id` (random,
  generated by the caller per call, the only correlation) and `limits` (the caller's caps, never above the service's). **It carries no user id, account id,
  item id, file name or path** (I3). Where a file extension matters (it selects a reader) the caller sends `kind` and `ext`, not the name.
- **Response.** Header `X-Sandbox-Outcome`: base64url JSON `{status, detail, stats}`, always present. Body: structured ops return UTF-8 JSON, byte ops return the
  raw bytes with `Content-Type` set. A body is never trusted for its status: the header decides.
- **Strict envelope validation.** Callers validate the header with a closed schema (known statuses, bounded sizes, no extra keys) and treat the body as **opaque bytes or schema-checked JSON**. Callers never
  parse sandbox binary output in-process: a returned PDF window goes to the model reader as bytes, a DOCX/PDF/PNG goes to the delivery path as bytes. An architecture test forbids importing a
  document, image or archive library in `src/adapters/sandbox_client/`.
- **Large bodies.** The drive admits files up to 45 MiB (`MAX_DOWNLOAD_BYTES`, `src/domain/drive_index_params.py:55`); Cloud Run's HTTP/1 request limit is 32 MiB. The
  services are deployed with end-to-end HTTP/2 (`--use-http2`; the server is Hypercorn, as elsewhere) and callers use an HTTP/2 client. **This is the main technical
  risk of the design** (S0-a). If bodies above 32 MiB fail, **32 MiB stays a known limitation for parsed files** (O6): there is no fallback in which the service pulls a file
  by URL, and the drive cap is not changed silently; files above the limit get the existing `too_large` outcome and the limit is documented.
- **Ops**

| Op | In | Out | Engine behind it |
|----|----|-----|------------------|
| `drive.extract` | file bytes, `kind`, `ext`, `limits` | JSON `ExtractionOutcome` minus billing fields (status, text, pages_total, truncated, brief, script_rendered, capture_signals) | `SubprocessContentExtractor` |
| `drive.pdf_page_count` | PDF bytes | JSON `{pages_total}` | same |
| `drive.pdf_window` | PDF bytes, first page, count | PDF bytes | same |
| `drive.pdf_windows` | PDF bytes, window plan | the windows, streamed one after another (option: one upload instead of one per window) | same |
| `chat.convert` | file bytes, `ext`, `mime` | JSON `{text}` | the conversion engine of `file_conversion.py` minus the audio branch (§6) |
| `media.probe` | download URL, or bytes (the `video_probe` case) | JSON `{duration_s, has_audio, container}` | a media child |
| `media.audio_windows` | download URL, window plan | Opus bytes, one window per response part or per call | media child, `ffmpeg` per §27.4 |
| `render.docx` | script text, spec JSON, timeout | DOCX bytes; on failure the script's error text | `NodeDocxRunner` |
| `render.pdf` | HTML | PDF bytes | `NodePuppeteerRunner` |
| `render.png` | HTML, width | PNG bytes | a per-request Playwright page |

- A binary result over the cap is `too_large`. Streaming is used for responses over a few MiB.
- Idempotent and pure: every op is a function of its input, so any call may be repeated.

### 4.8 Limits (per op, enforced inside; the caller's `limits` may only lower them)

Input size caps are the callers' existing ones (drive `size_cap(kind)`; chat 5 MiB, `file_conversion.py:30`; media `TRANSCRIBE_MAX_BYTES`/`_SECONDS`, §27.4). Child limits keep their
values (`RLIMIT_AS` 1 GiB, `RLIMIT_CPU` 90 s, wall 120 s, `RLIMIT_FSIZE` 64 MiB) until S1 re-measures them on the new host. Output caps: text 2,000,000 characters
(`MAX_TEXT_CHARS`), binary 32 MiB, DOCX/PDF/PNG as today. The service's own memory (2 GiB) is the outer wall: a child that reaches its rlimit is an outcome, an
instance killed by the platform is a transport failure (§4.9).

Capacity is shared: chat conversion (interactive) and the indexer (background) call the same light service. Chat requests are never queued behind indexing: the indexer is limited to a fraction of
`max-instances` by its own client-side concurrency (`SANDBOX_INDEXER_SLOTS`), and a `recycling` or 429 answer to chat is retried at once.

### 4.9 Failure semantics

The statuses **do not change**. The sandbox returns the ones callers already know; the HTTP adapter turns them back into the same objects.

| Situation | Seen by the caller as | Why |
|-----------|----------------------|-----|
| the parser result | `ExtractionStatus.TEXT/NO_TEXT/UNSUPPORTED/TOO_LARGE/ENCRYPTED/ERROR` as returned | unchanged domain outcome |
| child killed on time or memory | `TIMEOUT` / `MEMORY` as today (counted per-file retries, never routed to the reader) | `drive_parser_sandbox_resource_isolation.md` |
| `BUDGET_EXHAUSTED` | never from the sandbox | it is the reader's status |
| request too large (413) | `TOO_LARGE` | |
| 503 `recycling` | retried immediately (up to 3 times, short jitter), **never charged to the file** | §4.6 |
| 429, other 503, connection refused, an instance not started | new `SandboxUnavailable` (domain exception): **systemic**, work is deferred, nothing is charged to the file | the shape of `DriveModelUnavailable` (`src/domain/drive_extraction.py`), `charges_file=False` |
| 401 / 403 | `SandboxMisconfigured`: ERROR log, systemic defer | a deployment mistake, not a file |
| connection reset or 5xx mid-request, or a timeout | one immediate retry on a fresh instance; if it repeats: `ExtractionStatus.ERROR`, detail `sandbox_lost`, **charged to the file** (counts toward `MAX_ATTEMPTS`) | an instance killed by the platform is indistinguishable from a poison file; two losses mean the file. Same reasoning as `DriveModelUnavailable.charges_file` |
| a malformed envelope | `ERROR` | |

Details for parsers stay what they are: a failure type, a code position and a hash, never text (`failure_label`). The DOCX op is the exception **by purpose**: the
generating agent feeds the script's error back to the model to repair it (`doc_generator_agent.py`), so `render.docx` returns the error text, which is the model's
own script's, not a user's data. Chat: the same `[System: …]` alerts as today (`_conversion_alert`, `_size_alert`, `file_conversion.py:117-146`); `SandboxUnavailable` gets one
new alert in the same contract ("the file converter is temporarily unavailable; ask the user to send it again later"), because the "unsupported format" alert would be false.

### 4.10 Ports and adapters (P8)

No omnibus `ContentSandboxPort`. CLAUDE.md allows a port for a system boundary with two implementations and a test substitute, one per boundary, not for cleanliness. The
boundaries that already have a port get **a second adapter**; the two that do not get a small port:

| Capability | Port | In-process adapter (kept) | New HTTP adapter |
|------------|------|---------------------------|------------------|
| C1 parse | `ContentExtractorPort` (reused; its docstring already names a separate-service adapter, §9) | `SubprocessContentExtractor` | `SandboxContentExtractor` |
| C1 PDF ops | `PdfSplitter` (a Protocol in `drive_reader_runner.py`): promote to `ports/pdf_splitter_port.py` | same class | same HTTP class |
| C2 chat convert | **new** `DocumentConverterPort.convert(path, ext, mime) -> ConversionOutcome` | `LocalDocumentConverter` (today's `convert_file_to_text` body) | `SandboxDocumentConverter` |
| C3 media | `MediaAudioPort` (named in §27.4) and a `probe` method | media child | `SandboxMediaAdapter` |
| C4 | `DocxRunnerPort` (reused; docstring: "(future) Cloud Function HTTP call") | `NodeDocxRunner` | `SandboxDocxRunner` |
| C5 | `PuppeteerRunnerPort` (reused; same note) | `NodePuppeteerRunner` | `SandboxPuppeteerRunner` |
| C6 | `HtmlRendererPort` (reused; `start`/`stop` become no-ops) | `PlaywrightHtmlRenderer` | `SandboxHtmlRenderer` |

All HTTP adapters and their shared client (`SandboxHttpClient`: ID token, HTTP/2, the retry rule of §4.9, `request_id`, timeouts) live in **one subpackage**,
`src/adapters/sandbox_client/`, because adapters may not import across subpackages (REQ-ARCH-23); the client is the only thing they share. The contract (op names, the
envelope, caps, outcome (de)serialization) is pure and lives in `src/domain/sandbox_contract.py`, importable by both sides.

### 4.11 The service's own code

`sandbox_main.py` (entry, like `indexer_main.py`), `src/web/sandbox_app.py` (routes, the caller gate, size and header checks, request id), and a registry built in
`src/composition/sandbox_setup.py` that maps each op to its in-process adapter. A separate `Dockerfile.sandbox` with targets `light` and `render` copies only the modules the
ops need (domain, the engines, the parsers) and carries no credentialed-runtime code (no handlers, no agents, no stores). The service has no database import to leak (image-contents test, §4.3).

### 4.12 Composition and the switch (O9)

Each caller family reads one knob with `os.getenv` at its composition line (optional knob, per CLAUDE.md, not `load_settings()`):

| Knob | Values | Default during the transition |
|------|--------|-------------------------------|
| `SANDBOX_DRIVE` / `SANDBOX_CHAT` / `SANDBOX_MEDIA` / `SANDBOX_RENDER` | `sandbox` \| `inprocess` | `inprocess` until its slice is accepted, then `sandbox` |

- **The knob rides as a Cloud Build substitution from `.env`**, not as a hand-set variable: `--set-env-vars` replaces the whole set at each deploy and a
  switch set with `gcloud run services update` would silently revert (the precedent is `_DRIVE_UPGRADE_PASS`, `cloudbuild-dev.yaml:112-114`). A test pins that each knob is
  present in the deploy step.
- **The assistant flips each switch after deploy and reports to the owner (O9); the owner can roll back with one command** (the knob set back to `inprocess`, §8).
- **There is no automatic fallback.** If the sandbox is down the caller fails, per §4.9. Falling back to in-process would parse hostile bytes in the credentialed
  runtime exactly when the boundary is unavailable. The only fallback is the explicit knob, a conscious deploy-time act.
- **On Cloud Run an unset knob is `sandbox`** once the slice is accepted. A missing sandbox URL **fails the start of the indexer** (background; a failed start is visible and harmless) but **degrades the main service**: it starts, logs ERROR, raises an alert, and answers an affected request with the converter-unavailable alert (§4.9), because the main service also serves unrelated traffic. `inprocess` on Cloud Run logs an ERROR line at start.
- **After two weeks of observation per part (O9) with no sandbox-attributable defect the in-process path is removed from deployed callers** (S6-a): the knob goes, the adapters stay for dev/tests
  and as the sandbox's engine, and the parser libraries leave the credentialed runtimes' image (§7).

### 4.13 Tests run locally the same way

The ports are the seam. Unit tests use the in-process adapters (as `tests/unit/adapters/test_subprocess_content_extractor.py` does) or `AsyncMock(spec=Port)`. The HTTP adapters are tested
at the HTTP boundary (a fake ASGI sandbox app in-process via the real `sandbox_app`, per `ADAPTER_WIRE_TESTING.md`: mock where the SDK ends, not the port). A **contract
suite** runs the same cases against `inprocess` and `sandbox` adapters (S1-S5) and asserts equal outcomes. Locally `SANDBOX_*` unset means in-process; no container needed.

### 4.14 Observability and cost

- One structured line per request: `request_id`, op, input bytes, status, wall, peak RSS, recycled. No names, no content, no ids, no URL (I3). Callers log the same `request_id`.
  **The log channel is treated as untrusted:** the engine's stdout and stderr are never copied into log lines (only a length and a hash, as `failure_label` does), line size and line rate per request are
  capped, and anything that reads or alerts on the sandbox's logs filters by service name and treats the content as data, not instructions.
  Memory scope `SCOPE_SUBPROCESS` (existing) labels the child. No Logfire or BigQuery in the sandbox.
- **Cost (estimate, list prices to be re-checked):** request-based billing at about $0.000024 per vCPU-second and $0.0000025 per GiB-second. A 5 s parse at **2 vCPU** / 2 GiB is
  ≈ $0.00025; with a cold start per request (`always`, ~+2 s) ≈ $0.00035. Ten thousand files ≈ $2.5-4. Media: 1.07 CPU-s per audio minute (§27.2) ≈ $0.0015 per audio hour of CPU, negligible
  next to the $0.35 per hour of transcription. Idle cost with `min-instances=0`: zero. A warm minimum is not recommended (a warm instance contradicts `always`).
- Cold start, light image: expected 1-3 s, to be measured in S0-b.

## 5. Owner decisions and proposals

| # | Decision | Status |
|---|----------|--------|
| O1 | One separate deploy unit, zero-role identity, no secrets | owner (2026-10-19) |
| O2 | All of C1-C6 move there; LLM calls and platform APIs never do | owner (scope), author (list) |
| O3 | The drive parser's residual ends when the drive parsers move (S3) | owner |
| O4 | Transcripts for a second user wait for the media part (S5) | owner |
| **O5** | **Every request runs on a fresh instance (`recycle=always`) for all ops, chat uploads and PNG widgets included; +1-3 s accepted; real latency measured after deploy** (§4.6) | owner (2026-10-20) |
| **O6** | **No egress for pulling large files.** HTTP/2 end to end is tried for bodies above 32 MiB; if it fails, 32 MiB stays a known limitation for parsed files, with no signed-URL fallback (§4.7) | owner (2026-10-20) |
| **O7** | **The media op may have egress, only through the in-sandbox loopback proxy:** host allowlist by name suffix (never address pinning; raw-IP URLs refused; DNS per request); no redirects; the allowlist is configuration built from the real download-URL hosts seen in the transcripts measurement (drive RFC §27 T0), extendable without a code change (§4.5) | owner (2026-10-20) |
| **O8** | **Slice order and prerequisite:** `render.docx`, then the other renderers, then drive parsers, then chat uploads, then media; each part is enabled only after its caller's own service account exists (§7, §4.4) | owner (2026-10-20) |
| **O9** | **Per-part switch; two weeks of observation each; then the in-process path is removed.** The assistant flips each switch after deploy and reports; the owner can roll back with one command (§4.12, §8) | owner (2026-10-20) |
| **O10** | **Two services:** light (parsers, chat conversion, media audio, `ffprobe` of chat videos) and render (Chromium and Node: DOCX, PDF, PNG). `video_probe` moves to the light service; Gmail HTML cleaning stays in the main service by decision, a pure-Python parser (§4.2) | owner (2026-10-20) |
| **O11** | **The drive indexer is in the intended public-invoker set** (§8) as an accepted exception: internal ingress only, every caller's token verified in-app with a pinned audience; the per-runtime identity design relies on the same in-app check | owner (2026-10-20) |
| P0 | Per-runtime identities are a prerequisite for each op family; caller sets must not overlap (§4.4) | author, folded into O8 |
| P1 | A service, not a job (§4.1) | author |
| P4 | Platform IAM, in-app signature verification with pinned audience, per-op caller sets; not `--ingress=internal` (§4.4) | author |
| P6 | Every engine in a fresh child under an unprivileged uid with the full rlimit set; the server never touches untrusted bytes (§4.6) | author (design rule) |
| P8 | Existing ports get HTTP adapters; two small ports are new; no omnibus port (§4.10) | author |
| P10 | The transcript gate for any second user lifts when media is accepted (S5), not before (§9) | author, follows O4 |

## 6. Deltas: what this RFC decides beyond the owner's decisions

Numbered locally (S-Δn); they do not continue the drive RFC's Δ series.

- **S-Δ1. `convert_file_to_text` is split.** Today one function does parsing **and** calls `AudioTranscriptionPort` (an LLM provider call) for audio
  (`file_conversion.py:240-250`). The audio branch stays in the caller; only the markitdown/plain-text branch becomes `chat.convert`. The "no audio service" and
  "transcription failed" system notes stay where they are.
- **S-Δ2. The plain-text fast path** (`text/*`, `file_conversion.py:251-260`) stays in the caller: reading a UTF-8 file as text is not parsing. (Proposal; the sandbox would see it only as a no-op. OQ-S6.)
- **S-Δ3. `ExtractionRequest` is not serialized as is.** It carries `user_id`, `account_id`, `item_id`, `name` (`src/domain/drive_extraction.py`). The adapter strips them; billing fields
  (`model_calls`, `model_cost`, …) are never produced by the sandbox because they belong to the reader.
- **S-Δ4. The PDF ops join a port.** `PdfSplitter` moves from a Protocol in `composition/` to `ports/`, so the HTTP adapter can implement it without composition importing an adapter type.
- **S-Δ5. Rendering is per request, and Chromium runs in a child under its own uid.** `PlaywrightHtmlRenderer` keeps one warm browser (`render` takes a lock, reconnects); the sandbox launches per request. Widget latency rises by the
  browser's start; accepted when measured (S2), with no change of policy (O5).
- **S-Δ6. The transcript child moves.** §27.4 puts `SubprocessMediaExtractor` in the indexer image and `child.serve`. Under this RFC the media child runs in the sandbox; the indexer keeps
  window planning, the Gemini upload and the job states. §27.4's hardening (Δ99: protocol and demuxer allowlists) is unchanged and applies inside the sandbox.
- **S-Δ7. `video_probe`** (`ffprobe` on bytes in the main service) joins C3 as `media.probe` over bytes. Low risk, low effort, moved into the light service (O10).
- **S-Δ9. The existing OIDC verifier takes one expected email.** `verify_worker_oidc(authorization, expected_sa_email, audiences)` (`src/web/worker_oidc_verifier.py:38`) is extended or wrapped to accept a set per op; `/worker` behaviour is unchanged.
- **S-Δ8. Deployment honesty.** The decision record `drive_parser_sandbox_resource_isolation.md` gets a status line at S1 acceptance: superseded for the drive parser by this service; its
  description of what the child does and does not block remains true of the engine.

## 7. Slices

Order is an owner decision (O8). Each slice is accepted by its tests, its owner steps, and a stated measurement. None edits an existing test to pass (the reviewer rule applies as everywhere).
An op family is enabled only when its prerequisite identity exists (P0, O8).

### S0. Spike (no product code; scratch deployment, results to the private spike folder)
a. **Large bodies:** a 50 MiB request and response through Cloud Run to a Hypercorn app with `--use-http2`, from an `httpx` HTTP/2 client; memory bounded.
b. **Recycle:** respond, then the supervisor stops the instance; no truncation; a fresh instance for the next call; light-image cold start; **back-to-back bursts** (20 sequential and 5 parallel calls): rate of `recycling` answers, and whether `httpx` sees GOAWAY or `Connection: close`.
c. **Zero-role identity:** the service starts, pulls its image, logs to stdout; the identity's attempts at a store, a secret and a bucket are refused; **its token gets 403 on every enabled generative, AI and maps API** (else §4.3: no-NAT egress earlier for non-media ops).
d. **Audit:** no `allAuthenticatedUsers`; `allUsers` only on the intended public-invoker set (§8); record the command and result.
e. **Ingress and Authorization:** the caller's token reaches the app intact in `Authorization`; signature and audience verify in-app; one test caller admitted, another refused; `--ingress=internal` behaviour from another Cloud Run service without VPC egress.
f. **Child uid on gen2:** engine children start under a separate uid with `RLIMIT_NPROC`/`NOFILE` applied; the in-memory volume is size-limited and writable by the child's uid only; **whether Chromium's own sandbox starts** on gen2 (outcome recorded either way).
g. **Equivalence:** the drive evaluation corpus (`scripts/validation/drive_eval`) and the chat fixtures through in-process and deployed engines; byte-equal text expected; list differences.
h. **Limits on the new host:** rerun the Phase 0 bomb set (`scripts/infrastructure/drive_extract_s1.py`) with the child limits; peak memory per case.
i. **No-NAT dry run** for S6: a deny-all egress deployment that still serves a request.

### S1. The service skeleton and `render.docx`
- Rationale: the model-written script is the least constrained input, and the skeleton (auth, contract, child launcher, recycle, deploy tests) is proved on a consumer with one op, bytes out, no large input.
- Build: `src/domain/sandbox_contract.py`; `sandbox_main.py`; `src/web/sandbox_app.py`; `src/composition/sandbox_setup.py`; `Dockerfile.sandbox` (`render` target first); Cloud Build step; the child launcher shared by all ops (uid, rlimits, process-group kill);
  `src/adapters/sandbox_client/` with `SandboxHttpClient`, `SandboxDocxRunner`; `SandboxUnavailable`/`SandboxMisconfigured`; main wiring behind `SANDBOX_RENDER`.
- Tests: contract suite (`inprocess` vs `sandbox` `DocxRunnerPort`: success bytes, script error text, timeout, empty output); the field set of a request is pinned (I3); caller gate (signature, audience, per-op set, overlap check, fail closed); startup self-check;
  **image-contents test** (COPY allowlist, no environment or key file, no Firestore/Secret Manager/storage client importable); Cloud Build test (no `--set-secrets`, `--no-allow-unauthenticated`, no `--no-invoker-iam-check`, no public-invoker binding, the knob present);
  failure mapping incl. `recycling`; recycle (`always`, `abnormal`); the server process imports no document, image or archive library and the clients parse no binary output (architecture tests); wire tests at the HTTP boundary.
- Accept: S0 results in hand; DocGenerator e2e in `sandbox` mode; the engine child confirmed under its own uid.

### S2. The other renderers (C5, C6)
- Build: `SandboxPuppeteerRunner`, `SandboxHtmlRenderer`; Chromium in a child under its own uid; Chromium's own sandbox tested on gen2 (S0-f) and the result recorded; the child boundary is the claim either way.
- Tests: contract suite per runner; no outbound connection from the render service; blocked-host test for the request filter; PNG width; HTML timeout.
- Accept: widget render latency (cold) stated; PdfGenerator e2e.

### S3. Drive parsers (C1)
- Build: `SandboxContentExtractor`, `PdfSplitterPort` (+ `drive.pdf_windows`), the light image, indexer wiring behind `SANDBOX_DRIVE`; indexer-side cap `SANDBOX_INDEXER_SLOTS`. Prerequisite: the indexer's own identity (P0, O8).
- Tests: contract suite on every `ExtractionStatus` path; envelope round trip incl. `brief` and `capture_signals`; failure mapping (`recycling` uncharged; reset twice -> `ERROR sandbox_lost` charged); wire tests.
- Accept: S0-g equivalence on the drive corpus; one tick on the owner's drive in `sandbox` mode within the tick budget (drive RFC §7.1); zero `sandbox_lost`.

### S4. Chat uploads and on-demand reads (C2)
- Build: `DocumentConverterPort`, `LocalDocumentConverter`, `SandboxDocumentConverter`; the five call sites move to the port; the "converter unavailable" alert and the degraded-start behaviour of the main service. Prerequisite: the main service's own identity (P0, O8).
- Tests: all existing conversion cases through both adapters; the audio branch untouched; size alert at 5 MiB before any call; unavailable alert; a hostile-archive case gives the same alert as in-process.
- Accept: added p95 latency for a typical 1 MiB docx stated to the owner; Slack and Telegram e2e with docx, xlsx, pdf, csv.

### S5. Media for transcripts (C3)
- Build: `MediaAudioPort`, `SandboxMediaAdapter`; the range proxy and the ffmpeg argument set (§4.5); `video_probe` onto `media.probe`. Lands **before** §27's T1 enables transcription, so the indexer never decodes media in-process. Documentation delta to §27.4/§27.12 (S-Δ6).
- Tests: proxy rules (scheme, host suffix at a dot boundary, IP literal, redirect, DNS per request and no address pinning, size/rate cap); URL absent from logs; ffmpeg arguments pinned; window plan -> Opus bytes under the cap; `no_audio`, `too_long`, `too_large` stubs; 4-hour/8 GiB caps; `SandboxUnavailable` defers the job step.
- Accept: measurement on the 21:47 reference recording: bytes read, wall, CPU, in `sandbox` against in-process.

### S6. Close-out (after a soak)
a. Remove the in-process path from deployed callers (knobs deleted); split requirements so the credentialed runtimes' image drops the parser libraries, Chromium and Node (a build test lists what each image may import).
b. Network lock-down (§4.5) for both services.
c. Doc updates: drive RFC §9/§24.14/§27, `drive_parser_sandbox_resource_isolation.md`, `CLAUDE.md`, the deployment README, a decision record for the final shape.

## 8. Owner steps (placeholders; nothing here names a real resource)

0. **Prerequisite:** the per-runtime identities for the callers of the family being enabled (P0, O8).
1. **Create two identities** (`<sandbox-sa>`, `<sandbox-render-sa>`) and grant **nothing**. Verify: `gcloud projects get-iam-policy <PROJECT_ID> --flatten=bindings[].members --filter="bindings.members:<sandbox-sa>"` is empty.
2. **Audit once, then each deploy:** `gcloud projects get-iam-policy <PROJECT_ID>` contains no `allAuthenticatedUsers`; list service-level policies for the same. The **intended public-invoker set** is the main service's webhook, OAuth, Cabinet and MCP surface, the voice relay, and the drive indexer, and nothing else; the sandbox services are never in it. The indexer is in the set as an accepted exception (O11): it takes internal traffic only and verifies every caller's token in-app with a pinned audience, because Cloud Tasks' token audience (`{url}/worker`) is one platform IAM would not accept.
3. **Deploy the services** (a Cloud Build step each, no secrets, `--no-allow-unauthenticated`, `--service-account=<sandbox-sa>`, concurrency 1, `--use-http2`).
4. **Grant invoker:** `roles/run.invoker` on each sandbox service to the caller runtimes' identities only (indexer for the light service, main for both).
5. **Set the allowlist and URL values** in `.env` (the caller emails per op, the service URLs) as substitutions; never in the repository.
6. **Flip one knob per slice** after its acceptance; one soak window before S6.

Rollback: set the family's knob back to `inprocess` and deploy the caller (a substitution, not a hand edit, §4.12). The sandbox services can stay up unused. To stop one op family reaching a sandbox that misbehaves,
change the caller map in the **sandbox's** environment and **redeploy the sandbox** (the map is its environment, not the callers').

## 9. What this unblocks: transcripts for a second user (USER_DRIVE_INDEXING §27.12, OQ-T3)

§27 holds transcription to the owner only, "until the zero-credential parser service exists" (`drive_transcribe_enabled` default False, Δ125). When S5 (media) is accepted:
the indexer no longer decodes media in-process; the decoder runs in a service that holds nothing and sees one recording; the URL is the only thing handed in.
The gate then changes from "owner only" to the existing **per-user explicit opt-in** (`drive_transcribe_enabled`), no new mechanism. Two guards ship with S5 so the gate means what it says: (1) the in-process media
adapter refuses to run on Cloud Run (a start-time error), so the only media path in a deployed indexer is the sandbox; (2) `DRIVE_TRANSCRIBE_ENABLED` (the global switch) cannot be on while `SANDBOX_MEDIA` is
`inprocess` on Cloud Run. The same argument lifts the parser residual in `drive_parser_sandbox_resource_isolation.md` once S3 is accepted, which is that record's stated trigger.

## 10. Questions: resolved by the owner (2026-10-20) and what remains

Resolved (see §5):
- **OQ-S1 cold start per request:** accepted for all ops (O5).
- **OQ-S2 large bodies:** try HTTP/2; otherwise 32 MiB stays a known limit; no signed-URL fallback (O6).
- **OQ-S3 media egress:** allowed through the loopback proxy, name-suffix allowlist as configuration (O7).
- **OQ-S4 one service or two:** two (O10).
- **OQ-S5 `video_probe`:** moves to the light service (O10).
- **OQ-S7 soak and who flips:** two weeks per part; the assistant flips and reports; the owner can roll back with one command (O9).
- **OQ-S9 Gmail HTML cleaning:** stays in the main service by decision (O10).
- **OQ-S10 slice order:** as in O8.

Remaining engineering points (no owner input needed; the author's call, stated for the record):
- **OQ-S6. The plain-text fast path** stays in the caller (S-Δ2): moving a trivial UTF-8 read buys nothing and adds a hop to the commonest upload.
- **OQ-S8. Per-request signed grants** instead of caller-level invoker: no; the invoker binding plus the verified per-op set is enough while callers are two runtimes.

## 11. Parameters (initial)

| Name | Initial | Basis |
|------|---------|-------|
| `SANDBOX_RECYCLE` | `always` | I2/I6; measured in S0-b |
| light: memory / cpu / concurrency / max-instances / timeout | 2 GiB / 2 / 1 / 5 / 900 s | indexer's measured host; ffmpeg CPU (§27.2) |
| render: memory / cpu / concurrency / max-instances / timeout | 2 GiB / 2 / 1 / 3 / 300 s | Chromium; `node_timeout_s` 60 s, PDF similar |
| child limits (parsers) | `RLIMIT_AS` 1 GiB, `RLIMIT_CPU` 90 s, wall 120 s, `RLIMIT_FSIZE` 64 MiB | current values; S0-g re-measures |
| media child (per §27.4) | `RLIMIT_CPU` 150 s, wall 240 s, `RLIMIT_FSIZE` 64 MiB, `RLIMIT_AS` 1 GiB | §27.4 |
| request / response cap | 64 MiB each, streamed | 45 MiB drive cap plus envelope |
| `meta` size | 8 KiB | |
| client timeout | child wall + 15 s | |
| retry | one immediate retry on a lost connection or 5xx; none on 4xx | §4.9 |
| media URL host allowlist | name-suffix list in configuration, from the T0 measurement, extendable without code change | O7, §4.5 |
| `SANDBOX_INDEXER_SLOTS` | 1 of the light service's instances at a time | keeps chat free of indexer load |
| engine child limits added | `RLIMIT_NPROC` and `RLIMIT_NOFILE`, small fixed values (set at S0-f) | fork bomb, descriptor exhaustion |
| observation before removing in-process | 14 days per part | O9 |

## 12. Where the current code disagrees with the design (so the plan does not assume otherwise)

1. **The 45 MiB drive cap exceeds the HTTP/1 request limit** (§4.7); the design depends on HTTP/2 being proved first (S0-a).
2. **`convert_file_to_text` mixes parsing with an LLM provider call** (audio, `file_conversion.py:240-250`); it cannot be moved whole (S-Δ1).
3. **`ExtractionRequest` carries identity** (`user_id`, `account_id`, `item_id`, `name`); a naive serialization would break I3 (S-Δ3).
4. **The PDF page ops are outside `ContentExtractorPort`**; the reader reaches them through a Protocol in `composition/` (S-Δ4).
5. **`HtmlRendererPort` has a warm-browser lifecycle** (`start`/`stop`); the sandbox renders per request (S-Δ5).
6. **One image serves every runtime.** The main service, the indexer and the relay all run the same image (`Dockerfile`; `cloudbuild-dev.yaml`), so every credentialed runtime carries the parsers, Node and Chromium today.
   Removing them requires a requirements split (S6-a); until then the boundary protects, but the libraries are still present beside the credentials.
7. **`indexer_worker_app.py` reserves `/extract` for "Slice 3"** as an indexer route (docstring); that earlier seam is replaced by a separate service and should be edited out when S1 lands.
8. **`video_probe` is an unlisted consumer** in the main service (S-Δ7).
9. **`DocxRunnerPort` and `PuppeteerRunnerPort` already anticipate an HTTP adapter** (docstrings), which is consistent; their timeout arguments become the request's `limits`.
10. **The e-mail attachment path** reaches the converter through `email_search_service.py:244`, one of five call sites (§1).

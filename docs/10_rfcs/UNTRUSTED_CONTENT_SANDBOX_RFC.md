# RFC: Untrusted-content sandbox service — one place where hostile bytes are processed, holding nothing

**Status:** Proposed (rev 4). The parse family is built and live in both runtimes (O14). S1 (`render.docx`) is deployed behind
`SANDBOX_RENDER` (default `inprocess`; the switch is not yet flipped) and not accepted. S2 (`render.pdf`, `render.png`) is built
on its branch, not deployed. No S0 spike has run (`decisions/sandbox_render_service.md`). *O* items are owner decisions,
*P* items are author proposals, *S-Δ* items are this RFC's deltas.
**Date:** 2026-10-20 (rev 3: owner decisions O5–O11; rev 4: 2026-11-03, owner decisions O12–O13, code re-read at `2280ca78`; O15 added 2026-11-04; S5 built, O16-O17)
**Owner decisions (2026-10-19, "we need a proper sandbox with a limited scope"):**
- **O1. One separate Cloud Run deploy unit** whose own service account holds **no IAM role** and which is given **no secret**.
- **O2. It executes every piece of untrusted processing** the product does: document parsing for the drive, conversion of
  chat uploads, media decoding for transcripts, and the runners of model-written code and model-written HTML (§1).
- **O3. The drive parser stays a reasoned residual until it moves** (`decisions/drive_parser_sandbox_resource_isolation.md`
  names this service as its trigger's answer); this RFC is that answer, widened to the other consumers.
- **O4. Transcripts for a second user wait for this service** (USER_DRIVE_INDEXING_RFC §27.12, OQ-T3).

**Owner decisions (2026-11-03):**
- **O12. Order: the document service first, the sandbox's parse slices after it.** "Risk is not scary; do it the most
  logical and hexagonal way." EXTERNAL_FILES_RFC's document service (its U0 → U1 → U2 → U3 → U4, then U5) is built
  first. S3 (indexer) and S4 (main service) come after it, as adapter swaps under its parse ports
  (`ContentExtractorPort`, `PdfSplitterPort`), with no caller change. Until then the in-runtime parser child is the
  accepted residual, including for Google full-drive grants (`GOOGLE_DRIVE_RFC.md` §10). Recorded there as D14. By code
  dependency S3 may start once that RFC's U1 and U3 are in, before its U4/U5; S4 waits for U5. It
  amends O8's order for the parse slices; the renderers (S1, S2) stay an independent track.
- **O13. One parse engine, in principle** (EXTERNAL_FILES_RFC D15, its P4): the child's own readers serve every source;
  the final go follows that RFC's U0-b report on real files. Consequences here: no `DocumentConverterPort`,
  `LocalDocumentConverter`, `SandboxDocumentConverter` and no `chat.convert` op; one op family, `parse.*`, serves both
  runtimes (§4.7); S4 = admit the main service's identity to `parse.*` and flip its knob. S-Δ1 and S-Δ2 hold.

**Prerequisite (P0, O8): met in configuration.** Every runtime runs as its own service account: main, indexer, voice
relay and research job (`cloudbuild-dev.yaml:32`, `:157`, `:260`, `:217`; `decisions/runtime_service_accounts.md`).
Of these, only the main service's and the indexer's identities hold the KMS key grant that decrypts stored credentials
(`decisions/secrets_at_rest_cipher_and_digest.md`; `docs/07_deployment/README.md:351-354`); those are the two runtimes
whose parsers this service relieves.

Related: `EXTERNAL_FILES_RFC.md` (the document service; §4.8, §4.9); `GOOGLE_DRIVE_RFC.md` G2, §10;
`USER_DRIVE_INDEXING_RFC.md` §9, §24.3, §24.14, §27.4; `decisions/drive_parser_sandbox_resource_isolation.md`;
`decisions/worker_oidc_and_docx_sandbox.md`; `decisions/sandbox_render_service.md`; `LONG_RUNNING_TURNS_RFC.md` (style,
not dependency).

**Changes in rev 4** (O12, O13 and a re-read of the code):
- O12 and O13 recorded; the slice section (§7) is rewritten in the new order, S0 items tied to the slices they gate.
- C2 and the ops: one `parse.*` family for both runtimes; `chat.convert`, `DocumentConverterPort` and its two adapters
  are gone from the design (§1, §4.7, §4.10, §6).
- As built in S1: the caller gate maps one group per runtime identity to op prefixes and checks the groups disjoint;
  S-Δ9 is done (§4.4); the contract's statuses, body frame and recycle values are the built ones (§4.7, §4.6, §11).
- Media: transcription is built in the indexer (`MediaAudioPort`, the parser child, a loopback relay for header-carrying
  sources). A Google recording has no pre-authenticated URL, so the caller sends bytes (§4.5, GOOGLE_DRIVE_RFC G2);
  S5's input shape is an open engineering point (OQ-S11).
- §12 re-verified: item 7 is fixed and dropped; items moved; three new.
- **Review round 1** (2026-11-03): sized for the 100 MiB PDF cap (§4.7, §11, §12.1, S0-a); `SANDBOX_PARSE` fed by two substitution keys, one per
  runtime (§4.12); one adapter object for both parse ports, the batch `cut_pdf_windows` behind `parse.pdf_windows` (§7 S3, S4); S3 may start after
  EXTERNAL_FILES U1+U3 (O12); §12.13.
- **Author decisions new in rev 4** (minimal, listed in §5): the op family name `parse.*` (A1, shared with
  EXTERNAL_FILES A1); one knob name per runtime, `SANDBOX_PARSE`, replacing `SANDBOX_DRIVE` and `SANDBOX_CHAT` (A2).

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
| C1 | Drive document parsing (PDF, DOCX, PPTX, XLSX/XLS, ODF, EPUB, HTML, RTF, MSG, EML, text) | a file from a user's drive | indexer, child process under an unprivileged user with rlimits (`src/adapters/subprocess_content_extractor.py:116`, `:157`, spawn `:221-224`, the parser user `:318-345`; `src/adapters/drive_parser/child.py`, `readers.py`) | `ContentExtractorPort.extract` (`src/ports/content_extractor_port.py`); the PDF page ops (`pdf_page_count`, `cut_pdf_window`) are **not** on the port: they reach the reader through `PdfSplitter` (`src/composition/drive_reader_runner.py:73-78`), which EXTERNAL_FILES U1 promotes to `PdfSplitterPort` |
| C2 | Chat-upload conversion; on-demand reads of chat, workspace, delivered and drive files; email attachments | a file attached in Slack/Telegram; a file opened by `open_file` or the drive specialist's `open_drive_file`; an email attachment | main service, **in process**, a thread (`src/utils/file_conversion.py:208`; five call sites in three modules: `src/services/file_conversion_service.py:205`, `:250` and `:366`, `src/services/email_search_service.py:251`, `src/handlers/conversation_handler.py:904`). EXTERNAL_FILES U3 moves all of them into the parser child, behind its document service | none today (a free function); after EXTERNAL_FILES U3 the same `ContentExtractorPort` and `PdfSplitterPort` as C1 (O13) |
| C3 | Audio/video decoding for transcripts (`ffmpeg`, `ffprobe`) | a recording on a drive: a pre-authenticated URL (OneDrive) or a header-carrying source read through a loopback relay (Google, GOOGLE_DRIVE_RFC G2) | indexer, the parser child (`SubprocessMediaExtractor`, `src/adapters/drive_parser/media_extractor.py:68`; `LoopbackMediaRelay`, `media_relay.py:82`), behind `DRIVE_TRANSCRIBE_ENABLED` (`indexer_main.py:174`, `:232`); `ffprobe` on raw bytes also runs in the main service (`src/utils/video_probe.py:21`) | `MediaAudioPort` (`src/ports/media_audio_port.py`: `probe`, `extract_windows`); none for the probe |
| C4 | DOCX generation | a Node script written by a model | main service, Node subprocess with a module hook and a withheld environment (`src/adapters/node_docx_runner.py:33-69`), or the sandbox's `render.docx` behind `SANDBOX_RENDER` (S1, built; `src/composition/sandbox_client_setup.py:48`, `:68`; wired through `docx_runner_factory`, `src/composition/user_agent_factory.py:20`, `:213`, `:772`) | `DocxRunnerPort.run(js_code, spec_json, timeout) -> bytes` (`src/ports/docx_runner_port.py`) |
| C5 | HTML to PDF | HTML written by a model | main service, Node + Puppeteer subprocess, request filter in `pdf_generator/runner.js:18-51`, `:81-97` (`src/adapters/node_puppeteer_runner.py`), or the sandbox's `render.pdf` (S2, built on its branch; `SandboxPuppeteerRunner`) | `PuppeteerRunnerPort.run(html, timeout) -> bytes` |
| C6 | HTML to PNG (rich-content widgets) | HTML written by a model | main service, one shared headless Chromium (`src/adapters/playwright_html_renderer.py:110-123`; wired `src/composition/application_graph.py:956-962`; used `src/services/rich_content_service.py:164`), or the sandbox's `render.png` (S2, built on its branch; `SandboxHtmlRenderer`) | `HtmlRendererPort.render(html, width) -> bytes` plus `start`/`stop` |

Of the in-process runners, only C5 has a request filter (`runner.js`, which still lets public hosts through); C6 has none and runs with JavaScript on
(`playwright_html_renderer.py`). The sandbox's engine is stricter than both (§4.5, S2 delta D3).

The five converter call sites above are the full list found by grep (the e-mail attachment path is one of them); the markitdown builder
(`src/utils/document_hardening.py:38`) has no other user (`file_conversion.py:273`), though the same module's ZIP checks also serve the drive
parser (`drive_parser/readers.py:54`). HTML cleaning of e-mail bodies with BeautifulSoup in the Gmail adapter
(`src/adapters/gmail_provider_adapter.py`) stays in the main service **by decision** (O10): it is markup text cleaning of mail the user's own provider
delivered, not a decoder of attachments; revisit if its input class changes.

**What stays outside the sandbox, by rule.**
- **LLM API calls.** The readers and triage agents, transcription, the profiler: they need keys and they are the callers' job. The sandbox returns
  bytes (a PDF window, an Opus window); the caller sends them to a model.
- **Every call to a platform API** with the user's credentials (Graph, Google Drive, Slack, Telegram, Gmail) and every store (Firestore, GCS). The caller
  fetches the bytes, the sandbox never fetches on a user's behalf. The one exception is the media op's pre-authenticated URL (§4.5), which carries no
  credential of ours; a source that needs a header (Google) has no such URL, so its caller sends the bytes (GOOGLE_DRIVE_RFC G2).
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
| **sandbox-render** | C4, C5, C6 | Python shell, Node, the `docx` package, Puppeteer with **chrome-headless-shell only** (no full Chrome, **no Playwright**), the font set below | 2 GiB, 2 vCPU | 300 s |

One browser serves both HTML ops (author delta D1): Puppeteer's chrome-headless-shell, installed at build time by `puppeteer browsers install`
(the revision the locked Puppeteer expects) into a root-owned cache the engine uid can read but not write. The fonts are the Debian set the in-process
Playwright image renders with (Latin, Cyrillic, CJK, Thai, emoji). PNG equivalence with the in-process renderer is checked by width and validity, not
pixel by pixel; the image size is not yet measured (acceptance).

Why two: the render image carries the browser and runs code a model wrote, the light image carries decoders and runs bytes someone sent;
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

- **Identities (P0, O8): one caller group per runtime identity.** A caller group is one runtime's identity set and the op prefixes it may call; a
  runtime that shares an identity with the rest of the project is never a caller. Every runtime has its own identity (header), so the prerequisite is met
  in configuration; each op family is still enabled only when its slice is accepted. **Startup check (built):** the groups are read from
  `SANDBOX_CALLERS_<GROUP>` (`:`-separated emails), and the service refuses to start if one email sits in two groups, if a group is empty on Cloud Run,
  or if no group is configured there (`src/composition/sandbox_callers.py`, `load_caller_sets`, `assert_disjoint`). Disjointness is across groups, never
  across prefixes, so one prefix may belong to two groups (`parse.*`, O13).
- **Platform authentication.** The services are deployed `--no-allow-unauthenticated`. Each caller identity holds `roles/run.invoker` **on the sandbox service
  resource** only. The caller sends its ID token for the sandbox URL in `Authorization: Bearer` (never `X-Serverless-Authorization`),
  fetched from the metadata server (`src/adapters/sandbox_client/metadata_id_token_provider.py`; the relay's precedent `relay_main.py:107-118`). Deploy tests
  pin `--no-allow-unauthenticated`, forbid `--no-invoker-iam-check`, and forbid any public-invoker binding on a sandbox service (`tests/unit/test_cloudbuild_sandbox.py`).
- **In-app verification, in v1 (built, S-Δ9).** The app **verifies the token signature itself** (`verify_oidc_identity`,
  `src/web/worker_oidc_verifier.py:177`; `verify_worker_oidc` takes a set of emails, `:91`), checks `email_verified`, and **pins the audience** to the
  service's own URL (`SandboxCallerGate`, `src/web/sandbox_caller_gate.py`). The platform check is not the only gate: a mistaken binding does not silently admit a caller.
- **Per-op allowlist.** The verified `email` is mapped to its group's op prefixes; the gate knows no op by name. Groups: indexer `parse.*`, `media.*`;
  main `parse.*`, `render.*` (S1 configures the render group only). Values live in the environment, not in the repository.
- **Not `--ingress=internal`:** a call to a `run.app` URL counts as internal only through the caller's VPC, which would force VPC egress on the callers. Spike item S0-e.
- **Fail closed** on Cloud Run when the caller map or the service URL is empty; open only when `K_SERVICE` is unset and nothing is configured
  (`src/web/sandbox_caller_gate.py`; `worker_oidc_gate` precedent, `src/web/indexer_worker_app.py:42-64`).

### 4.5 Network egress (O15-tunnel, O16)

Built shape (S5, OQ-S11 closed, delta list in §7 S5): **no op family needs the network.** A recording never travels as a URL, a header or a byte
payload in a request: the caller opens a reverse tunnel (§4.7) and the sandbox asks for byte ranges through it.

| Op family | Needs the network? |
|-----------|--------------------|
| `parse.*`, `render.*` (input travels in the request) | no |
| `media.*` (every source, OneDrive and Google alike) | no: ranges come back over the caller's own tunnel stream |

- **The credential never leaves the caller.** The indexer holds the bearer or the pre-authenticated URL and answers the sandbox's range requests; the
  sandbox sees ranges of one recording and nothing else. The in-indexer `LoopbackMediaRelay` path is deleted in the same deploy (S5 card F).
- **Range proxy dropped, O7's host allowlist superseded.** The earlier design (a host-suffix allowlist, IP-literal and redirect rules, a fetcher under
  its own uid) is not built: with no outbound call there is nothing to allowlist. A test asserts the light service's code opens no socket outward;
  the S6 lock-down (no egress at all) covers the whole light service.
- **ffmpeg reads a loopback range server inside the media child**, not a server-side helper and not the network: the child serves `127.0.0.1` with a
  nonce path, GET/HEAD and single Range only, header caps, a handful of connections, and turns each read into a tunnel OPEN. The server parses only
  fixed-size records from the child.

### 4.6 Every engine in a child under its own uid; one request, one instance

- **Rule (applies to all ops, S1 included).** The server process (it holds the listener and the verified identity) **never parses, decodes, renders or executes
  anything from a payload**. For each request it streams the body to a file in a per-request directory, then spawns a **fresh child** for the engine (parser, markitdown, ffmpeg, the
  Node script, Chromium) under a **separate unprivileged uid**, with no supplementary groups, as the drive parser already does (`subprocess_content_extractor.py:221-224`, `child.py`). S1 built the shared launcher
  (`src/adapters/sandbox_engine/child_launcher.py`, `limits_exec.py`).
  Limits applied in the child before the engine loads (the browser child differs, below): `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE`, `RLIMIT_CORE` 0, **`RLIMIT_NPROC`** and **`RLIMIT_NOFILE`**; the wall clock kills the whole
  process group. Different ops use different uids where cheap. The result goes back through a file the child writes, which the
  server reads and **validates as an envelope only** (§4.7); the server never interprets binary output.
- **The browser child (S2, delta D2).** Chromium does not start under `RLIMIT_AS` 1 GiB (V8 and PartitionAlloc reserve tens of GiB of address space at
  start), so the render family's HTML ops use their own launcher with **`RLIMIT_DATA`** in place of `RLIMIT_AS`, **`RLIMIT_NPROC` 512** (it counts threads per uid, and
  Chromium starts with tens of them) and **`RLIMIT_NOFILE` 1024**; `RLIMIT_CPU` and `RLIMIT_FSIZE` are unchanged. The wall that bounds memory is the instance's 2 GiB
  with one request per instance. The numbers are chosen in the build; S0-f confirms them. Chromium's **own** sandbox is off (`--no-sandbox`) behind a code constant
  (`CHROMIUM_OWN_SANDBOX`, `src/adapters/sandbox_engine/html_render_engine.py`) until S0-f shows it starts on gen2; the claim is the uid boundary, the
  zero-role identity and the closed network, not Chromium's sandbox. Per-request profile and shared-memory files go to the workdir (`--disable-dev-shm-usage`).
- **`concurrency=1`, `max-instances=N`** (§11). One request per container at a time.
- **Recycle (`SANDBOX_RECYCLE`, O5):** `always` (default; the render service): the instance serves **one** request and ends; `abnormal` (the light/parse service only, owner decision 2026-11-03): a
  normal request, any `ok` answer including a parser status such as `no_text` or `encrypted` inside it, keeps the instance warm, and a request that ends in anything else after payload bytes
  were written (child out of memory, crash, timeout, a non-ok engine outcome, an oversize or too-slow body, an engine exception, a cancel before an answer, or any process still alive under the engine uid after an `ok` child ended: it escaped the child's process group, is killed, and the instance ends with one ERROR line) recycles it as `always` does; a client disconnect while an `ok` body streams does not recycle (nothing is left behind), so no instance serves a second
  request after an abnormal one (I2); `never` (local only). `abnormal` is refused for the render family. Why: with `always` every request was a 10-25 s cold start and Cloud Run sometimes routed the next
  request to the exiting instance (its own 503, no sandbox header). **`always` still applies to every render op (O5)**.
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
- **Large bodies.** The caps are per kind: the indexer admits a PDF up to 100 MiB (`MAX_PDF_DOWNLOAD_BYTES`, `src/domain/drive_index_params.py:57`;
  `size_cap`, `drive_index_policy.py:159`) and every other kind up to 45 MiB (`MAX_DOWNLOAD_BYTES`, `:56`); the main service's opens keep 45 MiB
  (`MAX_DRIVE_DOWNLOAD_BYTES`). The design is sized for the largest admitted kind, so the request cap covers a 100 MiB PDF (§11). Cloud Run's HTTP/1 request limit is 32 MiB. The
  services are deployed with end-to-end HTTP/2 (`--use-http2`; the server is Hypercorn, as elsewhere) and callers use an HTTP/2 client. **This is the main technical
  risk of the design** (S0-a). If bodies above 32 MiB fail, **32 MiB stays a known limitation for parsed files** (O6): there is no fallback in which the service pulls a file
  by URL, and no drive cap is changed silently (the 100 MiB PDF cap included); files above the proven limit get the existing `too_large` outcome and the limit is documented.
- **Ops**

| Op | In | Out | Engine behind it |
|----|----|-----|------------------|
| `parse.extract` | file bytes, `kind`, `ext`, `limits` | JSON `ExtractionOutcome` minus billing fields (status, text, pages_total, truncated, brief, script_rendered, capture_signals) | `SubprocessContentExtractor`'s child readers (one engine, O13) |
| `parse.pdf_page_count` | PDF bytes | JSON `{pages_total}` | same |
| `parse.pdf_window` | PDF bytes, first page, count | PDF bytes | same |
| `parse.pdf_windows` | PDF bytes, window plan | the windows, streamed one after another (one upload instead of one per window) | same |
| `media.probe` (built, S5; tunnel) | START body frame: source size, window plan, limits; ranges via the tunnel | RESULT frame: JSON `{duration_s, has_audio, container}` | `MediaEngine` + `media_child` (`src/adapters/sandbox_engine/media_engine.py`) |
| `media.audio_windows` (built, S5; tunnel) | START body frame: source size, window plan, limits; ranges via the tunnel | RESULT frame (windows, eof, `no_audio`, `too_long`) and Opus window bytes as DATA frames | same; `ffmpeg` per §27.4 |
| `render.docx` (built, S1) | body `u32-be script_length \| script \| spec`, `limits` | DOCX bytes; on a script error its text in the body, capped at 16 KiB | `DocxRenderEngine` (`src/adapters/sandbox_engine/docx_render_engine.py:47`), a new engine: `NodeDocxRunner` writes into the repo tree and sets no rlimits (`decisions/sandbox_render_service.md`) |
| `render.pdf` (built, S2) | body = the HTML as UTF-8; `limits` | PDF bytes; **no engine text in a failure** (fixed detail code only) | `HtmlRenderEngine` (`src/adapters/sandbox_engine/html_render_engine.py`), child `render_html_child.py` -> fixed `pdf_generator/render_html.js` (Puppeteer, chrome-headless-shell) |
| `render.png` (built, S2) | body `u32-be width \| html` (width 16..2048, `src/domain/sandbox_png_frame.py`); `limits` | PNG bytes; no engine text in a failure | the same engine and script, a per-request page |

  The PNG width travels in the **body frame**, not in `meta` (delta D6: the meta field set is pinned, I3). The server never splits the frame; the engine child does.
  Both HTML ops run with JavaScript off, every request other than `data:`, `about:` and `blob:` aborted (public hosts too), no host name resolving, a dead proxy as
  a last layer and DevTools over a pipe (delta D3; the in-process PDF path lets public hosts through, the in-process PNG path has no filter). Their non-ok answers carry
  a fixed detail code (`wall_clock`, `cpu_limit`, `file_size_limit`, `response_cap`, `bad_request`, `engine_exec_failed`, `browser_unavailable`, `render_failed`,
  `engine_killed`, `engine_exit`, `empty_output`) and never the browser's or Node's stderr, which can echo attacker-chosen URLs (delta D4; `render.docx` keeps returning its error
  text because a repair loop consumes it). The clients map statuses to fixed texts and turn the sandbox's errors into the ports' own errors (`PuppeteerRunnerError`,
  `HtmlRenderError`), so no agent changes (delta D5; `src/adapters/sandbox_client/sandbox_render_outcome.py`).

- **Ops are added by the slice that builds them** (`SandboxOp`, `src/domain/sandbox_contract.py:79-86`: the three `render.*` ops and the three `parse.*` ops, built). The outcome
  statuses as built are `ok`, `script_error`, `timeout`, `empty_output`, `too_large`, `memory`, `recycling`, `unconfigured`, `bad_request`, `error` (`:89-99`); the parser's
  `ExtractionStatus` values travel in the `parse.*` answers (below).
- **Status convention of the `parse.*` ops (P1, built, not deployed).** A header of `ok` means the child ran and wrote a result that passed
  validation (`decode_parse_result`: closed keys, the op's status set, scalar types, a size bound). The parser's own status (`text`, `no_text`,
  `unsupported`, `too_large`, `encrypted`, `error`) is the **header detail** of that `ok` answer and the `status` of the JSON body, so a `no_text` result
  keeps its `pages_total` and `capture_signals` (the server streams a body only on `ok`). A run failure is a non-ok header: `timeout`, `memory`
  (new: a killed child leaves no result), `error`, `bad_request`, `too_large`. Every non-ok answer is logged at WARNING. Ops built: `parse.extract`
  (meta `kind` required, `ext` optional), `parse.pdf_page_count` and `parse.pdf_windows` (meta `windows`: 1..200 `[first_page, page_count]` pairs;
  response `u32-be count | (u32-be length | bytes) x count`, all or nothing under one wall clock; a window whose result is not `text` answers `ok` with
  that status and a `{status, detail}` body, no frame). There is no single-window op: one window is a batch of one. Caps: request = the 100 MiB PDF
  cap plus 1 MiB, response the same figure (owner, 2026-11-03).
- **No `chat.convert` (O13).** The main service reads through the same `parse.*` ops as the indexer. The family is named for what it does, not for its
  first caller: `drive.*` would misname the main service's chat and email reads (A1).
- A binary result over the cap is `too_large`. Streaming is used for responses over a few MiB.
- Idempotent and pure: every op is a function of its input, so any call may be repeated.

**Media ops are tunnel ops (S5; `src/domain/sandbox_tunnel.py`, `src/web/sandbox_tunnel_app.py`).** `POST /v1/media.<op>` is a full-duplex HTTP/2
stream, routed by a raw ASGI app in front of Quart (Quart buffers bodies without backpressure). The request is one START body frame (a closed key set; no
url, name, id or path), not new meta keys (the pinned meta set is unchanged). Both directions carry length-prefixed frames: the sandbox sends OPEN(offset,
length) and window DATA, the caller answers range DATA, and a terminal RESULT frame ends the stream. **The response header only means "stream opened";
the RESULT frame decides the outcome.** Bounds: at most 256 opens, 2 concurrent open streams, a byte budget per call of `min(8 GiB, 2 x size + 64 MiB)`,
256 KiB per DATA frame, a 5 s heartbeat and 30 s idle limit; a breach kills the child and returns `error/tunnel_bounds`. The call ceiling is the media
one (240 s call, 150 s soft), separate from the parse ceiling. No token refresh between ranges: one call is shorter than the source's token life, and an
expired source is refused. A systemic outage surfaces to the stage as `ParserUnavailable` (the job is left uncharged, the tick ends); a violation
of the tunnel protocol is `MediaUnreadable("tunnel_violation")` and is charged to the file, since only the file can steer the decoder that far.

### 4.8 Limits (per op, enforced inside; the caller's `limits` may only lower them)

Input size caps are the callers' existing ones (drive `size_cap(kind)`; the main service's reads: EXTERNAL_FILES_RFC §4.5's caps table, 5 MiB for parsing, `file_conversion.py:31`; media `TRANSCRIBE_MAX_BYTES`/`_SECONDS`, §27.4). Child limits keep their
values (`RLIMIT_AS` 1 GiB, `RLIMIT_CPU` 90 s, wall 120 s, `RLIMIT_FSIZE` 64 MiB) until S1 re-measures them on the new host; the browser child's memory limit is `RLIMIT_DATA` (§4.6). Output caps: text 2,000,000 characters
(`MAX_TEXT_CHARS`), binary 32 MiB, DOCX/PDF/PNG as today. The service's own memory (2 GiB) is the outer wall: a child that reaches its rlimit is an outcome, an
instance killed by the platform is a transport failure (§4.9).

Capacity is shared: the main service's reads (interactive) and the indexer (background) call the same light service. *Superseded by O14 for the parse family: no knob, no slots, no soak.* Interactive requests are never queued behind indexing: the indexer is limited to a fraction of
`max-instances` by its own client-side concurrency (`SANDBOX_INDEXER_SLOTS`), and a `recycling` or 429 answer to an interactive read is retried at once.

### 4.9 Failure semantics

The statuses **do not change**. The sandbox returns the ones callers already know; the HTTP adapter turns them back into the same objects.

| Situation | Seen by the caller as | Why |
|-----------|----------------------|-----|
| the parser result | `ExtractionStatus.TEXT/NO_TEXT/UNSUPPORTED/TOO_LARGE/ENCRYPTED/ERROR` as returned | unchanged domain outcome |
| child killed on time or memory | `TIMEOUT` / `MEMORY` as today (counted per-file retries, never routed to the reader) | `drive_parser_sandbox_resource_isolation.md` |
| `BUDGET_EXHAUSTED` | never from the sandbox | it is the reader's status |
| request too large (413) | `TOO_LARGE` | |
| 503 `recycling` | retried once at once (2 attempts, short jitter); still recycling -> `SandboxUnavailable(reason=busy)`; **never charged to the file** | §4.6 |
| 503 **without** the sandbox's outcome header (Cloud Run's own "malformed response or connection error" from an instance that was exiting) | a lost instance: the same one retry as `recycling` (one budget per call, never past `deadline_at`); lost again -> `SandboxUnavailable(reason=unavailable)` with a WARNING line; **never charged to the file** | §4.6 |
| 429, other 503 that carries the sandbox's own header (`reason=busy`, no retry), connection refused, an instance not started (`reason=unavailable`, no retry) | new `SandboxUnavailable` (domain exception): **systemic**, work is deferred, nothing is charged to the file | the shape of `DriveModelUnavailable` (`src/domain/drive_extraction.py`), `charges_file=False` |
| 401 / 403 | `SandboxMisconfigured`: ERROR log, systemic defer | a deployment mistake, not a file |
| connection reset or 5xx mid-request, or a timeout | one immediate retry on a fresh instance (a retry starts only with time left before the caller's deadline, its wall lowered to that time); if it repeats: `ExtractionStatus.ERROR`, detail `sandbox_lost`, **charged to the file** (counts toward `MAX_ATTEMPTS`) | an instance killed by the platform is indistinguishable from a poison file; two losses mean the file. Same reasoning as `DriveModelUnavailable.charges_file` |
| a malformed envelope | `ERROR` | |

Details for parsers stay what they are: a failure type, a code position and a hash, never text (`failure_label`). The DOCX op is the exception **by purpose**: the
generating agent feeds the script's error back to the model to repair it (`doc_generator_agent.py`), so `render.docx` returns the error text, which is the model's
own script's, not a user's data. The main service's reads: the document service maps `SandboxUnavailable` and `SandboxMisconfigured` to `PARSER_UNAVAILABLE`
and its note ("the file converter is temporarily unavailable; ask the user to send it again later"), built and tested in EXTERNAL_FILES U3, because the
"unsupported format" alert would be false; every other outcome keeps today's `[System: …]` notes (`file_conversion.py:108-157`), rebuilt from statuses.

### 4.10 Ports and adapters (P8)

No omnibus `ContentSandboxPort`. CLAUDE.md allows a port for a system boundary with two implementations and a test substitute, one per boundary, not for cleanliness. Every
boundary already has a port, or gets one before its slice: the PDF ops through EXTERNAL_FILES U1. Each gets **a second adapter**. C2 needs no port of its own: with O13 the main
service reads through EXTERNAL_FILES's document service, which holds the same two ports as the indexer, so S4 swaps the same adapters in its composition line.

| Capability | Port | In-process adapter (kept) | New HTTP adapter |
|------------|------|---------------------------|------------------|
| C1 and C2 parse | `ContentExtractorPort` (reused; one port for both runtimes, O13) | `SubprocessContentExtractor` | `SandboxContentExtractor` |
| C1 and C2 PDF ops | `PdfSplitterPort` (`PdfSplitter`, a Protocol in `drive_reader_runner.py:73-78`, promoted to `src/ports/pdf_splitter_port.py` by EXTERNAL_FILES U1) | same class | same HTTP class |
| C3 media | `MediaAudioPort` (built, `src/ports/media_audio_port.py`: `probe`, `extract_windows`) | `SubprocessMediaExtractor` | `SandboxMediaAdapter` |
| C4 | `DocxRunnerPort` (reused; its docstring names the built `SandboxDocxRunner`, `src/ports/docx_runner_port.py:6-10`) | `NodeDocxRunner` | `SandboxDocxRunner` (built, S1) |
| C5 | `PuppeteerRunnerPort` (reused; same note) | `NodePuppeteerRunner` | `SandboxPuppeteerRunner` |
| C6 | `HtmlRendererPort` (reused; `start`/`stop` become no-ops) | `PlaywrightHtmlRenderer` | `SandboxHtmlRenderer` |

All HTTP adapters and their shared client (`SandboxHttpClient`: ID token, HTTP/2, the retry rule of §4.9, `request_id`, timeouts) live in **one subpackage**,
`src/adapters/sandbox_client/`, because adapters may not import across subpackages (REQ-ARCH-23); the client is the only thing they share. The contract (op names, the
envelope, caps, outcome (de)serialization) is pure and lives in `src/domain/sandbox_contract.py`, importable by both sides.

### 4.11 The service's own code

`sandbox_main.py` (entry, like `indexer_main.py`: a supervisor and a `--serve` Hypercorn process), `src/web/sandbox_app.py` (routes, size and header checks,
request id), the caller gate (`src/web/sandbox_caller_gate.py`, `src/composition/sandbox_callers.py`), and a registry built in `src/composition/sandbox_setup.py`
that maps each op to its engine (`src/adapters/sandbox_engine/`). A separate `Dockerfile.sandbox` copies only the modules the ops need (domain, the engines,
the parsers) and carries no credentialed-runtime code (no handlers, no agents, no stores); it has the `render` target today (Node and the `docx` package, no
Chromium yet), and S3 adds `light`. The service has no database import to leak (image-contents test, §4.3). All of this is built in S1.

### 4.12 Composition and the switch (O9)

**Superseded by O15 for the render family:** one knob, `SANDBOX_RENDER` / `SANDBOX_RENDER_URL`, selects DOCX, PDF and widget PNG together (`build_docx_runner`, `build_pdf_runner`, `build_html_renderer` in `src/composition/sandbox_client_setup.py`); no per-part switch for these three, no new key. Per-part acceptance and rollback of O9 therefore holds between families, not inside the render family.

**Superseded by O14 for the parse family:** no `SANDBOX_PARSE` knob, no two substitution keys, no observation period; each runtime builds the sandbox parser from a required `SANDBOX_PARSE_URL`. The text below holds for the render and media families.

Each caller family reads one knob with `os.getenv` at its composition line (optional knob, per CLAUDE.md, not `load_settings()`):

| Knob | Read by | Values | Default during the transition |
|------|---------|--------|-------------------------------|
| `SANDBOX_PARSE` (A2) | each runtime at its own parse composition line: the indexer's (`indexer_main.py:138`, today `build_drive_models` with the parser aliased as splitter, `drive_model_setup.py:124`), the main service's `build_document_service` | `sandbox` \| `inprocess` | `inprocess` until its slice is accepted, then `sandbox` |
| `SANDBOX_RENDER` (built, `src/composition/sandbox_client_setup.py`; O15: DOCX, PDF and PNG) | the main service | same | same |

One knob per runtime governs both its parser and its splitter, so a runtime never parses in one place and cuts windows in another; one
`SandboxContentExtractor` object implements both ports, as the in-process adapter does today (EXTERNAL_FILES §4.9).

**Two substitution keys for the one variable.** `SANDBOX_PARSE` is the variable each runtime reads, but the deploy feeds it from two separate `.env`
keys and Cloud Build substitutions, one per runtime (the indexer's for the indexer's deploy step, the main service's for the main step), each mapped
onto `SANDBOX_PARSE` in its own step's `--set-env-vars`, as `_SANDBOX_RENDER` is today (`cloudbuild-dev.yaml:35`, default `:390`). Why: one shared key
would flip both runtimes at once, which breaks O9's per-part switch (S3 and S4 are accepted and rolled back apart), and a key reused across steps is
the deploy-substitution trap the root `CLAUDE.md` names (one local value baked into every step that reads it). The deploy test pins both keys, each
in its own step. Rejected:
`SANDBOX_DRIVE` and `SANDBOX_CHAT` (rev 3): with one op family the second would also govern drive and email reads in the main service, and its name would
say otherwise.

- **The knob rides as a Cloud Build substitution from `.env`**, not as a hand-set variable: `--set-env-vars` replaces the whole set at each deploy and a
  switch set with `gcloud run services update` would silently revert (the precedent is `_DRIVE_UPGRADE_PASS`, `cloudbuild-dev.yaml:133-136`, `:169`, `:381`). A test pins that each knob is
  present in the deploy step.
- **The assistant flips each switch after deploy and reports to the owner (O9); the owner can roll back with one command** (the knob set back to `inprocess`, §8).
- **There is no automatic fallback.** If the sandbox is down the caller fails, per §4.9. Falling back to in-process would parse hostile bytes in the credentialed
  runtime exactly when the boundary is unavailable. The only fallback is the explicit knob, a conscious deploy-time act.
- **On Cloud Run an unset knob is `sandbox`** once the slice is accepted. A missing sandbox URL **fails the start of the indexer** (background; a failed start is visible and harmless) but **degrades the main service**: it starts, logs ERROR, raises an alert, and answers an affected request with the converter-unavailable note (§4.9); the render knob already does this (`sandbox_client_setup.py:57-59`), because the main service also serves unrelated traffic. `inprocess` on Cloud Run logs an ERROR line at start.
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
| **O5** | **Every request runs on a fresh instance (`recycle=always`) for all ops, chat uploads and PNG widgets included; +1-3 s accepted; real latency measured after deploy** (§4.6). **Amended 2026-11-03: the light (parse) service uses `abnormal` (fresh instance only after an abnormal request); the render service keeps `always`** | owner (2026-10-20, 2026-11-03) |
| **O6** | **No egress for pulling large files.** HTTP/2 end to end is tried for bodies above 32 MiB; if it fails, 32 MiB stays a known limitation for parsed files, with no signed-URL fallback (§4.7) | owner (2026-10-20) |
| **O7** (superseded by O15-tunnel: no egress, no allowlist) | **The media op may have egress, only through the in-sandbox loopback proxy:** host allowlist by name suffix (never address pinning; raw-IP URLs refused; DNS per request); no redirects; the allowlist is configuration built from the real download-URL hosts seen in the transcripts measurement (drive RFC §27 T0), extendable without a code change (§4.5) | owner (2026-10-20) |
| **O8** | **Slice order and prerequisite:** `render.docx`, then the other renderers, then drive parsers, then chat uploads, then media; each part is enabled only after its caller's own service account exists (§7, §4.4). The parse slices' place is amended by O12 | owner (2026-10-20) |
| **O9** | **Per-part switch; two weeks of observation each; then the in-process path is removed.** The assistant flips each switch after deploy and reports; the owner can roll back with one command (§4.12, §8) | owner (2026-10-20) |
| **O10** | **Two services:** light (parsers, chat conversion, media audio, `ffprobe` of chat videos) and render (Chromium and Node: DOCX, PDF, PNG). `video_probe` moves to the light service; Gmail HTML cleaning stays in the main service by decision, a pure-Python parser (§4.2) | owner (2026-10-20) |
| **O11** | **The drive indexer is in the intended public-invoker set** (§8) as an accepted exception: internal ingress only, every caller's token verified in-app with a pinned audience; the per-runtime identity design relies on the same in-app check | owner (2026-10-20) |
| **O12** | **Order:** EXTERNAL_FILES_RFC's document service first (U0–U4, then U5); S3 (indexer) and S4 (main) after it, as adapter swaps under `ContentExtractorPort` and `PdfSplitterPort`; the parser child is the accepted residual until then, Google full-drive grants included; S1/S2 an independent track (header, §7) | owner (2026-11-03) |
| **O13** | **One parse engine in principle** (EXTERNAL_FILES D15): no `DocumentConverterPort` or `chat.convert`; one `parse.*` family for both runtimes; S4 = identity + knob; final go after that RFC's U0-b report on real files (header, §4.7) | owner (2026-11-03) |
| **O14** | **The parse switch is one deploy (2026-11-03).** No `SANDBOX_PARSE` knob, no watch period, no soak, no client slot counter (`SANDBOX_INDEXER_SLOTS` is dropped). The main service and the indexer build `build_sandbox_parser(url)` from a required `SANDBOX_PARSE_URL` and refuse to start without it, locally too; neither composition has an in-process fallback or a `K_SERVICE` branch. One retry on `recycling`; concurrency 1, max-instances 5 as a cost guard. Rollback is a Cloud Run revision rollback of both callers. | owner |
| **O15** | **One knob for the render family (2026-11-04).** The existing `SANDBOX_RENDER` / `SANDBOX_RENDER_URL` also selects the PDF runner and the widget PNG renderer; no new env key, no new substitution. Replaces O9's per-part switch for this family. `sandbox` with an empty URL: ERROR + degraded feature on Cloud Run, startup error locally; never a fallback | owner |
| **O15-tunnel** | **OQ-S11 closed with the reverse tunnel for every source (2026-11-04)**, OneDrive included; no range proxy, no host allowlist, no egress for the light service (§4.5) | owner |
| **O16** | **Media is one deploy, no switch (2026-11-04):** no `SANDBOX_MEDIA` knob; the indexer reads recordings only through the sandbox from the deploy on; the in-process media reader and its relay are deleted. Rollback is a revision rollback of the indexer | owner |
| **O17** | **Second user's transcripts (2026-11-04): enabled right after S5's acceptance**, through the per-connection opt-in (§9) | owner |
| P0 | Per-runtime identities are a prerequisite for each op family; caller groups must not share an identity (§4.4). Met in configuration | author, folded into O8 |
| P1 | A service, not a job (§4.1) | author |
| P4 | Platform IAM, in-app signature verification with pinned audience, per-op caller sets; not `--ingress=internal` (§4.4) | author |
| P6 | Every engine in a fresh child under an unprivileged uid with the full rlimit set; the server never touches untrusted bytes (§4.6) | author (design rule) |
| P8 | Existing ports get HTTP adapters; no omnibus port; with O13 no port is new here (the PDF ops' port comes from EXTERNAL_FILES U1) (§4.10) | author |
| P10 | The transcript gate for any second user lifts when media is accepted (S5), not before (§9) | author, follows O4 |
| A1 | The parse op family is named `parse.*` (§4.7) | author decision, rev 4 |
| A2 | One knob per runtime, `SANDBOX_PARSE`, for its parser and splitter; replaces `SANDBOX_DRIVE` / `SANDBOX_CHAT` (§4.12) | author decision, rev 4 |

## 6. Deltas: what this RFC decides beyond the owner's decisions

Numbered locally (S-Δn); they do not continue the drive RFC's Δ series.

- **S-Δ1. Audio stays with the caller.** `convert_file_to_text` holds an audio branch that would call `AudioTranscriptionPort` (an LLM provider call;
  `file_conversion.py:236-252`); in production it is never given one, so it answers "transcription is not available". Whatever EXTERNAL_FILES U3 decides for
  audio stays in the caller; no `parse.*` op transcribes.
- **S-Δ2. The plain-text fast path stays in the caller:** reading a UTF-8 file as text is not parsing (`file_conversion.py:253-268`). After EXTERNAL_FILES U5
  it covers the kinds with no converter (`text`, `markdown`, `json`, `code`, `log`); `csv`, `html`, `eml`, `rtf` and `subtitles` run a converter and go to
  `parse.*` (EXTERNAL_FILES A3). OQ-S6.
- **S-Δ3. `ExtractionRequest` is not serialized as is.** It carries `user_id`, `account_id`, `item_id`, `name` (`src/domain/drive_extraction.py`). The adapter strips them; billing fields
  (`model_calls`, `model_cost`, …) are never produced by the sandbox because they belong to the reader.
- **S-Δ4. The PDF ops join a port.** `PdfSplitter` moves from a Protocol in `composition/` to `ports/`, so the HTTP adapter can implement it without composition importing an adapter type.
  EXTERNAL_FILES U1 does it, before S3.
- **S-Δ5. Rendering is per request, and Chromium runs in a child under its own uid.** `PlaywrightHtmlRenderer` keeps one warm browser (`render` takes a lock, reconnects); the sandbox launches per request. Widget latency rises by the
  browser's start; accepted when measured (S2), with no change of policy (O5).
- **S-Δ6. The transcript child moves.** `SubprocessMediaExtractor` runs in the indexer's parser child today (`src/adapters/drive_parser/media_extractor.py:68`, with
  `media_launch.py` duplicating the parser's launcher until both move). Under this RFC the media child runs in the sandbox behind `MediaAudioPort`; the indexer keeps
  window planning, the Gemini upload, the job states and, for header-carrying sources, the bytes it sends (§4.5, OQ-S11). §27.4's hardening (Δ99: protocol and demuxer
  allowlists) is unchanged and applies inside the sandbox.
- **S-Δ7. `video_probe`** (`ffprobe` on bytes in the main service) joins C3 as `media.probe` over bytes. Low risk, low effort, moved into the light service (O10).
- **S-Δ9. Done in S1.** `verify_worker_oidc` takes a set of accepted emails (`src/web/worker_oidc_verifier.py:91`), `verify_oidc_identity` returns the verified email
  with a pinned audience (`:177`), and `SandboxCallerGate` maps it to op prefixes per group; `/worker` behaviour is unchanged.
- **S-Δ8. Deployment honesty.** The decision record `drive_parser_sandbox_resource_isolation.md` gets a status line at S3 acceptance (and its main-runtime counterpart at S4): superseded for the parser by this service; its
  description of what the child does and does not block remains true of the engine.

## 7. Slices

Order: O8 as amended by O12. Two tracks, independent of each other:
- **Renderers** (S1, S2): model-written code and HTML in the main service. S1 is built; S2 follows it.
- **Parsers and media** (S3, S4, S5): **after EXTERNAL_FILES_RFC's document service** (O12). S3 and S4 are adapter swaps under the document service's
  parse ports, `ContentExtractorPort` and `PdfSplitterPort`, with no caller change. S3 needs that RFC's U1 (`PdfSplitterPort`) and U3 (the request limits
  the adapter maps onto `limits`); S4 also needs U5 (one engine, so `parse.*` carries no converter op, O13). S5 swaps under `MediaAudioPort`, which exists.

Each slice is accepted by its tests, its owner steps, and a stated measurement. None edits an existing test to pass (the reviewer rule applies as everywhere).
An op family is enabled only when its caller's identity exists (P0, met) and its slice is accepted.

### S0. Spike (no product code; scratch deployment, results to the private spike folder)
Not run. Each item gates the slices named after it; S1's acceptance needs b, c, d, e, f.
a. **Large bodies** (S3, S4; S5 if media takes bytes, OQ-S11): a 100 MiB request (the PDF cap) and a 64 MiB response through Cloud Run to a Hypercorn
   app with HTTP/2 end to end, from an `httpx` HTTP/2 client; memory and the work volume bounded.
b. **Recycle** (every slice; S1 first): respond, then the supervisor stops the instance; no truncation; a fresh instance for the next call; cold start
   per image; **back-to-back bursts** (20 sequential and 5 parallel calls): rate of `recycling` answers, and whether `httpx` sees GOAWAY or
   `Connection: close`; the flush barrier and the drain time (`--no-cpu-throttling`, `decisions/sandbox_render_service.md`).
c. **Zero-role identity** (S1): the service starts, pulls its image, logs to stdout; the identity's attempts at a store, a secret and a bucket are
   refused; **its token gets 403 on every enabled generative, AI and maps API** (else §4.3: no-NAT egress earlier for non-media ops).
d. **Audit** (S1, then every deploy): no `allAuthenticatedUsers`; `allUsers` only on the intended public-invoker set (§8); record the command and result.
e. **Ingress and Authorization** (S1): the caller's token reaches the app intact in `Authorization`; signature and audience verify in-app; one test
   caller admitted, another refused; `--ingress=internal` behaviour from another Cloud Run service without VPC egress.
f. **Child uid on gen2** (S1; the Chromium part gates S2): engine children start under a separate uid with `RLIMIT_NPROC`/`NOFILE` applied; the
   in-memory volume is size-limited and writable by the child's uid only; **whether Chromium's own sandbox starts** on gen2 (outcome recorded either way).
g. **Equivalence** (S3: the drive evaluation corpus, `scripts/validation/drive_eval`; S4: the chat and email fixtures, which EXTERNAL_FILES U0-b already
   compares in process): in-process against deployed engines; byte-equal text expected; list differences.
h. **Limits on the new host** (S3): rerun the Phase 0 bomb set (`scripts/infrastructure/drive_extract_s1.py`) with the child limits; peak memory per case.
i. **No-NAT dry run** (S6): a deny-all egress deployment that still serves a request.

### S1. The service skeleton and `render.docx` — built, not deployed, not accepted
- Rationale: the model-written script is the least constrained input, and the skeleton (auth, contract, child launcher, recycle, deploy tests) is proved on
  a consumer with one op, bytes out, no large input.
- Built (`decisions/sandbox_render_service.md`): `src/domain/sandbox_contract.py`; `sandbox_main.py`; `src/web/sandbox_app.py` and the caller gate;
  `src/composition/sandbox_setup.py`; `Dockerfile.sandbox` (`render` target); the Cloud Build steps (`cloudbuild-dev.yaml:372-418`, a no-op while the
  sandbox's identity value is empty); the child launcher (`src/adapters/sandbox_engine/`); `src/adapters/sandbox_client/` with `SandboxHttpClient` and
  `SandboxDocxRunner`; `SandboxUnavailable`/`SandboxMisconfigured` (`src/domain/sandbox_errors.py`); main wiring behind `SANDBOX_RENDER`.
- Tests (built): contract suite (`inprocess` vs `sandbox` `DocxRunnerPort`: success bytes, script error text, timeout, empty output); the field set of a
  request is pinned (I3); caller gate (signature, audience, per-group prefixes, disjoint groups, fail closed); startup self-check; image-contents test;
  Cloud Build test (no `--set-secrets`, `--no-allow-unauthenticated`, no `--no-invoker-iam-check`, no public-invoker binding, the knob present); failure
  mapping incl. `recycling`; recycle (`always`, `never`); the architecture tests (the server imports no document, image or archive library; the clients
  parse no binary output); wire tests at the HTTP boundary.
- Accept: S0 b–f in hand; the owner steps (§8); DocGenerator e2e in `sandbox` mode; the engine child confirmed under its own uid.

### S2. The other renderers (C5, C6) — built on its branch, not deployed, not accepted
- Build: `render.pdf`, `render.png`; `SandboxPuppeteerRunner`, `SandboxHtmlRenderer`; one browser (chrome-headless-shell) in the `render` image, in a child under its own uid;
  Chromium's own sandbox tested on gen2 (S0-f) and the result recorded; the child boundary is the claim either way.
- Built (`decisions/sandbox_render_service.md` § S2): `HtmlRenderEngine` and `render_html_child.py` (`src/adapters/sandbox_engine/`), the fixed
  `pdf_generator/render_html.js`, the browser launcher and the two registry entries in `src/composition/sandbox_setup.py` (`StartupPlan.browser_cache_dir`, from `PUPPETEER_CACHE_DIR`),
  the two clients in `src/adapters/sandbox_client/`, the `render` target's browser and fonts. No new deploy key.
- Author deltas recorded here (engineering, S-Δ style): **D1** one browser, no Playwright in the render image (§4.2); **D2** `RLIMIT_DATA`, NPROC 512, NOFILE 1024 for the
  browser child (§4.6, §11); **D3** JavaScript off and every sub-resource blocked for PNG as well, PDF loses the in-process path's public hosts (§4.7); **D4** no engine text in
  `render.pdf` / `render.png` failures (§4.7); **D5** the clients turn sandbox errors into the ports' errors (§4.7); **D6** the PNG width travels in a body frame, not in `meta` (§4.7).
- Tests: contract suite per runner; no outbound connection from the render service; blocked-host test for the request filter; PNG width; HTML timeout; the image contains the
  browser executable (a missing browser answers `error/browser_unavailable` at request time, not at construction).
- Accept: widget render latency (cold) stated, measured at acceptance; PdfGenerator e2e; S0-f's Chromium-sandbox outcome recorded; image size recorded.
- Main-service choice (O15): the same `SANDBOX_RENDER` knob as S1 selects `SandboxPuppeteerRunner` / `SandboxHtmlRenderer` or the in-process pair (`build_pdf_runner`, `build_html_renderer`); no new key.

### S3. Drive parsers (C1) — after the document service (O12)
- Status: P4 built: both runtimes wired, not deployed (O14 supersedes the knob, slots and soak below for the parse family).
- What it is: an adapter swap in the indexer under the document service's parse ports: `SubprocessContentExtractor` → `SandboxContentExtractor`
  (`ContentExtractorPort` and `PdfSplitterPort`: one object serves both, as today), at the indexer's composition line (`indexer_main.py:138`). No caller changes.
- **P1 (service side) built, not deployed:** the three `parse.*` ops (§4.7) on the `light` target (`SANDBOX_SERVICE=light`, groups MAIN and INDEXER), over
  `drive_parser/child.py` unchanged; no client, no deploy step, no caller. The rest of this list is P2 and later.
- **P2 (client side) built, not wired:** `SandboxContentExtractor` (`src/adapters/sandbox_client/`) behind both parse ports, `build_sandbox_parser(url)`
  (`src/composition/sandbox_parse_setup.py`), and `SandboxHttpClient`'s file bodies, `windows` meta and deadline-capped retries. An outage of the service ends as
  `drive_extraction.ParserUnavailable` (busy | unavailable | misconfigured), never as the file's outcome: the document service answers the converter note, the indexer
  abandons the run uncounted and ends the tick. Nothing calls it (`SANDBOX_PARSE` and the indexer slots are P4). The reader cuts its windows in groups of `MODEL_IN_FLIGHT`, one
  `cut_pdf_windows` (`parse.pdf_windows`) request per group, never all at once and never one request per window; a half is cut from its window's file.
- Build: the `parse.*` ops (`parse.extract`, `parse.pdf_page_count`, `parse.pdf_windows`; one window is a batch of one) on the light image (the `light` target);
  `SandboxContentExtractor` (both ports, including `PdfSplitterPort`'s batch `cut_pdf_windows` → `parse.pdf_windows` and its deadline → `limits`);
  indexer wiring behind `SANDBOX_PARSE` (its own substitution key); indexer-side cap `SANDBOX_INDEXER_SLOTS`; the indexer's caller group: one
  `GROUP_PREFIXES` row (`src/composition/sandbox_setup.py:37`, today RENDER only; the RENDER-only start check `:87-91` widens with it) plus its identity.
- Needs: EXTERNAL_FILES U1 and U3; S0 a, b, g, h.
- Tests: contract suite on every `ExtractionStatus` path; envelope round trip incl. `brief` and `capture_signals`; the request's deadline mapped onto
  `limits` (only lowering, EXTERNAL_FILES §4.4); failure mapping (`recycling` uncharged; reset twice -> `ERROR sandbox_lost` charged); wire tests.
- Accept: S0-g equivalence on the drive corpus; one tick on the owner's drive in `sandbox` mode within the tick budget (drive RFC §7.1); zero
  `sandbox_lost`. Then `drive_parser_sandbox_resource_isolation.md` gets its status line (S-Δ8) and GOOGLE_DRIVE_RFC §10's residual ends for the indexer.

### S4. The main service's reads (C2) — after the document service, with one engine (O12, O13)
- Status: P4 built: both runtimes wired, not deployed (O14 supersedes the knob and soak below).
- What it is: the same swap in the main service's `build_document_service`, behind its `SANDBOX_PARSE` (its own substitution key). No caller changes:
  `PARSER_UNAVAILABLE` and its note already exist (EXTERNAL_FILES U3).
- Build: admit the main service's identity to the `parse` prefix (its caller group's `GROUP_PREFIXES` row gains `parse`); the degraded-start behaviour of the main service (§4.12). Nothing
  else: no converter port, no `chat.convert`.
- Needs: EXTERNAL_FILES U3 and U5 (D15's final go); S0 a, b, g.
- Tests: the document service's existing cases through both adapters; the audio path untouched (S-Δ1); the 5 MiB check before any call; the
  unavailable note; a hostile-archive case gives the same note as in process.
- Accept: added p95 latency for a typical 1 MiB docx stated to the owner; a scan opened in chat end to end within D7's deadline, through
  `parse.pdf_windows`, reached through `PdfSplitterPort.cut_pdf_windows` (EXTERNAL_FILES §4.4); Slack and Telegram e2e with docx, xlsx, pdf, csv.

### S5. Media for transcripts (C3)
- Status: **built, not deployed.** Built shape and deltas against rev 4 (design record: the S5 design, owner decisions O15-tunnel, O16, O17 below):
  1. OQ-S11 closed with the **reverse tunnel for every source**, OneDrive included (owner approval 2026-11-04, after a spike proved the route).
  2. §4.5's range proxy and O7's host allowlist dropped; the light service needs no egress.
  3. No `SANDBOX_MEDIA` knob; the O14 pattern (a required URL; rollback is a revision rollback); the §4.12 table row and §9 guard 2 are gone.
  4. A loopback range server inside the media child, not a server-side helper.
  5. The request is a START body frame, not new meta keys.
  6. Tunnel ops: the response header means "stream opened", the terminal RESULT frame decides (§4.7).
  7. The media ceiling (240/150 s) is separate from the parse ceiling (120/90 s).
  8. A systemic outage is raised as `ParserUnavailable` (not `SandboxUnavailable`); same effect, no stage change.
  9. `video_probe` -> `media.probe` is split to S5b (the main service gains the `media` prefix with an in-memory range source).
  10. The in-process media path (`media_extractor.py`, `media_launch.py`, `media_relay.py`) is deleted in the same deploy; O9's two-week keep does not apply.
  11. ffmpeg enters the light image, so the parse cold start may grow (measured in acceptance).
  12. `MediaUnreadable("tunnel_violation")` is charged to the file.
  13. Byte budget per call `min(8 GiB, 2 x size + 64 MiB)` and at most 256 opens (the old relay had 8 GiB per run and no open cap).
  14. No token refresh between ranges.
- What it is: an adapter swap in the indexer under `MediaAudioPort`: `SubprocessMediaExtractor` -> `SandboxMediaAdapter`. The indexer holds the source and
  answers range requests over the tunnel; the sandbox decodes.
- Image: the light target adds ffmpeg (`apt-get install --no-install-recommends`), `media_engine.py`, `media_child.py` and the tunnel closure
  (`sandbox_tunnel.py`, `sandbox_slot.py`, `sandbox_tunnel_app.py`; the render target copies the tunnel closure too, since `sandbox_main.py` is shared). The
  indexer's caller group gains the `media` prefix (MAIN keeps `parse` only until S5b). The deploy script is unchanged: timeout 900 s covers the 240 s ceiling
  and the 320 MiB volume is enough (nothing is written but window files).
- Tests: the tunnel codec and bounds; the engine with a scripted child; the child's loopback server; the `@slow` contract suite (real server, real
  ffmpeg, a local HTTPS Range upstream) asserting outcomes equal the old in-process extractor on the same files.
- Accept: acceptance in the cloud on one OneDrive and one Google recording, wall and bytes read against the old path, and the light cold start before and after ffmpeg.

## 8. Owner steps (placeholders; nothing here names a real resource)

0. **Prerequisite:** the per-runtime identities for the callers of the family being enabled (P0, O8): in place for every runtime.
1. **Create two identities** (`<sandbox-sa>`, `<sandbox-render-sa>`) and grant **nothing**. Verify: `gcloud projects get-iam-policy <PROJECT_ID> --flatten=bindings[].members --filter="bindings.members:<sandbox-sa>"` is empty.
2. **Audit once, then each deploy:** `gcloud projects get-iam-policy <PROJECT_ID>` contains no `allAuthenticatedUsers`; list service-level policies for the same. The **intended public-invoker set** is the main service's webhook, OAuth, Cabinet and MCP surface, the voice relay, and the drive indexer, and nothing else; the sandbox services are never in it. The indexer is in the set as an accepted exception (O11): it takes internal traffic only and verifies every caller's token in-app with a pinned audience, because Cloud Tasks' token audience (`{url}/worker`) is one platform IAM would not accept.
3. **Deploy the services** (a Cloud Build step each, no secrets, `--no-allow-unauthenticated`, `--service-account=<sandbox-sa>`, concurrency 1, `--use-http2`).
4. **Grant invoker:** `roles/run.invoker` on each sandbox service to the caller runtimes' identities only (indexer for the light service, main for both).
5. **Set the caller groups and URL values** in `.env` (`SANDBOX_CALLERS_<GROUP>` per runtime, the service URLs) as substitutions; never in the repository.
6. **Flip one knob per slice** after its acceptance; one soak window before S6.

Rollback: for the parse family (O14) a Cloud Run revision rollback of the main service and the indexer, then a revert. For the other families: set the family's knob back to `inprocess` and deploy the caller (a substitution, not a hand edit, §4.12). The sandbox services can stay up unused. To stop one op family reaching a sandbox that misbehaves,
change the caller map in the **sandbox's** environment and **redeploy the sandbox** (the map is its environment, not the callers').

## 9. What this unblocks: transcripts for a second user (USER_DRIVE_INDEXING §27.12, OQ-T3)

§27 holds transcription to the owner only, "until the zero-credential parser service exists" (Δ125; T1 live for the owner). The opt-in is per drive connection
(`DriveConnection.transcribe_enabled`, `src/domain/drive_connection.py:64`), together with the global `DRIVE_TRANSCRIBE_ENABLED`
(`src/web/user_cabinet_app.py:1506-1509`). When S5 (media) is accepted: the indexer no longer decodes media in-process; the decoder runs in a service that
holds nothing and sees one recording; a URL or the recording's bytes are the only things handed in. The gate then changes from "owner only" to that existing
**per-connection explicit opt-in**, no new mechanism. One guard ships with S5 so the gate means what it says: the in-process media path is deleted, so the only media path in a deployed indexer is the sandbox (there is no `SANDBOX_MEDIA` knob to leave on `inprocess`). **Owner decision O17 (2026-11-04): transcripts are enabled for a second user right after S5's acceptance.** The same argument lifts the parser residual in `drive_parser_sandbox_resource_isolation.md` once S3 is accepted, which is that record's stated trigger.

## 10. Questions: resolved by the owner (2026-10-20) and what remains

Resolved (see §5):
- **OQ-S1 cold start per request:** accepted for all ops (O5).
- **OQ-S2 large bodies:** try HTTP/2; otherwise 32 MiB stays a known limit; no signed-URL fallback (O6).
- **OQ-S3 media egress:** none: the reverse tunnel (O15-tunnel) supersedes the proxy and O7.
- **OQ-S4 one service or two:** two (O10).
- **OQ-S5 `video_probe`:** moves to the light service (O10).
- **OQ-S7 soak and who flips:** two weeks per part; the assistant flips and reports; the owner can roll back with one command (O9).
- **OQ-S9 Gmail HTML cleaning:** stays in the main service by decision (O10).
- **OQ-S10 slice order:** as in O8.

Remaining engineering points (no owner input needed; the author's call, stated for the record):
- **OQ-S6. The plain-text fast path** stays in the caller (S-Δ2): moving a trivial UTF-8 read buys nothing and adds a hop to the commonest upload.
- **OQ-S8. Per-request signed grants** instead of caller-level invoker: no; the invoker binding plus the verified per-op set is enough while callers are two runtimes.

Open (new in rev 4):
- **OQ-S11. Resolved (2026-11-04): the reverse tunnel, for every source** (§4.5, §4.7, §7 S5; O15-tunnel). A spike proved the route. Candidates (a) one long upload, (b) per-window byte ranges sent by the caller and (c) a pull URL served by the caller were not taken.

## 11. Parameters (initial)

| Name | Initial | Basis |
|------|---------|-------|
| `SANDBOX_RECYCLE` | `always` (`never` local only; no other value) | I2/I6; measured in S0-b |
| light: memory / cpu / concurrency / max-instances / timeout | 2 GiB / 2 / 1 / 5 / 900 s | indexer's measured host; ffmpeg CPU (§27.2) |
| render: memory / cpu / concurrency / max-instances / timeout | 2 GiB / 2 / 1 / 3 / 300 s | Chromium; `node_timeout_s` 60 s, PDF similar |
| child limits (parsers) | `RLIMIT_AS` 1 GiB, `RLIMIT_CPU` 90 s, wall 120 s, `RLIMIT_FSIZE` 64 MiB | current values; S0-g re-measures |
| media child (per §27.4) | `RLIMIT_CPU` 150 s, wall 240 s, `RLIMIT_FSIZE` 64 MiB, `RLIMIT_AS` 1 GiB | §27.4 |
| request / response cap | request 101 MiB (100 MiB + 1 MiB), response 101 MiB, streamed (built in P1) | the largest admitted kind, a 100 MiB PDF (`drive_index_params.py:57`), plus margin; the work volume is sized for it (S0-a) |
| `meta` size | 8 KiB | |
| client timeout | child wall + 15 s | |
| retry | one immediate retry on a lost connection or 5xx; none on 4xx | §4.9 |
| media URL host allowlist | none (superseded by the reverse tunnel) | O15-tunnel, §4.5 |
| `SANDBOX_INDEXER_SLOTS` | superseded by O14 (dropped) | none: no client slot counter |
| engine child limits added | `RLIMIT_NPROC` 64, `RLIMIT_NOFILE` 64 (chosen in the S1 build; S0-f confirms) | fork bomb, descriptor exhaustion |
| browser child (`render.pdf`, `render.png`) | `RLIMIT_DATA` in place of `RLIMIT_AS`; `RLIMIT_NPROC` 512, `RLIMIT_NOFILE` 1024 (chosen in the S2 build; S0-f confirms); `CHROMIUM_OWN_SANDBOX` off until S0-f | Chromium reserves tens of GiB of address space and starts tens of threads; the instance's 2 GiB is the real wall |
| `render.png` width | 16..2048, in the body frame | `sandbox_png_frame.py` |
| observation before removing in-process | 14 days per part (superseded by O14 for the parse family) | O9 |

## 12. Where the current code disagrees with the design (so the plan does not assume otherwise)

Re-verified at `2280ca78`. Rev 3's item 7 (`/extract` reserved on the indexer) is fixed: the docstring now points here (`src/web/indexer_worker_app.py:23`).

1. **The drive's caps exceed the HTTP/1 request limit**: 45 MiB for most kinds and 100 MiB for PDFs in the indexer (`drive_index_params.py:56-57`,
   `drive_index_policy.py:159`; §4.7); the design depends on HTTP/2 being proved at 100 MiB first (S0-a, not run).
2. **`convert_file_to_text` holds an audio branch** for an LLM provider call (`file_conversion.py:236-252`); it is never given a transcriber in production, and
   it cannot be moved whole (S-Δ1).
3. **`ExtractionRequest` carries identity** (`user_id`, `account_id`, `item_id`, `name`; `drive_extraction.py:50-59`); a naive serialization would break I3 (S-Δ3).
4. **The PDF page ops are outside `ContentExtractorPort`**; the reader reaches them through a Protocol in `composition/` (`drive_reader_runner.py:73`;
   S-Δ4, EXTERNAL_FILES U1).
5. **`HtmlRendererPort` has a warm-browser lifecycle** (`start`/`stop`, `html_renderer_port.py:36`, `:41`; stopped at `application_graph.py:201-202`); the
   sandbox renders per request (S-Δ5). Composition also holds the concrete `PlaywrightHtmlRenderer` type (`application_graph.py:181`, built `:959`), not the port.
6. **One image serves every credentialed runtime.** Main, indexer, research job and relay run the same image (`cloudbuild-dev.yaml:16`, `:33`, `:158`,
   `:213`, `:261`), built from `Dockerfile`, which installs ffmpeg, Node and two Chromium builds (`Dockerfile:13`, `:25-26`, `:36`). Only the sandbox has its own
   (`Dockerfile.sandbox`). Removing them requires a requirements split (S6-a).
7. **`video_probe` is an unlisted consumer** in the main service (`video_probe.py:21`, used at `video_generation_agent.py:36`; S-Δ7).
8. **`PuppeteerRunnerPort` named a future Cloud Function** (closed in S2: it now names `SandboxPuppeteerRunner`, `puppeteer_runner_port.py:6-10`); `DocxRunnerPort` names the built
   `SandboxDocxRunner` (`docx_runner_port.py:6-10`). Their timeout arguments become the request's `limits`.
9. **The main service's reads have five converter call sites** (§1, C2), the e-mail attachment path among them (`email_search_service.py:251`); EXTERNAL_FILES
   U3 replaces all five with its document service before S4.
10. **Media decoding runs in the indexer today**, for the owner (T1), through `SubprocessMediaExtractor` and, for Google, a `LoopbackMediaRelay` inside the
    token-holding process (`media_extractor.py:68`, `media_relay.py:82`); the media launcher duplicates the parser's (`media_launch.py`). S5 assumes this
    code, not a future one (S-Δ6).
11. **The media op's URL premise holds for OneDrive only.** A Google source needs the `Authorization` header (GOOGLE_DRIVE_RFC G2), so §4.5's proxy cannot
    serve it (OQ-S11).
12. **`ContentExtractorPort`'s docstring names no separate-service adapter** (`src/ports/content_extractor_port.py:1-6`), unlike the two runner ports; S3
    updates it with `SandboxContentExtractor`.
13. **`GROUP_PREFIXES`'s comment plans `drive`/`chat` prefixes** (`src/composition/sandbox_setup.py:35-36`: "INDEXER -> drive, media; MAIN -> chat");
    with O13 the rows are INDEXER -> `parse`, `media` and MAIN -> `parse`, `render`. S3 edits it with the row it adds.
14. **The in-process HTML paths filter less than this RFC's first reading implied.** `runner.js` blocks private and metadata hosts but lets public hosts through;
    `PlaywrightHtmlRenderer` has no request filter and runs JavaScript on. The widget and PDF prompts forbid scripts and remote loads; nothing enforces it in process. The sandbox engine
    enforces it (S2 delta D3). Hardening the in-process renderer is a separate, optional change.
15. **`Dockerfile.sandbox`'s header comment** named only the Node DOCX engine; it now names the browser and fonts of the `render` target.

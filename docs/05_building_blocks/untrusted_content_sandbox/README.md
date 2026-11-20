<!-- living-doc
covers:
  sandbox_main.py
  src/adapters/sandbox_engine/
  src/adapters/sandbox_client/__init__.py
  src/adapters/sandbox_client/bytes_range_source.py
  src/adapters/sandbox_client/media_range_source.py
  src/adapters/sandbox_client/metadata_id_token_provider.py
  src/adapters/sandbox_client/sandbox_content_extractor.py
  src/adapters/sandbox_client/sandbox_html_renderer.py
  src/adapters/sandbox_client/sandbox_http_client.py
  src/adapters/sandbox_client/sandbox_media_adapter.py
  src/adapters/sandbox_client/sandbox_render_outcome.py
  src/adapters/sandbox_client/sandbox_tunnel_client.py
  src/composition/sandbox_*.py
  src/domain/sandbox_*.py
  src/web/sandbox_*.py
  scripts/infrastructure/sandbox_spike/
-->
# Untrusted-Content Sandbox

## Purpose

Everything the product does with bytes it did not write (a drive file, an email attachment, a recording, a script or
HTML page a model wrote) runs in **separate Cloud Run services that hold nothing**: no secret, a service account meant to
hold no IAM role, no network egress (the design intent, per the RFC; the deploy scripts and tests enforce the pieces named
under Invariants, the IAM and VPC state itself is cloud state). A bug in a parser, ffmpeg, Node or Chromium then reaches a process that has nothing to
steal, instead of the main service or the indexer, which hold every user's credentials and index.

There are two services built from one entry point (`sandbox_main.py`), selected by `SANDBOX_SERVICE`:

| Family | Op prefixes | Callers | Recycle |
|---|---|---|---|
| `light` | `parse`, `media` (the tunnel ops) | main service, indexer | `abnormal` |
| `render` | `render` | main service | `always` |

The ops themselves are `src/domain/sandbox_contract.py::SandboxOp`; which engine serves which is
`src/composition/sandbox_setup.py::build_engine_registry` and `build_tunnel_registry`.

What it is not: it is not a place that decides anything. It returns parsed text, rendered bytes or media windows; the
caller treats every byte of the answer as untrusted, exactly as before. It stores nothing and knows no user. Widget PNGs
are **not** rendered here: the main service renders them in-process (owner ruling O16, see the RFC), so `render.png` and its client `SandboxHtmlRenderer` are built and tested but unwired
(`src/composition/sandbox_client_setup.py::build_html_renderer`).

## Owned data

None persistent. The service has no database, no bucket and no cache (invariant I6: stateless). What exists is per request
and removed with it:

- a request directory under the work root (`SANDBOX_WORK_ROOT`, an in-memory volume on Cloud Run) holding the streamed
  body and whatever the engine child writes; the child's uid owns it, so **the server never trusts a path inside it**;
- an output directory under the server-only output root (`SANDBOX_OUTPUT_ROOT`, closed to group and other, refused at start when it lies
  inside the work root, `src/composition/sandbox_setup.py::_prepare_output_root`), where the engine copies the validated result and from
  which the response is streamed.

Configuration is environment only: `SANDBOX_URL` (own URL, the token audience), `SANDBOX_CALLERS_<GROUP>`,
`SANDBOX_RECYCLE`, the roots and the engine user. These are optional knobs read from the mapping passed in, never through
`load_settings()`. On the caller side: `SANDBOX_RENDER_URL` (read with `getenv`, optional) and `SANDBOX_PARSE_URL`
(the light URL; a `load_settings()` key in the main service, an environment read in the indexer; required by the main service and the indexer). Deploy keys and owner steps:
[`docs/07_deployment/README.md`](../../07_deployment/README.md) § Sandbox render service, § Sandbox light (parse) service,
§ Sandbox network.

## Flows

### Start-up (two processes in one file)

1. `sandbox_main.py::supervise` (the container's PID 1) calls `src/composition/sandbox_setup.py::validate_startup`: refuses a secret-shaped
   environment name or `GOOGLE_APPLICATION_CREDENTIALS` (`src/domain/sandbox_startup_check.py::check_sandbox_environment`), an unknown
   family or recycle policy, `never` on Cloud Run, `abnormal` outside the light family, and caller groups that do not
   belong to the family.
2. On Cloud Run it makes `/tmp` and `/dev/shm` root-only (`src/composition/sandbox_setup.py::close_shared_temp_dirs`; the platform mounts
   world-writable tmpfs over both and the engine uid would otherwise have memory outside every limit) and refuses to start
   when that does not hold. Off Cloud Run it touches nothing.
3. It spawns `python sandbox_main.py --serve` (`make_spawn`) in its own process group with the write end of a pipe, and
   imports no payload code itself (`src/web/sandbox_supervisor.py::SandboxSupervisor`).
4. `sandbox_main.py::build_app` builds the engines (`build_engine_registry`, `build_tunnel_registry`), the caller gate and
   the Quart blueprint; on the light family it also puts the raw ASGI tunnel route in front of Quart
   (`src/web/sandbox_tunnel_app.py::dispatch_tunnel`). An empty `SANDBOX_URL` is not a refusal: the service comes up
   `unconfigured` (only `/health`, every op answers 503) so the first pass of the two-pass deploy, which learns the URL,
   can succeed. An empty caller set on Cloud Run **is** a refusal.

### One call (parse and render ops)

`POST /v1/<op>` in `src/web/sandbox_app.py::create_sandbox_blueprint`, in this order:

1. **Gate.** `src/web/sandbox_caller_gate.py::SandboxCallerGate.authorize` verifies the Google ID token in `Authorization`
   (signature, `email_verified`, audience equal to the service's own URL: `src/web/worker_oidc_verifier.py::verify_oidc_identity`),
   maps the email to its group's op prefixes and refuses a prefix outside them: 401 unauthenticated, 403 `forbidden_op`.
   The gate knows no op by name. The op arrives raw from the path and is authorized **before** it is validated.
2. Unknown op 404. Then `src/web/sandbox_slot.py::SandboxSlot.try_acquire`: a busy or draining instance answers 503 `recycling` (`Connection: close`).
3. `X-Sandbox-Meta` is decoded against a closed schema, limits are clamped to the service ceiling (a caller can only lower
   them), the body is streamed to the request directory counting bytes itself.
4. The engine runs (below). The answer always carries `X-Sandbox-Outcome`; the body is the output bytes or, on failure, a
   text capped at `ERROR_TEXT_CAP_BYTES`.
5. After the answer a done-callback signals `src/web/sandbox_recycler.py::PipeRecycler.request_served`; the supervisor stops the server and exits so the
   platform replaces the instance.

**Recycle policy.** Under `always` every request that began writing payload bytes ends the instance. Under `abnormal` (light
only) an `ok` answer keeps it warm, including a parser verdict such as `no_text` or `encrypted` carried inside it; any other
end after payload bytes (engine non-ok, engine exception, body too large or too slow, internal error, a cancel before an
answer) recycles it, and so does an `ok` after which a process under the engine uid still lives (it escaped the child's
process group): `src/web/sandbox_app.py::kill_uid_processes` kills it and the instance ends. A refusal before any payload byte
recycles nothing.

### Engine child

Every engine runs in a **fresh child**, never in the server (invariant I7: the process that accepts requests never touches
untrusted bytes).

1. `src/adapters/sandbox_engine/child_launcher.py::ChildLauncher.run` starts it under the unprivileged uid
   (`DEFAULT_ENGINE_USER`, resolved at start; a missing user, root or an unswitchable uid refuses start, never falls back),
   no supplementary groups, a fixed environment allowlist (`TMPDIR` and `HOME` are its workdir), its own session and
   process group (killed whole at the wall clock). Its stderr file is never returned or logged, only length and SHA-256 (`render.docx` returns the script's own error text, see Invariants).
2. `src/adapters/sandbox_engine/limits_exec.py` applies the rlimits (memory, CPU, file size, `NPROC`, `NOFILE`, core dumps off) **before** the engine loads, then
   execs it, so a parser or Node starts already limited.
3. The engine reads the child's output only through descriptors opened relative to one directory fd with `O_NOFOLLOW |
   O_NONBLOCK`, `fstat`-ed to a single-link regular file within the cap, and copies it into a fresh directory under the
   output root. The response streams from that copy.

| Engine | Child | Note |
|---|---|---|
| `src/adapters/sandbox_engine/parse_engine.py::ParseEngine` (one per `parse.*` op) | `parse_child.py` runs the drive parser's own child (`drive_parser/child.py`) unchanged; `pdf_windows` runs one child per window | the parser's own status (`text`, `no_text`, `encrypted`, ...) is the `detail` of an `ok` answer, in the JSON body (`src/domain/sandbox_parse_result.py`) |
| `src/adapters/sandbox_engine/docx_render_engine.py::DocxRenderEngine` | `render_docx_child.py` writes the shared security prelude plus the model's script and execs `node` | `RLIMIT_DATA`, not `RLIMIT_AS` (`DOCX_MEMORY_MODE`) |
| `src/adapters/sandbox_engine/html_render_engine.py::HtmlRenderEngine` | `render_html_child.py` execs the fixed Puppeteer script on chrome-headless-shell | own launcher: `RLIMIT_DATA`, higher `NPROC` / `NOFILE` (`BROWSER_*`) |
| `src/adapters/sandbox_engine/media_engine.py::MediaEngine` | `media_child.py`: the drive parser's media code against a loopback range server inside the child | tunnel op, see below |

### Wire contract

`src/domain/sandbox_contract.py`: the request is the raw body plus one header, `X-Sandbox-Meta` (base64url JSON), the answer is
a body plus `X-Sandbox-Outcome`. Both envelopes are **closed schemas**: a key outside the set is an error, which is how I3
(no user id, account id, item id, file name or path ever reaches the service: only a random `request_id`, limits, `kind`,
`ext` and, for `parse.pdf_windows`, the page-window plan) is enforced on the wire; `tests/unit/domain/test_sandbox_contract.py` pins the field sets. The header decides the
status; the HTTP code is for humans and proxies. Binary body framings are `src/domain/sandbox_docx_frame.py`, `src/domain/sandbox_png_frame.py` and
`src/domain/sandbox_windows_frame.py`; the server streams a body to a file untouched and only the engine child splits it. `render.pdf`'s body is the HTML as is.

### Media tunnel (`media.*`)

A recording never travels as bytes in one request, nor as a URL or credential. The indexer (or the main service, for the
duration probe of an edit-video source it already holds in memory) opens **one full-duplex HTTP/2 stream** and plays the
byte-range server for it.

1. `src/adapters/sandbox_client/sandbox_media_adapter.py::SandboxMediaAdapter` learns the size from one upstream range, then
   `src/adapters/sandbox_client/sandbox_tunnel_client.py::SandboxTunnelClient.run` sends the headers (`request_id` and limits only) and a START frame
   with sizes and the plan. It speaks raw HTTP/2 (`h2`), because `httpx` sends a whole body before it reads.
2. `src/web/sandbox_tunnel_app.py::SandboxTunnelApp` (raw ASGI: Quart would buffer and cap an open-ended body) hands START to
   `MediaEngine`. `media_child.py` serves ffmpeg on `127.0.0.1` with a nonce path, GET/HEAD and a single range only; each
   ffmpeg read becomes an OPEN record on a pipe, which the route forwards as an OPEN frame.
3. The client answers each OPEN from the drive (`src/adapters/sandbox_client/media_range_source.py::HttpRangeSource`, or `bytes_range_source.py` for
   memory) with DATA frames and END, and the drive URL and bearer never leave the caller. The sandbox sees offsets and bytes.
4. After the run the route sends one WINDOW frame per Opus window and a RESULT frame (the verdict). The **caller closes
   first**: on RESULT it ends its request half and sends nothing more; the route ends the response only after that.
5. Frame codecs, bounds and the ledger (`TunnelLedger`: opens, open streams, bytes) are in `src/domain/sandbox_tunnel.py`. Both
   sides run the ledger; the caller's is authoritative, because a compromised sandbox is the threat.

### Caller side

- `src/adapters/sandbox_client/metadata_id_token_provider.py::MetadataIdTokenProvider` mints the ID token (audience = the sandbox URL, normalised once);
  it is cached until shortly before expiry and never logged. A metadata-server blip is `SandboxUnavailable`, not a fault.
- `src/adapters/sandbox_client/sandbox_http_client.py::SandboxHttpClient.call` is the one HTTP caller of parse and render ops. It classifies **by HTTP
  status first, then the outcome header** (Cloud Run's own IAM 401/403 carry none). Misconfiguration (401, 403, 404, 405) is
  `SandboxMisconfigured`. A 503 `recycling` or a 503 without the sandbox's header (an instance that was exiting) is retried
  once and, if it persists, ends as `SandboxUnavailable` (`busy` or `unavailable`): never charged to the input. A reset,
  timeout or other header-less 5xx is retried once on a fresh instance and then ends as outcome `error` / `sandbox_lost`,
  which **is** a verdict. Retries stay inside the caller's deadline (`MAX_RECYCLING_ATTEMPTS`, `MAX_LOST_ATTEMPTS`).
- `src/adapters/sandbox_client/sandbox_content_extractor.py::SandboxContentExtractor` is one object behind `ContentExtractorPort`, `PdfSplitterPort`
  and `ContainerFactsPort`; `sandbox_docx_runner.py` and `sandbox_puppeteer_runner.py` implement the DOCX and PDF runner
  ports; `sandbox_render_outcome.py` maps a failed render to the port's own fixed error text.
- Wiring: `src/composition/sandbox_client_setup.py` (DOCX and PDF; an empty `SANDBOX_RENDER_URL` degrades those two features
  with an ERROR, nothing else), `src/composition/sandbox_parse_setup.py::build_sandbox_parser` and `src/composition/sandbox_media_setup.py::build_sandbox_media`
  (built from `SANDBOX_PARSE_URL` in `composition/boot_setup.py` and in `indexer_main.py`; an empty URL refuses start).

## Invariants and traps

- **Outage is never a file's fault.** `SandboxUnavailable` / `SandboxMisconfigured` end as
  `ParserUnavailable` (`charges_file = False`): the file is not charged an attempt and the next tick retries it. A lost
  stream twice in the tunnel is charged (`MediaUnreadable`), a `recycling` 503 is not. A **bare 5xx without the sandbox's
  outcome header** is a lost instance, not a verdict on the input.
- **No engine text leaves except a script's own failure.** `render.docx` returns the model's script error (capped) so the
  model can repair it; every other op answers a fixed detail code. A browser's or parser's stderr can quote the document
  or an attacker's URL. A script can forge `bad_request`, so callers do not read it as "not the script's fault"
  (`SERVER_ONLY_BAD_REQUEST_DETAILS` in `src/adapters/sandbox_client/sandbox_http_client.py` are the details only the server produces).
- **The workdir belongs to the attacker.** Any code that reads a file the child wrote does it through the validated
  descriptor discipline above; a symlink, FIFO, device or hard-linked file is refused. Do not add a path-based read.
- **Isolation tests, not trust.** `tests/unit/test_sandbox_architecture.py`: server, client and engine modules import no
  document, image or archive library, clients never decode a reply body (they use the domain codecs), and no server,
  composition or engine code opens an outbound connection (the clients do by design; the one documented server-side
  exception is the OIDC verifier's fetch of Google's public keys).
  `tests/unit/test_sandbox_import_closure.py`: `sandbox_main.py` reaches no agent, service, handler, infrastructure,
  Firestore repo or model SDK. `tests/unit/test_dockerfile_sandbox.py`: the image copies an allowlist of paths.
- **Zero role means zero role.** Cloud Build tests refuse a `--set-secrets` or a public invoker on a sandbox step, and the
  deploy scripts refuse a service account whose name does not carry the sandbox prefix (`test_cloudbuild_sandbox*.py`).
  To cut a caller off, remove its `run.invoker` binding. **Never empty a caller list for that**: an empty list on Cloud
  Run refuses the new revision, so the old one would keep serving (RFC and deploy notes).
- **Caller sets are `:`-separated and disjoint.** A comma is refused (the values travel through Make, the shell and Cloud
  Build); one email in two groups refuses start (`src/composition/sandbox_callers.py::load_caller_sets`, `assert_disjoint`). `SandboxCallerGate` itself refuses every call on a partial configuration (a map without a URL, or the reverse); through
  `sandbox_main.py` callers without a URL come up `unconfigured` and a URL without callers refuses start. The gate is open only when `K_SERVICE` is
  unset and nothing at all is configured, and then the server binds loopback (`sandbox_main.py::bind_host`).
- **Node and Chromium cannot live under `RLIMIT_AS`.** They reserve address space they never commit. Both launchers bound `RLIMIT_DATA` instead
  (`DOCX_MEMORY_MODE`, `BROWSER_MEMORY_MODE`; reasons in `sandbox_render_service.md`); the instance's
  memory, with one request per instance, is the real wall. Chromium's own sandbox is off by the code constant
  `CHROMIUM_OWN_SANDBOX`: the claim is uid separation, zero role and no network, not Chromium's sandbox.
- **No egress, so nothing may be fetched.** Render input travels in the request; Chromium aborts every request except
  `data:`, `about:` and `blob:`; media comes back over the caller's tunnel. A change that makes an op fetch a URL breaks
  the network lock-down.
- **Residuals the owner accepted:** a grandchild that calls `setsid()` leaves the process group and survives its kill
  (recycle bounds it to one instance; under `abnormal` the uid sweep above ends the instance); pid reuse after the leader is
  reaped is negligible at concurrency 1.
- **Deploy order.** A new op is usable by a caller only after the sandbox revision that serves it is live. The macro check of
  an email attachment (`parse.container_facts`) **fails closed**: an answer of `error` or `too_large`, or no answer, refuses
  the send as "could not be checked".
- **The deploy keys of the two services differ.** Render takes its callers from `SANDBOX_CALLERS_RENDER`; light from
  `SANDBOX_CALLERS_MAIN` and `SANDBOX_CALLERS_INDEXER`. Both runtimes that call light share `SANDBOX_PARSE_URL`, and the
  media adapter uses that same URL (no separate media knob).

## Links

- RFC (design record, with the owner rulings O1-O18): [`UNTRUSTED_CONTENT_SANDBOX_RFC.md`](../../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md);
  its spike scripts are in `scripts/infrastructure/sandbox_spike/`.
- Decisions: [`sandbox_render_service.md`](../../04_solution_strategy/decisions/sandbox_render_service.md) (skeleton, limits
  and the live failures behind them), [`sandbox_media_tunnel.md`](../../04_solution_strategy/decisions/sandbox_media_tunnel.md)
  (why a reverse tunnel, and the close-order fix), [`drive_parser_sandbox_resource_isolation.md`](../../04_solution_strategy/decisions/drive_parser_sandbox_resource_isolation.md)
  (the residual risk this block closed), [`worker_oidc_and_docx_sandbox.md`](../../04_solution_strategy/decisions/worker_oidc_and_docx_sandbox.md)
  (the OIDC verifier the gate reuses), [`email_send_macro_check_in_sandbox.md`](../../04_solution_strategy/decisions/email_send_macro_check_in_sandbox.md),
  [`runtime_service_accounts.md`](../../04_solution_strategy/decisions/runtime_service_accounts.md) (one identity per runtime, which
  makes per-runtime caller groups possible).
- Neighbours: [User Drive](../user_drive/README.md) (the indexer that parses and transcribes through this block),
  [File Conversion](../file_conversion/README.md), [Document Generation](../document_generation/README.md) (DOCX and PDF render).
- Deployment: [`docs/07_deployment/README.md`](../../07_deployment/README.md) § Sandbox network, render service, light service.

## Where to start reading

- `sandbox_main.py::build_app` and `supervise`: the two processes and what refuses start.
- `src/web/sandbox_app.py::create_sandbox_blueprint`: the one call, gate to recycle.
- `src/adapters/sandbox_engine/child_launcher.py::ChildLauncher` and `limits_exec.py`: what a child is and is not.
- `src/domain/sandbox_contract.py`: the closed envelopes and the limits ceilings.
- `src/adapters/sandbox_client/sandbox_http_client.py::SandboxHttpClient.call`: how callers classify every failure.
- `src/web/sandbox_tunnel_app.py::SandboxTunnelApp` with `src/domain/sandbox_tunnel.py`: the media tunnel.

# Decision: Untrusted-content sandbox, slice S1 — a separate zero-role service runs `render.docx`

**Date:** 2026-10-20
**Status:** Built on `feat/sandbox-s1`, behind `SANDBOX_RENDER` (default `inprocess`). **Not accepted**: acceptance needs the S0
spike results, the owner steps and the owner's go. Not deployed.
**Context:** [UNTRUSTED_CONTENT_SANDBOX_RFC.md](../../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md) §4, §7 (S1), §8, §11, §12;
[worker_oidc_and_docx_sandbox.md](worker_oidc_and_docx_sandbox.md); deploy steps in
[`../../07_deployment/README.md`](../../07_deployment/README.md) § Sandbox render service.

## Decision

The script a model wrote for a DOCX (the least constrained input the product runs) executes in a **separate Cloud Run
service** whose runtime identity holds **no IAM role** and which is given **no secret**. The main service keeps
`DocxRunnerPort`; `SandboxDocxRunner` (`src/adapters/sandbox_client/`) is a second implementation next to
`NodeDocxRunner`, chosen once at startup by `SANDBOX_RENDER` (`inprocess` | `sandbox`). There is **never an automatic
fallback** to the in-process runner: a sandbox that is down degrades the DOCX feature, nothing else.

Shape of the service (`sandbox_main.py`, `Dockerfile.sandbox` target `render`):

- **Two processes in one file.** `python sandbox_main.py` is the supervisor (PID 1, reaps orphans, imports no payload
  code); `--serve` is the Quart/Hypercorn server (HTTP/2 prior knowledge). One request per instance
  (`concurrency=1`, `SANDBOX_RECYCLE=always`): after the response the supervisor stops the instance.
- **The engine is a child under another uid** (`sandboxengine`, resolved at start). `ChildLauncher` applies rlimits
  (`RLIMIT_AS`, `CPU`, `FSIZE`, `NPROC`, `NOFILE`), a fixed environment allowlist, its own session and a process-group kill.
  The server never opens a path inside the engine-writable work directory: outputs are copied by the engine wrapper into
  a server-owned `0700` directory and streamed from there (ruling R8).
- **Callers are authenticated in-app**: the gate verifies the Google OIDC signature, the audience (the service's own URL,
  `SANDBOX_URL`) and the caller's email against `SANDBOX_CALLERS_RENDER`, per op prefix. On Cloud Run an empty or
  missing caller set refuses the start; with no URL the service starts in `unconfigured` mode (only `/health`, every
  op answers 503) so the first pass of the two-pass deploy can succeed.
- **Contract**: `render.docx` body is `u32-be script_length | script | spec`; the request carries only an opaque
  `request_id`, limits, `kind` and `ext` (a test pins the field set, I3). A script's own failure text travels in the
  response body (capped at 16 KiB), the outcome header carries only the status.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Cloud Run **Job** per render | Cold start per call plus a trigger API call and result channel; the skeleton (auth, contract, launcher, recycle) is needed by later slices as a request/response service anyway (RFC §4.1). |
| **Cloud Function** | Same zero-role goal, but a second packaging and runtime model for Node plus Python children; no gain over a Cloud Run service with the same image family. |
| In-process guard only (today's `NodeDocxRunner` + prelude) | Bounds a script's privileges inside a container that holds the main service's credentials. The service boundary removes the credentials instead of fencing them. |
| Reuse `NodeDocxRunner` as the service engine | It writes into `docx_generator/`, runs `node` as the caller's uid and applies no rlimits. A new `DocxRenderEngine` + child keeps its observable contract (error texts, prelude), pinned by the contract suite. |

## Why

The same argument as the drive parser's decision: the residual risk of running hostile bytes beside credentials is
accepted only until a boundary exists. S1 builds the boundary with the smallest consumer (one op, bytes out, no large
input) so auth, contract, launcher and recycle are proved before the parsers move (S3).

## Parameters taken here (S0 has not run)

RFC §11 initial values, each constant's comment says "revisit after S0-<x>": wall 120 s, `RLIMIT_AS` 1 GiB, `CPU` 90 s,
`FSIZE` 64 MiB, request and response cap 64 MiB, `meta` 8 KiB, error text cap 16 KiB. **`RLIMIT_NPROC` / `RLIMIT_NOFILE`
= 64 / 64 were chosen in this build** (RFC leaves "small fixed values, set at S0-f"). Service: 2 GiB, 2 CPU, concurrency 1,
max 3 instances, timeout 300 s; in-memory work volume at `/work`, **256Mi placeholder**.

## S0 items S1 still waits on

- **Flush barrier (G7 / S0-b):** that the response is fully flushed before the supervisor stops the instance is unproven.
- **`RLIMIT_AS` vs Node's `DATA` mapping (G6 / S0-f):** whether 1 GiB address space suits `node` on gen2.
- **NPROC / NOFILE 64 / 64** may be low for Node; confirm on gen2 (S0-f).
- **`/dev/shm` writability (S0-f):** whether the engine uid can write to `/dev/shm` on gen2 (instance memory outside
  `RLIMIT_AS`). `/tmp` is root-only (0700) in the image; `/dev/shm` is the platform's and must be measured.
- **Post-response drain (S0-b):** the service deploys with `--no-cpu-throttling` so the recycle sequence (which runs after
  the response) is not stalled by request-based CPU throttling. Measure the drain time with these exact flags and size the
  client's recycling retry window to it; otherwise a call arriving during the drain can exhaust the 3 retries.
- **Work volume size** (256Mi placeholder) against the real 64 MiB file ceilings.
- **Real Cloud Run status for an instance killed mid-request** (OOM or crash): a bare 503 would make a poison input look
  like a systemic outage and be retried; the client's 5xx rule depends on the recorded answer.
- Also unproven without a cloud: the image has never been built (apt `nodejs`/`npm`, the world-writable gate), the zero-role
  checks (S0-c), HTTP/2 end to end (S0-a).

## Accepted residuals

- A grandchild that calls `setsid()` leaves the process group and survives the group kill; recycle (`always`) bounds it
  to the one instance.
- `killpg` after the leader is reaped has a pid-reuse window; negligible at concurrency 1 and one request per instance.
- The engine's stderr temp file lives in the server's temp area and counts against instance memory. It is read in full for
  hashing and bounded by `RLIMIT_FSIZE`; the 16 KiB cap applies to `err.txt` (the text handed back), not to the stderr file.
- **Python 3.13 (CI) vs 3.11 (image).** The suite runs on 3.13 in CI while the image is `python:3.11-slim`; a 3.12+ only
  feature would pass CI and fail in the image (the image build itself is unproven until S0). The real-node tests skip in CI
  (no `npm install` there), so the Node path is exercised only locally and in the first real deploy.
- `GPG_KEY` (the public id of the Python release key, defined by the base image) is allowlisted in the startup
  self-check; an allowlist entry, not a looser pattern.
- Without any caller configuration and no `SANDBOX_URL`, off Cloud Run, the gate is open for local development; the server then binds
  `127.0.0.1`. On Cloud Run an open gate cannot happen (`K_SERVICE` is inherited by the server child).
- Orphaned grandchildren reparent to PID 1 (the supervisor); reaping is tested with a fake kernel only.
- `bad_request` from a child can be forged by the script (no leak); callers do not treat it as "not the script's fault".

## Rollback and hard stop

Set `SANDBOX_RENDER_DEV` back to `inprocess` (or empty) and deploy the main service; the render service can stay up unused.
To cut a misbehaving caller off at once, remove its `roles/run.invoker` binding. Do **not** empty
`SANDBOX_RENDER_CALLERS_DEV` for that: an empty caller list on Cloud Run refuses the new revision, so the old revision keeps
serving and nothing is stopped.

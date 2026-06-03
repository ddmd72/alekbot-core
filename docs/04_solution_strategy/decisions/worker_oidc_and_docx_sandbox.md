# Decision: `/worker` OIDC verification + `fs` block in the DOCX sandbox

**Date:** 2026-06-03
**Status:** Accepted
**Context:** External security/architecture review flagged three "critical" vulnerabilities.
Each was verified against the actual code under the real deployment model (solo project,
**single live tenant**, live service runs `APP_ENV=development` as `alek-bot-dev`).

## Findings triage

1. **`/worker` unauthenticated** — *correct, real risk.* The endpoint
   ([`main.py`](../../../main.py) `/worker` route) dispatched task payloads with no auth, on a
   Cloud Run service deployed `--allow-unauthenticated` + `allUsers/run.invoker`. Anyone knowing
   the URL + a valid `user_id` could trigger background tasks → denial-of-wallet (LLM spend).
2. **`fs` not blocked in the DOCX sandbox** — *literally true, low real risk.* LLM-generated
   Node runs as a subprocess; `_SECURITY_PRELUDE` blocked network/process modules but left `fs`.
   Mitigated already: network egress blocked + secrets withheld from the subprocess env
   (`_SAFE_ENV_KEYS`), and the output doc returns to the same single user. Cheap defense-in-depth.
3. **No `account_id`/`owner_id` on `get_fact_by_id`/`get_lineage`/`load_session`** —
   *technically true, ~zero real risk.* **Dropped.** Single tenant; lookup keys are
   server-derived (`fact_id` is a UUID from already-account-scoped search; `session_id =
   user_id:channel_id`), not attacker-controllable. The review overstated this as CRITICAL.

## Decision

Implement **#1 and #2**; defer **#3**.

- **#1:** In-app Google OIDC verification on `/worker`
  ([`src/web/worker_oidc_verifier.py`](../../../src/web/worker_oidc_verifier.py)). Enforcement is
  **symmetric with the enqueue side**: `GcpTaskQueue` attaches `oidc_token` only when
  `SERVICE_ACCOUNT_EMAIL` is set, so the route enforces verification under that same condition.
  Live cloud sets it → enforce; local dev doesn't → bypass (manual `curl` triggers keep working).
- **#2:** Add `fs`/`fs/promises` to the prelude `BLOCKED` set. Verified empirically first that the
  `docx` lib builds in-memory (`Packer.toBuffer` → stdout) and never needs `fs`.

## Why these shapes

- **In-app, not ingress:** the service must stay `--allow-unauthenticated` — the same Cloud Run
  service hosts public Slack/Telegram webhooks, OAuth callbacks, the Cabinet UI and the remote
  MCP server. Locking the whole service at the ingress is impossible; `/worker` verifies itself.
- **`SERVICE_ACCOUNT_EMAIL` as the gate** (not `is_production`): the live deploy runs as
  `development`, so `is_production` is always False — it cannot distinguish cloud from laptop. The
  SA-email presence is the one signal that already governs the enqueue side; reusing it keeps both
  sides in lockstep and self-configuring.
- **#2 scoped to `fs` only:** under the real-risk lens, also blocking `eval`/`Function`/
  `process.env` adds nothing once network egress is blocked — that would be portfolio-narrative
  gold-plating, explicitly out of scope.
- **Identity-only check, audience NOT pinned** (revised post-deploy): `/worker` is driven by
  Cloud Tasks *and* eight Cloud Scheduler jobs whose token audiences are inconsistent — Cloud
  Tasks defaults the audience to `<service_url>/worker`, Cloud Scheduler jobs use the bare Cloud
  Run URL, and `CLOUD_RUN_SERVICE_URL` is not reliably set. The first deploy pinned audience to
  `<CLOUD_RUN_SERVICE_URL>/worker` and 401'd all legitimate Scheduler traffic (reminders,
  consolidation sweep, billing, task-subscription renew, email indexing, daily review). Fix:
  verify Google signature + `email == SA` + `email_verified`, no audience pin. The threat
  (anonymous internet POSTs) carries no Google token at all and is fully stopped by signature +
  SA-email; audience pinning would only block a same-SA token minted for a different audience —
  negligible here. Companion infra change: the six Scheduler jobs that sent **no** OIDC token were
  reconfigured with `--oidc-service-account-email` (no code can authenticate a tokenless caller).

## Rejected alternatives

- *Remove `--allow-unauthenticated` from the service:* breaks all public webhooks/OAuth/MCP.
- *Shared-secret header on `/worker`:* weaker than Google-signed OIDC, and OIDC was already
  half-wired on the enqueue side — completing it is the smaller, stronger change.
- *Implement #3 multi-tenant filtering:* touches the repository port + many call sites for no
  security gain on a single-tenant system.

## Trigger to revisit

Standing up a genuinely multi-tenant production environment → revisit **#3** (filter reads by
`account_id`/`owner_id`). If DOCX generation ever needs legitimate file I/O → revisit the **#2**
block (neuter only `fs.readFile*` rather than the whole module).

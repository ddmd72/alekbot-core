# Decision: per-runtime service accounts and split OIDC identities

**Status:** Step 0 (code) landed; later steps move runtimes onto their own service accounts.
**Context:** every runtime (main, indexer, relay, research job) and every build ran as the project's default
service account with broad project roles. One secret, `SERVICE_ACCOUNT_EMAIL`, played five roles: the main
`/worker` expected email, the `/voice/*` expected email, the indexer's expected email, the OIDC identity attached
to every enqueued task, and the GCS signBlob signer.

## Decision

- **One identity per runtime**, each with only the roles it uses (Firestore, queue enqueue, its own secrets,
  bucket/dataset grants at resource level). Service-account names and emails live in Secret Manager / `.env`,
  never in tracked files.
- **Two role-less invoker identities** instead of one: one for tasks and Scheduler jobs that target the main
  service, one (`DRIVE_INVOKER_SA_EMAIL`) for the drive queue that targets the indexer. With a single invoker, a
  compromised indexer (the runtime that parses hostile files) could enqueue a task signed by the identity main's
  `/worker` accepts and reach any `task_type` for any user.
  **Honest limit:** this closes the `/worker` door, not the data door. Firestore has no collection-level IAM, so a
  compromised indexer can still write any document in the database (prompts, whitelist, reminders) and read every
  user's OAuth credentials. The real fence is a separate database for indexer-owned collections (out of scope).
- **Accept before send, flip only senders, drain before forgetting.** Verifiers accept a set (see
  `worker_oidc_and_docx_sandbox.md`), so any mix of old and new instances accepts every identity alive at that
  moment. New keys: `DRIVE_INVOKER_SA_EMAIL`, `VOICE_RELAY_SA_EMAIL` (required config, in `load_settings()`),
  `OIDC_TRANSITION_SA_EMAILS` (optional knob, `os.getenv`).
- **The GCS signer is the runtime's own identity** (`GcsMediaAdapter`): a locally-signing credential signs as
  before; otherwise IAM signBlob as the refreshed credential's `service_account_email`. The constructor email is
  an explicit override only; main no longer passes `SERVICE_ACCOUNT_EMAIL`. An unresolved signer
  (empty or the pre-refresh placeholder `default`) is an error, never a silent bad signature.
- **Build:** the scheduler step reads the invoker identity from the secret (not from the project number), runs
  under `set -euo pipefail`, pins an OIDC audience on every job and manages all jobs, including the relay
  keep-alive that used to be created by hand. Logging is `CLOUD_LOGGING_ONLY` (required for a user-specified
  build service account).

## Invariant

The research job's grants must stay a subset of main's. Main may run the job with argument overrides, and the
job's command is a generic interpreter, so main can execute arbitrary code as the job's identity. Any future grant
to the job is reviewed against this.

## Alternatives rejected

A single invoker (see above); pinning one audience on main's `/worker` (it 401'd all Scheduler traffic in
2026-08, callers use inconsistent audiences); a per-service secret for each expected email without a transition
list (no zero-downtime path, since `:latest` is resolved per instance at cold start).

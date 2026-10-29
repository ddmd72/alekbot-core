"""
Indexer service entrypoint — the SECOND Cloud Run **service** of the drive index
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §9 "Host: a separate indexer service", §12.5, §13; owner
decision D5). Same image as the main service, its own entry point and its own environment. It runs
the whole `drive_sync` tick, so the main service (1 GiB, already at 74-90 % memory) never indexes.

Builds only what the tick needs: Firestore (`us-production`), the user repository (the tick's gate
reads the opt-in flag uncached), OAuth credentials + the drive adapter, the drive index repository,
the Gemini embedding adapter, the place table, a task queue bound to the drive queue and to this
service's own `/worker` (continuations and nudge re-queues come back here), and the content side of
Slice 2+4 (§24.2): the child-process parser, the document reader and image triage agents over Claude
and OpenAI, the Firestore prompt builder and the quota service that bills their calls to the user's
account (`src/composition/drive_model_setup.py`), and Search v2's profiler and passage-hint runners
(§25.4.4) on the same wiring; with the transcription switch on, the transcript stage and the transcript mirror's
step, whose files are localized (the localization service and the UI-language resolution; §27.9.1), while the
mirror's ledger is wired whatever the switch (§27.9.2). No `ServiceContainer`: it would build every agent,
LLM adapter and chat surface for a process that uses none of them. No `load_settings()`: it would
chase every key this service does not have into Secret Manager on each cold start.

HTTP: Quart on hypercorn, the main service's stack. The relay's plain `websockets` server is
built for WebSockets, not POST routes; `aiohttp.web` would add a second HTTP framework idiom to the
codebase. Quart gives the same request semantics and test client as the main `/worker`.

Environment variables (set at deploy time in cloudbuild-dev.yaml):
  GOOGLE_CLOUD_PROJECT          GCP project id (Firestore, Cloud Tasks).
  APP_ENV                       "development" -> `development_` collections, queue `drive-sync-dev`.
  DRIVE_INDEXER_URL             This service's own URL: the drive queue's target. Empty on the first
                                pass of the two-pass first deploy: the service starts, /worker answers 503.
  GEMINI_API_KEY                Secret: card and chunk embeddings, and the drive agents' Gemini Flash fallback.
  ANTHROPIC_API_KEY             Secret: the drive reader and triage agents for a user whose configuration picks Claude.
  OPENAI_API_KEY                Secret: the same agents on OpenAI (the default, §24.2).
  BILLING_SLACK_WEBHOOK_URL     Secret, optional: the advisory daily budget alert of the quota service.
  DRIVE_UPGRADE_PASS            Optional emergency switch (§25.8): "off" stops every version upgrade pass. Deployed
                                from DRIVE_UPGRADE_PASS_DEV in .env (Makefile), so it survives a deploy (XC-7).
  DRIVE_TRANSCRIBE_ENABLED      Optional kill switch (§27.13) of audio/video transcription: "on" (1/on/true/yes) builds
                                the transcript stage; default off, then the object graph is the pre-transcription one.
                                Deployed from DRIVE_TRANSCRIBE_ENABLED_DEV in .env (Makefile), default off.
  DRIVE_TRANSCRIBE_USD_CAP      Optional: a positive number that LOWERS the $20 monthly transcription cap (never raises
                                it; empty = $20). Deployed from DRIVE_TRANSCRIBE_USD_CAP_DEV in .env, on this service and
                                on the main service (the Cabinet shows it).
  DRIVE_PARSER_USER             The unprivileged user the parser and media children run as (Dockerfile).
  MICROSOFT_TODO_CLIENT_ID      Secret: the drive app registration (token refresh).
  MICROSOFT_TODO_CLIENT_SECRET  Secret: same.
  SERVICE_ACCOUNT_EMAIL         Secret: the /worker OIDC gate and the OIDC identity of the tasks this
                                service enqueues. Unset locally (gate open); unset on Cloud Run
                                (K_SERVICE set) the gate fails closed (RFC Δ21).
  DRIVE_INVOKER_SA_EMAIL        Secret, optional: the dedicated OIDC identity of the drive queue. When set it is the
                                accepted /worker identity and the identity of the tasks this service enqueues;
                                unset, SERVICE_ACCOUNT_EMAIL plays both roles (today's behaviour).
  OIDC_TRANSITION_SA_EMAILS     Optional comma list of extra accepted /worker identities (migration window).
  K_SERVICE                     Injected by Cloud Run; tells Cloud Run from local dev.
  PORT                          Injected by Cloud Run; 8080 locally.

Routing and the OIDC gate live in `src/web/indexer_worker_app.py` and are tested there;
`tests/unit/test_indexer_main.py` is a wiring smoke test of `build_app()` with Google clients replaced.
"""
import asyncio
import os
import signal
from typing import Any, Awaitable, Callable

from google.cloud import firestore
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from quart import Quart

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.adapters.microsoft import OneDriveAdapter
from src.adapters.slack.webhook_adapter import SlackWebhookAdapter
from src.adapters.subprocess_content_extractor import PARSER_USER_ENV, SubprocessContentExtractor
from src.composition.cipher_setup import build_cipher
from src.composition.drive_indexing_setup import (
    build_drive_index_repository, build_drive_mirror_repository, build_drive_task_queue, build_place_lookup,
)
from src.composition.drive_model_setup import build_drive_mirror, build_drive_models, build_drive_transcription
from src.composition.drive_note_setup import (
    DRIVE_NOTES_ENABLED_ENV, build_drive_note_lifecycle, build_drive_note_repository, drive_notes_switch_on,
)
from src.composition.memory_telemetry_setup import build_memory_telemetry
from src.composition.prompt_assembly_setup import build_prompt_assembly_service
from src.config.environment import EnvironmentConfig
from src.domain.drive_transcript_plan import effective_monthly_cap
from src.services.drive_indexing_service import DriveIndexingService
from src.utils.logger import logger, quiet_third_party_loggers

quiet_third_party_loggers()
from src.web.worker_oidc_verifier import parse_sa_emails  # noqa: E402  # quiet_third_party_loggers() must run first
from src.web.indexer_worker_app import create_indexer_worker_blueprint, indexer_audiences, worker_oidc_gate  # noqa: E402  # quiet_third_party_loggers() must run first

# Cloud Run allows ~10 s between SIGTERM and SIGKILL (a revision rollout on every deploy).
# `serve_until_shutdown` waits this long for an in-flight tick, then cancels it and waits for its
# `finally` to release the lease, so the next tick is not refused as `busy` for the lease's 31 minutes.
_GRACEFUL_TIMEOUT_S = 8


def build_app() -> Quart:
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    sa_email = (os.environ.get("SERVICE_ACCOUNT_EMAIL") or "").strip() or None
    drive_invoker = (os.environ.get("DRIVE_INVOKER_SA_EMAIL") or "").strip() or None
    # Accepted: the drive invoker (falls back to the single legacy identity) plus the optional transition list.
    accepted = parse_sa_emails(drive_invoker or sa_email, os.environ.get("OIDC_TRANSITION_SA_EMAILS"))
    gate = worker_oidc_gate(accepted, indexer_audiences(os.environ.get("DRIVE_INDEXER_URL")),
                            on_cloud_run=bool(os.environ.get("K_SERVICE")))
    # The credential store's cipher (SECRETS_AT_REST_RFC §2.6, I6): the indexer has no load_settings(), so the key
    # name comes from its environment; an empty one refuses the start, before any client is built.
    cipher = build_cipher(os.environ.get("KMS_CRYPTO_KEY"))
    env_config = EnvironmentConfig()
    db = firestore.AsyncClient(project=project, database=env_config.firestore_database_id)

    app = Quart(__name__)
    queue = build_drive_task_queue(
        {"GOOGLE_CLOUD_PROJECT": project, "SERVICE_ACCOUNT_EMAIL": sa_email, "DRIVE_INVOKER_SA_EMAIL": drive_invoker},
        env_config,
    )
    if queue is None:
        # First pass of the two-pass first deploy (Makefile, _INDEXER_URL): start anyway so the deploy
        # step succeeds; /worker answers 503 and nothing is enqueued to it until the second pass.
        logger.warning("[Indexer] DRIVE_INDEXER_URL is not set: /worker disabled until the next deploy")
        app.register_blueprint(create_indexer_worker_blueprint(None, gate))
        return app
    oauth_credentials = FirestoreCredentialStore(db, env_config, cipher=cipher)
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    embeddings = GeminiEmbeddingAdapter(api_key=os.environ["GEMINI_API_KEY"])
    users = FirestoreUserRepository(db, env_config, account_repo)
    # The reader and triage prompts live in Firestore (§24.2); a failure here fails the start, never a fallback prompt.
    assembly_service, profile_repo = build_prompt_assembly_service(db, env_config)
    billing_webhook = (os.environ.get("BILLING_SLACK_WEBHOOK_URL") or "").strip()
    models = build_drive_models(
        # the image's unprivileged user (Dockerfile); a named user missing from the container fails the start (R3 F3-1)
        parser=SubprocessContentExtractor(sandbox_user=os.environ.get(PARSER_USER_ENV)),
        users=users,
        account_repo=account_repo,
        assembly_service=assembly_service,
        keys={key: os.environ.get(key) for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")},
        alert_sink=SlackWebhookAdapter(billing_webhook) if billing_webhook else None,
    )
    drive = OneDriveAdapter(
        oauth_credentials=oauth_credentials,
        client_id=os.environ["MICROSOFT_TODO_CLIENT_ID"],
        client_secret=os.environ["MICROSOFT_TODO_CLIENT_SECRET"],
    )
    index_repo = build_drive_index_repository(db, env_config)
    # The transcript mirror's roots and ledger (§27.9.2-§27.9.4): ALWAYS built, whatever the kill switch — the bot's
    # folder stays opaque to classification, deletions leave their marks and the purge keeps the pinned transcripts
    # even while transcription is off (§27.9.12). The mirror step itself is built only with the switch on (below).
    mirror_repo = build_drive_mirror_repository(db, env_config)
    # Transcription (§27.13): the kill switch and the optional cap knob are read ONCE, here. The cap only lowers
    # $20 (T-R3) and the same value goes to the stage and to the tick's cap math. Switch off: no stage is built.
    transcribe_on = transcribe_enabled(os.environ.get("DRIVE_TRANSCRIBE_ENABLED"))
    transcribe_cap = effective_monthly_cap(os.environ.get("DRIVE_TRANSCRIBE_USD_CAP"))
    transcripts = build_drive_transcription(
        enabled=transcribe_on, repo=index_repo, drive=drive, models=models,
        keys={"GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY")}, parser_user=os.environ.get(PARSER_USER_ENV),
        monthly_cap_usd=transcribe_cap, mirror=mirror_repo,
    )
    # The mirror step (§27.9.5) with its localization (the read-me and the file headers in the user's UI language),
    # only while transcription runs; switch off: none of it, and the object graph keeps only the ledger.
    mirror = build_drive_mirror(
        enabled=transcribe_on and transcripts is not None, repo=index_repo, mirror_repo=mirror_repo, drive=drive,
        users=users, account_repo=account_repo, profile_repo=profile_repo, assembly_service=assembly_service,
    )
    # The owner's notes (DRIVE_FILE_NOTES_RFC §5.1, §7): the tick merges them into the records, makes the archive checks
    # and runs the daily audit. DRIVE_NOTES_ENABLED (part A's optional kill switch, os.getenv, default on) off gives the
    # tick no store and no lifecycle: it does no notes work and saves a zero notes merge version, so turning it on again
    # re-reads every note (§3.6, §5.5). One store instance for the tick's `notes=` and the lifecycle.
    notes_on = drive_notes_switch_on(os.environ.get(DRIVE_NOTES_ENABLED_ENV))
    note_repo = build_drive_note_repository(db, env_config) if notes_on else None
    note_lifecycle = build_drive_note_lifecycle(enabled=notes_on, notes=note_repo, index=index_repo, drive=drive)
    indexing = DriveIndexingService(
        drive=drive,
        repo=index_repo,
        embeddings=embeddings,
        queue=queue,
        users=users,
        place_of=build_place_lookup(),
        embedding_space=GeminiEmbeddingAdapter.EMBEDDING_SPACE,
        extractors=models.extractors,
        triage=models.triage,
        # Search v2 (§25.4.4): the profiler and the passage-hint runners, over the same context builder, prompt
        # builder and quota service as the reader and triage
        profiler=models.profiler,
        passages=models.passages,
        upgrade_pass=upgrade_pass_enabled(os.environ.get("DRIVE_UPGRADE_PASS")),
        transcripts=transcripts,
        transcribe_enabled=transcribe_on and transcripts is not None,
        transcribe_cap_usd=transcribe_cap,
        mirror_repo=mirror_repo,
        mirror=mirror.mirror,
        localization=mirror.localization,
        language=mirror.language,
        notes=note_repo,
        note_lifecycle=note_lifecycle,
    )

    app.register_blueprint(create_indexer_worker_blueprint(indexing, gate))
    logger.info(f"[Indexer] ready (OIDC gate {'on' if sa_email else 'off'})")
    return app


def upgrade_pass_enabled(value: Any) -> bool:
    """DRIVE_UPGRADE_PASS (§25.8): an optional emergency switch, on unless set to 0/off/false/no."""
    return (str(value or "on")).strip().lower() not in ("0", "off", "false", "no")


def transcribe_enabled(value: Any) -> bool:
    """DRIVE_TRANSCRIBE_ENABLED (§27.13): the transcription kill switch, OFF unless set to 1/on/true/yes."""
    return (str(value or "off")).strip().lower() in ("1", "on", "true", "yes")


async def serve_until_shutdown(app: Any, config: Any, shutdown: asyncio.Event, *,
                               serve_fn: Callable[..., Awaitable[None]] = serve) -> None:
    """Serve until `shutdown` is set, then wait at most `config.graceful_timeout` for in-flight requests,
    cancel what is left and wait for it to unwind. Hypercorn's own graceful timeout does not cancel an
    in-flight request on Python >= 3.12.1, and its request tasks are not children of `serve`, so
    cancelling `serve` alone would leave the tick running into SIGKILL with its lease held."""
    serve_task = asyncio.create_task(serve_fn(app, config, shutdown_trigger=shutdown.wait))
    signalled = asyncio.create_task(shutdown.wait())
    await asyncio.wait({serve_task, signalled}, return_when=asyncio.FIRST_COMPLETED)
    if serve_task.done():
        signalled.cancel()
        serve_task.result()   # a server that stopped on its own (bind failure) raises here
        return
    await asyncio.wait({serve_task}, timeout=config.graceful_timeout)
    current = asyncio.current_task()
    leftover = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if leftover:
        logger.warning(f"[Indexer] graceful timeout ({config.graceful_timeout} s) passed; cancelling "
                       f"{len(leftover)} task(s)")
    for task in leftover:
        task.cancel()
    await asyncio.gather(*leftover, return_exceptions=True)


async def main() -> None:
    memory_telemetry = build_memory_telemetry("alek-indexer")
    if memory_telemetry:
        await memory_telemetry.start()
    app = build_app()
    config = HypercornConfig()
    config.bind = [f"0.0.0.0:{int(os.environ.get('PORT', '8080'))}"]
    config.accesslog = None
    config.errorlog = "-"
    config.graceful_timeout = _GRACEFUL_TIMEOUT_S

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)
    await serve_until_shutdown(app, config, shutdown)
    if memory_telemetry:
        await memory_telemetry.stop()
    logger.info("[Indexer] shut down")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
run_indexing.py — Local integration script for the email indexing pipeline.

Runs the full EmailIndexingService pipeline against a real Gmail account
using a cached OAuth token. Use this for first-run validation and debugging
before wiring into the Cloud Run + Cloud Tasks production flow.

Setup (one-time):
  pip install google-auth-oauthlib
  Google Cloud Console → APIs & Services → Credentials
    → OAuth 2.0 Client ID → Desktop app → Download JSON
    → save as scripts/email/credentials.json
  .env must contain: GEMINI_API_KEY, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
  .env must contain: DEV_USER_ID, DEV_ACCOUNT_ID

Usage:
  python scripts/email/run_indexing.py
  python scripts/email/run_indexing.py --after 2025-01-01
  python scripts/email/run_indexing.py --provider gmail --dry-run
  python scripts/email/run_indexing.py --after 2024-06-01 --batch-size 25
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import argparse

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
TOKEN_FILE = Path(__file__).parent.parent / "memory" / "gmail_token.json"


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def get_google_credentials():
    """Load OAuth token from file, refresh if expired, run browser flow if missing."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing OAuth token...")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                print(f"\nERROR: credentials.json not found at:\n  {CREDENTIALS_FILE}")
                print("\nGet it from: Google Cloud Console → APIs & Services → Credentials")
                print("Create: OAuth 2.0 Client ID → Desktop app → Download JSON → rename to credentials.json")
                sys.exit(1)
            print("Opening browser for OAuth consent...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())
        print(f"Token cached at {TOKEN_FILE}")

    return creds


def build_oauth_credentials(google_creds, user_id: str) -> "OAuthCredentials":
    """Convert google.oauth2.credentials.Credentials → OAuthCredentials domain object."""
    from src.domain.email import OAuthCredentials

    expiry = google_creds.expiry
    if expiry and expiry.tzinfo is not None:
        # Strip timezone info — OAuthCredentials uses naive UTC datetimes
        expiry = expiry.replace(tzinfo=None)
    if not expiry:
        # No expiry info → set far future so the adapter doesn't trigger a refresh
        expiry = datetime(2099, 1, 1)

    email_address = getattr(google_creds, "id_token", {}) or {}
    if isinstance(email_address, dict):
        email_address = email_address.get("email", "")

    return OAuthCredentials(
        user_id=user_id,
        provider="gmail",
        access_token=google_creds.token,
        refresh_token=google_creds.refresh_token or "",
        token_expiry=expiry,
        scopes=list(google_creds.scopes or SCOPES),
        email_address=email_address or "",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args):
    import google.cloud.firestore as firestore_module
    from google.cloud.firestore import AsyncClient

    from src.config.environment import EnvironmentConfig
    from src.adapters.gmail_provider_adapter import GmailProviderAdapter
    from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
    from src.adapters.firestore_email_job_repo import FirestoreEmailJobRepository
    from src.adapters.firestore_email_exclusions_adapter import FirestoreEmailExclusionsAdapter
    from src.adapters.gemini_adapter import GeminiAdapter
    from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
    from src.adapters.claude_adapter import ClaudeAdapter
    from src.agents.email_classification_agent import EmailClassificationAgent
    from src.domain.agent import AgentConfig
    from src.domain.user import PerformanceTier, UserBotConfig
    from src.services.email_indexing_service import EmailIndexingService, GMAIL_DEFAULT_QUERY
    from src.services.provider_registry import ProviderRegistry
    from src.services.agent_context_builder import AgentContextBuilder
    # Prompt builder
    from src.adapters.security.regex_adapter import RegexSecurityAdapter
    from src.adapters.security.composite_adapter import CompositeAdapter
    from src.adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
    from src.adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
    from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
    from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
    from src.services.prompt_v3.context_formatter import ContextFormatter
    from src.services.prompt_v3.biographical_formatter import BiographicalFactsFormatter
    from src.services.prompt_builder import PromptBuilder

    # --- env / config ---
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        print("ERROR: GEMINI_API_KEY not set in .env")
        sys.exit(1)

    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")

    user_id = os.getenv("DEV_USER_ID")
    account_id = os.getenv("DEV_ACCOUNT_ID")
    if not user_id or not account_id:
        print("ERROR: DEV_USER_ID and DEV_ACCOUNT_ID must be set in .env")
        sys.exit(1)

    firestore_db = os.getenv("FIRESTORE_DATABASE", "us-production")
    gcp_project = os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")

    env_config = EnvironmentConfig()

    # --- OAuth ---
    print("Loading Gmail OAuth credentials...")
    google_creds = get_google_credentials()
    credentials = build_oauth_credentials(google_creds, user_id)
    print(f"  Authenticated as: {credentials.email_address or '(email unknown)'}")

    # --- Firestore ---
    print(f"Connecting to Firestore database: {firestore_db}")
    if gcp_project:
        db = AsyncClient(project=gcp_project, database=firestore_db)
    else:
        db = AsyncClient(database=firestore_db)

    # --- Adapters ---
    gmail = GmailProviderAdapter(client_id=client_id, client_secret=client_secret)
    email_repo = FirestoreIndexedEmailRepository(db_client=db, env_config=env_config)
    job_repo = FirestoreEmailJobRepository(db_client=db, env_config=env_config)
    exclusions_repo = FirestoreEmailExclusionsAdapter(db_client=db, env_config=env_config)

    # --- LLM + embedding ---
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    llm = GeminiAdapter(api_key=gemini_api_key)
    claude_llm = ClaudeAdapter(api_key=anthropic_api_key)
    embedding = GeminiEmbeddingAdapter(api_key=gemini_api_key)

    # --- Prompt builder (same wiring as ServiceContainer) ---
    security_port = CompositeAdapter(
        adapters=[RegexSecurityAdapter()],
        strategy="worst_case",
    )
    token_repo = FirestoreTokenRepository(
        db=db,
        system_collection=f"{env_config.domain_prompt_tokens_collection}_system",
        user_collection=f"{env_config.domain_prompt_tokens_collection}_user",
        security_port=security_port,
    )
    blueprint_repo = FirestoreBlueprintRepository(
        db=db,
        collection_name=env_config.domain_prompt_blueprints_collection,
    )
    profile_repo = FirestoreAgentProfileRepository(
        db=db,
        profiles_collection=env_config.domain_prompt_profiles_collection,
        overrides_collection=env_config.domain_prompt_overrides_collection,
    )
    assembly_service = PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=security_port,
        formatter=ContextFormatter(),
        bio_formatter=BiographicalFactsFormatter(),
    )
    # PromptBuilder implements PromptBuilderPort (required by agents).
    # repo=None is safe: email classifier uses include_biographical=False.
    prompt_builder = PromptBuilder(repo=None, assembly_service=assembly_service)

    # --- Provider registry + context builder (mirrors ServiceContainer) ---
    registry = ProviderRegistry()
    registry.register("gemini", llm)
    registry.register("claude", claude_llm)
    context_builder = AgentContextBuilder(registry, cache_strategy=None)

    tier_map = {
        "eco": PerformanceTier.ECO,
        "balanced": PerformanceTier.BALANCED,
        "performance": PerformanceTier.PERFORMANCE,
    }
    # Pass tier via UserBotConfig so context_builder resolves it correctly.
    email_config = UserBotConfig(agent_tiers={"email_classifier": tier_map[args.tier]})
    email_context = context_builder.build("email_classifier", email_config)

    # --- Agents + Services ---
    classifier = EmailClassificationAgent(
        config=AgentConfig(agent_id="email_classifier", agent_type="email_classifier"),
        execution_context=email_context,
        prompt_builder=prompt_builder,
        gmail=gmail,
        user_id=user_id,
    )
    indexer = EmailIndexingService(
        gmail=gmail,
        email_repo=email_repo,
        job_repo=job_repo,
        exclusions_repo=exclusions_repo,
        classifier=classifier,
        embedding=embedding,
        oauth=None,
    )

    # --- Job ---
    if args.dry_run:
        print("\n[DRY RUN] Skipping actual indexing. Pipeline wired successfully.")
        return

    date_from = None
    if args.after:
        try:
            date_from = datetime.strptime(args.after, "%Y-%m-%d")
        except ValueError:
            print(f"ERROR: --after must be YYYY-MM-DD, got: {args.after}")
            sys.exit(1)

    job = indexer.create_job(
        user_id=user_id,
        provider="gmail",
        triggered_by="manual_script",
        resume_token=args.resume_token,
    )

    gmail_query = None if args.no_filter else GMAIL_DEFAULT_QUERY

    print(f"\nCreating indexing job: {job.job_id[:8]}...")
    print(f"  Provider: gmail")
    print(f"  Date from: {date_from.strftime('%Y-%m-%d') if date_from else 'all time'}")
    print(f"  Gmail query: {gmail_query or '(none — full mailbox)'}")
    if args.resume_token:
        print(f"  Resuming from token: {args.resume_token[:20]}...")
    await job_repo.create_job(job)

    print("\nStarting indexing pipeline...\n")
    started = datetime.utcnow()

    try:
        job = await indexer.run_indexing_job(
            job=job,
            credentials=credentials,
            account_id=account_id,
            max_pages=args.max_pages,
            date_from=date_from,
            gmail_query=gmail_query,
            page_size=args.count,
        )
    except Exception as exc:
        print(f"\n💥 Job failed: {exc}")
        print(f"   Status: {job.status}")
        print(f"   Resume token: {job.next_page_token}")
        raise

    elapsed = (datetime.utcnow() - started).total_seconds()

    print("\n" + "=" * 60)
    print("INDEXING COMPLETE")
    print("=" * 60)
    print(f"  Job ID:           {job.job_id}")
    print(f"  Status:           {job.status}")
    print(f"  Emails fetched:   {job.emails_fetched}")
    print(f"  Emails stored:    {job.emails_stored}")
    print(f"  Emails failed:    {job.emails_failed}")
    print(f"  Embedding pending:{job.embedding_pending}")
    print(f"  Elapsed:          {elapsed:.1f}s")
    print("=" * 60)

    if job.embedding_pending:
        print(f"\nNote: {job.embedding_pending} emails need embedding repair.")
        print("Run EmailEmbeddingRepairService (or re-run this script) to fix them.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Gmail email indexing pipeline")
    parser.add_argument(
        "--after",
        metavar="YYYY-MM-DD",
        help="Only index emails after this date (overrides stored cursor)",
    )
    parser.add_argument(
        "--provider",
        default="gmail",
        choices=["gmail"],
        help="Email provider (default: gmail)",
    )
    parser.add_argument(
        "--resume-token",
        metavar="TOKEN",
        help="Gmail page token to resume from",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Wire up the pipeline but skip actual indexing",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N Gmail pages (100 emails/page). Default: unlimited.",
    )
    parser.add_argument(
        "--no-filter",
        action="store_true",
        help="Disable Gmail category filter — fetch ALL mail (debug only, not for production runs).",
    )
    parser.add_argument(
        "--tier",
        default="balanced",
        choices=["eco", "balanced", "performance"],
        help="Performance tier for email classification (default: balanced).",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=300,
        metavar="N",
        help="Emails per page (default: 300, max: 500).",
    )
    args = parser.parse_args()

    asyncio.run(main(args))

"""
Email Domain Models
===================

All domain models for the Email Indexing system.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §5.

Import rules: only stdlib + pydantic. No adapters/, services/, config/.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

# The stored grant is `src/domain/oauth_credential.py` (`OAuthCredential`).
ACCOUNT_REALM_GOOGLE = "google"
ACCOUNT_REALM_MICROSOFT = "microsoft"

# The `provider` of the Gmail index's stored records: the indexing state (doc `{user_id}_{provider}`) and the indexing
# jobs. A key of the email index's own data, not a credential kind (a credential's kind comes from the connector
# registry's spec, MULTI_DRIVE_RFC §4.2, T-6); it equals the Gmail kind's spelling only by history.
GMAIL_INDEX_PROVIDER = "gmail"


# ---------------------------------------------------------------------------
# Email fetch primitives (transient — not stored in Firestore)
# ---------------------------------------------------------------------------

@dataclass
class EmailMetadata:
    """
    Returned by EmailProviderPort.list_emails().
    The email gate decides from it. NOT stored in Firestore.
    """
    email_id: str
    provider: str
    subject: str
    from_address: str
    date: datetime
    labels: List[str]
    snippet: str            # First ~200 chars — classification signal only


@dataclass
class EmailFullContent:
    """
    Returned by EmailProviderPort.batch_get_full_content().
    Used by the indexing job for kept mail (attachment names, the card step) and
    EmailSearchAgent's get_email_details. Attachment bytes are never read here: `list_parts` / `get_part` do that.
    Body text is never stored in Firestore.
    """
    email_id: str
    body_text: str                          # Plain text body (HTML stripped by adapter)
    body_html: Optional[str]               # Original HTML (for structured extraction)
    attachments: List[str]                 # Attachment filenames only
    # The address parts of the To and Cc headers, in header order, as written (no case change); () when neither
    # header is present. Bcc is never visible.
    recipients: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EmailPart:
    """
    One attachment of a message, as `EmailProviderPort.list_parts()` lists it (EXTERNAL_FILES_RFC §4.7 P8a).
    Transient: never stored. `part_id` is the MIME part id the `email:` ref carries (`src/domain/email_ref.py`);
    `filename` the name as the sender wrote it (the index stores the same names); `mime` the part's declared type,
    a hint only (the bytes decide); `size` the provider's size in bytes. `handle` is opaque to everything but the
    adapter that listed the part (Gmail: the attachment id, empty for a part whose data is inline): it is valid only
    with the listing it came from, so it is passed back to `get_part` within one open and never stored.
    `displayed_inline` is True when the part's own headers ask to show it inside the body: `Content-Disposition:
    inline`, or a `Content-ID` with no `attachment` disposition. Not about where the data sits: `handle == ""` (data
    inside the message) is a different fact.
    """
    part_id: str
    filename: str
    mime: str
    size: int
    handle: str
    displayed_inline: bool = False


# ---------------------------------------------------------------------------
# Classification output
# ---------------------------------------------------------------------------

@dataclass
class EmailClassificationResult:
    """
    The email gate's decision for one listed mail (email index v2 §4; the rule in `email_gate.py`).
    keep=False → the mail is not stored. `reason` is a value of `KEEP_REASONS` / `DROP_REASONS`, or None when the
    answer gave none known; `forced` marks a keep the code rule made (the answer left the mail out, or its reason did
    not fit its decision).
    """
    email_id: str
    keep: bool
    reason: Optional[str] = None
    forced: bool = False
    # Set only by a classifier failure (parse_error | exception class): the email was never judged, so it is neither
    # kept nor read as noise.
    failure: Optional[str] = None


# ---------------------------------------------------------------------------
# Stored entity
# ---------------------------------------------------------------------------

class IndexedEmail(BaseModel):
    """
    Stored in Firestore ({env}_domain_email_facts_v1).
    The mail's record beside its card (email index v2): headers, attachment names and the lifecycle marks. Search
    and memory read the card. Stored documents that still carry removed v1 fields (the vectors; the gate's fact
    sentence `text`, `tags`, `category`, `metadata`) parse: unknown fields are ignored.
    Doc ID = email_id (idempotent upsert on retry).
    """
    # Identifiers
    email_id: str           # = Firestore document ID
    user_id: str
    account_id: str
    source: str             # "gmail" | "outlook"

    # Email-specific fields (top-level for display + search)
    subject: str
    from_address: str
    email_date: datetime        # original email date
    attachments: List[str] = []  # attachment filenames

    # Lifecycle
    state: str = "current"       # "current" | "archived"
    indexed_at: datetime
    consolidated_at: Optional[datetime] = None  # set when batch sent to ConsolidationAgent


# ---------------------------------------------------------------------------
# Indexing state + job
# ---------------------------------------------------------------------------

@dataclass
class IndexingState:
    """
    Cursor tracking per user per provider.
    Stored in {env}_email_indexing_state. Doc ID: {user_id}_{provider}.
    Each cursor written ONLY by its owning mode, ONLY at job completion.

    indexed_through:        incremental — newest email date seen (forward frontier)
    oldest_indexed_through: backfill   — date_from used (oldest date queried)
    cursor_reindex:         reindex    — date_from used (oldest date queried, ~now-3yr)

    Incremental bootstrap (when indexed_through is null):
        date_from = max(oldest_indexed_through, cursor_reindex)
        or today if all three are null.
    """
    user_id: str
    provider: str
    indexed_through: Optional[datetime] = None          # None = incremental never run
    oldest_indexed_through: Optional[datetime] = None   # None = backfill never run
    cursor_reindex: Optional[datetime] = None           # None = reindex never run
    # R1 (SECRETS_AT_REST_RFC §6.2, F1/F2): written only by `IndexedEmailRepository.set_reconnect_flags`, never by
    # `update_indexing_state` (which owns the cursors). The once-per-transition notice flag, and the deliberate
    # disconnect mark (a consent clears both).
    reconnect_notified: bool = False
    disconnected_by_user_at: Optional[datetime] = None


class IndexingJob(BaseModel):
    """
    One record per indexing run.
    Used for resume-on-retry (next_page_token), Cabinet job history, and error reporting.
    Stored in {env}_email_indexing_jobs_v1.
    """
    job_id: str
    user_id: str
    account_id: str = ""                    # needed by worker to build IndexedEmail
    provider: str
    triggered_by: str                       # "cabinet" | "scheduler" | "script"
    status: str                             # "running"|"completed"|"failed"|"failed_auth"
    mode: str = "incremental"              # "incremental" | "reindex" | "backfill"
    next_page_token: Optional[str] = None   # primary resume cursor; set → job not yet complete
    last_email_date: Optional[datetime] = None  # fallback cursor if page token expired
    backfill_until: Optional[datetime] = None   # stop date for backfill (stored for worker resume)
    # Running max/min email dates across all Cloud Tasks invocations.
    # indexed_through in IndexingState is ONLY written at job completion using these values.
    max_email_date: Optional[datetime] = None   # newest email date seen across all pages
    min_email_date: Optional[datetime] = None   # oldest email date seen across all pages
    emails_fetched: int = 0
    emails_stored: int = 0
    emails_failed: int = 0
    errors: List[Dict[str, Any]] = []       # capped at 100: {email_id, stage, error}
    started_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------

class EmailExclusion(BaseModel):
    """
    Sender/domain/subject pattern to skip before LLM classification.
    Auto-populated when classifier detects recurring low-value senders.
    Stored in {env}_email_exclusions. Doc ID = exclusion_id.
    """
    exclusion_id: str = ""  # Populated by adapter from Firestore doc.id; set before save
    user_id: str
    pattern_type: str   # "sender_email" | "sender_domain" | "subject_pattern"
    pattern: str
    reason: str
    created_at: datetime

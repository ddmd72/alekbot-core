"""
Email Domain Models
===================

All domain models for the Email Indexing system.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §5.

Import rules: only stdlib + pydantic. No adapters/, services/, config/.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

@dataclass
class OAuthCredentials:
    """OAuth tokens for a connected email provider. Stored in Firestore."""
    user_id: str
    provider: str           # "gmail" | "outlook"
    access_token: str
    refresh_token: str
    token_expiry: datetime
    scopes: List[str]
    email_address: str      # provider account email (display only)


# ---------------------------------------------------------------------------
# Email fetch primitives (transient — not stored in Firestore)
# ---------------------------------------------------------------------------

@dataclass
class EmailMetadata:
    """
    Returned by EmailProviderPort.list_emails().
    Used during classification. NOT stored in Firestore.
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
    Used by EmailClassificationService (ambiguous snippet) and
    EmailSearchAgent Mode B (deep search + markitdown attachment parsing).
    Attachment binaries populated only when deep=True is passed to the adapter.
    Body text is never stored in Firestore.
    """
    email_id: str
    body_text: str                          # Plain text body (HTML stripped by adapter)
    body_html: Optional[str]               # Original HTML (for structured extraction)
    attachments: List[str]                 # Attachment filenames only
    attachment_binaries: Dict[str, bytes]  # filename → bytes; empty dict if deep=False


# ---------------------------------------------------------------------------
# Classification output
# ---------------------------------------------------------------------------

@dataclass
class EmailClassificationResult:
    """
    Output of EmailClassificationService per email.
    valuable=False → email discarded, not written to Firestore.
    """
    email_id: str
    valuable: bool
    category: Optional[str]   # travel|finance|healthcare|work|legal|personal|subscription
    fact: Optional[str]        # Self-contained fact sentence; becomes IndexedEmail.text
    tags: List[str]
    reason: str                # Brief explanation (~15 words) — for debug/audit only


# ---------------------------------------------------------------------------
# Stored entity
# ---------------------------------------------------------------------------

class IndexedEmail(BaseModel):
    """
    Stored in Firestore ({env}_domain_email_facts_v1).
    Mirrors FactEntity structure to enable identical RRF search pattern.
    Doc ID = email_id (idempotent upsert on retry).
    """
    # Identifiers
    email_id: str           # = Firestore document ID
    user_id: str
    account_id: str
    source: str             # "gmail" | "outlook"

    # Content — mirrors FactEntity
    text: str                                       # extracted fact sentence
    vector: Optional[List[float]] = None            # embed(text)
    tags_vector: Optional[List[float]] = None       # embed(tags joined)
    metadata_vector: Optional[List[float]] = None   # embed(structured values: amounts, dates, refs)
    attachments_vector: Optional[List[float]] = None  # embed(attachment filenames); None if no attachments

    # Classification
    tags: List[str]
    category: str
    metadata: Dict[str, Any]    # subject, from_address, snippet + structured entities

    # Email-specific fields (top-level for display + search)
    subject: str
    from_address: str
    email_date: datetime        # original email date
    attachments: List[str] = []  # attachment filenames

    # Lifecycle
    state: str = "current"       # "current" | "archived"
    indexed_at: datetime
    embedding_pending: bool = False   # True if vectors not yet computed (repair job picks these up)
    consolidated_at: Optional[datetime] = None  # set when batch sent to ConsolidationAgent


# ---------------------------------------------------------------------------
# Indexing state + job
# ---------------------------------------------------------------------------

@dataclass
class IndexingState:
    """
    Cursor tracking per user per provider.
    Stored in {env}_email_indexing_state. Doc ID: {user_id}_{provider}.
    Advances only after each chunk completes successfully (idempotent retry).
    """
    user_id: str
    provider: str
    indexed_through: Optional[datetime]  # None = never indexed


class IndexingJob(BaseModel):
    """
    One record per indexing run.
    Used for resume-on-retry (next_page_token), Cabinet job history, and error reporting.
    Stored in {env}_email_indexing_jobs_v1.
    """
    job_id: str
    user_id: str
    provider: str
    triggered_by: str                       # "cabinet" | "scheduler" | "script"
    status: str                             # "running"|"completed"|"failed"|"failed_auth"
    next_page_token: Optional[str] = None   # primary resume cursor
    last_email_date: Optional[datetime] = None  # fallback cursor if page token expired
    emails_fetched: int = 0
    emails_stored: int = 0
    emails_failed: int = 0
    embedding_pending: int = 0
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

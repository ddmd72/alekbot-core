"""
File-access policy constants (domain).

Link-lifetime policy for capability tokens that gate private file access. These
are business-rule values (how long a delivered-file link stays valid), not a
property of any one service — keeping them in the domain lets both
FileAccessTokenService and FileLinkService depend on the policy without importing
each other (REQ-ARCH-22: services must not import services).
"""

# Default capability-link lifetime: documents, generated HTML/PDF/DOCX,
# deep-research reports, user uploads.
DEFAULT_FILE_LINK_TTL = 30 * 24 * 3600  # 30 days

# Shorter lifetime for the most sensitive artifact (daily email review = inbox PII),
# which is also Cabinet-cookie-gated at the /f route.
EMAIL_REVIEW_FILE_LINK_TTL = 5 * 24 * 3600  # 5 days

"""
Sandbox client errors (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.9). Pure data.

Two tiny exception classes of one contract live in this module, by decision (CLAUDE.md's one-class
rule is waived here on purpose; precedent `drive_match`).

Public interface:
    SandboxUnavailable     the service cannot take the work now (429, other 503, refused connection):
                           systemic, work is deferred, nothing is charged to the file
    SandboxMisconfigured   401 / 403: a deployment mistake, not a file; ERROR log, systemic defer
"""
from __future__ import annotations


class SandboxUnavailable(Exception):
    """The sandbox cannot serve the request right now. Never the fault of the input."""

    # Same shape as `DriveModelUnavailable`: a deferral must not count toward a file's attempts.
    charges_file = False


class SandboxMisconfigured(Exception):
    """The sandbox rejected the caller's identity (401 / 403): the deployment is wrong."""

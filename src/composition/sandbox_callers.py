"""
Sandbox caller sets (UNTRUSTED_CONTENT_SANDBOX_RFC §4.4; plan G18, G24).

A *group* is one runtime's identity set, i.e. the op prefixes one caller may use (indexer: `drive.*` + `media.*`; main:
`chat.*` + `render.*`; S1's render service: group RENDER). Sets are read from `SANDBOX_CALLERS_<GROUP>` as `:`-separated
emails: the values travel through Make, the shell, Cloud Build substitutions and `--set-env-vars`, so a comma is a loud
error (G24). Disjointness is checked across groups, never across prefixes. No email value is ever logged or placed in an
error message: only group names and counts.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Mapping

from ..utils.logger import logger

ENV_PREFIX = "SANDBOX_CALLERS_"


class SandboxCallersError(Exception):
    """Start-up refusal: unusable caller configuration."""


def load_caller_sets(env: Mapping[str, str]) -> Dict[str, FrozenSet[str]]:
    """Read every `SANDBOX_CALLERS_<GROUP>` from `env`; normalise (strip, lowercase) and check disjointness.

    On Cloud Run (`K_SERVICE` set) no group at all, or an empty group, raises. Locally empty groups are dropped.
    """
    on_cloud_run = bool(env.get("K_SERVICE"))
    sets: Dict[str, FrozenSet[str]] = {}
    for key in sorted(env):
        if not key.startswith(ENV_PREFIX):
            continue
        group = key[len(ENV_PREFIX):]
        raw = env[key] or ""
        if "," in raw:
            logger.error(f"Sandbox callers: group {group} uses a comma; lists are ':'-separated")
            raise SandboxCallersError(f"group {group}: caller list must be ':'-separated, a comma is not allowed")
        entries = [p.strip() for p in raw.split(":") if p.strip()]
        for entry in entries:
            if any(c.isspace() for c in entry) or ";" in entry or entry.count("@") != 1:
                logger.error(f"Sandbox callers: group {group} holds a malformed entry")
                raise SandboxCallersError(f"group {group}: malformed caller entry (one '@', no spaces or ';')")
        emails = frozenset(e.lower() for e in entries)
        if not emails:
            if on_cloud_run:
                logger.error(f"Sandbox callers: group {group} is empty on Cloud Run")
                raise SandboxCallersError(f"group {group}: caller set is empty on Cloud Run")
            logger.warning(f"Sandbox callers: group {group} is empty (local, ignored)")
            continue
        sets[group] = emails
    if not sets and on_cloud_run:
        logger.error("Sandbox callers: no caller group configured on Cloud Run")
        raise SandboxCallersError("no caller group configured on Cloud Run")
    assert_disjoint(sets)
    logger.info(f"Sandbox callers: {len(sets)} group(s) loaded: {', '.join(sorted(sets))}")
    return sets


def assert_disjoint(sets: Mapping[str, FrozenSet[str]]) -> None:
    """Refuse start when one email sits in two groups (no runtime admitted through another's identity)."""
    seen: Dict[str, str] = {}
    for group in sorted(sets):
        for email in sets[group]:
            if email in seen:
                logger.error(f"Sandbox callers: groups {seen[email]} and {group} share a caller")
                raise SandboxCallersError(f"groups {seen[email]} and {group} share a caller identity")
            seen[email] = group

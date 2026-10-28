"""What the G3 eval can swap without touching Firestore: the prompt files and the specialist's model.

`--prompts-from-snapshot`: the drive specialist's prompt (four tokens, a blueprint, a profile) and Smart's edited
`PROTOCOL_AGENT_SELECTION` exist only as files in `prompts_snapshot/` until the owner uploads them. The overlays of the
consolidation dry-run kit (`scripts/consolidation/s3_dry_run_kit.py`, the one implementation) serve exactly those seven
files from the snapshot and every other id from the real repositories; nothing is written (the overlays are read only).

`--specialist-provider` / `--specialist-tier`: applied to a COPY of the user config the eval hands to
`build_drive_fixture`; the stored config is never touched.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from scripts.consolidation.s3_dry_run_kit import (
    SNAPSHOT_DIR, SnapshotBlueprintOverlay, SnapshotProfileOverlay, SnapshotTokenOverlay)

SPECIALIST_AGENT_TYPE = "drive"   # DRIVE_SPECIALIST.agent_type

OVERLAY_TOKEN_IDS: Tuple[str, ...] = (
    "COGNITIVE_PROCESS_DRIVE", "PROTOCOL_DRIVE_SEARCH", "PROTOCOL_DRIVE_NOTES", "OUTPUT_FORMAT_DRIVE",
    "PROTOCOL_AGENT_SELECTION")
OVERLAY_BLUEPRINT_IDS: Tuple[str, ...] = ("drive_agent_v1",)
OVERLAY_PROFILE_IDS: Tuple[str, ...] = ("drive",)

PROMPTS_FIRESTORE = "firestore"
PROMPTS_SNAPSHOT = "snapshot"


def overlay_files(snapshot_dir: Path = SNAPSHOT_DIR) -> Tuple[Path, ...]:
    """The seven snapshot files the overlay serves (a token lives under `system/` or `user/`)."""
    root = Path(snapshot_dir)
    files = []
    for token_id in OVERLAY_TOKEN_IDS:
        found = [p for sub in ("system", "user") if (p := root / "tokens" / sub / f"{token_id}.groovy").is_file()]
        files.append(found[0] if found else root / "tokens" / "system" / f"{token_id}.groovy")
    files += [root / "blueprints" / f"{b}.yaml" for b in OVERLAY_BLUEPRINT_IDS]
    files += [root / "profiles" / f"{p}.yaml" for p in OVERLAY_PROFILE_IDS]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise FileNotFoundError(f"the snapshot lacks {missing}")
    return tuple(files)


def install_snapshot_prompts(service: Any, snapshot_dir: Path = SNAPSHOT_DIR) -> None:
    """Swap the assembly service's three repositories for overlays and drop its cache. Call before any agent is built."""
    overlay_files(snapshot_dir)   # a missing file fails here, not as a silent Firestore read of the old prompt
    service.token_repo = SnapshotTokenOverlay(service.token_repo, service.security_port, snapshot_dir, OVERLAY_TOKEN_IDS)
    service.profile_repo = SnapshotProfileOverlay(service.profile_repo, snapshot_dir, OVERLAY_PROFILE_IDS)
    service.blueprint_repo = SnapshotBlueprintOverlay(service.blueprint_repo, snapshot_dir, OVERLAY_BLUEPRINT_IDS)
    service.invalidate_cache()


def specialist_config(config: Any, provider: Optional[str], tier: Optional[str]) -> Any:
    """`config` itself when nothing is overridden; otherwise a deep copy with the specialist's provider and tier set."""
    if provider is None and tier is None:
        return config
    from src.domain.user import PerformanceTier

    copy = config.model_copy(deep=True)
    if provider is not None:
        copy.agent_providers = {**(copy.agent_providers or {}), SPECIALIST_AGENT_TYPE: provider}
    if tier is not None:
        copy.agent_tiers = {**(copy.agent_tiers or {}), SPECIALIST_AGENT_TYPE: PerformanceTier(tier)}
    return copy


def run_setup(prompts: str, provider: Optional[str], tier: Optional[str]) -> Mapping[str, str]:
    """The fields that say what ran, for the report and the console."""
    return {"prompts": prompts, "specialist_provider": provider or "config", "specialist_tier": tier or "config"}


def setup_line(setup: Mapping[str, str]) -> str:
    return (f"prompts: {setup['prompts']} | specialist provider: {setup['specialist_provider']}, "
            f"tier: {setup['specialist_tier']}")

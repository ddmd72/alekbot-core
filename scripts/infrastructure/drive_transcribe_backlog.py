"""
Backlog estimate before transcription is turned on (USER_DRIVE_INDEXING_RFC §27.15 step 5): READ-ONLY. Walks one user's
index, sums the duration of the audio and video records still to transcribe and prints the minutes and what they would
cost.

Duration: a video record's `media.video_duration_ms`; an audio record has no duration here, so its bytes are
counted at 10.8 MB per hour (the spike's figure, §27.2) and the line says it is an estimate. A video without the facet is
estimated the same way, as its own line. Files over TRANSCRIBE_MAX_BYTES (8 GiB, `too_large`) or over
TRANSCRIBE_MAX_SECONDS (4 h, `too_long`) get a stub and no model call, so they are counted apart and not priced. Records
whose transcript is already `current` are counted and left out of the estimate.

Cost lines: the audio-hour rates $0.08 (the promotional batch price, Δ132), $0.16 (after 2026-12-31) and $0.40 (the old
ceiling), and the HELD-PRICE figure (G7): what the stage would RESERVE, `file_estimate_usd` at TRANSCRIBE_HOUR_USD_ESTIMATE
plus each window's prompt allowance, which the monthly cap counts. The held price is the repo's billing price, higher than the
provider's promotional one on purpose (never under-report).

Output goes to stdout and to `scripts/memory/drive_transcribe_backlog.txt` (gitignored; Δ67's rule). Only totals: no file
name, id or path is printed or written.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_transcribe_backlog.py --user <user_id> [--connection <id>]
        [--out <path under scripts/memory/>]
Exit code: 0, or 1 when nothing is stored for the connection.
"""
import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.infrastructure.drive_connection_arg import resolve_connection  # noqa: E402
from src.domain.connection_scope import ConnectionScope  # noqa: E402
from src.domain.drive_index import DriveIndexRecord  # noqa: E402
from src.domain.drive_index_params import (  # noqa: E402
    TRANSCRIBE_HOUR_USD_ESTIMATE, TRANSCRIBE_MAX_BYTES, TRANSCRIBE_MAX_SECONDS,
)
from src.domain.drive_index_policy import TRANSCRIBED_KINDS  # noqa: E402
from src.domain.drive_transcript_plan import CURRENT, file_estimate_usd  # noqa: E402
from src.ports.drive_index_repository import DriveIndexRepository  # noqa: E402

BYTES_PER_HOUR = 10.8e6                       # the spike's audio figure (§27.2)
RATES_USD_PER_HOUR: Tuple[float, ...] = (0.08, 0.16, 0.40)
_PAGE = 500
_DEFAULT_OUT = Path(__file__).resolve().parents[2] / "scripts" / "memory" / "drive_transcribe_backlog.txt"


@dataclass
class Backlog:
    """Totals per bucket; seconds are sums, never per-file values."""
    counts: Dict[str, int] = field(default_factory=dict)
    seconds: Dict[str, float] = field(default_factory=dict)
    held_usd: float = 0.0

    def add(self, bucket: str, seconds: float = 0.0) -> None:
        self.counts[bucket] = self.counts.get(bucket, 0) + 1
        self.seconds[bucket] = self.seconds.get(bucket, 0.0) + seconds


def _video_seconds(rec: DriveIndexRecord) -> Optional[float]:
    ms = rec.media.video_duration_ms
    return ms / 1000.0 if ms > 0 else None


def classify(rec: DriveIndexRecord) -> Tuple[str, float]:
    """(bucket, seconds) of one record; the bucket `skip` is a record that is not media."""
    if rec.is_folder or rec.kind not in TRANSCRIBED_KINDS:
        return "skip", 0.0
    if rec.transcript_state == CURRENT:
        return "already_transcribed", 0.0
    if rec.size_bytes > TRANSCRIBE_MAX_BYTES:
        return "too_large", 0.0
    seconds = _video_seconds(rec) if rec.kind == "video" else None
    if seconds is not None:
        bucket = "video"
    else:
        seconds = rec.size_bytes / BYTES_PER_HOUR * 3600.0
        bucket = "audio_by_bytes" if rec.kind == "audio" else "video_by_bytes"
    if seconds > TRANSCRIBE_MAX_SECONDS:
        return "too_long", 0.0
    return bucket, seconds


def tally(records: Sequence[DriveIndexRecord], into: Optional[Backlog] = None) -> Backlog:
    out = into or Backlog()
    for rec in records:
        bucket, seconds = classify(rec)
        if bucket == "skip":
            continue
        out.add(bucket, seconds)
        if seconds > 0:
            out.held_usd += file_estimate_usd(seconds)
    return out


_PRICED = ("video", "audio_by_bytes", "video_by_bytes")
_LABELS = {"video": "video, duration facet", "audio_by_bytes": "audio, estimated from bytes (10.8 MB/h)",
           "video_by_bytes": "video without a duration facet, estimated from bytes",
           "already_transcribed": "already transcribed (not in the estimate)",
           "too_large": "over 8 GiB (stub, no model)", "too_long": "over 4 h (stub, no model)"}


def report(backlog: Backlog) -> List[str]:
    lines: List[str] = []
    for bucket in (*_PRICED, "already_transcribed", "too_large", "too_long"):
        if bucket in backlog.counts:
            tail = f", {backlog.seconds[bucket] / 60.0:,.0f} min" if bucket in _PRICED else ""
            lines.append(f"{_LABELS[bucket]}: {backlog.counts[bucket]} files{tail}")
    hours = sum(backlog.seconds.get(b, 0.0) for b in _PRICED) / 3600.0
    lines.append(f"to transcribe: {sum(backlog.counts.get(b, 0) for b in _PRICED)} files, {hours * 60:,.0f} min "
                 f"({hours:,.1f} audio hours)")
    for rate in RATES_USD_PER_HOUR:
        lines.append(f"  at ${rate:.2f} per hour: ${hours * rate:,.2f}")
    lines.append(f"  held price (what the stage reserves, ${TRANSCRIBE_HOUR_USD_ESTIMATE:.2f} per hour plus the window "
                 f"prompts, G7): ${backlog.held_usd:,.2f}")
    return lines


async def scan(repo: DriveIndexRepository, scope: ConnectionScope) -> Backlog:
    backlog, after = Backlog(), None
    while True:
        page = await repo.records_page(scope, after, _PAGE)
        tally(page, backlog)
        if len(page) < _PAGE:
            return backlog
        after = page[-1].item_id


async def _main(user_id: str, connection_id: Optional[str], out: Path) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    connections = FirestoreDriveConnectionRepository(db, env.drive_connections_collection,
                                                     env.drive_user_settings_collection)
    conn, why = await resolve_connection(connections, user_id, connection_id)
    if conn is None:
        print(f"refused: {why}")
        return 1
    repo = FirestoreDriveIndexRepository(db, env)
    if await repo.get_sync_state(conn.scope) is None:
        print("no sync state: nothing is indexed for this connection")
        return 1
    text = "\n".join(report(await scan(repo, conn.scope))) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"(written to {out})")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", required=True, help="the user whose index is read")
    parser.add_argument("--connection", default=None, help="the connection (default: the user's one live connection)")
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT, help="report file (keep it under scripts/memory/)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.user, args.connection, args.out)))

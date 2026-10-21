"""
Transcript eval (USER_DRIVE_INDEXING_RFC §27.14 "Eval", the T1 gate) — OWNER-RUN, LIVE, PAID.

    python -m scripts.validation.drive_transcript_fixtures            # once: make the media (see that script)
    python -m scripts.validation.drive_eval.transcript_eval            # prints the estimate and stops
    python -m scripts.validation.drive_eval.transcript_eval --yes      # runs it
    python -m scripts.validation.drive_eval.transcript_eval --yes --record   # first run: writes bars_transcripts.json

Each clip of the generated fixture set (`tests/fixtures/drive_transcripts/gold.json`, media under `scripts/memory/`) goes
through the same steps the indexer's stage takes, with the same domain code: the window plan, ffmpeg cut to Opus, upload to
the provider's file store, one Gemini batch per round through `DriveTranscriptRunner` (the transcriber agent and its prompt
from Firestore), the coverage and time checks that send a window back (resubmitted up to MAX_ATTEMPTS, then a gap marker),
and `stitch_job`. Then the metrics of `transcript_metrics.py` against the gold: word error rate, speaker-turn agreement,
timestamp error at probe lines, seam duplicates and losses, speaker over-count, interpolation share, coverage resubmits,
with Ukrainian reported separately.

COST: it prints an estimate (the stage's own reservation per clip, plus a resubmit allowance) and REQUIRES `--yes` before it
calls the provider (memory `feedback_no_live_runs_without_ok`: a live run over a few dollars needs the owner's word). No
account is billed: results are read, not booked. Needs GEMINI_API_KEY and Firestore access to the prompt documents (.env).

Bars: `--record` writes `bars_transcripts.json` from THIS run's measurements (§24.11 practice: floored or ceilinged so a bar
never passes a run that did not earn it); no bar is invented here, and none ships until a first live run. Without bars the
run prints its table and says so; with bars it names every metric that is worse and exits 1.

The report (per-clip numbers, no transcript text) goes to `scripts/memory/transcript_eval_report.json`.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.batch_transcription import BatchStatus
from src.domain.drive_index_params import MAX_ATTEMPTS
from src.domain.drive_transcript import (
    JobWindow, StitchedTranscript, TranscriptJob, WindowOutcome, WindowSpec, WindowTranscript, WindowInput,
)
from src.domain.drive_transcript_job import stitch_job
from src.domain.drive_transcript_plan import file_estimate_usd
from src.domain.drive_transcript_windows import coverage_ok, plan_windows, times_ok

from .transcript_metrics import aggregate, clip_metrics, make_bars, regressions

_REPO = Path(__file__).resolve().parents[3]
GOLD = _REPO / "tests" / "fixtures" / "drive_transcripts" / "gold.json"
BARS = _REPO / "tests" / "fixtures" / "drive_transcripts" / "bars_transcripts.json"
MEDIA = _REPO / "scripts" / "memory" / "drive_transcripts"
REPORT = _REPO / "scripts" / "memory" / "transcript_eval_report.json"
RESUBMIT_ALLOWANCE = 0.25        # the estimate adds this share for windows sent back
POLL_EVERY_S = 60.0
MAX_WAIT_S = 6 * 3600.0
EVAL_ACCOUNT = "eval-account"
_MIME = "audio/ogg"

Cut = Callable[[Path, WindowSpec, Path], None]
Sleep = Callable[[float], Awaitable[None]]


class EvalError(Exception):
    """No verdict: a missing medium, a batch that failed or never finished, a setup that cannot run."""


def load_gold(path: Path = GOLD) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def gold_sha256(path: Path = GOLD) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def estimate_usd(clips: Sequence[Mapping[str, Any]]) -> float:
    """Upper bound of the run: the stage's reservation for each clip, with an allowance for resubmitted windows."""
    return sum(file_estimate_usd(float(c["duration_s"])) for c in clips) * (1.0 + RESUBMIT_ALLOWANCE)


def media_path(clip: Mapping[str, Any], media_dir: Path = MEDIA) -> Path:
    return media_dir / f"{clip['id']}.{'m4a' if clip['kind'] == 'audio' else 'mp4'}"


def ffmpeg_cut(source: Path, spec: WindowSpec, out: Path) -> None:
    """The window as the stage's child cuts it: audio only, mono 16 kHz Opus 24 kbit/s (media.py's output flags)."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{spec.start_s:.3f}", "-t", f"{spec.duration_s:.3f}",
                    "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus",
                    "-b:a", "24k", "-f", "opus", str(out)], check=True, capture_output=True)


async def _wait(port: Any, batch: Any, sleep: Sleep, poll_every_s: float, max_wait_s: float) -> None:
    waited = 0.0
    while True:
        status = await port.poll(batch)
        if status is BatchStatus.SUCCEEDED:
            return
        if status in (BatchStatus.FAILED, BatchStatus.CANCELLED, BatchStatus.EXPIRED):
            raise EvalError(f"the batch ended {status.value}")
        if waited >= max_wait_s:
            raise EvalError("the batch did not finish in time")
        await sleep(poll_every_s)
        waited += poll_every_s


async def transcribe_clip(clip: Mapping[str, Any], source: Path, *, port: Any, runner: Any, cut: Cut, workdir: Path,
                          sleep: Sleep, poll_every_s: float = POLL_EVERY_S, max_wait_s: float = MAX_WAIT_S,
                          ) -> Tuple[StitchedTranscript, Tuple[WindowSpec, ...], int]:
    """One clip through the stage's steps. Returns (the stitched transcript, the window plan, the number of windows sent
    back by the coverage or time checks)."""
    specs = plan_windows(float(clip["duration_s"]))
    refs: Dict[int, Any] = {}
    results: Dict[int, WindowTranscript] = {}
    attempts: Dict[int, int] = {s.index: 0 for s in specs}
    gaps: set = set()
    resubmits = 0
    try:
        for spec in specs:
            window_path = workdir / f"{clip['id']}.{spec.index}.opus"
            cut(source, spec, window_path)
            refs[spec.index] = await port.upload_audio(str(window_path), _MIME)
        open_windows = list(specs)
        while open_windows:
            inputs = [WindowInput(key=f"{clip['id']}:{s.index}", window=s, file_name=refs[s.index].name,
                                  file_mime=_MIME) for s in open_windows]
            batch = await runner.submit(inputs, display_name=str(clip["id"]))
            await _wait(port, batch, sleep, poll_every_s, max_wait_s)
            collected = await runner.collect(batch, account_id=EVAL_ACCOUNT, bill=False)
            still: List[WindowSpec] = []
            for spec in open_windows:
                result = collected.get(f"{clip['id']}:{spec.index}")
                if result is not None and result.outcome is WindowOutcome.REFUSED:
                    gaps.add(spec.index)
                    continue
                transcript = result.transcript if result is not None and result.outcome is WindowOutcome.OK else None
                good = (transcript is not None and coverage_ok(spec, transcript.segments, transcript.no_speech)
                        and times_ok(spec, transcript.segments))
                if good:
                    results[spec.index] = transcript     # type: ignore[assignment]
                    continue
                attempts[spec.index] += 1
                resubmits += 1
                if attempts[spec.index] >= MAX_ATTEMPTS:
                    gaps.add(spec.index)
                else:
                    still.append(spec)
            open_windows = still
        job = TranscriptJob(user_id=EVAL_ACCOUNT, item_id=str(clip["id"]), content_key=str(clip["id"]),
                            duration_s=float(clip["duration_s"]),
                            windows=tuple(JobWindow(spec=s, state="done", gap=s.index in gaps) for s in specs))
        return stitch_job(job, results), specs, resubmits
    finally:
        for ref in refs.values():
            try:
                await port.delete_file(ref)
            except Exception as exc:   # noqa: BLE001 — a leaked eval file expires on its own; the run must still report
                print(f"could not delete an uploaded file: {type(exc).__name__}", file=sys.stderr)


def score(clip: Mapping[str, Any], stitched: StitchedTranscript, specs: Sequence[WindowSpec],
          resubmits: int) -> Dict[str, Any]:
    return clip_metrics(clip, stitched, window_specs=specs, coverage_resubmits=resubmits)


def format_table(table: Mapping[str, Any]) -> List[str]:
    lines: List[str] = []
    for scope in ("by_family", "by_language"):
        lines.append(scope)
        for name, row in table[scope].items():
            cells = ", ".join(f"{k}={v:.4f}" for k, v in row.items() if k != "clips")
            lines.append(f"  {name} ({int(row['clips'])} clips): {cells}")
    return lines


async def run_eval(clips: Sequence[Mapping[str, Any]], *, port: Any, runner: Any, cut: Cut, media_dir: Path,
                   sleep: Sleep, poll_every_s: float = POLL_EVERY_S) -> List[Dict[str, Any]]:
    """Every clip in order; a missing medium is `EvalError` before any call (no half-paid run)."""
    missing = [c["id"] for c in clips if not media_path(c, media_dir).exists()]
    if missing:
        raise EvalError(f"no media for {missing}: run drive_transcript_fixtures first")
    out: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="transcript-eval-") as tmp:
        for clip in clips:
            stitched, specs, resubmits = await transcribe_clip(clip, media_path(clip, media_dir), port=port,
                                                               runner=runner, cut=cut, workdir=Path(tmp), sleep=sleep,
                                                               poll_every_s=poll_every_s)
            out.append(score(clip, stitched, specs, resubmits))
    return out


def build_live() -> Tuple[Any, Any, str]:
    """(batch port, transcript runner, model). Raises EvalError instead of falling back to anything."""
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise EvalError("GEMINI_API_KEY is not set (.env)")
    from google.cloud import firestore

    from src.adapters.firestore_account_repo import FirestoreAccountRepository
    from src.adapters.gemini_adapter import GeminiAdapter
    from src.adapters.gemini_batch_adapter import GeminiBatchAdapter
    from src.composition.drive_transcript_runner import DriveTranscriptRunner
    from src.composition.service_container import ServiceContainer
    from src.config.settings import load_settings
    from src.domain.llm import PerformanceTier
    from src.services.prompt_builder import PromptBuilder

    model = GeminiAdapter.MODEL_TIERS[PerformanceTier.BALANCED]
    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    settings = load_settings()
    env_config = settings["ENVIRONMENT_CONFIG"]
    accounts = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    container = ServiceContainer(config=settings, db_client=db, env_config=env_config, account_repo=accounts)
    port = GeminiBatchAdapter(api_key=key, model=model)
    runner = DriveTranscriptRunner(port, PromptBuilder(repo=None, assembly_service=container.assembly_service),
                                   quota_service=None)
    return port, runner, model


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval.transcript_eval",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--yes", action="store_true", help="run the live, paid eval (default: print the estimate only)")
    parser.add_argument("--record", action="store_true", help="write the bars from this run instead of comparing")
    parser.add_argument("--only", action="append", default=[], help="run only these clip ids (repeatable)")
    parser.add_argument("--gold", type=Path, default=GOLD)
    parser.add_argument("--bars", type=Path, default=BARS)
    parser.add_argument("--media-dir", type=Path, default=MEDIA)
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    gold = load_gold(args.gold)
    clips = [c for c in gold["clips"] if not args.only or c["id"] in args.only]
    if not clips:
        print("no clips selected", file=sys.stderr)
        return 2
    print(f"{len(clips)} clips, {sum(float(c['duration_s']) for c in clips) / 3600:.2f} audio hours; "
          f"estimated cost up to ${estimate_usd(clips):.2f} (held price, resubmit allowance included)")
    if not args.yes:
        print("estimate only: re-run with --yes to call the provider")
        return 0
    try:
        port, runner, model = build_live()
        measured = asyncio.run(run_eval(clips, port=port, runner=runner, cut=ffmpeg_cut, media_dir=args.media_dir,
                                        sleep=asyncio.sleep))
    except EvalError as exc:
        print(f"no verdict: {exc}", file=sys.stderr)
        return 2
    table = aggregate(measured)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"clips": measured, "table": table}, indent=1, sort_keys=True), encoding="utf-8")
    print("\n".join(format_table(table)))
    if args.record:
        bars = make_bars(table, recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         gold_sha256=gold_sha256(args.gold), model=model)
        args.bars.write_text(json.dumps(bars, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"bars recorded: {args.bars}")
        return 0
    if not args.bars.exists():
        print("no bars yet: run once with --record to set them from a live run")
        return 0
    recorded = json.loads(args.bars.read_text(encoding="utf-8"))
    if recorded.get("gold_sha256") != gold_sha256(args.gold):
        print("the gold changed since the bars were recorded: no verdict; re-record", file=sys.stderr)
        return 2
    problems = regressions(recorded, table)
    print("\n".join(problems) if problems else "within the bars")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

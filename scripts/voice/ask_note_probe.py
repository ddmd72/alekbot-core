#!/usr/bin/env python3
"""
Probe P1 + P2 of VOICE_CALL_CONTROL_CHANNEL_RFC §10 (xAI, grok-voice-3.0): does the interpreter honour the
"Ask Lelik" note on speech in a given language, and still translate without it? Is `speech_started` reliable enough
to carry the note (one per utterance, before the commit, also when a pause splits a question)?

Each case is one fresh xAI session run the way the relay runs an interpreter call: the interpreter persona token
(`prompts_snapshot/tokens/user/VOICE_PERSONA_INTERPRETER.groovy`, the edited one) plus a short line on the caller's
languages, provider-owned turns (the adapter's own turn detection), the web pickup note and Lelik's opening, then one
synthesized utterance streamed in real time. In the "note" cases the production `AskLatch` is armed before the
utterance and the production note (`_ASK_NOTE`) is sent on every `speech_started` the latch says to mark, exactly as
`VoiceSessionService` does.

Cases, each repeated --runs times:
  ask+note           an `ask:` sample with the button pressed   (P1: Lelik answers the caller, does not translate)
  ask-no-note        the same sample without the note           (P1: Lelik translates it, whatever it says)
  translate-no-note  a `translate:` sample, no note             (P1: plain translation)
P2 is read from every case: speech_started vs committed counts, and whether each committed caller turn's
previous_item_id is the note.

Samples come from a UTF-8 text file passed on the command line, one utterance per line:
  ask: Which language should you use with the dentist?
  ask: Can you say that more politely? | I did not mean to be rude.
  translate: These tablets are for my pollen allergy.
  # a comment
` | ` inside a line puts a pause (--pause-s) there: the split-question case of P2. Keep non-English samples in a
gitignored path (e.g. scripts/memory/), never in the repo.

Speech is synthesized with macOS `say` (a voice for --lang must be installed: System Settings > Accessibility >
Spoken Content > System voice > Manage Voices) and `afconvert` to G.711 mu-law 8 kHz.

Usage:
  python scripts/voice/ask_note_probe.py --lang pt-PT --caller-language Portuguese --other-language English \
      --samples scripts/memory/ask_samples_pt.txt [--voice NAME] [--runs 3] [--pause-s 1.5] \
      [--out scripts/memory/ask_probe_pt.json] [--dry-run]
--dry-run synthesizes the audio, prints the plan and the cost estimate, and connects to nothing.
Cost: xAI bills $0.08 per audio minute; the estimate is printed before any session opens.
"""
import argparse
import asyncio
import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(ROOT))

from src.adapters.xai_realtime_adapter import _MODEL, _TURN_DETECTION  # noqa: E402
from src.domain.voice_ask_latch import AskLatch  # noqa: E402
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES  # noqa: E402
from src.services.voice_session_service import _ASK_NOTE, _WEB_PICKUP_NOTE  # noqa: E402

URL = f"wss://api.x.ai/v1/realtime?model={_MODEL}"
PERSONA_TOKEN = ROOT / "prompts_snapshot" / "tokens" / "user" / "VOICE_PERSONA_INTERPRETER.groovy"
PRICE_PER_AUDIO_MIN = 0.08
_FRAME_BYTES = 160  # 20 ms of mu-law 8 kHz
_LEAD_SILENCE_S = 1.0
_TAIL_SILENCE_S = 2.5
# Per case, on top of the utterance: the opening exchange and the reply (an estimate, for the printed cost).
_OVERHEAD_S = 20.0
_REPLY_WAIT_S = 30.0
_QUIET = {"response.output_audio.delta", "response.audio.delta", "response.output_audio_transcript.delta", "ping"}


@dataclass(frozen=True)
class Sample:
    kind: str                 # "ask" or "translate"
    parts: Tuple[str, ...]    # one part, or several split by a pause

    @property
    def text(self) -> str:
        return " | ".join(self.parts)


def read_samples(path: Path) -> List[Sample]:
    samples = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, sep, rest = line.partition(":")
        kind = kind.strip().lower()
        if not sep or kind not in ("ask", "translate") or not rest.strip():
            raise SystemExit(f"{path}:{number}: expected 'ask: ...' or 'translate: ...'")
        samples.append(Sample(kind, tuple(part.strip() for part in rest.split(" | ") if part.strip())))
    if not samples:
        raise SystemExit(f"{path}: no samples")
    return samples


def pick_voice(lang: str) -> str:
    """The first installed `say` voice whose locale matches --lang (pt-PT -> pt_PT, or any pt_* for 'pt')."""
    wanted = lang.replace("-", "_").lower()
    listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    for line in listing.splitlines():
        head = line.split("#", 1)[0].split()
        if len(head) < 2:
            continue
        locale = head[-1].lower()
        if locale == wanted or (len(wanted) == 2 and locale.startswith(wanted + "_")):
            return " ".join(head[:-1])
    raise SystemExit(f"no installed `say` voice for {lang}; install one (System Settings > Accessibility > "
                     f"Spoken Content > System voice > Manage Voices) or pass --voice")


def speech(text: str, voice: str) -> bytes:
    """mu-law 8 kHz bytes of `text` spoken by `voice`."""
    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = os.path.join(tmp, "s.aiff"), os.path.join(tmp, "s.wav")
        subprocess.run(["say", "-v", voice, "-o", aiff, text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "ulaw@8000", "-c", "1", aiff, wav], check=True)
        blob = Path(wav).read_bytes()
    pos = 12
    while pos < len(blob):
        chunk_id, size = blob[pos:pos + 4], struct.unpack("<I", blob[pos + 4:pos + 8])[0]
        if chunk_id == b"data":
            return blob[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    raise RuntimeError("no data chunk in the synthesized audio")


def silence(seconds: float) -> bytes:
    return b"\xff" * int(8000 * seconds)


def utterance_audio(sample: Sample, voice: str, pause_s: float) -> bytes:
    audio = silence(_LEAD_SILENCE_S)
    for index, part in enumerate(sample.parts):
        if index:
            audio += silence(pause_s)
        audio += speech(part, voice)
    return audio + silence(_TAIL_SILENCE_S)


def persona_text() -> str:
    """The persona token's body, without its front matter."""
    text = PERSONA_TOKEN.read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


def instructions(caller_language: str, other_language: str) -> str:
    return (f"{persona_text()}\n\nFrom your memory of the caller: they speak {caller_language}. "
            f"The person in front of them speaks {other_language}.")


def plan(samples: List[Sample], runs: int) -> List[Tuple[int, Sample, bool]]:
    cases = []
    for run in range(1, runs + 1):
        for sample in samples:
            if sample.kind == "ask":
                cases.append((run, sample, True))
            cases.append((run, sample, False))
    return cases


def case_name(sample: Sample, with_note: bool) -> str:
    if sample.kind == "ask":
        return "ask+note" if with_note else "ask-no-note"
    return "translate-no-note"


class Case:
    """One session: the events it saw, in order, reduced to what P1 and P2 read."""

    def __init__(self, ws: Any, with_note: bool) -> None:
        self.ws = ws
        self.t0 = time.monotonic()
        self.latch = AskLatch(time.monotonic) if with_note else None
        self.speech_started = 0
        self.speech_stopped = 0
        self.committed: List[Dict[str, Any]] = []
        self.items: List[Dict[str, Any]] = []
        self.note_item_ids: List[str] = []
        self.notes_sent = 0
        self.awaiting_note_item = 0
        self.user_transcripts: List[str] = []
        self.replies: List[str] = []
        self.response_statuses: List[Optional[str]] = []
        self.release: Optional[str] = None
        self.errors: List[Any] = []
        self.billable_audio_seconds = 0.0
        self.last_speech_stopped_at: Optional[float] = None
        self.reply_done_after_speech = False

    def now(self) -> float:
        return time.monotonic() - self.t0

    async def send(self, event: Dict[str, Any]) -> None:
        await self.ws.send(json.dumps(event))

    async def note(self) -> None:
        await self.send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": _ASK_NOTE}]}})
        self.notes_sent += 1
        self.awaiting_note_item += 1

    async def handle(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        if kind not in _QUIET:
            print(f"  [{self.now():6.2f}s] {kind}")
        if kind == "input_audio_buffer.speech_started":
            self.speech_started += 1
            if self.latch is not None and self.latch.speech_started(event.get("item_id")).insert_note:
                await self.note()
        elif kind == "input_audio_buffer.speech_stopped":
            self.speech_stopped += 1
            self.last_speech_stopped_at = self.now()
            self.reply_done_after_speech = False
            if self.latch is not None:
                self.latch.speech_stopped(event.get("item_id"))
        elif kind == "input_audio_buffer.committed":
            self.committed.append({"item_id": event.get("item_id"), "previous_item_id": event.get("previous_item_id")})
        elif kind in ("conversation.item.added", "conversation.item.created"):
            item = event.get("item") or {}
            self.items.append({"id": item.get("id"), "role": item.get("role"), "type": item.get("type"),
                               "previous_item_id": event.get("previous_item_id")})
            if item.get("role") == "system" and self.awaiting_note_item:
                self.awaiting_note_item -= 1
                self.note_item_ids.append(item.get("id"))
        elif kind == "response.created":
            if self.latch is not None:
                self.latch.response_created()
        elif kind in ("response.output_audio.delta", "response.audio.delta"):
            if self.latch is not None:
                self.latch.reply_audio()
        elif kind == "response.output_audio_transcript.done":
            self.replies.append(event.get("transcript", ""))
        elif kind == "conversation.item.input_audio_transcription.completed":
            self.user_transcripts.append(event.get("transcript", ""))
        elif kind == "response.done":
            self.response_statuses.append((event.get("response") or {}).get("status"))
            usage = event.get("usage") or {}
            self.billable_audio_seconds = max(self.billable_audio_seconds, float(usage.get("billable_audio_seconds", 0)))
            if self.last_speech_stopped_at is not None:
                self.reply_done_after_speech = True
            if self.latch is not None:
                report = self.latch.response_done().report
                if report is not None and report.reason is not None:
                    self.release = report.reason.value
        elif kind == "error":
            self.errors.append(event.get("error", event))

    def summary(self) -> Dict[str, Any]:
        preceded = [entry["previous_item_id"] in self.note_item_ids for entry in self.committed]
        return {
            "speech_started": self.speech_started, "speech_stopped": self.speech_stopped,
            "committed": len(self.committed), "notes_sent": self.notes_sent, "note_item_ids": self.note_item_ids,
            "committed_preceded_by_note": preceded, "latch_release": self.release,
            "user_transcripts": self.user_transcripts, "replies": self.replies,
            "response_statuses": self.response_statuses, "billable_audio_seconds": self.billable_audio_seconds,
            "errors": self.errors,
        }


async def recv_until(case: Case, predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while (left := deadline - time.monotonic()) > 0:
        try:
            raw = await asyncio.wait_for(case.ws.recv(), left)
        except asyncio.TimeoutError:
            return False
        if isinstance(raw, bytes):
            continue
        event = json.loads(raw)
        await case.handle(event)
        if predicate(event):
            return True
    return False


async def stream(case: Case, audio: bytes) -> None:
    for i in range(0, len(audio), _FRAME_BYTES):
        await case.send({"type": "input_audio_buffer.append",
                         "audio": base64.b64encode(audio[i:i + _FRAME_BYTES]).decode()})
        await asyncio.sleep(0.02)


async def run_case(key: str, prompt: str, audio: bytes, with_note: bool) -> Dict[str, Any]:
    import websockets

    spec = VOICE_PROVIDER_PROFILES["xai"].session
    ws = await websockets.connect(URL, additional_headers={"Authorization": f"Bearer {key}"}, max_size=None)
    case = Case(ws, with_note)
    try:
        await case.send({"type": "session.update", "session": {
            "voice": spec.voice, "instructions": prompt, "turn_detection": _TURN_DETECTION,
            "audio": {"input": {"format": {"type": "audio/pcmu"}}, "output": {"format": {"type": "audio/pcmu"}}},
            "reasoning": {"effort": spec.reasoning_effort}}})
        await recv_until(case, lambda e: e.get("type") in ("session.updated", "error"), 10)
        # The call opens as on the web: the pickup note, then Lelik's own first line.
        await case.send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": _WEB_PICKUP_NOTE}]}})
        await case.send({"type": "response.create"})
        await recv_until(case, lambda e: e.get("type") in ("response.done", "error"), _REPLY_WAIT_S)
        opening = list(case.replies)
        case.replies.clear()
        if case.latch is not None:
            case.latch.arm(1)  # the caller pressed "Ask Lelik"
        streamer = asyncio.ensure_future(stream(case, audio))
        while not streamer.done():
            await recv_until(case, lambda e: False, 0.2)
        await streamer
        # The reply to the last speech, then a moment for its transcripts.
        await recv_until(case, lambda e: e.get("type") == "error" or (
            e.get("type") == "response.done" and case.reply_done_after_speech), _REPLY_WAIT_S)
        await recv_until(case, lambda e: False, 2.0)
        return {"opening": opening, **case.summary()}
    finally:
        await ws.close()


def estimate_usd(audios: List[bytes]) -> float:
    seconds = sum(len(audio) / 8000 + _OVERHEAD_S for audio in audios)
    return round(seconds / 60 * PRICE_PER_AUDIO_MIN, 2)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", required=True, help="locale of the samples, e.g. pt-PT (picks the `say` voice)")
    parser.add_argument("--samples", required=True, type=Path, help="UTF-8 file of 'ask:' / 'translate:' lines")
    parser.add_argument("--caller-language", required=True, help="the caller's language as Lelik knows it")
    parser.add_argument("--other-language", default="English", help="the other person's language")
    parser.add_argument("--voice", help="a `say` voice name (default: the first one installed for --lang)")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--pause-s", type=float, default=1.5, help="pause at ' | ' inside a sample")
    parser.add_argument("--out", type=Path, help="write the results as JSON here (keep it in a gitignored path)")
    parser.add_argument("--dry-run", action="store_true", help="synthesize and estimate only; no session is opened")
    args = parser.parse_args()

    samples = read_samples(args.samples)
    voice = args.voice or pick_voice(args.lang)
    prompt = instructions(args.caller_language, args.other_language)
    audio = {sample: utterance_audio(sample, voice, args.pause_s) for sample in samples}
    cases = plan(samples, args.runs)
    print(f"model {_MODEL}, voice {voice!r}, {len(samples)} samples, {len(cases)} sessions, "
          f"estimate ~${estimate_usd([audio[sample] for _, sample, _ in cases])}")
    for _, sample, with_note in cases[:len(cases) // max(args.runs, 1)]:
        print(f"  {case_name(sample, with_note):18} {len(audio[sample]) / 8000:5.1f} s  {sample.text}")
    if args.dry_run:
        return

    from src.config.settings import load_settings

    key = (load_settings().get("XAI_API_KEY") or "").strip()  # the stored secret ends in "\n"
    if not key:
        raise SystemExit("XAI_API_KEY is not set")
    results = []
    for run, sample, with_note in cases:
        name = case_name(sample, with_note)
        print(f"\n===== run {run}: {name}: {sample.text}")
        try:
            outcome = await run_case(key, prompt, audio[sample], with_note)
        except Exception as exc:
            outcome = {"exception": repr(exc)}
        results.append({"run": run, "case": name, "kind": sample.kind, "note": with_note, "sample": sample.text,
                        **outcome})
        print(json.dumps(results[-1], ensure_ascii=False, indent=2))

    print("\n===== SUMMARY (P2: speech_started / committed / committed turns preceded by the note) =====")
    for entry in results:
        if "exception" in entry:
            print(f"run {entry['run']} {entry['case']:18} EXCEPTION {entry['exception']}")
            continue
        print(f"run {entry['run']} {entry['case']:18} started={entry['speech_started']} "
              f"committed={entry['committed']} notes={entry['notes_sent']} "
              f"preceded={entry['committed_preceded_by_note']} release={entry['latch_release']} "
              f"errors={len(entry['errors'])}")
        for reply in entry["replies"]:
            print(f"    reply: {reply}")
    if args.out:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nresults written to {args.out}")


if __name__ == "__main__":
    asyncio.run(main())

"""
Generate the transcript eval fixtures (USER_DRIVE_INDEXING_RFC §27.14 "Eval") — scripted dialogue rendered by a local
text-to-speech engine, in the style of `drive_capture_fixtures.py`. Nothing here is anyone's voice, speech or file.

    python -m scripts.validation.drive_transcript_fixtures                      # media + gold (needs TTS and ffmpeg)
    python -m scripts.validation.drive_transcript_fixtures --gold-only          # the gold JSON only (no TTS, no ffmpeg)
    python -m scripts.validation.drive_transcript_fixtures --list               # clip ids, durations, TTS call count

Only the script and the gold JSON are committed (`tests/fixtures/drive_transcripts/gold.json`: each line's text, speaker
and INTENDED start). The media is written to `scripts/memory/drive_transcripts/` (gitignored) and re-made on demand.

Intended starts are a fixed schedule, not a measurement: line k of a clip starts at `first_start_s + k * spacing_s` and is
padded with silence up to the next slot, so the gold does not depend on how long a voice takes to say a line (a line that
does not fit its slot is a fixture error, never a silent overlap). The audio is assembled sample by sample from the
engine's per-line renders (16 kHz mono PCM); only the final encode needs ffmpeg.

Families (the eval's metrics in `drive_eval/transcript_eval.py` read the `family` field):
  dialogue_<lang>   two voices, en / es / uk / ru, as audio (m4a) and as video (mp4 with a black video track)
  mixed             one speaker pair alternating uk / ru / en line by line
  four_speakers     four voices in one clip: the speaker over-count metric's input
  no_speech_tone    tone and silence only: the model must answer `no_speech`
  no_speech_music   a synthetic chord progression, no voice
  seams_70min       70 minutes of unique lines, one every 40 s: windows meet inside it (seam duplicates and losses)
  unique_22min      22 minutes of unique lines, one every 30 s (the T0 gap, Δ129: the silent `STOP` on unique audio)
  early_<n>s        short clips, the first line at 3 s: the early-timestamp effect
Engines: `say` on macOS, `espeak-ng` elsewhere; a language the engine has no voice for is reported and its clips skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import struct
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_REPO = Path(__file__).resolve().parents[2]
GOLD_PATH = _REPO / "tests" / "fixtures" / "drive_transcripts" / "gold.json"
MEDIA_DIR = _REPO / "scripts" / "memory" / "drive_transcripts"
SAMPLE_RATE = 16_000
GOLD_VERSION = 1
LANGUAGES = ("en", "es", "uk", "ru")

# macOS `say` voices and espeak-ng voices, two per language (speaker 0 / speaker 1; the four-speaker clip uses en 0..3).
SAY_VOICES: Dict[str, Tuple[str, ...]] = {"en": ("Samantha", "Daniel", "Karen", "Alex"), "es": ("Monica", "Jorge"),
                                          "uk": ("Lesya", "Lesya"), "ru": ("Milena", "Yuri")}
ESPEAK_VOICES: Dict[str, Tuple[str, ...]] = {"en": ("en+f3", "en+m3", "en+f5", "en+m7"), "es": ("es+f3", "es+m3"),
                                             "uk": ("uk+f3", "uk+m3"), "ru": ("ru+f3", "ru+m3")}

DIALOGUES: Dict[str, Tuple[str, ...]] = {
    "en": ("Good morning, did you finish the quarterly summary for the committee?",
           "Almost, I still need the figures from the warehouse in the north.",
           "Send me the draft tonight and I will read it on the train tomorrow.",
           "The numbers show a small increase in deliveries during the autumn months.",
           "That matches what the drivers told us about the new route through the valley.",
           "Then we should mention the route change on the first page of the report.",
           "Agreed, and I will add a short chart showing the monthly totals.",
           "Perfect, let us meet on Thursday at ten to go through the final version."),
    "es": ("Buenos días, ¿terminaste el resumen trimestral para el comité?",
           "Casi, todavía me faltan las cifras del almacén del norte.",
           "Envíame el borrador esta noche y lo leeré mañana en el tren.",
           "Los números muestran un pequeño aumento de entregas durante el otoño.",
           "Eso coincide con lo que dijeron los conductores sobre la nueva ruta por el valle.",
           "Entonces deberíamos mencionar el cambio de ruta en la primera página del informe.",
           "De acuerdo, y añadiré un gráfico corto con los totales de cada mes.",
           "Perfecto, nos vemos el jueves a las diez para revisar la versión final."),
    "uk": ("Доброго ранку, ти закінчив квартальний звіт для комітету?",
           "Майже, мені ще потрібні цифри зі складу на півночі.",
           "Надішли мені чернетку сьогодні ввечері, і я прочитаю її завтра в поїзді.",
           "Цифри показують невелике зростання доставок протягом осінніх місяців.",
           "Це збігається з тим, що водії розповідали про новий маршрут через долину.",
           "Тоді варто згадати про зміну маршруту на першій сторінці звіту.",
           "Згоден, і я додам короткий графік із підсумками за кожен місяць.",
           "Чудово, зустрінемося в четвер о десятій, щоб переглянути остаточну версію."),
    "ru": ("Доброе утро, ты закончил квартальный отчёт для комитета?",
           "Почти, мне ещё нужны цифры со склада на севере.",
           "Пришли мне черновик сегодня вечером, и я прочитаю его завтра в поезде.",
           "Цифры показывают небольшой рост поставок в течение осенних месяцев.",
           "Это совпадает с тем, что водители рассказывали о новом маршруте через долину.",
           "Тогда стоит упомянуть об изменении маршрута на первой странице отчёта.",
           "Согласен, и я добавлю короткий график с итогами за каждый месяц.",
           "Отлично, встретимся в четверг в десять, чтобы просмотреть окончательную версию."),
}
_ADJ = ("quiet", "bright", "heavy", "narrow", "old", "green", "distant", "small")
_NOUN = ("lantern", "bridge", "ledger", "garden", "engine", "window", "harbour", "orchard")
_VERB = ("crosses", "follows", "paints", "measures", "protects", "reaches", "circles", "borders")
_PLACE = ("station", "market", "river", "tower", "meadow", "chapel", "depot", "quarry")


class FixtureError(Exception):
    """A clip cannot be built as specified (a line that does not fit its slot, a missing engine or a missing voice)."""


@dataclass(frozen=True)
class Line:
    speaker: int
    start_s: float
    text: str
    language: str


@dataclass(frozen=True)
class ClipSpec:
    clip_id: str
    family: str
    kind: str                       # "audio" | "video"
    language: str                   # the clip's main language ("mixed" for the alternating clip)
    duration_s: float
    speakers: int
    lines: Tuple[Line, ...] = ()
    synth: str = ""                 # "" speech | "tone" | "music"
    probes: Tuple[float, ...] = ()  # the starts the timestamp-error metric reads


def unique_lines(count: int, seed: int) -> List[str]:
    """`count` English lines, none repeated (each carries its own number); deterministic."""
    rng = random.Random(seed)
    out = []
    for n in range(1, count + 1):
        out.append(f"Item {n}. The {rng.choice(_ADJ)} {rng.choice(_NOUN)} {rng.choice(_VERB)} the "
                   f"{rng.choice(_ADJ)} {rng.choice(_NOUN)} near the {rng.choice(_PLACE)} on day {rng.randint(2, 28)}.")
    return out


def _scheduled(texts: Sequence[str], languages: Sequence[str], speakers: int, spacing_s: float,
               first_start_s: float) -> Tuple[Line, ...]:
    return tuple(Line(speaker=i % speakers, start_s=first_start_s + i * spacing_s, text=text,
                      language=languages[i % len(languages)]) for i, text in enumerate(texts))


def _probes(lines: Sequence[Line], every: int = 4) -> Tuple[float, ...]:
    return tuple(line.start_s for line in lines[::every])


def _speech(clip_id: str, family: str, kind: str, language: str, texts: Sequence[str], languages: Sequence[str],
            speakers: int, spacing_s: float, first_start_s: float, tail_s: float = 8.0) -> ClipSpec:
    lines = _scheduled(texts, languages, speakers, spacing_s, first_start_s)
    duration = lines[-1].start_s + spacing_s + tail_s if lines else tail_s
    return ClipSpec(clip_id, family, kind, language, duration, speakers, lines, probes=_probes(lines))


def build_specs() -> List[ClipSpec]:
    """Every clip of the fixture set, pure and deterministic: the gold JSON is a function of this list alone."""
    specs: List[ClipSpec] = []
    for lang in LANGUAGES:
        for kind in ("audio", "video"):
            specs.append(_speech(f"dialogue_{lang}_{kind}", f"dialogue_{lang}", kind, lang, DIALOGUES[lang],
                                 [lang], 2, 12.0, 4.0))
    mixed_texts = [DIALOGUES[lang][i] for i in range(4) for lang in ("uk", "ru", "en")]
    specs.append(_speech("mixed_audio", "mixed", "audio", "mixed", mixed_texts, ["uk", "ru", "en"], 2, 12.0, 4.0))
    specs.append(_speech("four_speakers_audio", "four_speakers", "audio", "en", DIALOGUES["en"], ["en"], 4, 12.0, 4.0))
    specs.append(ClipSpec("no_speech_tone_audio", "no_speech_tone", "audio", "", 180.0, 0, synth="tone"))
    specs.append(ClipSpec("no_speech_music_video", "no_speech_music", "video", "", 180.0, 0, synth="music"))
    specs.append(_speech("seams_70min_audio", "seams_70min", "audio", "en", unique_lines(105, seed=70), ["en"], 2,
                         40.0, 10.0, tail_s=0.0))
    specs[-1] = _fit(specs[-1], 4200.0)
    specs.append(_speech("unique_22min_audio", "unique_22min", "audio", "en", unique_lines(44, seed=22), ["en"], 2,
                         30.0, 10.0, tail_s=0.0))
    specs[-1] = _fit(specs[-1], 1320.0)
    for seconds in (20, 45, 90):
        texts = unique_lines(max(2, seconds // 15), seed=seconds)
        spec = _speech(f"early_{seconds}s_audio", f"early_{seconds}s", "audio", "en", texts, ["en"], 2, 12.0, 3.0)
        specs.append(_fit(spec, float(seconds)))
    return specs


def _fit(spec: ClipSpec, duration_s: float) -> ClipSpec:
    """Pin a clip to an exact duration; every line must start before the end."""
    if spec.lines and spec.lines[-1].start_s >= duration_s:
        raise FixtureError(f"{spec.clip_id}: the last line starts at {spec.lines[-1].start_s}s, after {duration_s}s")
    return ClipSpec(spec.clip_id, spec.family, spec.kind, spec.language, duration_s, spec.speakers, spec.lines,
                    spec.synth, spec.probes)


def gold(specs: Sequence[ClipSpec]) -> Dict[str, object]:
    """The committed JSON: each line's text, speaker and intended start; nothing about the media bytes."""
    return {"version": GOLD_VERSION, "sample_rate": SAMPLE_RATE, "clips": [
        {"id": s.clip_id, "family": s.family, "kind": s.kind, "language": s.language,
         "duration_s": s.duration_s, "speakers": s.speakers, "synth": s.synth, "probes": list(s.probes),
         "lines": [{"speaker": ln.speaker, "start_s": ln.start_s, "language": ln.language, "text": ln.text}
                   for ln in s.lines]} for s in specs]}


def write_gold(path: Path, specs: Sequence[ClipSpec]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gold(specs), ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# -- rendering -------------------------------------------------------------------------------------------------------

Tts = Callable[[str, str, int, Path], None]          # (text, language, speaker, out wav path); any wav params
Encode = Callable[[Path, Path, str], None]           # (assembled wav, output path, "audio" | "video")


def _engine() -> str:
    if sys.platform == "darwin" and shutil.which("say"):
        return "say"
    if shutil.which("espeak-ng"):
        return "espeak-ng"
    raise FixtureError("no TTS engine: install espeak-ng, or run on macOS (`say`)")


def system_tts(text: str, language: str, speaker: int, out: Path) -> None:
    """One line through the local engine, then to 16 kHz mono PCM by ffmpeg. The engine's voice list is its own."""
    engine = _engine()
    voices = (SAY_VOICES if engine == "say" else ESPEAK_VOICES).get(language)
    if not voices:
        raise FixtureError(f"no {engine} voice for language {language}")
    raw = out.with_suffix(".raw.wav")
    voice = voices[speaker % len(voices)]
    if engine == "say":
        cmd = ["say", "-v", voice, "-o", str(raw), "--file-format=WAVE", "--data-format=LEI16@22050", "--", text]
    else:
        cmd = ["espeak-ng", "-v", voice, "-w", str(raw), "--", text]
    subprocess.run(cmd, check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-ar", str(SAMPLE_RATE), "-ac", "1",
                    "-sample_fmt", "s16", str(out)], check=True, capture_output=True)
    raw.unlink(missing_ok=True)


def system_encode(wav: Path, out: Path, kind: str) -> None:
    if kind == "audio":
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "aac", "-b:a", "48k", str(out)]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=5", "-i", str(wav),
               "-c:v", "mpeg4", "-q:v", "12", "-c:a", "aac", "-b:a", "48k", "-shortest", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)


class _Pcm:
    """Streams 16 kHz mono int16 into a wav file in chunks, so a 70-minute clip is never held in memory."""

    def __init__(self, path: Path) -> None:
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(SAMPLE_RATE)
        self.frames = 0

    def silence(self, seconds: float) -> None:
        remaining = max(0, round(seconds * SAMPLE_RATE))
        while remaining:
            step = min(remaining, SAMPLE_RATE * 10)
            self._wav.writeframes(b"\x00\x00" * step)
            remaining -= step
            self.frames += step

    def write(self, data: bytes) -> None:
        self._wav.writeframes(data)
        self.frames += len(data) // 2

    def close(self) -> None:
        self._wav.close()


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, SAMPLE_RATE):
            raise FixtureError("the TTS render is not 16 kHz mono 16-bit PCM")
        return w.readframes(w.getnframes())


def _tone_chunk(freq: float, seconds: float, amp: float, start: int = 0) -> bytes:
    n = round(seconds * SAMPLE_RATE)
    return struct.pack(f"<{n}h", *(int(amp * 32767 * math.sin(2 * math.pi * freq * (start + i) / SAMPLE_RATE))
                                   for i in range(n)))


def _synth_audio(spec: ClipSpec, pcm: _Pcm) -> None:
    if spec.synth == "tone":     # 2 s of 440 Hz, then 8 s of silence, repeated
        elapsed = 0.0
        while elapsed < spec.duration_s:
            pcm.write(_tone_chunk(440.0, min(2.0, spec.duration_s - elapsed), 0.3))
            elapsed += 2.0
            if elapsed < spec.duration_s:
                pcm.silence(min(8.0, spec.duration_s - elapsed))
                elapsed += 8.0
        return
    chords = ((261.63, 329.63, 392.0), (293.66, 349.23, 440.0), (329.63, 392.0, 493.88), (261.63, 329.63, 392.0))
    elapsed, k = 0.0, 0                              # music: a chord progression, 4 s a chord, no voice
    while elapsed < spec.duration_s:
        seconds = min(4.0, spec.duration_s - elapsed)
        n = round(seconds * SAMPLE_RATE)
        chord = chords[k % len(chords)]
        pcm.write(struct.pack(f"<{n}h", *(int(0.12 * 32767 * sum(math.sin(2 * math.pi * f * i / SAMPLE_RATE)
                                                              for f in chord) / 3) for i in range(n))))
        elapsed += seconds
        k += 1


def render_clip(spec: ClipSpec, work: Path, out_dir: Path, *, tts: Tts, encode: Encode) -> Path:
    """Assemble the clip's wav on the intended schedule and encode it. Every line must fit before the next one's start."""
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = work / f"{spec.clip_id}.wav"
    pcm = _Pcm(wav)
    try:
        if spec.synth:
            _synth_audio(spec, pcm)
        else:
            cursor = 0.0
            for index, line in enumerate(spec.lines):
                line_wav = work / f"{spec.clip_id}.{index:03d}.wav"
                tts(line.text, line.language, line.speaker, line_wav)
                data = _read_pcm(line_wav)
                line_wav.unlink(missing_ok=True)
                seconds = len(data) / 2 / SAMPLE_RATE
                following = spec.lines[index + 1].start_s if index + 1 < len(spec.lines) else spec.duration_s
                if line.start_s + seconds >= following:
                    raise FixtureError(f"{spec.clip_id}: line {index} runs {seconds:.1f}s from {line.start_s}s, "
                                       f"past the next slot at {following}s")
                pcm.silence(line.start_s - cursor)
                pcm.write(data)
                cursor = line.start_s + seconds
            pcm.silence(spec.duration_s - cursor)
    finally:
        pcm.close()
    out = out_dir / f"{spec.clip_id}.{'m4a' if spec.kind == 'audio' else 'mp4'}"
    encode(wav, out, spec.kind)
    wav.unlink(missing_ok=True)
    return out


def tts_call_count(specs: Sequence[ClipSpec]) -> int:
    return sum(len(s.lines) for s in specs)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_transcript_fixtures",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--gold", type=Path, default=GOLD_PATH, help="where the gold JSON is written")
    parser.add_argument("--out-dir", type=Path, default=MEDIA_DIR, help="media output (keep it under scripts/memory/)")
    parser.add_argument("--gold-only", action="store_true", help="write the gold JSON and no media")
    parser.add_argument("--list", action="store_true", help="print the clips and the TTS call count, write nothing")
    parser.add_argument("--only", action="append", default=[], help="render only these clip ids (repeatable)")
    args = parser.parse_args(argv)
    specs = build_specs()
    if args.list:
        for s in specs:
            print(f"{s.clip_id}: {s.kind}, {s.duration_s:.0f}s, {len(s.lines)} lines, {s.speakers} speakers")
        print(f"{tts_call_count(specs)} TTS calls")
        return 0
    write_gold(args.gold, specs)
    print(f"gold: {args.gold} ({len(specs)} clips)")
    if args.gold_only:
        return 0
    failed = 0
    for spec in specs:
        if args.only and spec.clip_id not in args.only:
            continue
        try:
            path = render_clip(spec, args.out_dir / "work", args.out_dir, tts=system_tts, encode=system_encode)
            print(f"{spec.clip_id}: {path.name} {_digest(path)}")
        except (FixtureError, subprocess.CalledProcessError, FileNotFoundError) as exc:
            failed += 1
            print(f"{spec.clip_id}: skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

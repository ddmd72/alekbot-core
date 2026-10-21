"""scripts/validation/drive_transcript_fixtures.py (USER_DRIVE_INDEXING_RFC §27.14): the clip set, the gold JSON and the
assembly of a clip on the intended schedule. TTS and ffmpeg are stubbed: no engine, no media tool, no network."""
import json
import struct
import wave

import pytest

from scripts.validation import drive_transcript_fixtures as fx
from src.domain.drive_index_params import TRANSCRIBE_WINDOW_S
from src.domain.drive_transcript_windows import plan_windows


def _stub_tts(seconds_per_char=0.05):
    def tts(text, language, speaker, out):
        n = round(len(text) * seconds_per_char * fx.SAMPLE_RATE)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(fx.SAMPLE_RATE)
            w.writeframes(struct.pack(f"<{n}h", *([8000] * n)))
    return tts


def _copy_encode(wav, out, kind):
    out.write_bytes(wav.read_bytes())


def _first_loud_sample_s(path):
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    for i, s in enumerate(samples):
        if abs(s) > 100:
            return i / fx.SAMPLE_RATE
    return None


def test_the_clip_set_has_every_family_of_the_rfc():
    families = {s.family for s in fx.build_specs()}
    assert {"dialogue_en", "dialogue_es", "dialogue_uk", "dialogue_ru", "mixed", "four_speakers", "no_speech_tone",
            "no_speech_music", "seams_70min", "unique_22min", "early_20s", "early_45s", "early_90s"} <= families


def test_video_family_exists_for_each_language():
    kinds = {(s.family, s.kind) for s in fx.build_specs()}
    assert all((f"dialogue_{lang}", "video") in kinds and (f"dialogue_{lang}", "audio") in kinds for lang in fx.LANGUAGES)


def test_the_seam_clip_is_70_minutes_and_crosses_a_window_boundary():
    spec = next(s for s in fx.build_specs() if s.family == "seams_70min")
    assert spec.duration_s == 4200.0 and len(plan_windows(spec.duration_s)) >= 2
    assert any(spec.lines[i].start_s < TRANSCRIBE_WINDOW_S <= spec.lines[i + 1].start_s for i in range(len(spec.lines) - 1))


def test_unique_clip_is_one_window_past_22_minutes_and_has_no_repeated_line():
    spec = next(s for s in fx.build_specs() if s.family == "unique_22min")
    texts = [ln.text for ln in spec.lines]
    assert spec.duration_s == 1710.0 and len(plan_windows(spec.duration_s)) == 1
    assert spec.duration_s > 22 * 60 and len(set(texts)) == len(texts) == 57


def test_early_clips_start_their_first_line_at_three_seconds():
    for s in fx.build_specs():
        if s.family.startswith("early_"):
            assert s.lines[0].start_s == 3.0


def test_unique_lines_are_deterministic_and_distinct():
    assert fx.unique_lines(30, seed=5) == fx.unique_lines(30, seed=5)
    assert len(set(fx.unique_lines(200, seed=5))) == 200


def test_every_line_starts_inside_its_clip_and_in_order():
    for s in fx.build_specs():
        starts = [ln.start_s for ln in s.lines]
        assert starts == sorted(starts) and all(0 <= t < s.duration_s for t in starts)


def test_dialogue_speakers_alternate_and_four_speakers_use_four():
    specs = {s.clip_id: s for s in fx.build_specs()}
    assert {ln.speaker for ln in specs["dialogue_en_audio"].lines} == {0, 1}
    assert {ln.speaker for ln in specs["four_speakers_audio"].lines} == {0, 1, 2, 3}
    assert {ln.language for ln in specs["mixed_audio"].lines} == {"uk", "ru", "en"}


def test_the_gold_json_matches_the_specs_and_carries_text_and_intended_starts():
    gold = fx.gold(fx.build_specs())
    clip = next(c for c in gold["clips"] if c["id"] == "dialogue_uk_audio")
    assert clip["lines"][0] == {"speaker": 0, "start_s": 4.0, "language": "uk", "text": fx.DIALOGUES["uk"][0]}
    assert clip["probes"] and clip["duration_s"] > clip["lines"][-1]["start_s"]
    json.dumps(gold, ensure_ascii=False)


def test_the_committed_gold_is_what_the_script_generates():
    committed = json.loads(fx.GOLD_PATH.read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(fx.gold(fx.build_specs())))


def test_the_committed_gold_holds_no_media_and_no_path():
    text = fx.GOLD_PATH.read_text(encoding="utf-8")
    assert "scripts/memory" not in text and "/Users/" not in text and ".m4a" not in text


def test_write_gold_round_trips(tmp_path):
    fx.write_gold(tmp_path / "sub" / "gold.json", fx.build_specs())
    assert json.loads((tmp_path / "sub" / "gold.json").read_text(encoding="utf-8"))["version"] == fx.GOLD_VERSION


def test_render_places_each_line_at_its_intended_start(tmp_path):
    spec = next(s for s in fx.build_specs() if s.clip_id == "dialogue_en_audio")
    out = fx.render_clip(spec, tmp_path / "w", tmp_path / "o", tts=_stub_tts(), encode=_copy_encode)
    assert out.suffix == ".m4a" and _first_loud_sample_s(out) == pytest.approx(spec.lines[0].start_s, abs=0.01)
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() / fx.SAMPLE_RATE == pytest.approx(spec.duration_s, abs=0.01)


def test_render_marks_video_clips_for_an_mp4(tmp_path):
    spec = next(s for s in fx.build_specs() if s.clip_id == "dialogue_en_video")
    kinds = []
    out = fx.render_clip(spec, tmp_path / "w", tmp_path / "o", tts=_stub_tts(),
                         encode=lambda wav, o, kind: (kinds.append(kind), o.write_bytes(b"x")))
    assert out.suffix == ".mp4" and kinds == ["video"]


def test_a_line_that_does_not_fit_its_slot_is_an_error_not_an_overlap(tmp_path):
    spec = next(s for s in fx.build_specs() if s.clip_id == "dialogue_en_audio")
    with pytest.raises(fx.FixtureError, match="past the next slot"):
        fx.render_clip(spec, tmp_path / "w", tmp_path / "o", tts=_stub_tts(seconds_per_char=1.0), encode=_copy_encode)


def test_tone_clip_is_tone_and_silence(tmp_path):
    spec = next(s for s in fx.build_specs() if s.family == "no_speech_tone")
    out = fx.render_clip(spec, tmp_path / "w", tmp_path / "o", tts=_stub_tts(), encode=_copy_encode)
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() / fx.SAMPLE_RATE == pytest.approx(spec.duration_s, abs=0.01)
    assert _first_loud_sample_s(out) == 0.0 or _first_loud_sample_s(out) < 0.01


def test_music_clip_has_energy_and_no_tts_call(tmp_path):
    spec = next(s for s in fx.build_specs() if s.family == "no_speech_music")

    def no_tts(*a):
        raise AssertionError("music is synthesized, not spoken")
    out = fx.render_clip(spec, tmp_path / "w", tmp_path / "o", tts=no_tts, encode=_copy_encode)
    assert _first_loud_sample_s(out) is not None


def test_a_missing_voice_is_a_fixture_error(monkeypatch, tmp_path):
    monkeypatch.setattr(fx, "_engine", lambda: "espeak-ng")
    monkeypatch.setitem(fx.ESPEAK_VOICES, "uk", ())
    with pytest.raises(fx.FixtureError, match="no espeak-ng voice"):
        fx.system_tts("x", "uk", 0, tmp_path / "x.wav")


def test_main_gold_only_writes_json_and_nothing_else(tmp_path, capsys):
    code = fx.main(["--gold", str(tmp_path / "g.json"), "--out-dir", str(tmp_path / "m"), "--gold-only"])
    assert code == 0 and (tmp_path / "g.json").exists() and not (tmp_path / "m").exists()


def test_main_list_writes_nothing(tmp_path, capsys):
    code = fx.main(["--gold", str(tmp_path / "g.json"), "--list"])
    assert code == 0 and not (tmp_path / "g.json").exists() and "TTS calls" in capsys.readouterr().out

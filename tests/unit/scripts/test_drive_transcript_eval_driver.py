"""scripts/validation/drive_eval/transcript_eval.py: the estimate, the --yes guard and the per-clip driver, with a fake batch
port and runner. Nothing leaves the process."""
import json

import pytest

from scripts.validation.drive_eval import transcript_eval as ev
from src.domain.batch_transcription import BatchRef, BatchStatus, FileRef
from src.domain.drive_transcript import Segment, WindowOutcome, WindowResult, WindowTranscript


class FakePort:
    def __init__(self, statuses=(BatchStatus.SUCCEEDED,)):
        self.uploaded, self.deleted, self.polls = [], [], 0
        self._statuses = list(statuses)

    async def upload_audio(self, path, mime):
        self.uploaded.append(path)
        return FileRef(name=f"files/{len(self.uploaded)}", uri="u", expires_at=None)

    async def poll(self, batch):
        self.polls += 1
        return self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]

    async def delete_file(self, ref):
        self.deleted.append(ref.name)


class FakeRunner:
    """Answers each window key from a script of per-attempt transcripts."""

    def __init__(self, script):
        self.script, self.submits, self.calls = script, [], {}

    async def submit(self, inputs, *, display_name):
        self.submits.append([i.key for i in inputs])
        return BatchRef(name="batches/x", keys=tuple(i.key for i in inputs))

    async def collect(self, batch, *, account_id, bill=True):
        assert bill is False                                   # the eval books nothing
        out = {}
        for key in batch.keys:
            n = self.calls[key] = self.calls.get(key, 0) + 1
            answer = self.script[key][min(n, len(self.script[key])) - 1]
            out[key] = answer if isinstance(answer, WindowResult) else WindowResult(key, WindowOutcome.OK, answer)
        return out


async def _no_sleep(_):
    return None


def _clip(duration=100.0, clip_id="c"):
    return {"id": clip_id, "family": "dialogue_en", "language": "en", "kind": "audio", "duration_s": duration,
            "speakers": 2, "synth": "", "probes": [4.0],
            "lines": [{"speaker": 0, "start_s": 4.0, "text": "Good morning to you all", "language": "en"},
                      {"speaker": 1, "start_s": 92.0, "text": "See you on Thursday then", "language": "en"}]}


def _ok_window(*pairs):
    return WindowTranscript(("en",), 2, False, tuple(Segment(t, s, text) for t, s, text in pairs))


GOOD = _ok_window((4.0, 1, "Good morning to you all"), (92.0, 2, "See you on Thursday then"))
SHORT = _ok_window((4.0, 1, "Good morning to you all"))                    # last segment far before 90% of the window


async def _drive(clip, script, tmp_path, port=None):
    port = port or FakePort()
    runner = FakeRunner(script)
    cuts = []
    stitched, specs, resubmits = await ev.transcribe_clip(clip, tmp_path / "m.m4a", port=port, runner=runner,
                                                         cut=lambda src, spec, out: cuts.append(spec.index),
                                                         workdir=tmp_path, sleep=_no_sleep)
    return stitched, specs, resubmits, port, runner, cuts


async def test_a_good_single_window_clip_is_stitched_and_its_file_deleted(tmp_path):
    stitched, specs, resubmits, port, runner, cuts = await _drive(_clip(), {"c:0": [GOOD]}, tmp_path)
    assert len(specs) == 1 and resubmits == 0 and cuts == [0]
    assert [s.text for s in stitched.segments] == ["Good morning to you all", "See you on Thursday then"]
    assert port.deleted == ["files/1"] and runner.submits == [["c:0"]]


async def test_a_short_window_is_resubmitted_then_accepted(tmp_path):
    stitched, _, resubmits, _, runner, _ = await _drive(_clip(), {"c:0": [SHORT, GOOD]}, tmp_path)
    assert resubmits == 1 and runner.submits == [["c:0"], ["c:0"]] and len(stitched.segments) == 2 and not stitched.truncated


async def test_a_window_that_never_covers_becomes_a_gap_after_max_attempts(tmp_path):
    stitched, _, resubmits, _, runner, _ = await _drive(_clip(), {"c:0": [SHORT]}, tmp_path)
    assert resubmits == 3 and len(runner.submits) == 3 and stitched.truncated


async def test_a_refused_window_is_a_gap_at_once(tmp_path):
    refused = WindowResult("c:0", WindowOutcome.REFUSED)
    stitched, _, resubmits, _, runner, _ = await _drive(_clip(), {"c:0": [refused]}, tmp_path)
    assert resubmits == 0 and len(runner.submits) == 1 and stitched.truncated


async def test_a_long_clip_cuts_and_uploads_every_window(tmp_path):
    clip = _clip(duration=4200.0, clip_id="s")
    good = lambda t: _ok_window((t, 1, "x y z"), (t + 1700.0, 1, "x y z"))   # noqa: E731 — covers 90 % of 1800 s
    script = {"s:0": [good(5.0)], "s:1": [good(5.0)], "s:2": [_ok_window((5.0, 1, "x"), (700.0, 1, "y"))]}
    stitched, specs, resubmits, port, runner, cuts = await _drive(clip, script, tmp_path)
    assert len(specs) == 3 and cuts == [0, 1, 2] and len(port.uploaded) == 3 and resubmits == 0
    assert runner.submits == [["s:0", "s:1", "s:2"]] and sorted(port.deleted) == ["files/1", "files/2", "files/3"]


async def test_a_failed_batch_is_no_verdict_and_still_deletes_the_files(tmp_path):
    with pytest.raises(ev.EvalError, match="failed"):
        await _drive(_clip(), {"c:0": [GOOD]}, tmp_path, port=FakePort((BatchStatus.FAILED,)))


async def test_polling_waits_until_the_batch_succeeds(tmp_path):
    port = FakePort((BatchStatus.RUNNING, BatchStatus.RUNNING, BatchStatus.SUCCEEDED))
    await _drive(_clip(), {"c:0": [GOOD]}, tmp_path, port=port)
    assert port.polls == 3


async def test_a_batch_that_never_finishes_is_no_verdict(tmp_path):
    runner = FakeRunner({"c:0": [GOOD]})
    with pytest.raises(ev.EvalError, match="in time"):
        await ev.transcribe_clip(_clip(), tmp_path / "m", port=FakePort((BatchStatus.RUNNING,)), runner=runner,
                                 cut=lambda *a: None, workdir=tmp_path, sleep=_no_sleep, poll_every_s=10, max_wait_s=30)


async def test_run_eval_stops_before_any_call_when_media_is_missing(tmp_path):
    port = FakePort()
    with pytest.raises(ev.EvalError, match="no media"):
        await ev.run_eval([_clip()], port=port, runner=FakeRunner({}), cut=lambda *a: None, media_dir=tmp_path,
                          sleep=_no_sleep)
    assert port.uploaded == []


async def test_run_eval_scores_each_clip(tmp_path):
    (tmp_path / "c.m4a").write_bytes(b"x")
    rows = await ev.run_eval([_clip()], port=FakePort(), runner=FakeRunner({"c:0": [GOOD]}), cut=lambda *a: None,
                             media_dir=tmp_path, sleep=_no_sleep)
    assert rows[0]["id"] == "c" and rows[0]["wer"] == 0.0 and rows[0]["coverage_resubmits"] == 0


def test_the_estimate_is_the_stage_reservation_with_an_allowance():
    from src.domain.drive_transcript_plan import file_estimate_usd
    assert ev.estimate_usd([_clip(3600.0)]) == pytest.approx(file_estimate_usd(3600.0) * 1.25)


@pytest.mark.datafile
def test_without_yes_it_prints_the_estimate_and_never_builds_the_live_ports(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ev, "build_live", lambda: (_ for _ in ()).throw(AssertionError("live call without --yes")))
    code = ev.main(["--media-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and "estimated cost" in out and "--yes" in out


@pytest.mark.datafile
def test_the_committed_gold_is_estimable():
    gold = ev.load_gold()
    assert 0 < ev.estimate_usd(gold["clips"]) < 100


@pytest.mark.datafile
def test_main_exits_two_without_a_verdict_when_media_is_missing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ev, "build_live", lambda: (FakePort(), FakeRunner({}), "m"))
    code = ev.main(["--yes", "--only", "dialogue_en_audio", "--media-dir", str(tmp_path), "--report", str(tmp_path / "r.json")])
    assert code == 2 and "no verdict" in capsys.readouterr().err


def test_record_writes_bars_and_a_later_run_compares(monkeypatch, tmp_path, capsys):
    gold = {"clips": [_clip()]}
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    (tmp_path / "c.m4a").write_bytes(b"x")
    monkeypatch.setattr(ev, "build_live", lambda: (FakePort(), FakeRunner({"c:0": [GOOD]}), "model-x"))
    monkeypatch.setattr(ev, "ffmpeg_cut", lambda *a: None)
    common = ["--yes", "--gold", str(gold_path), "--bars", str(tmp_path / "bars.json"), "--media-dir", str(tmp_path),
              "--report", str(tmp_path / "r.json")]
    assert ev.main(common + ["--record"]) == 0
    bars = json.loads((tmp_path / "bars.json").read_text(encoding="utf-8"))
    assert bars["by_family"]["dialogue_en"]["wer"] == 0.0 and bars["model"] == "model-x"
    assert ev.main(common) == 0 and "within the bars" in capsys.readouterr().out
    monkeypatch.setattr(ev, "build_live", lambda: (FakePort(), FakeRunner({"c:0": [_ok_window((4.0, 1, "Good morning"), (92.0, 2, "x"))]}), "m"))
    assert ev.main(common) == 1
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert "Good morning to you all" not in json.dumps(report)     # no transcript text in the report


def test_without_bars_the_run_says_so(monkeypatch, tmp_path, capsys):
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps({"clips": [_clip()]}), encoding="utf-8")
    (tmp_path / "c.m4a").write_bytes(b"x")
    monkeypatch.setattr(ev, "build_live", lambda: (FakePort(), FakeRunner({"c:0": [GOOD]}), "m"))
    monkeypatch.setattr(ev, "ffmpeg_cut", lambda *a: None)
    code = ev.main(["--yes", "--gold", str(gold_path), "--bars", str(tmp_path / "none.json"), "--media-dir", str(tmp_path),
                    "--report", str(tmp_path / "r.json")])
    assert code == 0 and "no bars yet" in capsys.readouterr().out

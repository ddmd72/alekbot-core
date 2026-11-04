"""Media tunnel START and RESULT: closed schemas, F7's media shape checks, the child result mapping (S5 card C0)."""
from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_tunnel import (
    START_MAX_BYTES, TunnelProbe, TunnelResult, TunnelStart, TunnelViolation, TunnelWindow, TunnelWindows,
    decode_result, decode_start, encode_result, encode_start, result_from_child, start_keys, tunnel_budget,
)

MiB = 1024 * 1024
# The recording behind these requests ("Community garden meeting (Alex, Kim, Jordan).mp4", 900 MiB) never travels:
# START carries its size only.
SIZE = 900 * MiB
PROBE = TunnelStart(op=SandboxOp.MEDIA_PROBE, source_bytes=SIZE, budget_bytes=tunnel_budget(SIZE), wall_s=45.0)
EACH = TunnelStart(op=SandboxOp.MEDIA_AUDIO_WINDOWS, source_bytes=SIZE, budget_bytes=tunnel_budget(SIZE), wall_s=225.0,
                   container="mov,mp4,m4a,3gp,3g2,mj2", duration_known=True,
                   windows=(TunnelWindow(0, 0.0, 1800.0), TunnelWindow(1, 1740.0, 1800.0), TunnelWindow(2, 3480.0, 120.0)))
# "Coimbra weekend voice memo.m4a" saved without a duration: a pass over three contiguous windows.
PASS = TunnelStart(op=SandboxOp.MEDIA_AUDIO_WINDOWS, source_bytes=40 * MiB, budget_bytes=tunnel_budget(40 * MiB),
                   wall_s=225.0, container="webm", duration_known=False,
                   windows=(TunnelWindow(3, 5220.0, 1800.0), TunnelWindow(4, 6960.0, 1800.0),
                            TunnelWindow(5, 8700.0, 1800.0)))


def _json(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj).encode()


# -- START ------------------------------------------------------------------------------------------------------------

def test_start_round_trips() -> None:
    for start in (PROBE, EACH, PASS):
        assert decode_start(encode_start(start), start.op) == start


def test_start_key_sets_are_pinned_and_carry_no_identity() -> None:
    assert start_keys(SandboxOp.MEDIA_PROBE) == {"source_bytes", "budget_bytes", "wall_s"}
    assert start_keys(SandboxOp.MEDIA_AUDIO_WINDOWS) == {
        "source_bytes", "budget_bytes", "wall_s", "container", "duration_known", "windows"}
    for op in (SandboxOp.MEDIA_PROBE, SandboxOp.MEDIA_AUDIO_WINDOWS):
        assert not {"url", "name", "id", "path"} & start_keys(op)
    assert set(json.loads(encode_start(EACH))) == start_keys(SandboxOp.MEDIA_AUDIO_WINDOWS)
    with pytest.raises(TunnelViolation):
        start_keys(SandboxOp.PARSE_EXTRACT)


@pytest.mark.parametrize("extra", [
    {"url": "https://drive.example.com/files/f-1?alt=media"},
    {"name": "Community garden meeting (Alex, Kim, Jordan).mp4"},
    {"id": "f-1"},
    {"path": "/Recordings/Coimbra weekend voice memo.m4a"},
    {"headers": {"Authorization": "Bearer tok-garden-17"}},
])
def test_start_refuses_any_key_outside_the_schema(extra: Dict[str, Any]) -> None:
    for start in (PROBE, EACH):
        obj = {**json.loads(encode_start(start)), **extra}
        with pytest.raises(TunnelViolation):
            decode_start(_json(obj), start.op)


def test_start_decoded_for_the_other_op_is_refused() -> None:
    with pytest.raises(TunnelViolation):
        decode_start(encode_start(EACH), SandboxOp.MEDIA_PROBE)
    with pytest.raises(TunnelViolation):
        decode_start(encode_start(PROBE), SandboxOp.MEDIA_AUDIO_WINDOWS)
    with pytest.raises(TunnelViolation):
        decode_start(encode_start(PROBE), SandboxOp.PARSE_EXTRACT)


@pytest.mark.parametrize("change", [
    {"source_bytes": 0}, {"source_bytes": 8 * 1024 ** 3 + 1}, {"source_bytes": "1000"}, {"source_bytes": True},
    {"budget_bytes": 0}, {"budget_bytes": tunnel_budget(SIZE) + 1}, {"budget_bytes": 1.5},
    {"wall_s": 0}, {"wall_s": 240.5}, {"wall_s": "60"},
    {"container": ""}, {"container": "mp4;drop"}, {"container": "a" * 81}, {"container": 7},
    {"duration_known": 1},
    {"windows": []}, {"windows": [[i, i * 60.0, 60.0] for i in range(65)]},
    {"windows": [[0, 0.0, 60.0], [0, 60.0, 60.0]]},           # duplicate index
    {"windows": [[0, 0.0]]}, {"windows": [{"index": 0, "start_s": 0.0, "duration_s": 1.0}]},
    {"windows": [[-1, 0.0, 60.0]]}, {"windows": [[1000, 0.0, 60.0]]}, {"windows": [[0, -1.0, 60.0]]},
    {"windows": [[0, 0.0, 0.0]]}, {"windows": [[True, 0.0, 60.0]]}, {"windows": [[0, 0.0, 1e400]]},
    {"windows": "0-1800"},
])
def test_start_refuses_out_of_range_fields(change: Dict[str, Any]) -> None:
    obj = {**json.loads(encode_start(EACH)), **change}
    with pytest.raises(TunnelViolation):
        decode_start(_json(obj), SandboxOp.MEDIA_AUDIO_WINDOWS)


def test_pass_takes_a_contiguous_run_of_at_most_three() -> None:
    base = json.loads(encode_start(PASS))
    for windows in ([[3, 0.0, 60.0], [5, 60.0, 60.0]], [[i, i * 60.0, 60.0] for i in range(4)],
                    [[4, 0.0, 60.0], [3, 60.0, 60.0]]):
        with pytest.raises(TunnelViolation):
            decode_start(_json({**base, "windows": windows}), SandboxOp.MEDIA_AUDIO_WINDOWS)


def test_probe_takes_no_window_fields() -> None:
    for change in ({"container": "webm"}, {"duration_known": False}, {"windows": (TunnelWindow(0, 0.0, 1.0),)}):
        with pytest.raises(TunnelViolation):
            TunnelStart(**{**PROBE.__dict__, **change})
    with pytest.raises(TunnelViolation):
        TunnelStart(**{**EACH.__dict__, "windows": ((0, 0.0, 1800.0),)})


@pytest.mark.parametrize("payload", [
    b"", b"[]", b"not json", b"\xff\xfe", b'{"wall_s": NaN, "source_bytes": 1, "budget_bytes": 1}',
    b'{"wall_s": 1, "wall_s": 2, "source_bytes": 1, "budget_bytes": 1}',   # duplicate key
    b"{" + b" " * START_MAX_BYTES + b"}",
])
def test_start_refuses_malformed_payloads(payload: bytes) -> None:
    with pytest.raises(TunnelViolation):
        decode_start(payload, SandboxOp.MEDIA_PROBE)


def test_start_refuses_a_non_tunnel_op() -> None:
    with pytest.raises(TunnelViolation):
        TunnelStart(**{**PROBE.__dict__, "op": SandboxOp.PARSE_EXTRACT})
    with pytest.raises(TunnelViolation):
        TunnelStart(**{**PROBE.__dict__, "op": "media.probe"})


# -- RESULT -----------------------------------------------------------------------------------------------------------

WINDOWS_OK = TunnelResult(status=SandboxStatus.OK, media=TunnelWindows(
    status=SandboxStatus.OK, windows=(TunnelWindow(0, 0.0, 1800.0), TunnelWindow(1, 1740.0, 1800.0),
                                      TunnelWindow(2, 3480.0, 95.5)),
    eof=True, duration_s=3575.5, reached_bound=False))
PROBE_OK = TunnelResult(status=SandboxStatus.OK, media=TunnelProbe(
    duration_s=3575.5, duration_known=True, has_audio=True, container="mov,mp4,m4a,3gp,3g2,mj2"))


def test_result_round_trips() -> None:
    timeout = TunnelResult(status=SandboxStatus.OK, media=TunnelWindows(
        status=SandboxStatus.TIMEOUT, windows=(TunnelWindow(3, 5220.0, 1800.0),), eof=False, duration_s=0.0,
        reached_bound=False, detail="wall in window"))
    bound = TunnelResult(status=SandboxStatus.OK, media=TunnelWindows(
        status=SandboxStatus.OK, windows=PASS.windows, eof=False, duration_s=0.0, reached_bound=True))
    cases = [(PROBE_OK, PROBE), (WINDOWS_OK, EACH), (timeout, PASS), (bound, PASS),
             (TunnelResult(status=SandboxStatus.ERROR, detail="tunnel_bounds"), EACH),
             (TunnelResult(status=SandboxStatus.TIMEOUT), PROBE), (TunnelResult(status=SandboxStatus.MEMORY), EACH)]
    for result, start in cases:
        assert decode_result(encode_result(result, start), start) == result


def _ok_windows_obj() -> Dict[str, Any]:
    return json.loads(encode_result(WINDOWS_OK, EACH))


@pytest.mark.parametrize("mutate", [
    lambda o: o.update(extra=1),
    lambda o: o.pop("detail"),
    lambda o: o["media"].update(url="https://drive.example.com/f-1"),
    lambda o: o["media"]["windows"][0].update(path="/tmp/window_0.opus"),
    lambda o: o["media"]["windows"].append({"index": 7, "start_s": 0.0, "duration_s": 60.0}),   # not requested
    lambda o: o["media"]["windows"].append(dict(o["media"]["windows"][0])),                     # duplicate
    lambda o: o["media"]["windows"][0].update(duration_s=0),
    lambda o: o["media"]["windows"][0].update(start_s=-5.0),
    lambda o: o["media"]["windows"][0].update(index=1.0),
    lambda o: o["media"].update(duration_s=14_402.0),
    lambda o: o["media"].update(eof=False),                    # a duration without eof
    lambda o: o["media"].update(reached_bound=True),           # reached_bound is a pass's, and not with eof
    lambda o: o["media"].update(status="memory"),
    lambda o: o["media"].update(status="bogus"),
    lambda o: o["media"].update(eof=1),
    lambda o: o["media"].update(windows="window_0.opus"),
    lambda o: o.update(status="bogus"),
    lambda o: o["media"].update(detail="x" * 257),
    lambda o: o.update(status="recycling"),
    lambda o: o.update(status="error"),                        # media without ok
    lambda o: o.pop("media"),                                  # ok without media
    lambda o: o.update(media=[]),
])
def test_result_refuses_shapes_outside_the_schema(mutate: Any) -> None:
    obj = _ok_windows_obj()
    mutate(obj)
    with pytest.raises(TunnelViolation):
        decode_result(_json(obj), EACH)


@pytest.mark.parametrize("raw", [
    b'{"status":"ok","detail":"","media":{"duration_s":Infinity,"duration_known":true,"has_audio":true,"container":"mp3"}}',
    b'{"status":"ok","detail":"","media":{"duration_s":NaN,"duration_known":true,"has_audio":true,"container":"mp3"}}',
    b'{"status":"ok","detail":"","media":{"duration_s":1e400,"duration_known":true,"has_audio":true,"container":"mp3"}}',
    b'{"status":"ok","detail":"","media":{"duration_s":0,"duration_known":true,"has_audio":true,"container":"mp3"}}',
    b'{"status":"ok","detail":"","media":{"duration_s":12,"duration_known":false,"has_audio":true,"container":"mp3"}}',
    b'{"status":"ok","detail":"","media":{"duration_s":12,"duration_known":true,"has_audio":1,"container":"mp3"}}',
    b'{"status":"ok","detail":"","media":{"duration_s":12,"duration_known":true,"has_audio":true,"container":"m p3"}}',
    b'{"status":"ok","detail":"","media":{"duration_s":12,"duration_known":true,"has_audio":true}}',
    b'{"status":"ok","detail":"","media":{"duration_s":"12","duration_known":true,"has_audio":true,"container":"mp3"}}',
    b'{"status":"ok","detail":"","media":{"duration_s":-1,"duration_known":false,"has_audio":true,"container":"mp3"}}',
    b'{"status":"ok","detail":7}',
])
def test_probe_result_refuses_bad_media(raw: bytes) -> None:
    with pytest.raises(TunnelViolation):
        decode_result(raw, PROBE)


def test_result_media_must_fit_its_op() -> None:
    each_mode_bound = TunnelResult(status=SandboxStatus.OK, media=TunnelWindows(
        status=SandboxStatus.OK, windows=(TunnelWindow(0, 0.0, 1800.0),), eof=False, duration_s=0.0,
        reached_bound=True))
    with pytest.raises(TunnelViolation):                       # reached_bound belongs to a pass
        encode_result(each_mode_bound, EACH)
    with pytest.raises(TunnelViolation):
        encode_result(WINDOWS_OK, PROBE)
    with pytest.raises(TunnelViolation):
        encode_result(PROBE_OK, EACH)
    with pytest.raises(TunnelViolation):
        decode_result(encode_result(PROBE_OK, PROBE), EACH)


# -- the child's result -----------------------------------------------------------------------------------------------

def test_child_probe_results() -> None:
    known = {"status": "ok", "duration_s": 3575.5, "duration_known": True, "has_audio": True,
             "container": "mov,mp4,m4a,3gp,3g2,mj2"}
    assert result_from_child(known, PROBE) == PROBE_OK
    unknown = result_from_child({**known, "duration_s": 0.0, "duration_known": False, "container": "matroska,webm"},
                                PROBE)
    assert unknown.media == TunnelProbe(duration_s=0.0, duration_known=False, has_audio=True, container="matroska,webm")
    assert result_from_child({"status": "timeout", "detail": "probe wall"}, PROBE) == TunnelResult(
        status=SandboxStatus.TIMEOUT, detail="probe wall")
    assert result_from_child({"status": "error", "detail": "ffprobe exit 1" + "x" * 300}, PROBE) == TunnelResult(
        status=SandboxStatus.ERROR, detail=("ffprobe exit 1" + "x" * 300)[:100])
    for bad in ({**known, "has_audio": "yes"}, {**known, "duration_s": float("nan")}, {**known, "container": None},
                {"status": "done"}, {}):
        with pytest.raises(TunnelViolation):
            result_from_child(bad, PROBE)


def test_child_windows_results() -> None:
    written = [{"index": 0, "start_s": 0.0, "duration_s": 1800.0}, {"index": 1, "start_s": 1740.0, "duration_s": 1800.0},
               {"index": 2, "start_s": 3480.0, "duration_s": 95.5}]
    assert result_from_child({"status": "ok", "windows": written, "eof": True, "duration_s": 3575.5}, EACH) == WINDOWS_OK
    # Not at the end: the duration and reached_bound are dropped (each-mode never reaches a bound), detail kept on timeout.
    partial = result_from_child({"status": "timeout", "windows": written[:1], "duration_s": 99.0,
                                 "reached_bound": True, "detail": "wall in window"}, EACH)
    assert partial.media == TunnelWindows(status=SandboxStatus.TIMEOUT, windows=(TunnelWindow(0, 0.0, 1800.0),),
                                          eof=False, duration_s=0.0, reached_bound=False, detail="wall in window")
    bound = result_from_child({"status": "ok", "eof": False, "reached_bound": True,
                               "windows": [{"index": w.index, "start_s": w.start_s, "duration_s": w.duration_s}
                                           for w in PASS.windows]}, PASS)
    assert isinstance(bound.media, TunnelWindows) and bound.media.reached_bound
    assert result_from_child({"status": "error", "detail": "no audio produced"}, PASS).status is SandboxStatus.ERROR
    for bad in ({"status": "ok"}, {"status": "ok", "windows": [{"index": 9, "start_s": 0.0, "duration_s": 1.0}]},
                {"status": "ok", "windows": ["window_0.opus"]},
                {"status": "ok", "windows": written, "eof": True},                    # eof without a duration
                {"status": "ok", "windows": written, "eof": True, "duration_s": 14_402.0}):
        with pytest.raises(TunnelViolation):
            result_from_child(bad, EACH)

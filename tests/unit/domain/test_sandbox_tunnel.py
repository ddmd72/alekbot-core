"""Media tunnel wire: frame codec and reader, budget, ledger, mirrored constants, engine result (S5 card C0)."""
from __future__ import annotations

import struct

import pytest

from src.adapters.drive_parser import media as drive_media
from src.domain import drive_index_params as params
from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_tunnel import (
    BODY_FRAMES, DATA_MAX, FRAME_HEADER_BYTES, MAX_OPEN_STREAMS, MAX_OPENS, MEDIA_CEILING, MEDIA_MAX_SECONDS,
    MEDIA_MAX_WINDOWS, MEDIA_PASS_MAX_WINDOWS, RESPONSE_FRAMES, RESULT_MAX_BYTES, SOURCE_MAX_BYTES, START_MAX_BYTES,
    WINDOW_MAX_BYTES, Frame, FrameReader, FrameType, SandboxTunnelEngine, TunnelEngineResult, TunnelLedger,
    TunnelResult, TunnelViolation, TunnelWindow, TunnelWindows, encode_frame, encode_frame_header, tunnel_budget,
)

MiB = 1024 * 1024
GiB = 1024 * MiB


def _raw(kind: int, frame_id: int, value: int, length: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BIQI", kind, frame_id, value, length) + payload


# -- frames -----------------------------------------------------------------------------------------------------------

def test_header_layout_is_17_bytes_big_endian() -> None:
    wire = encode_frame(Frame(FrameType.DATA, id=3, value=4096, payload=b"abc"))
    assert FRAME_HEADER_BYTES == 17
    assert wire == bytes([2]) + (3).to_bytes(4, "big") + (4096).to_bytes(8, "big") + (3).to_bytes(4, "big") + b"abc"


def test_round_trip_both_directions_in_one_feed_and_byte_by_byte() -> None:
    body = [Frame(FrameType.START, payload=b'{"wall_s":200}'), Frame(FrameType.DATA, 1, 0, b"\x00" * 1000),
            Frame(FrameType.DATA, 1, 1000, b"\x01" * 24), Frame(FrameType.END, 1, 1)]
    response = [Frame(FrameType.OPEN, 1, 0), Frame(FrameType.BEAT), Frame(FrameType.CLOSE, 1),
                Frame(FrameType.WINDOW, 0, 0, b"OggS"), Frame(FrameType.RESULT, payload=b"{}")]
    for frames, accept in ((body, BODY_FRAMES), (response, RESPONSE_FRAMES)):
        wire = b"".join(encode_frame(f) for f in frames)
        assert FrameReader(accept).feed(wire) == frames
        reader = FrameReader(accept)
        got = [f for i in range(len(wire)) for f in reader.feed(wire[i:i + 1])]
        assert got == frames
        reader.end()


def test_streamed_window_header_matches_whole_frame() -> None:
    payload = b"\x7f" * 70_000
    assert encode_frame_header(FrameType.WINDOW, 4, 0, len(payload)) + payload == encode_frame(
        Frame(FrameType.WINDOW, 4, 0, payload))


@pytest.mark.parametrize("frame", [
    Frame(FrameType.START, id=1, payload=b"{}"),            # START carries no id
    Frame(FrameType.START),                                  # empty START
    Frame(FrameType.START, payload=b"x" * (START_MAX_BYTES + 1)),
    Frame(FrameType.DATA, id=0, payload=b"x"),               # DATA needs a stream
    Frame(FrameType.DATA, id=1),                             # empty DATA
    Frame(FrameType.DATA, id=1, payload=b"x" * (DATA_MAX + 1)),
    Frame(FrameType.END, id=1, value=2),                     # END is eof (0) or upstream error (1)
    Frame(FrameType.END, id=1, payload=b"x"),
    Frame(FrameType.OPEN, id=0),
    Frame(FrameType.CLOSE, id=1, value=5),
    Frame(FrameType.BEAT, id=2),
    Frame(FrameType.WINDOW, id=0, value=1, payload=b"x"),
    Frame(FrameType.WINDOW, id=0),
    Frame(FrameType.RESULT, payload=b"x" * (RESULT_MAX_BYTES + 1)),
    Frame(FrameType.DATA, id=1 << 32, payload=b"x"),         # id past u32
    Frame(FrameType.OPEN, id=1, value=1 << 64),              # value past u64
])
def test_encoder_refuses_frames_outside_their_rule(frame: Frame) -> None:
    with pytest.raises(TunnelViolation):
        encode_frame(frame)


@pytest.mark.parametrize("wire", [
    _raw(9, 0, 0, 0),                                       # unknown type
    _raw(0, 0, 0, 0),
    _raw(FrameType.WINDOW, 0, 0, WINDOW_MAX_BYTES + 1),      # oversize, refused from the header alone
    _raw(FrameType.RESULT, 0, 0, RESULT_MAX_BYTES + 1),
    _raw(FrameType.OPEN, 1, 0, 0),                           # a body frame of the other direction
    _raw(FrameType.BEAT, 0, 0, 4, b"beat"),
])
def test_response_reader_refuses_bad_headers_before_buffering(wire: bytes) -> None:
    reader = FrameReader(RESPONSE_FRAMES if wire[0] != FrameType.OPEN else BODY_FRAMES)
    with pytest.raises(TunnelViolation):
        reader.feed(wire[:FRAME_HEADER_BYTES])
    with pytest.raises(TunnelViolation):                     # failed for good
        reader.feed(b"")
    with pytest.raises(TunnelViolation):
        reader.end()


def test_truncated_frame_is_refused_at_end_of_stream() -> None:
    whole = encode_frame(Frame(FrameType.WINDOW, 2, 0, b"x" * 100))
    for cut in (5, FRAME_HEADER_BYTES, len(whole) - 1):
        reader = FrameReader(RESPONSE_FRAMES)
        assert reader.feed(whole[:cut]) == []
        with pytest.raises(TunnelViolation):
            reader.end()
    FrameReader(RESPONSE_FRAMES).end()                       # an empty stream is not truncated


def test_body_begins_with_one_start() -> None:
    data = encode_frame(Frame(FrameType.DATA, 1, 0, b"x"))
    start = encode_frame(Frame(FrameType.START, payload=b"{}"))
    with pytest.raises(TunnelViolation):
        FrameReader(BODY_FRAMES).feed(data)
    with pytest.raises(TunnelViolation):
        FrameReader(BODY_FRAMES).feed(start + start)


def test_nothing_follows_result() -> None:
    result = encode_frame(Frame(FrameType.RESULT, payload=b"{}"))
    reader = FrameReader(RESPONSE_FRAMES)
    assert [f.kind for f in reader.feed(result)] == [FrameType.RESULT] and reader.result_seen
    with pytest.raises(TunnelViolation):
        reader.feed(encode_frame(Frame(FrameType.BEAT)))
    for tail in (result, encode_frame(Frame(FrameType.WINDOW, 0, 0, b"x")), b"\x00"):
        with pytest.raises(TunnelViolation):                 # two RESULTs, a frame or a stray byte after RESULT
            FrameReader(RESPONSE_FRAMES).feed(result + tail)


# -- budget and ledger ------------------------------------------------------------------------------------------------

def test_budget_formula() -> None:
    assert tunnel_budget(1) == 2 + 64 * MiB
    assert tunnel_budget(100 * MiB) == 264 * MiB
    assert tunnel_budget(4 * GiB) == 8 * GiB                 # 8 GiB + 64 MiB capped
    assert tunnel_budget(SOURCE_MAX_BYTES) == SOURCE_MAX_BYTES
    for bad in (0, -1, SOURCE_MAX_BYTES + 1, True, 1.5):
        with pytest.raises(TunnelViolation):
            tunnel_budget(bad)  # type: ignore[arg-type]


def test_ledger_follows_a_seek_and_counts_dropped_bytes() -> None:
    ledger = TunnelLedger(source_bytes=1000, budget_bytes=tunnel_budget(1000))
    ledger.open(1, 0)
    assert ledger.data(1, 0, 400) and ledger.newest == 1
    ledger.open(2, 900)                                       # ffmpeg seeks: the new stream before the old closes
    assert ledger.newest == 2 and ledger.is_open(1)
    ledger.close(1)
    assert ledger.data(1, 400, 100) is False                  # in flight for a closed id: counted, dropped
    assert ledger.end(1) is False
    assert ledger.data(2, 900, 100) and ledger.end(2)
    ledger.close(2)
    assert (ledger.opens, ledger.bytes_in, ledger.newest) == (2, 600, None)


@pytest.mark.parametrize("steps", [
    [("open", 1, 1000)],                                      # offset == size
    [("open", 1, -1)],
    [("open", 0, 0)],
    [("open", 1, 0), ("open", 1, 5)],                         # id reused while open
    [("open", 1, 0), ("close", 1), ("open", 1, 0)],           # id reused after close
    [("open", 1, 0), ("open", 2, 0), ("open", 3, 0)],         # a third open stream
    [("close", 7)],
    [("open", 1, 0), ("close", 1), ("close", 1)],
    [("data", 5, 0, 10)],                                     # never opened
    [("open", 1, 0), ("data", 1, 10, 10)],                    # not at the stream's position
    [("open", 1, 990), ("data", 1, 990, 20)],                 # past the source's end
    [("open", 1, 0), ("data", 1, 0, DATA_MAX + 1)],
    [("open", 1, 0), ("data", 1, 0, 0)],
    [("open", 1, 0), ("end", 1), ("data", 1, 0, 10)],         # DATA after END
    [("open", 1, 0), ("end", 1), ("end", 1)],
    [("end", 3)],
])
def test_ledger_refuses(steps: list) -> None:
    ledger = TunnelLedger(source_bytes=1000, budget_bytes=tunnel_budget(1000))
    with pytest.raises(TunnelViolation):
        for name, *args in steps:
            getattr(ledger, name)(*args)


def test_ledger_caps_opens_per_call() -> None:
    ledger = TunnelLedger(source_bytes=1000, budget_bytes=tunnel_budget(1000))
    for stream in range(1, MAX_OPENS + 1):
        ledger.open(stream, 0)
        ledger.close(stream)
    with pytest.raises(TunnelViolation):
        ledger.open(MAX_OPENS + 1, 0)
    assert MAX_OPENS == 256 and MAX_OPEN_STREAMS == 2


def test_ledger_budget_counts_every_byte_including_dropped() -> None:
    ledger = TunnelLedger(source_bytes=10 * DATA_MAX, budget_bytes=3 * DATA_MAX)
    ledger.open(1, 0)
    ledger.data(1, 0, DATA_MAX)
    ledger.close(1)
    ledger.data(1, DATA_MAX, DATA_MAX)                        # dropped, still counted
    ledger.open(2, 0)
    ledger.data(2, 0, DATA_MAX)
    with pytest.raises(TunnelViolation):
        ledger.data(2, DATA_MAX, 1)
    with pytest.raises(TunnelViolation):
        TunnelLedger(source_bytes=1000, budget_bytes=tunnel_budget(1000) + 1)


# -- mirrors and engine result ----------------------------------------------------------------------------------------

def test_mirrors_equal_their_sources() -> None:
    assert MEDIA_CEILING.wall_s == params.TRANSCRIBE_EXTRACT_WALL_S == 240
    assert MEDIA_CEILING.cpu_s == params.TRANSCRIBE_CHILD_CPU_S == 150
    assert SOURCE_MAX_BYTES == params.TRANSCRIBE_MAX_BYTES == 8 * GiB
    assert MEDIA_PASS_MAX_WINDOWS == params.MEDIA_PASS_MAX_WINDOWS == 3
    assert MEDIA_MAX_SECONDS == params.TRANSCRIBE_MAX_SECONDS
    assert MEDIA_MAX_WINDOWS == drive_media.MAX_WINDOWS_PER_RUN
    assert (MEDIA_CEILING.memory_bytes, MEDIA_CEILING.file_bytes, WINDOW_MAX_BYTES) == (GiB, 64 * MiB, 64 * MiB)


def test_media_ops_are_contract_ops() -> None:
    assert SandboxOp("media.probe") is SandboxOp.MEDIA_PROBE
    assert SandboxOp("media.audio_windows") is SandboxOp.MEDIA_AUDIO_WINDOWS


def test_engine_result_windows_must_match_the_result() -> None:
    media = TunnelWindows(status=SandboxStatus.OK, windows=(TunnelWindow(0, 0.0, 1800.0), TunnelWindow(1, 1740.0, 60.0)),
                          eof=True, duration_s=1800.0, reached_bound=False)
    ok = TunnelResult(status=SandboxStatus.OK, media=media)
    kwargs = dict(output_dir="/work/out/r1", peak_rss_bytes=1, wall_s=1.0, opens=1, bytes_in=1)
    TunnelEngineResult(result=ok, windows=((0, "/work/out/r1/0"), (1, "/work/out/r1/1")), **kwargs)  # type: ignore[arg-type]
    for files in (((0, "a"),), ((1, "b"), (0, "a")), ((0, "a"), (1, "b"), (2, "c"))):
        with pytest.raises(TunnelViolation):
            TunnelEngineResult(result=ok, windows=files, **kwargs)  # type: ignore[arg-type]
    failed = TunnelResult(status=SandboxStatus.ERROR, detail="tunnel_bounds")
    TunnelEngineResult(result=failed, windows=(), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(TunnelViolation):
        TunnelEngineResult(result=failed, windows=((0, "a"),), **kwargs)  # type: ignore[arg-type]


def test_engine_protocol_is_structural() -> None:
    class _Engine:
        async def start(self, workdir, limits, start):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    assert isinstance(_Engine(), SandboxTunnelEngine)
    assert not isinstance(object(), SandboxTunnelEngine)

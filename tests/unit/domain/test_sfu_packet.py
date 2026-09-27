"""Cloudflare Realtime SFU WebSocket-adapter framing: protobuf Packet{1 seq, 2 ts, 5 payload}."""
import pytest

from src.domain.sfu_packet import SfuPacket, decode_sfu_packet, encode_sfu_packet


def test_round_trip_with_large_values_and_frame_payload():
    data = encode_sfu_packet(300, 4_000_000_000, b"\x01\x02" * 1920)
    assert decode_sfu_packet(data) == SfuPacket(300, 4_000_000_000, b"\x01\x02" * 1920)


def test_known_bytes_match_protobuf_encoding():
    # field 1 varint 1, field 2 varint 960, field 5 bytes b"ab"
    assert encode_sfu_packet(1, 960, b"ab") == b"\x08\x01\x10\xc0\x07\x2a\x02ab"


def test_unknown_fields_are_skipped():
    data = b"\x18\x05" + encode_sfu_packet(7, 0, b"x")  # field 3 varint first
    assert decode_sfu_packet(data) == SfuPacket(7, 0, b"x")


@pytest.mark.parametrize("data", [b"\x08", b"\x2a\x05ab", b"\x0b", b"\x08\xff\xff"])
def test_malformed_input_raises_value_error(data):
    with pytest.raises(ValueError):
        decode_sfu_packet(data)

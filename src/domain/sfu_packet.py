"""Cloudflare Realtime SFU WebSocket adapter: protobuf Packet codec."""
from typing import NamedTuple, Tuple

# Cloudflare Realtime SFU WebSocket adapter: `message Packet { uint32 sequenceNumber = 1;
# uint32 timestamp = 2; bytes payload = 5; }` (developers.cloudflare.com, websocket-adapter).
# Three fields do not justify a protoc build step.
_TAG_SEQUENCE = 0x08
_TAG_TIMESTAMP = 0x10
_TAG_PAYLOAD = 0x2A


class SfuPacket(NamedTuple):
    sequence_number: int
    timestamp: int
    payload: bytes


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> Tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def encode_sfu_packet(sequence_number: int, timestamp: int, payload: bytes) -> bytes:
    return (bytes([_TAG_SEQUENCE]) + _varint(sequence_number & 0xFFFFFFFF)
            + bytes([_TAG_TIMESTAMP]) + _varint(timestamp & 0xFFFFFFFF)
            + bytes([_TAG_PAYLOAD]) + _varint(len(payload)) + payload)


def decode_sfu_packet(data: bytes) -> SfuPacket:
    sequence_number = timestamp = 0
    payload = b""
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        field_no, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
            if field_no == 1:
                sequence_number = value
            elif field_no == 2:
                timestamp = value
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated length-delimited field")
            if field_no == 5:
                payload = bytes(data[pos:pos + length])
            pos += length
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        if pos > len(data):
            raise ValueError("truncated fixed-width field")
    return SfuPacket(sequence_number, timestamp, payload)

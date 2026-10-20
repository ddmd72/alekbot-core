"""Shared by the S0 spike app and its client: the deterministic byte stream both sides hash.

A seed and a length name a stream; both ends can regenerate it, so a hash match proves the bytes survived the trip in
that direction without either side storing the payload.
"""
import hashlib
from typing import Iterator

CHUNK = 64 * 1024
_MIB = 1024 * 1024


def stream(seed: str, total: int) -> Iterator[bytes]:
    """`total` bytes in CHUNK pieces; piece i is repeated SHA-256 of seed:i."""
    sent = 0
    index = 0
    while sent < total:
        block = hashlib.sha256(f"{seed}:{index}".encode()).digest() * (CHUNK // 32)
        piece = block[: min(CHUNK, total - sent)]
        yield piece
        sent += len(piece)
        index += 1


def stream_sha256(seed: str, total: int) -> str:
    digest = hashlib.sha256()
    for piece in stream(seed, total):
        digest.update(piece)
    return digest.hexdigest()


def mib(count: int) -> int:
    return count * _MIB

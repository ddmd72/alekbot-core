"""BytesRangeSource: the in-memory range source of the main service's video probe (S5b)."""
import pytest

from src.adapters.sandbox_client.bytes_range_source import BytesRangeSource
from src.domain.sandbox_tunnel import DATA_MAX

DATA = bytes((i * 7 + 3) % 251 for i in range(DATA_MAX * 2 + 1000))


async def _read(source: BytesRangeSource, offset: int) -> bytes:
    return b"".join([chunk async for chunk in source.stream(offset)])


async def test_size_is_the_length():
    assert await BytesRangeSource(DATA).size() == len(DATA)


async def test_stream_from_zero_is_everything_in_bounded_chunks():
    chunks = [c async for c in BytesRangeSource(DATA).stream(0)]
    assert b"".join(chunks) == DATA and all(0 < len(c) <= DATA_MAX for c in chunks)


async def test_stream_from_the_middle_is_exactly_the_tail():
    assert await _read(BytesRangeSource(DATA), 12345) == DATA[12345:]


async def test_offset_at_the_end_yields_nothing():
    assert await _read(BytesRangeSource(DATA), len(DATA)) == b""


@pytest.mark.parametrize("offset", [-1, len(DATA) + 1])
async def test_an_offset_outside_the_source_raises(offset):
    with pytest.raises(ValueError):
        await _read(BytesRangeSource(DATA), offset)


async def test_aclose_is_a_no_op():
    source = BytesRangeSource(DATA)
    await source.aclose()
    assert await source.size() == len(DATA)

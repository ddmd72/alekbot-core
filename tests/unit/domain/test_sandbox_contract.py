"""Sandbox call contract (UNTRUSTED_CONTENT_SANDBOX_RFC §4.7, §4.8, I3)."""
import base64
import dataclasses
import json
from typing import Any, Dict

import pytest

from src.domain.sandbox_contract import (
    META_MAX_BYTES,
    SandboxContractError,
    SandboxEngine,
    SandboxEngineResult,
    SandboxLimits,
    SandboxMeta,
    SandboxOp,
    SandboxOutcome,
    SandboxStatus,
    clamp_limits,
    decode_meta,
    decode_outcome,
    encode_meta,
    encode_outcome,
    new_request_id,
)

CEILING = SandboxLimits(wall_s=120.0, memory_bytes=1 << 30, cpu_s=90.0, file_bytes=64 << 20)


def _b64(obj: Any) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _meta_obj(**extra: Any) -> Dict[str, Any]:
    obj: Dict[str, Any] = {
        "request_id": new_request_id(),
        "limits": {"wall_s": 10.0, "memory_bytes": 1 << 20, "cpu_s": 5.0, "file_bytes": 1 << 20},
    }
    obj.update(extra)
    return obj


def test_meta_field_set_is_pinned() -> None:
    # I3: adding a field to the contract must fail this test and force a review.
    assert {f.name for f in dataclasses.fields(SandboxMeta)} == {"request_id", "limits", "kind", "ext"}
    assert {f.name for f in dataclasses.fields(SandboxLimits)} == {
        "wall_s", "memory_bytes", "cpu_s", "file_bytes",
    }


@pytest.mark.parametrize(
    "key", ["user_id", "account_id", "item_id", "name", "path", "file_name", "whatever"]
)
def test_meta_rejects_identity_fields(key: str) -> None:
    with pytest.raises(SandboxContractError):
        decode_meta(_b64(_meta_obj(**{key: "x"})))


def test_meta_rejects_unknown_limit_key() -> None:
    obj = _meta_obj()
    obj["limits"]["user_id"] = 1
    with pytest.raises(SandboxContractError):
        decode_meta(_b64(obj))


def test_meta_rejects_over_8_kib() -> None:
    assert META_MAX_BYTES == 8192
    with pytest.raises(SandboxContractError):
        decode_meta(_b64(_meta_obj(kind="a" * 9000)))
    with pytest.raises(SandboxContractError):
        decode_meta("A" * (META_MAX_BYTES + 1))


@pytest.mark.parametrize("bad", ["", "!!!not-base64!!!", _b64([1, 2]), _b64({"limits": {}})])
def test_meta_rejects_malformed(bad: str) -> None:
    with pytest.raises(SandboxContractError):
        decode_meta(bad)


def test_meta_roundtrip() -> None:
    meta = SandboxMeta(request_id=new_request_id(), limits=CEILING, kind="docx", ext="docx")
    assert decode_meta(encode_meta(meta)) == meta
    bare = SandboxMeta(request_id=new_request_id(), limits=CEILING)
    assert decode_meta(encode_meta(bare)) == bare


def test_request_id_is_random_urlsafe_and_long_enough() -> None:
    ids = {new_request_id() for _ in range(200)}
    assert len(ids) == 200
    for rid in ids:
        assert len(rid) >= 16
        assert all(c.isalnum() or c in "-_" for c in rid)


def test_outcome_roundtrip() -> None:
    out = SandboxOutcome(status=SandboxStatus.OK, detail="", stats={"wall_ms": 12, "rss": 3})
    assert decode_outcome(encode_outcome(out)) == out


def test_outcome_closed_schema_unknown_status_and_extra_keys_rejected() -> None:
    with pytest.raises(SandboxContractError):
        decode_outcome(_b64({"status": "weird", "detail": "", "stats": {}}))
    with pytest.raises(SandboxContractError):
        decode_outcome(_b64({"status": "ok", "detail": "", "stats": {}, "extra": 1}))
    with pytest.raises(SandboxContractError):
        decode_outcome(_b64({"status": "ok", "detail": ""}))
    with pytest.raises(SandboxContractError):
        decode_outcome(_b64({"status": "ok", "detail": "", "stats": {"a": "text"}}))


def test_outcome_detail_bounded() -> None:
    with pytest.raises(SandboxContractError):
        SandboxOutcome(status=SandboxStatus.ERROR, detail="x" * 5000, stats={})
    with pytest.raises(SandboxContractError):
        decode_outcome(_b64({"status": "error", "detail": "x" * 5000, "stats": {}}))


def test_status_and_op_values() -> None:
    assert {s.value for s in SandboxStatus} == {
        "ok", "script_error", "timeout", "empty_output", "too_large",
        "recycling", "unconfigured", "bad_request", "error",
    }
    assert [o.value for o in SandboxOp] == ["render.docx"]


def test_clamp_limits_never_exceeds_ceiling() -> None:
    huge = SandboxLimits(wall_s=1e9, memory_bytes=1 << 60, cpu_s=1e9, file_bytes=1 << 60)
    assert clamp_limits(huge, CEILING) == CEILING


def test_clamp_limits_lowers_when_requested() -> None:
    small = SandboxLimits(wall_s=5.0, memory_bytes=1 << 20, cpu_s=2.0, file_bytes=1024)
    assert clamp_limits(small, CEILING) == small


@pytest.mark.parametrize("field", ["wall_s", "memory_bytes", "cpu_s", "file_bytes"])
def test_clamp_rejects_nonpositive(field: str) -> None:
    kwargs = dataclasses.asdict(CEILING)
    kwargs[field] = 0
    with pytest.raises(SandboxContractError):
        clamp_limits(SandboxLimits(**kwargs), CEILING)


def test_engine_result_is_frozen() -> None:
    res = SandboxEngineResult(
        status=SandboxStatus.OK, output_path="/w/out.docx", detail="", peak_rss_bytes=1, wall_s=0.1
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.detail = "x"  # type: ignore[misc]


async def test_engine_protocol_is_satisfiable() -> None:
    class Fake:
        async def run(self, body_path: str, workdir: str, limits: SandboxLimits) -> SandboxEngineResult:
            return SandboxEngineResult(SandboxStatus.EMPTY_OUTPUT, None, "none", 0, 0.0)

    engine: SandboxEngine = Fake()
    res = await engine.run("/b", "/w", CEILING)
    assert res.status is SandboxStatus.EMPTY_OUTPUT and res.output_path is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["wall_s", "memory_bytes", "cpu_s", "file_bytes"])
def test_meta_rejects_nan_and_infinite_limits(field: str, bad: float) -> None:
    obj = _meta_obj()
    obj["limits"][field] = bad  # json.dumps emits NaN / Infinity / -Infinity literals
    with pytest.raises(SandboxContractError):
        decode_meta(_b64(obj))


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["wall_s", "memory_bytes", "cpu_s", "file_bytes"])
def test_clamp_rejects_non_finite(field: str, bad: float) -> None:
    kwargs = dataclasses.asdict(CEILING)
    kwargs[field] = bad
    with pytest.raises(SandboxContractError):
        clamp_limits(SandboxLimits(**kwargs), CEILING)

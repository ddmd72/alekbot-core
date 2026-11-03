"""Recycle policy `abnormal` (light service; UNTRUSTED_CONTENT_SANDBOX_RFC §4.6, I2): a normal request keeps the instance
warm, every abnormal end recycles it. Policy `always` (the render service) is unchanged."""
import asyncio

import pytest

from src.domain.sandbox_contract import SandboxStatus
from tests.unit.web.test_sandbox_app import FakeEngine, make_app, outcome, post


async def test_ok_request_keeps_the_instance_warm_for_the_next_one(tmp_path):
    app, engine, rec, _ = make_app(tmp_path, recycle="abnormal")
    for _ in range(3):
        r = await post(app)
        assert r.status_code == 200 and outcome(r).status == SandboxStatus.OK
        assert r.headers.get("Connection", "").lower() != "close"
    assert rec.calls == 0 and len(engine.calls) == 3


async def test_a_parser_status_inside_an_ok_answer_is_normal(tmp_path):
    app, _, rec, _ = make_app(tmp_path, engine=FakeEngine(tmp_path, detail="no_text"), recycle="abnormal")
    r = await post(app)
    assert outcome(r).status == SandboxStatus.OK and outcome(r).detail == "no_text"
    assert rec.calls == 0
    assert (await post(app)).status_code == 200


@pytest.mark.parametrize("status", [SandboxStatus.SCRIPT_ERROR, SandboxStatus.TIMEOUT, SandboxStatus.ERROR,
                                    SandboxStatus.EMPTY_OUTPUT, SandboxStatus.MEMORY, SandboxStatus.TOO_LARGE])
async def test_every_non_ok_engine_outcome_recycles_once_and_the_next_request_is_refused(tmp_path, status):
    app, engine, rec, _ = make_app(tmp_path, engine=FakeEngine(tmp_path, status=status, detail="boom"),
                                   recycle="abnormal")
    r = await post(app)
    assert outcome(r).status == status and rec.calls == 1
    assert r.headers.get("Connection", "").lower() == "close"
    again = await post(app)
    assert again.status_code == 503 and outcome(again).status == SandboxStatus.RECYCLING
    assert len(engine.calls) == 1 and rec.calls == 1


async def test_engine_exception_recycles(tmp_path):
    app, _, rec, work_root = make_app(tmp_path, engine=FakeEngine(tmp_path, raises=RuntimeError("x")),
                                      recycle="abnormal")
    r = await post(app)
    assert r.status_code == 500 and rec.calls == 1 and list(work_root.iterdir()) == []


async def test_a_body_over_the_cap_recycles(tmp_path, monkeypatch):
    from src.web import sandbox_app
    monkeypatch.setattr(sandbox_app, "REQUEST_CAP_BYTES", 3)
    app, _, rec, _ = make_app(tmp_path, recycle="abnormal")
    r = await post(app, body=b"abcdef")
    assert r.status_code == 413 and rec.calls == 1


async def test_a_request_refused_before_any_payload_byte_does_not_recycle(tmp_path):
    app, _, rec, _ = make_app(tmp_path, recycle="abnormal")
    assert (await post(app, headers={})).status_code == 401
    assert rec.calls == 0
    assert (await post(app)).status_code == 200


async def test_a_busy_instance_still_answers_recycling_without_recycling_itself(tmp_path):
    gate = asyncio.Event()
    engine = FakeEngine(tmp_path, gate=gate)
    app, _, rec, _ = make_app(tmp_path, engine=engine, recycle="abnormal")
    first = asyncio.ensure_future(post(app))
    await asyncio.wait_for(engine.started.wait(), 5)
    second = await post(app)
    assert second.status_code == 503 and outcome(second).status == SandboxStatus.RECYCLING
    gate.set()
    assert (await first).status_code == 200
    assert rec.calls == 0 and (await post(app)).status_code == 200


async def test_policy_always_is_unchanged_an_ok_request_still_recycles(tmp_path):
    app, _, rec, _ = make_app(tmp_path, recycle="always")
    r = await post(app)
    assert r.status_code == 200 and r.headers.get("Connection", "").lower() == "close"
    assert rec.calls == 1
    assert (await post(app)).status_code == 503

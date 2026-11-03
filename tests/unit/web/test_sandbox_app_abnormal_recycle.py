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


def _root_app(tmp_path, monkeypatch, leftovers):
    from types import SimpleNamespace
    from quart import Quart
    from src.domain.sandbox_contract import SandboxOp
    from src.web import sandbox_app
    from tests.unit.web.test_sandbox_app import CEILING, FakeGate, FakeRecycler
    monkeypatch.setattr(sandbox_app.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sandbox_app.os, "chown", lambda p, u, g: None)
    monkeypatch.setattr(sandbox_app.pwd, "getpwnam", lambda n: SimpleNamespace(pw_uid=4242, pw_gid=4343))
    seen = []
    monkeypatch.setattr(sandbox_app, "_kill_uid_processes", lambda uid: seen.append(uid) or leftovers)
    work_root = tmp_path / "work"
    work_root.mkdir()
    engine, rec = FakeEngine(tmp_path), FakeRecycler()
    app = Quart(__name__)
    app.register_blueprint(sandbox_app.create_sandbox_blueprint(
        {SandboxOp.RENDER_DOCX: engine}, FakeGate(), rec, CEILING,
        {"work_root": str(work_root), "engine_user": "sbx", "recycle": "abnormal",
         "output_root": str(engine.out_root)}))
    return app, rec, seen


async def test_a_leftover_process_after_an_ok_recycles_and_logs_an_error(tmp_path, monkeypatch, caplog):
    app, rec, seen = _root_app(tmp_path, monkeypatch, leftovers=1)
    with caplog.at_level("ERROR"):
        r = await post(app)
    assert r.status_code == 200 and rec.calls == 1 and seen == [4242]
    assert r.headers.get("Connection", "").lower() == "close"
    assert any(x.levelname == "ERROR" and "survived" in x.getMessage() for x in caplog.records)
    assert (await post(app)).status_code == 503


async def test_no_leftover_process_after_an_ok_stays_warm(tmp_path, monkeypatch, caplog):
    app, rec, seen = _root_app(tmp_path, monkeypatch, leftovers=0)
    with caplog.at_level("ERROR"):
        assert (await post(app)).status_code == 200
        assert (await post(app)).status_code == 200
    assert rec.calls == 0 and seen == [4242, 4242]
    assert not [x for x in caplog.records if x.levelname == "ERROR"]


def test_kill_uid_processes_ignores_other_uids():
    import os
    from src.web import sandbox_app
    if not os.path.isdir("/proc"):
        pytest.skip("no /proc")
    assert sandbox_app._kill_uid_processes(2 ** 31 - 2) == 0

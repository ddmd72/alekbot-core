"""T4: sandbox server app (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6-§4.9, §4.14; plan G2, G8)."""
import asyncio
import logging
import os
from types import SimpleNamespace
from typing import List, Optional

import pytest
from quart import Quart

from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import (
    SandboxEngineResult,
    SandboxLimits,
    SandboxMeta,
    SandboxOp,
    SandboxStatus,
)
from src.web import sandbox_app

CEILING = SandboxLimits(
    wall_s=sc.CEILING_WALL_S,
    memory_bytes=sc.CEILING_MEMORY_BYTES,
    cpu_s=sc.CEILING_CPU_S,
    file_bytes=sc.CEILING_FILE_BYTES,
)
GOOD = {"Authorization": "Bearer good"}
MARKER = "SECRET-BODY-MARKER-12345"
ENGINE_STDOUT = "ENGINE-STDOUT-LEAK-67890"


class FakeGate:
    def authorize(self, authorization: Optional[str], op: str):
        if authorization == "Bearer good":
            return SimpleNamespace(allowed=True, reason=None)
        if authorization == "Bearer other":
            return SimpleNamespace(allowed=False, reason="forbidden_op")
        return SimpleNamespace(allowed=False, reason="unauthenticated")


class FakeRecycler:
    def __init__(self):
        self.calls = 0

    def request_served(self) -> None:
        self.calls += 1


class FakeEngine:
    def __init__(self, tmp_path, status=SandboxStatus.OK, payload=b"DOCXBYTES", detail="", gate=None, raises=None, always_output=False):
        self.out_root = tmp_path / "engine-out"
        self.out_root.mkdir(exist_ok=True)
        self.status = status
        self.payload = payload
        self.detail = detail
        self.gate = gate
        self.raises = raises
        self.always_output = always_output
        self.calls: List[tuple] = []
        self.started = asyncio.Event()

    async def run(self, body_path, workdir, limits):
        self.calls.append((body_path, workdir, limits, open(body_path, "rb").read()))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.raises:
            raise self.raises
        out_dir = self.out_root / os.path.basename(workdir)
        out_dir.mkdir()
        out = out_dir / "out.bin"
        out.write_bytes(self.payload)
        return SandboxEngineResult(
            status=self.status,
            output_path=str(out) if (self.status == SandboxStatus.OK or self.always_output) else None,
            detail=self.detail,
            peak_rss_bytes=1234,
            wall_s=0.5,
        )


def make_app(tmp_path, engine=None, recycle="always", recycler=None):
    engine = engine or FakeEngine(tmp_path)
    recycler = recycler or FakeRecycler()
    work_root = tmp_path / "work"
    work_root.mkdir(exist_ok=True)
    app = Quart(__name__)
    bp = sandbox_app.create_sandbox_blueprint(
        {SandboxOp.RENDER_DOCX: engine},
        FakeGate(),
        recycler,
        CEILING,
        {"work_root": str(work_root), "engine_user": None, "recycle": recycle,
         "output_root": str(engine.out_root)},
    )
    app.register_blueprint(bp)
    return app, engine, recycler, work_root


def meta_header(limits: SandboxLimits = CEILING, request_id: Optional[str] = None) -> str:
    return sc.encode_meta(SandboxMeta(request_id=request_id or sc.new_request_id(), limits=limits))


def hdrs(**extra):
    h = dict(GOOD)
    h[sc.META_HEADER] = meta_header()
    h.update(extra)
    return h


def outcome(resp) -> sc.SandboxOutcome:
    return sc.decode_outcome(resp.headers[sc.OUTCOME_HEADER])


async def post(app, body=b"payload", headers=None, op="render.docx"):
    return await app.test_client().post(f"/v1/{op}", data=body, headers=headers if headers is not None else hdrs())


async def test_health(tmp_path):
    app, *_ = make_app(tmp_path)
    resp = await app.test_client().get("/health")
    assert resp.status_code == 200


async def test_ok_streams_engine_output(tmp_path):
    app, engine, _, _ = make_app(tmp_path)
    resp = await post(app, body=MARKER.encode())
    assert resp.status_code == 200
    assert await resp.get_data() == b"DOCXBYTES"
    assert outcome(resp).status == SandboxStatus.OK
    assert engine.calls[0][3] == MARKER.encode()


async def test_requires_outcome_header_on_every_answer(tmp_path, monkeypatch):
    # 200
    app, *_ = make_app(tmp_path)
    assert sc.OUTCOME_HEADER in (await post(app)).headers
    # 400
    app, *_ = make_app(tmp_path)
    r = await post(app, headers=dict(GOOD))
    assert r.status_code == 400 and outcome(r).status == SandboxStatus.BAD_REQUEST
    # 404
    app, *_ = make_app(tmp_path)
    r = await post(app, op="nope.op")
    assert r.status_code == 404 and sc.OUTCOME_HEADER in r.headers
    # 413
    app, *_ = make_app(tmp_path)
    monkeypatch.setattr(sandbox_app, "REQUEST_CAP_BYTES", 4)
    r = await post(app, body=b"123456")
    assert r.status_code == 413 and outcome(r).status == SandboxStatus.TOO_LARGE
    # 503
    app, *_ = make_app(tmp_path)
    await post(app)
    r = await post(app)
    assert r.status_code == 503 and outcome(r).status == SandboxStatus.RECYCLING
    # 401 / 403
    app, *_ = make_app(tmp_path)
    r = await post(app, headers={})
    assert r.status_code == 401 and sc.OUTCOME_HEADER in r.headers
    r = await post(app, headers={"Authorization": "Bearer other"})
    assert r.status_code == 403 and sc.OUTCOME_HEADER in r.headers


async def test_unknown_op_404(tmp_path):
    app, engine, rec, _ = make_app(tmp_path)
    r = await post(app, op="drive.extract")
    assert r.status_code == 404
    assert not engine.calls and rec.calls == 0


async def test_gate_runs_before_unknown_op(tmp_path):
    app, *_ = make_app(tmp_path)
    r = await post(app, op="nope.op", headers={})
    assert r.status_code == 401


async def test_meta_unknown_key_400(tmp_path):
    import base64, json
    raw = json.dumps({"request_id": sc.new_request_id(), "user_id": "u", "limits": {
        "wall_s": 1, "memory_bytes": 1, "cpu_s": 1, "file_bytes": 1}}).encode()
    bad = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    app, engine, *_ = make_app(tmp_path)
    r = await post(app, headers={**GOOD, sc.META_HEADER: bad})
    assert r.status_code == 400 and not engine.calls


async def test_meta_over_limit_400(tmp_path):
    app, engine, *_ = make_app(tmp_path)
    r = await post(app, headers={**GOOD, sc.META_HEADER: "A" * (sc.META_MAX_BYTES + 1)})
    assert r.status_code == 400 and not engine.calls


async def test_nonpositive_limits_400(tmp_path):
    app, engine, rec, _ = make_app(tmp_path)
    bad = meta_header(SandboxLimits(wall_s=0, memory_bytes=1, cpu_s=1, file_bytes=1))
    r = await post(app, headers={**GOOD, sc.META_HEADER: bad})
    assert r.status_code == 400 and not engine.calls


async def test_body_over_cap_413_without_buffering_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_app, "REQUEST_CAP_BYTES", 1000)
    app, engine, rec, work_root = make_app(tmp_path)
    app.config["MAX_CONTENT_LENGTH"] = None  # Quart does not enforce it on a streamed body (S0.1): our count must
    async with app.test_client().request("/v1/render.docx", method="POST", headers=hdrs()) as conn:
        for _ in range(20):
            await conn.send(b"x" * 100)
        await conn.send_complete()
    resp = await conn.as_response()
    assert resp.status_code == 413
    assert not engine.calls
    assert list(work_root.iterdir()) == []


async def test_content_length_over_cap_413_no_recycle(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_app, "REQUEST_CAP_BYTES", 10)
    app, engine, rec, work_root = make_app(tmp_path)
    r = await post(app, body=b"y" * 50, headers=hdrs(**{"Content-Length": "50"}))
    assert r.status_code == 413 and rec.calls == 0 and not engine.calls
    assert list(work_root.iterdir()) == []


async def test_recycler_called_when_the_streamed_body_crosses_the_cap_midway(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_app, "REQUEST_CAP_BYTES", 250)
    app, engine, rec, _ = make_app(tmp_path)
    app.config["MAX_CONTENT_LENGTH"] = None
    async with app.test_client().request("/v1/render.docx", method="POST", headers=hdrs()) as conn:
        for _ in range(5):
            await conn.send(b"x" * 100)
        await conn.send_complete()
    resp = await conn.as_response()
    assert resp.status_code == 413
    assert rec.calls == 1


async def test_second_request_gets_503_recycling(tmp_path):
    app, engine, rec, _ = make_app(tmp_path)
    assert (await post(app)).status_code == 200
    r = await post(app)
    assert r.status_code == 503 and outcome(r).status == SandboxStatus.RECYCLING
    assert len(engine.calls) == 1 and rec.calls == 1


async def test_busy_request_gets_503_recycling(tmp_path):
    gate = asyncio.Event()
    engine = FakeEngine(tmp_path, gate=gate)
    app, _, rec, _ = make_app(tmp_path, engine=engine, recycle="never")
    first = asyncio.ensure_future(post(app))
    await asyncio.wait_for(engine.started.wait(), 5)
    second = await post(app)
    assert second.status_code == 503 and outcome(second).status == SandboxStatus.RECYCLING
    gate.set()
    assert (await first).status_code == 200
    assert (await post(app)).status_code == 200  # never: free again after the first finished


async def test_recycler_called_once_after_ok_response(tmp_path):
    app, _, rec, _ = make_app(tmp_path)
    await post(app)
    assert rec.calls == 1


@pytest.mark.parametrize("status", [SandboxStatus.SCRIPT_ERROR, SandboxStatus.TIMEOUT, SandboxStatus.ERROR,
                                    SandboxStatus.EMPTY_OUTPUT, SandboxStatus.BAD_REQUEST])
async def test_recycler_called_after_engine_failure_status(tmp_path, status):
    app, _, rec, _ = make_app(tmp_path, engine=FakeEngine(tmp_path, status=status, detail="boom"))
    r = await post(app)
    assert r.status_code != 200
    assert outcome(r).status == status
    assert rec.calls == 1


async def test_engine_exception_is_error_and_recycles(tmp_path):
    app, _, rec, work_root = make_app(tmp_path, engine=FakeEngine(tmp_path, raises=RuntimeError("x")))
    r = await post(app)
    assert r.status_code == 500 and outcome(r).status == SandboxStatus.ERROR
    assert rec.calls == 1 and list(work_root.iterdir()) == []


async def test_recycler_not_called_for_401(tmp_path):
    app, _, rec, _ = make_app(tmp_path)
    await post(app, headers={})
    await post(app, headers={"Authorization": "Bearer other"})
    assert rec.calls == 0
    assert (await post(app)).status_code == 200  # the single use is intact


async def test_recycler_not_called_for_content_length_413_or_meta_400(tmp_path, monkeypatch):
    app, _, rec, _ = make_app(tmp_path)
    r = await post(app, headers=dict(GOOD))
    assert r.status_code == 400 and rec.calls == 0
    monkeypatch.setattr(sandbox_app, "REQUEST_CAP_BYTES", 3)
    r = await post(app, body=b"abcdef", headers=hdrs(**{"Content-Length": "6"}))
    assert r.status_code == 413 and rec.calls == 0
    monkeypatch.undo()
    assert (await post(app)).status_code == 200  # the instance still serves


async def test_recycler_not_called_when_policy_never(tmp_path):
    app, _, rec, _ = make_app(tmp_path, recycle="never")
    await post(app)
    await post(app)
    assert rec.calls == 0


async def test_recycling_answer_carries_connection_close(tmp_path):
    app, *_ = make_app(tmp_path)
    r = await post(app)
    assert r.headers.get("Connection", "").lower() == "close"
    r2 = await post(app)
    assert r2.status_code == 503 and r2.headers.get("Connection", "").lower() == "close"


async def test_no_connection_close_when_not_recycling(tmp_path):
    app, *_ = make_app(tmp_path, recycle="never")
    r = await post(app)
    assert r.headers.get("Connection", "").lower() != "close"


async def test_quart_limits_come_from_the_contract(tmp_path):
    app, *_ = make_app(tmp_path)
    assert app.config["MAX_CONTENT_LENGTH"] == sc.REQUEST_CAP_BYTES
    floor = sc.CEILING_WALL_S + sandbox_app.TIMEOUT_MARGIN_S
    assert app.config["BODY_TIMEOUT"] >= floor
    assert app.config["RESPONSE_TIMEOUT"] >= floor
    assert app.config["BODY_TIMEOUT"] != 60 and app.config["RESPONSE_TIMEOUT"] != 60


async def test_limits_are_clamped_to_ceiling(tmp_path):
    app, engine, *_ = make_app(tmp_path)
    big = SandboxLimits(wall_s=10_000, memory_bytes=1 << 40, cpu_s=10_000, file_bytes=1 << 40)
    small = SandboxLimits(wall_s=5, memory_bytes=1 << 20, cpu_s=3, file_bytes=1 << 10)
    await post(app, headers={**GOOD, sc.META_HEADER: meta_header(big)})
    assert engine.calls[0][2] == CEILING
    app, engine, *_ = make_app(tmp_path)
    await post(app, headers={**GOOD, sc.META_HEADER: meta_header(small)})
    assert engine.calls[0][2] == small


async def test_failure_text_travels_in_body_capped(tmp_path):
    long_text = "é" * (sc.ERROR_TEXT_CAP_BYTES * 2)
    app, *_ = make_app(tmp_path, engine=FakeEngine(tmp_path, status=SandboxStatus.SCRIPT_ERROR, detail=long_text))
    r = await post(app)
    body = await r.get_data()
    assert r.status_code == 422
    assert r.headers["Content-Type"].startswith("text/plain")
    assert 0 < len(body) <= sc.ERROR_TEXT_CAP_BYTES
    body.decode("utf-8")  # still valid UTF-8
    assert outcome(r).detail == ""  # the header carries only the status


async def test_log_line_contains_no_identity_or_content(tmp_path, caplog):
    from src.utils.logger import logger as app_logger
    app, *_ = make_app(tmp_path, engine=FakeEngine(tmp_path, status=SandboxStatus.SCRIPT_ERROR, detail=ENGINE_STDOUT))
    rid = sc.new_request_id()
    with caplog.at_level(logging.DEBUG):
        app_logger.addHandler(caplog.handler)
        try:
            await post(app, body=MARKER.encode(), headers=hdrs(**{sc.META_HEADER: meta_header(request_id=rid)}))
        finally:
            app_logger.removeHandler(caplog.handler)
    lines = [r.getMessage() for r in caplog.records if "sandbox" in r.getMessage().lower()]
    assert len(lines) == 1
    line = lines[0]
    assert len(line) < 400
    assert MARKER not in line and ENGINE_STDOUT not in line and "good" not in line
    assert rid in line
    fields = {tok.split("=")[0] for tok in line.split() if "=" in tok}
    assert fields == {"request_id", "op", "input_bytes", "status", "wall_s", "peak_rss_bytes", "recycled"}


async def test_workdir_removed_on_success_and_failure_and_cancel(tmp_path):
    app, engine, rec, work_root = make_app(tmp_path)
    await post(app)
    assert list(work_root.iterdir()) == []
    assert list(engine.out_root.iterdir()) == []  # dirname(output_path) removed too

    app, engine, rec, work_root = make_app(tmp_path, engine=FakeEngine(tmp_path, status=SandboxStatus.TIMEOUT, detail="t"))
    await post(app)
    assert list(work_root.iterdir()) == []

    # Cancel: the handler task receives CancelledError while the engine runs (the test client does not
    # propagate a client-side cancel to the server task, so the engine raises it, the same code path).
    app, engine, rec, work_root = make_app(tmp_path, engine=FakeEngine(tmp_path, raises=asyncio.CancelledError()))
    try:
        await post(app)
    except BaseException:  # noqa: BLE001 - the test client may surface the cancel in any form
        pass
    assert list(work_root.iterdir()) == []
    assert rec.calls == 1


async def test_per_request_directory_name_is_request_id_only(tmp_path):
    app, engine, *_ = make_app(tmp_path)
    rid = sc.new_request_id()
    await post(app, headers=hdrs(**{sc.META_HEADER: meta_header(request_id=rid)}))
    body_path, workdir, _, _ = engine.calls[0]
    assert os.path.basename(workdir) == rid
    assert body_path == os.path.join(workdir, "body")


async def test_workdir_mode_0700_and_body_0600(tmp_path):
    seen = {}

    class Spy(FakeEngine):
        async def run(self, body_path, workdir, limits):
            seen["dir"] = os.stat(workdir).st_mode & 0o777
            seen["body"] = os.stat(body_path).st_mode & 0o777
            return await super().run(body_path, workdir, limits)

    app, *_ = make_app(tmp_path, engine=Spy(tmp_path))
    await post(app)
    assert seen == {"dir": 0o700, "body": 0o600}


async def test_duplicate_request_id_does_not_touch_existing_dir(tmp_path):
    app, engine, rec, work_root = make_app(tmp_path, recycle="never")
    rid = sc.new_request_id()
    (work_root / rid).mkdir()
    (work_root / rid / "keep").write_text("x")
    r = await post(app, headers=hdrs(**{sc.META_HEADER: meta_header(request_id=rid)}))
    assert r.status_code == 400 and not engine.calls
    assert (work_root / rid / "keep").exists()


async def test_root_chowns_workdir_and_body_to_engine_user(tmp_path, monkeypatch):
    chowned = []
    monkeypatch.setattr(sandbox_app.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sandbox_app.os, "chown", lambda p, u, g: chowned.append((os.path.basename(str(p)), u, g)))
    monkeypatch.setattr(sandbox_app.pwd, "getpwnam", lambda n: SimpleNamespace(pw_uid=4242, pw_gid=4343))
    work_root = tmp_path / "work"
    work_root.mkdir()
    engine = FakeEngine(tmp_path)
    app = Quart(__name__)
    app.register_blueprint(sandbox_app.create_sandbox_blueprint(
        {SandboxOp.RENDER_DOCX: engine}, FakeGate(), FakeRecycler(), CEILING,
        {"work_root": str(work_root), "engine_user": "sbx-engine", "recycle": "never"}))
    await post(app)
    names = [c[0] for c in chowned]
    assert "body" in names and len(names) == 2
    assert all(c[1:] == (4242, 4343) for c in chowned)


def test_root_without_engine_user_is_a_construction_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_app.os, "geteuid", lambda: 0)
    with pytest.raises(ValueError):
        sandbox_app.create_sandbox_blueprint(
            {}, FakeGate(), FakeRecycler(), CEILING,
            {"work_root": str(tmp_path), "engine_user": None, "recycle": "always"})


def test_root_with_unknown_engine_user_is_a_construction_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_app.os, "geteuid", lambda: 0)
    with pytest.raises(ValueError):
        sandbox_app.create_sandbox_blueprint(
            {}, FakeGate(), FakeRecycler(), CEILING,
            {"work_root": str(tmp_path), "engine_user": "no-such-user-xyz", "recycle": "always"})


def test_bad_recycle_policy_is_a_construction_error(tmp_path):
    with pytest.raises(ValueError):
        sandbox_app.create_sandbox_blueprint(
            {}, FakeGate(), FakeRecycler(), CEILING,
            {"work_root": str(tmp_path), "engine_user": None, "recycle": "abnormal"})


def test_module_imports_no_document_library():
    src = open(sandbox_app.__file__).read()
    for lib in ("docx", "PIL", "zipfile", "fitz", "pypdf", "openpyxl"):
        assert f"import {lib}" not in src and f"from {lib}" not in src


# ---- fix round 1 -------------------------------------------------------------------------------------------

async def _drive_asgi(app, body=b"payload", disconnect_after_start=False, events=None, headers=None):
    """Raw ASGI call, to see frame order and to disconnect at a chosen moment."""
    events = events if events is not None else []
    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "http.request", "body": body, "more_body": False})
    h = headers if headers is not None else hdrs()
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/v1/render.docx",
        "raw_path": b"/v1/render.docx", "query_string": b"", "root_path": "", "client": ("127.0.0.1", 1),
        "server": ("localhost", 80), "extensions": {},
        "headers": [(k.lower().encode(), v.encode()) for k, v in h.items()],
    }

    async def receive():
        return await inbox.get()

    async def send(message):
        if message["type"] == "http.response.start":
            events.append("start")
            if disconnect_after_start:
                await inbox.put({"type": "http.disconnect"})
                await asyncio.sleep(30)  # the cancel lands here, before the body context is entered
        elif message["type"] == "http.response.body":
            events.append("end" if not message.get("more_body") else "body")

    try:
        await app(scope, receive, send)
    except BaseException:  # noqa: BLE001
        pass
    await asyncio.sleep(0.05)
    return events


class OrderRecycler(FakeRecycler):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def request_served(self) -> None:
        super().request_served()
        self.events.append("recycled")


async def test_recycler_fires_after_the_final_frame(tmp_path):
    events: list = []
    app, _, rec, _ = make_app(tmp_path, recycler=OrderRecycler(events))
    await _drive_asgi(app, events=events)
    assert events[-1] == "recycled" and events[-2] == "end"
    assert rec.calls == 1


@pytest.mark.parametrize("policy", ["always", "never"])
async def test_cancel_after_handoff_recycles_once_releases_busy_and_closes_file(tmp_path, monkeypatch, policy):
    handles = []
    real_open = sandbox_app._open_output

    def spy(path):
        opened = real_open(path)
        handles.append(opened[0])
        return opened

    monkeypatch.setattr(sandbox_app, "_open_output", spy)
    app, _, rec, work_root = make_app(tmp_path, recycle=policy)
    await _drive_asgi(app, disconnect_after_start=True)
    assert rec.calls == (1 if policy == "always" else 0)
    assert handles and handles[0].closed
    assert list(work_root.iterdir()) == []
    if policy == "never":
        assert (await post(app)).status_code == 200  # busy was released


async def test_os_open_failure_has_outcome_header_and_no_recycle(tmp_path, monkeypatch):
    real_open = os.open

    def failing(path, flags, *a, **kw):
        if str(path).endswith("/body"):
            raise OSError(28, "no space")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(sandbox_app.os, "open", failing)
    app, engine, rec, work_root = make_app(tmp_path)
    r = await post(app)
    assert r.status_code == 500 and outcome(r).status == SandboxStatus.ERROR
    assert rec.calls == 0 and not engine.calls and list(work_root.iterdir()) == []


async def test_mkdir_failure_has_outcome_header(tmp_path, monkeypatch):
    def failing(path, mode=0o777):
        raise PermissionError(13, "denied")

    app, _, rec, _ = make_app(tmp_path)
    monkeypatch.setattr(sandbox_app.os, "mkdir", failing)
    r = await post(app)
    assert r.status_code == 500 and outcome(r).status == SandboxStatus.ERROR and rec.calls == 0


async def test_os_write_failure_has_outcome_header_and_recycles_once(tmp_path, monkeypatch):
    def failing(fd, data):
        raise OSError(28, "no space")

    monkeypatch.setattr(sandbox_app.os, "write", failing)
    app, engine, rec, work_root = make_app(tmp_path)
    r = await post(app)
    assert r.status_code == 500 and outcome(r).status == SandboxStatus.ERROR
    assert rec.calls == 1 and not engine.calls and list(work_root.iterdir()) == []


async def test_405_and_unrouted_paths_carry_the_outcome_header(tmp_path):
    app, *_ = make_app(tmp_path)
    r = await app.test_client().get("/v1/render.docx", headers=GOOD)
    assert r.status_code == 405 and sc.OUTCOME_HEADER in r.headers
    r = await app.test_client().get("/nowhere")
    assert r.status_code == 404 and sc.OUTCOME_HEADER in r.headers
    assert sc.OUTCOME_HEADER not in (await app.test_client().get("/health")).headers


async def test_stalled_upload_is_cut_at_the_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_app, "TIMEOUT_MARGIN_S", 0.2)
    work_root = tmp_path / "work"
    work_root.mkdir()
    engine = FakeEngine(tmp_path)
    rec = FakeRecycler()
    app = Quart(__name__)
    app.register_blueprint(sandbox_app.create_sandbox_blueprint(
        {SandboxOp.RENDER_DOCX: engine}, FakeGate(), rec,
        SandboxLimits(wall_s=0.1, memory_bytes=1 << 30, cpu_s=1, file_bytes=1 << 20),
        {"work_root": str(work_root), "engine_user": None, "recycle": "always"}))
    async with app.test_client().request("/v1/render.docx", method="POST", headers=hdrs()) as conn:
        await conn.send(b"some bytes then silence")
    resp = await conn.as_response()
    assert resp.status_code == 408 and outcome(resp).status == SandboxStatus.BAD_REQUEST
    assert not engine.calls and rec.calls == 1 and list(work_root.iterdir()) == []


async def test_quarts_own_cap_firing_is_a_413_and_recycles(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_app, "REQUEST_CAP_BYTES", 1000)
    app, engine, rec, work_root = make_app(tmp_path)
    assert app.config["MAX_CONTENT_LENGTH"] == 1000
    async with app.test_client().request("/v1/render.docx", method="POST", headers=hdrs()) as conn:
        for _ in range(20):
            await conn.send(b"x" * 100)
        await conn.send_complete()
    resp = await conn.as_response()
    assert resp.status_code == 413 and not engine.calls
    assert rec.calls == 1 and list(work_root.iterdir()) == []


async def test_non_ok_result_with_output_path_removes_its_dir(tmp_path):
    engine = FakeEngine(tmp_path, status=SandboxStatus.SCRIPT_ERROR, detail="bad", always_output=True)
    app, _, rec, _ = make_app(tmp_path, engine=engine)
    r = await post(app)
    assert r.status_code == 422
    assert list(engine.out_root.iterdir()) == []


async def test_bogus_output_path_is_never_removed_or_streamed(tmp_path):
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "out.bin").write_bytes(b"keep")

    class Bogus(FakeEngine):
        async def run(self, body_path, workdir, limits):
            return SandboxEngineResult(SandboxStatus.OK, str(precious / "out.bin"), "", 1, 0.1)

    app, _, rec, _ = make_app(tmp_path, engine=Bogus(tmp_path))
    r = await post(app)
    assert r.status_code == 500 and outcome(r).status == SandboxStatus.ERROR
    assert (precious / "out.bin").read_bytes() == b"keep"
    assert rec.calls == 1


async def test_without_output_root_only_the_render_out_prefix_is_removable(tmp_path):
    out_ok = tmp_path / "x" / "render_out_abc"
    out_bad = tmp_path / "x" / "other"
    out_ok.mkdir(parents=True)
    out_bad.mkdir()
    (out_ok / "o").write_bytes(b"1")
    (out_bad / "o").write_bytes(b"2")
    paths = iter([out_bad / "o", out_ok / "o"])

    class Pick(FakeEngine):
        async def run(self, body_path, workdir, limits):
            return SandboxEngineResult(SandboxStatus.OK, str(next(paths)), "", 1, 0.1)

    work_root = tmp_path / "work"
    work_root.mkdir()
    app = Quart(__name__)
    app.register_blueprint(sandbox_app.create_sandbox_blueprint(
        {SandboxOp.RENDER_DOCX: Pick(tmp_path)}, FakeGate(), FakeRecycler(), CEILING,
        {"work_root": str(work_root), "engine_user": None, "recycle": "never"}))
    r = await post(app)
    assert r.status_code == 500 and (out_bad / "o").exists()
    r = await post(app)
    assert r.status_code == 200 and not out_ok.exists()

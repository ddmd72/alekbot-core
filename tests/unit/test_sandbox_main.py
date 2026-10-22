"""T5: sandbox_main entry point: supervisor mode, --serve app assembly, start-up refusals (RFC §4.4, §4.6, §4.11, §4.12; plan G4, G7, G9)."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import sandbox_main
from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxEngineResult, SandboxLimits, SandboxMeta, SandboxOp, SandboxStatus
from src.web import sandbox_app

REPO = Path(__file__).resolve().parents[2]
CALLER = "caller@example.invalid"


@pytest.fixture
def env(tmp_path):
    return {
        "PATH": os.environ.get("PATH", ""),
        "SANDBOX_URL": "https://sandbox.example.invalid",
        "SANDBOX_CALLERS_RENDER": CALLER,
        "SANDBOX_WORK_ROOT": str(tmp_path / "work"),
        "SANDBOX_OUTPUT_ROOT": str(tmp_path / "out"),
    }


class FakeRecycler:
    def __init__(self):
        self.calls = 0
        self.events = None

    def request_served(self):
        self.calls += 1
        if self.events is not None:
            self.events.append("recycler")


class AllowGate:
    def __init__(self, *args, **kwargs):
        self.args = args

    def authorize(self, authorization, op):
        return SimpleNamespace(allowed=True, reason=None)


class FakeEngine:
    def __init__(self, out_root: Path):
        self.out_root = out_root

    async def run(self, body_path, workdir, limits):
        out_dir = self.out_root / ("render_out_" + os.path.basename(workdir))
        out_dir.mkdir(parents=True)
        out = out_dir / "out.bin"
        out.write_bytes(b"R" * 300_000)
        return SandboxEngineResult(status=SandboxStatus.OK, output_path=str(out), detail="", peak_rss_bytes=1, wall_s=0.1)


def _no_engines(plan):
    raise AssertionError("the engine registry must not be built here")


# ---- app assembly --------------------------------------------------------------------------------------------

def test_build_app_wires_engines_gate_recycler_and_the_one_output_root(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(sandbox_main, "build_engine_registry", lambda plan: seen.setdefault("plan", plan) and {SandboxOp.RENDER_DOCX: object()})
    real = sandbox_main.create_sandbox_blueprint

    def spy(engines, gate, recycler, ceiling, config):
        seen.update(engines=engines, gate=gate, recycler=recycler, ceiling=ceiling, config=dict(config))
        return real(engines, gate, recycler, ceiling, config)

    monkeypatch.setattr(sandbox_main, "create_sandbox_blueprint", spy)
    recycler = FakeRecycler()
    app = sandbox_main.build_app(env, recycler)

    plan = seen["plan"]
    assert {r.rule for r in app.url_map.iter_rules()} >= {"/health", "/v1/<op>"}
    assert isinstance(seen["gate"], sandbox_main.SandboxCallerGate)
    assert seen["recycler"] is recycler
    assert seen["config"]["output_root"] == os.path.realpath(plan.output_root)
    assert seen["config"]["work_root"] == plan.work_root
    assert seen["config"]["recycle"] == "always"
    assert seen["ceiling"] == SandboxLimits(sc.CEILING_WALL_S, sc.CEILING_MEMORY_BYTES, sc.CEILING_CPU_S, sc.CEILING_FILE_BYTES)


async def test_unconfigured_serves_health_and_answers_every_op_503_without_building_engines(env, monkeypatch):
    env["SANDBOX_URL"] = ""
    monkeypatch.setattr(sandbox_main, "build_engine_registry", _no_engines)
    app = sandbox_main.build_app(env, FakeRecycler())
    client = app.test_client()
    assert (await client.get("/health")).status_code == 200
    for path in ("/v1/render.docx", "/v1/anything"):
        reply = await client.post(path, data=b"x")
        assert reply.status_code == 503
        assert sc.decode_outcome(reply.headers[sc.OUTCOME_HEADER]).status == SandboxStatus.UNCONFIGURED


def test_build_app_under_always_refuses_a_server_without_the_supervisors_pipe(env, monkeypatch):
    monkeypatch.setattr(sandbox_main, "build_engine_registry", lambda plan: {})
    with pytest.raises(sandbox_main.SandboxStartupError, match="supervisor"):
        sandbox_main.build_app(env, None)


def test_build_app_never_policy_needs_no_pipe(env, monkeypatch):
    env["SANDBOX_RECYCLE"] = "never"
    monkeypatch.setattr(sandbox_main, "build_engine_registry", lambda plan: {})
    sandbox_main.build_app(env, None)


@pytest.mark.parametrize("policy", ["abnormal", "sometimes", ""])
def test_unknown_policy_refuses_start(env, monkeypatch, policy):
    env["SANDBOX_RECYCLE"] = policy
    monkeypatch.setattr(sandbox_main, "SandboxSupervisor", lambda *a, **k: pytest.fail("must not start"))
    assert sandbox_main.supervise(env, install_signals=False) != 0


async def test_server_waits_for_response_flush_before_signalling(env, monkeypatch, tmp_path):
    """The recycler may fire only after the final response frame has gone out (S0-b / G7: the supervisor's SIGTERM
    then lets Hypercorn finish the connection)."""
    out_root = tmp_path / "out"
    monkeypatch.setattr(sandbox_main, "build_engine_registry", lambda plan: {SandboxOp.RENDER_DOCX: FakeEngine(out_root)})
    monkeypatch.setattr(sandbox_main, "SandboxCallerGate", AllowGate)
    Path(env["SANDBOX_WORK_ROOT"]).mkdir()
    events = []
    recycler = FakeRecycler()
    recycler.events = events
    app = sandbox_main.build_app(env, recycler)

    meta = sc.encode_meta(SandboxMeta(request_id=sc.new_request_id(), limits=SandboxLimits(
        sc.CEILING_WALL_S, sc.CEILING_MEMORY_BYTES, sc.CEILING_CPU_S, sc.CEILING_FILE_BYTES)))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "2", "method": "POST", "scheme": "http",
             "path": "/v1/render.docx", "raw_path": b"/v1/render.docx", "query_string": b"", "root_path": "",
             "headers": [(b"host", b"x"), (b"x-sandbox-meta", meta.encode()), (b"content-length", b"7")],
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
    import asyncio
    inbox = [{"type": "http.request", "body": b"payload", "more_body": False}]
    stream_closed = asyncio.Event()

    async def receive():
        if inbox:
            return inbox.pop(0)
        # Like hypercorn: `http.disconnect` only once the stream is closed after the final frame. An immediate
        # disconnect models a client that left before the answer; Quart >= 0.21 then cancels the handler at once.
        await stream_closed.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            events.append("body-final" if not message.get("more_body") else "body-chunk")
            if not message.get("more_body"):
                stream_closed.set()

    await asyncio.wait_for(app.asgi_app(scope, receive, send), timeout=30)  # hang guard: fail, never stall CI
    await asyncio.sleep(0.05)
    assert events.count("recycler") == 1
    assert events.index("recycler") > events.index("body-final")
    assert events[-1] == "recycler"


# ---- supervisor mode -----------------------------------------------------------------------------------------

def test_main_refuses_start_on_cloud_run_with_empty_caller_set(env, monkeypatch):
    env["K_SERVICE"] = "svc"
    del env["SANDBOX_CALLERS_RENDER"]
    monkeypatch.setattr(sandbox_main, "SandboxSupervisor", lambda *a, **k: pytest.fail("must not start"))
    assert sandbox_main.main([], env, install_signals=False) != 0


def test_main_starts_locally_without_callers(env, monkeypatch):
    del env["SANDBOX_CALLERS_RENDER"]
    env["SANDBOX_URL"] = ""
    built = {}

    class Fake:
        def __init__(self, policy, spawn, **kwargs):
            built["policy"] = policy

        def run(self):
            return 0

        def terminate(self):
            pass

    monkeypatch.setattr(sandbox_main, "SandboxSupervisor", Fake)
    assert sandbox_main.main([], env, install_signals=False) == 0
    assert built["policy"] == "always"


def test_open_plan_builds_an_open_gate_that_allows_render(env, monkeypatch):
    del env["SANDBOX_CALLERS_RENDER"]
    env["SANDBOX_URL"] = ""
    seen = {}
    monkeypatch.setattr(sandbox_main, "build_engine_registry", lambda plan: {})
    real = sandbox_main.create_sandbox_blueprint

    def spy(engines, gate, *rest):
        seen["gate"] = gate
        return real(engines, gate, *rest)

    monkeypatch.setattr(sandbox_main, "create_sandbox_blueprint", spy)
    sandbox_main.build_app(env, FakeRecycler())
    assert seen["gate"].authorize(None, "render.docx").allowed is True


def test_open_mode_binds_loopback_only(env):
    from src.composition.sandbox_setup import validate_startup
    open_env = {k: v for k, v in env.items() if k not in ("SANDBOX_CALLERS_RENDER", "SANDBOX_URL")}
    assert sandbox_main.bind_host(validate_startup(open_env, on_cloud_run=False)) == "127.0.0.1"
    assert sandbox_main.bind_host(validate_startup(env, on_cloud_run=False)) == "0.0.0.0"
    cloud = {**env, "K_SERVICE": "svc"}
    assert sandbox_main.bind_host(validate_startup(cloud, on_cloud_run=True)) == "0.0.0.0"


@pytest.mark.parametrize("raw", ["abc", "99999"])
def test_malformed_recycle_fd_is_a_logged_refusal_not_a_traceback(env, monkeypatch, raw):
    env["SANDBOX_RECYCLE_FD"] = raw
    monkeypatch.setattr(sandbox_main, "build_engine_registry", lambda plan: {})
    monkeypatch.setattr(sandbox_main, "run_server", lambda *a, **k: pytest.fail("must not serve"))
    assert sandbox_main.main(["--serve"], env) == 2


def test_main_starts_locally_without_service_url(env, monkeypatch):
    """Locally (K_SERVICE unset) no URL is needed to start: the service comes up unconfigured. The brief's
    `without_callers` variant is impossible: T7's validate_startup requires the RENDER group even locally
    (pinned by test_validate_startup_missing_render_group_refused)."""
    env["SANDBOX_URL"] = ""
    built = {}

    class Fake:
        def __init__(self, policy, spawn, **kwargs):
            built["policy"] = policy

        def run(self):
            return 0

        def terminate(self):
            pass

    monkeypatch.setattr(sandbox_main, "SandboxSupervisor", Fake)
    assert sandbox_main.main([], env, install_signals=False) == 0
    assert built["policy"] == "always"


def test_secret_like_env_refuses_start(env, monkeypatch):
    env["SOME_API_KEY"] = "do-not-log-me"
    monkeypatch.setattr(sandbox_main, "SandboxSupervisor", lambda *a, **k: pytest.fail("must not start"))
    assert sandbox_main.main([], env, install_signals=False) != 0


def test_supervisor_creates_the_work_root_traversable_but_not_listable(env, monkeypatch):
    class Fake:
        def __init__(self, *a, **k):
            pass

        def run(self):
            return 0

        def terminate(self):
            pass

    monkeypatch.setattr(sandbox_main, "SandboxSupervisor", Fake)
    assert sandbox_main.supervise(env, install_signals=False) == 0
    mode = stat.S_IMODE(os.stat(env["SANDBOX_WORK_ROOT"]).st_mode)
    assert mode == 0o711          # others may traverse to their own 0700 request directory, never list or write


def test_work_root_that_is_a_symlink_refuses_start(env, tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.symlink(target, env["SANDBOX_WORK_ROOT"])
    monkeypatch.setattr(sandbox_main, "SandboxSupervisor", lambda *a, **k: pytest.fail("must not start"))
    assert sandbox_main.supervise(env, install_signals=False) != 0


@pytest.mark.datafile
def test_server_child_inherits_cloud_run_marker_url_and_caller_sets(tmp_path):
    """A scrubbed child environment would build an OPEN gate on Cloud Run (T6): the child must see all of these."""
    out = tmp_path / "seen.json"
    probe = ("import json,os,sys;"
             "json.dump({k: os.environ.get(k) for k in ('K_SERVICE','SANDBOX_URL','SANDBOX_CALLERS_RENDER',"
             "'SANDBOX_RECYCLE_FD')}, open(sys.argv[1],'w'))")
    env = {"K_SERVICE": "svc", "SANDBOX_URL": "https://sandbox.example.invalid", "SANDBOX_CALLERS_RENDER": CALLER,
           "PATH": os.environ.get("PATH", "")}
    spawn = sandbox_main.make_spawn([sys.executable, "-c", probe, str(out)], env)
    read_fd, write_fd = os.pipe()
    pid = spawn(write_fd)
    os.waitpid(pid, 0)
    seen = json.loads(out.read_text())
    assert seen["K_SERVICE"] == "svc"
    assert seen["SANDBOX_URL"] == env["SANDBOX_URL"]
    assert seen["SANDBOX_CALLERS_RENDER"] == CALLER
    assert seen["SANDBOX_RECYCLE_FD"] == str(write_fd)
    os.close(read_fd)
    os.close(write_fd)


@pytest.mark.datafile
def test_spawn_starts_the_server_in_its_own_group(tmp_path):
    out = tmp_path / "pg.txt"
    probe = "import os,sys;open(sys.argv[1],'w').write(f'{os.getpgrp()} {os.getpid()}')"
    spawn = sandbox_main.make_spawn([sys.executable, "-c", probe, str(out)], {"PATH": os.environ.get("PATH", "")})
    read_fd, write_fd = os.pipe()
    pid = spawn(write_fd)
    os.waitpid(pid, 0)
    group, own = (int(x) for x in out.read_text().split())
    assert group == own != os.getpgrp()
    os.close(read_fd)
    os.close(write_fd)


@pytest.mark.datafile
@pytest.mark.slow
def test_sandbox_main_import_closure_excludes_the_main_service_stack():
    code = ("import sys, sandbox_main;"
            "bad=[m for m in sys.modules if m.startswith(('src.infrastructure','src.agents','src.services',"
            "'src.handlers','google.cloud.firestore','src.adapters.firestore'))]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().splitlines()[-1] == "[]", out.stdout + out.stderr

"""T5: sandbox supervisor, recycler and recycle policy (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6; plan G4, G7)."""
import ast
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from src.web.sandbox_recycler import PipeRecycler
from src.web.sandbox_supervisor import SandboxSupervisor

REPO = Path(__file__).resolve().parents[3]
SERVER_PID = 4242


class FakeKernel:
    """waitpid/kill/killpg doubles. `children` are pids waitpid(-1) may return; the server obeys SIGTERM unless told not to."""

    def __init__(self, obey_sigterm: bool = True):
        self.exited = []          # (pid, raw status)
        self.signals = []         # (pid, signal)
        self.group_signals = []
        self.obey_sigterm = obey_sigterm
        self.waitpid_calls = []

    def exit(self, pid: int, code: int) -> None:
        self.exited.append((pid, code << 8))

    def die_by_signal(self, pid: int, sig: int) -> None:
        self.exited.append((pid, sig))

    def waitpid(self, pid, options):
        self.waitpid_calls.append(pid)
        for index, (child, status) in enumerate(self.exited):
            if pid in (-1, child):
                del self.exited[index]
                return child, status
        return 0, 0

    def kill(self, pid, sig):
        self.signals.append((pid, sig))
        if sig == signal.SIGTERM and self.obey_sigterm:
            self.exit(pid, 0)
        if sig == signal.SIGKILL:
            self.die_by_signal(pid, signal.SIGKILL)

    def killpg(self, pgid, sig):
        self.group_signals.append((pgid, sig))


def make_supervisor(kernel: FakeKernel, spawn, policy: str = "always", **extra) -> SandboxSupervisor:
    params = dict(waitpid=kernel.waitpid, kill=kernel.kill, killpg=kernel.killpg,
                  poll_s=0.01, kill_grace_s=0.2, is_init=False)
    params.update(extra)
    return SandboxSupervisor(policy, spawn, **params)


def sigterms(kernel: FakeKernel):
    return [entry for entry in kernel.signals if entry[1] == signal.SIGTERM]


def test_always_stops_server_after_one_served_request():
    kernel = FakeKernel()
    held = {}

    def spawn(write_fd):
        held["fd"] = os.dup(write_fd)
        os.write(held["fd"], b"1")      # the recycler fired
        return SERVER_PID

    code = make_supervisor(kernel, spawn).run()
    os.close(held["fd"])
    assert code == 0
    assert sigterms(kernel) == [(SERVER_PID, signal.SIGTERM)]
    assert kernel.group_signals == [(SERVER_PID, signal.SIGKILL)]      # leftovers of the server's group are cleared


def test_never_leaves_server_running():
    kernel = FakeKernel()
    held = {}

    def spawn(write_fd):
        held["fd"] = os.dup(write_fd)
        os.write(held["fd"], b"1")      # even a stray byte must not stop a `never` server
        return SERVER_PID

    supervisor = make_supervisor(kernel, spawn, policy="never")
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", supervisor.run()))
    thread.start()
    time.sleep(0.3)
    assert thread.is_alive()
    assert kernel.signals == []
    supervisor.terminate()
    thread.join(5)
    os.close(held["fd"])
    assert result["code"] == 0
    assert sigterms(kernel) == [(SERVER_PID, signal.SIGTERM)]


@pytest.mark.parametrize("policy", ["abnormal", "sometimes", "", "ALWAYS", "never "])
def test_unknown_policy_refuses_start(policy):
    spawned = []
    with pytest.raises(ValueError, match="recycle policy"):
        SandboxSupervisor(policy, lambda fd: spawned.append(fd) or 1)
    assert spawned == []


def test_server_crash_ends_supervisor_nonzero():
    kernel = FakeKernel()

    def spawn(write_fd):
        kernel.exit(SERVER_PID, 3)
        return SERVER_PID

    assert make_supervisor(kernel, spawn).run() == 3
    assert kernel.signals == []                      # nothing to stop
    assert kernel.group_signals == [(SERVER_PID, signal.SIGKILL)]      # leftovers of its group are cleaned


def test_server_killed_by_signal_ends_supervisor_nonzero():
    kernel = FakeKernel()

    def spawn(write_fd):
        kernel.die_by_signal(SERVER_PID, signal.SIGSEGV)
        return SERVER_PID

    assert make_supervisor(kernel, spawn).run() == 128 + signal.SIGSEGV


def test_unexpected_clean_exit_is_still_a_failure():
    kernel = FakeKernel()

    def spawn(write_fd):
        kernel.exit(SERVER_PID, 0)
        return SERVER_PID

    assert make_supervisor(kernel, spawn).run() != 0


def test_sigterm_forwarded_to_server():
    kernel = FakeKernel()
    supervisor = make_supervisor(kernel, lambda fd: SERVER_PID)
    threading.Timer(0.1, supervisor.terminate).start()
    assert supervisor.run() == 0
    assert sigterms(kernel) == [(SERVER_PID, signal.SIGTERM)]


def test_server_ignoring_sigterm_is_killed_after_the_grace():
    kernel = FakeKernel(obey_sigterm=False)
    supervisor = make_supervisor(kernel, lambda fd: SERVER_PID)
    threading.Timer(0.05, supervisor.terminate).start()
    code = supervisor.run()
    assert (SERVER_PID, signal.SIGKILL) in kernel.signals
    assert code != 0


def test_orphans_are_reaped_when_supervisor_is_pid1_and_server_status_is_kept():
    kernel = FakeKernel()

    def spawn(write_fd):
        kernel.exit(9001, 0)             # an orphaned grandchild reparented to us
        kernel.exit(9002, 1)
        kernel.exit(SERVER_PID, 5)
        return SERVER_PID

    code = make_supervisor(kernel, spawn, is_init=True).run()
    assert code == 5
    assert kernel.exited == []           # every zombie was collected
    assert -1 in kernel.waitpid_calls


def test_only_the_server_is_waited_for_when_not_pid1():
    kernel = FakeKernel()

    def spawn(write_fd):
        kernel.exit(9001, 0)
        kernel.exit(SERVER_PID, 0)
        return SERVER_PID

    make_supervisor(kernel, spawn, is_init=False).run()
    assert -1 not in kernel.waitpid_calls
    assert kernel.exited == [(9001, 0)]


def test_policy_constants_match_the_startup_plan():
    from src.composition import sandbox_setup
    from src.web import sandbox_supervisor

    assert set(sandbox_supervisor.POLICIES) == {sandbox_setup.RECYCLE_ALWAYS, sandbox_setup.RECYCLE_NEVER}


# ---- PipeRecycler -------------------------------------------------------------------------------------------

def test_pipe_recycler_writes_one_byte_once():
    read_fd, write_fd = os.pipe()
    recycler = PipeRecycler(write_fd)
    recycler.request_served()
    recycler.request_served()
    assert os.read(read_fd, 10) == b"1"
    os.set_blocking(read_fd, False)
    with pytest.raises(BlockingIOError):
        os.read(read_fd, 10)
    os.close(read_fd)
    os.close(write_fd)


def test_pipe_recycler_never_blocks_on_a_full_pipe():
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, True)
    filler = b"x" * 4096
    os.set_blocking(write_fd, False)
    try:
        while True:
            os.write(write_fd, filler)
    except BlockingIOError:
        pass
    os.set_blocking(write_fd, True)      # the recycler must make it non-blocking itself
    started = time.monotonic()
    PipeRecycler(write_fd).request_served()
    assert time.monotonic() - started < 1
    os.close(read_fd)
    os.close(write_fd)


def test_pipe_recycler_with_a_closed_pipe_does_not_raise():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    PipeRecycler(write_fd).request_served()      # EPIPE is logged, not raised
    os.close(write_fd)


def test_pipe_recycler_without_pipe_is_a_logged_noop():
    PipeRecycler(None).request_served()


# ---- imports -----------------------------------------------------------------------------------------------

def _static_imports(path: Path):
    names = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(("." * node.level) + (node.module or ""))
            names += [alias.name for alias in node.names]
    return names


def test_supervisor_imports_no_payload_code():
    forbidden = ("adapters", "sandbox_engine", "sandbox_app", "sandbox_setup", "docx", "parser", "quart",
                 "hypercorn", "infrastructure", "agents", "services", "handlers", "firestore")
    for module in ("sandbox_supervisor.py", "sandbox_recycler.py"):
        imports = _static_imports(REPO / "src" / "web" / module)
        bad = [name for name in imports if any(word in name for word in forbidden)]
        assert bad == [], (module, bad)


def test_supervisor_process_loads_no_payload_code_at_runtime():
    code = ("import sys; import src.web.sandbox_supervisor, src.web.sandbox_recycler;"
            "bad=[m for m in sys.modules if m.startswith(('src.adapters','src.agents','src.services','src.handlers',"
            "'src.infrastructure','quart','hypercorn','docx','google.cloud.firestore'))]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "[]", out.stdout + out.stderr


# ---- real two-process run ----------------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _alive(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _group_alive(pgid: int) -> bool:
    rows = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True).stdout.splitlines()
    return any(len(r.split()) == 2 and int(r.split()[0]) == pgid and not r.split()[1].startswith("Z") for r in rows)


def _h2c_post(port: int, body: bytes):
    """HTTP/2 with prior knowledge (cleartext, what Cloud Run speaks to a --use-http2 service); httpx cannot do it."""
    import h2.config
    import h2.connection
    import h2.events

    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    conn.send_headers(1, [(":method", "POST"), (":path", "/v1/x"), (":scheme", "http"),
                          (":authority", "localhost"), ("content-length", str(len(body)))])
    conn.send_data(1, body, end_stream=True)
    status, data = None, b""
    with socket.create_connection(("127.0.0.1", port), timeout=20) as sock:
        sock.sendall(conn.data_to_send())
        done = False
        while not done:
            chunk = sock.recv(65536)
            if not chunk:
                break
            for event in conn.receive_data(chunk):
                if isinstance(event, h2.events.ResponseReceived):
                    status = int(dict(event.headers)[b":status"])
                elif isinstance(event, h2.events.DataReceived):
                    data += event.data
                    conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.StreamEnded):
                    done = True
            sock.sendall(conn.data_to_send())
    return status, data, "HTTP/2"


def _wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


SERVER_SCRIPT = textwrap.dedent("""
    import asyncio, os, subprocess, sys
    sys.path.insert(0, {repo!r})
    import sandbox_main
    from src.web.sandbox_recycler import PipeRecycler

    recycler = PipeRecycler(int(os.environ["SANDBOX_RECYCLE_FD"]))
    grandchild = subprocess.Popen(["sleep", "60"])      # same process group as the server, like an engine leftover
    open({gfile!r}, "w").write(str(grandchild.pid))

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({{"type": "lifespan.startup.complete"}})
                else:
                    await send({{"type": "lifespan.shutdown.complete"}})
                    return
        await send({{"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"5")]}})
        await send({{"type": "http.response.body", "body": b"hello"}})
        recycler.request_served()

    asyncio.run(sandbox_main.run_server(app, os.environ))
""")


@pytest.mark.parametrize("protocol", ["http1", "h2c"])
def test_supervisor_ends_the_process_group_after_one_request(tmp_path, protocol):
    import httpx
    import sandbox_main

    port = _free_port()
    gfile = tmp_path / "grandchild.pid"
    script = tmp_path / "server.py"
    script.write_text(SERVER_SCRIPT.format(repo=str(REPO), gfile=str(gfile)))
    env = {k: v for k, v in os.environ.items() if k != "K_SERVICE"}
    env["PORT"] = str(port)
    recorded = {}
    inner = sandbox_main.make_spawn([sys.executable, str(script)], env)

    def spawn(write_fd):
        recorded["pid"] = inner(write_fd)
        return recorded["pid"]

    responses = []

    def client():
        def ready():
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return True
            except OSError:
                return False
        if not _wait_for(ready, 120):          # a saturated xdist host takes a while to import the server
            return
        if protocol == "h2c":
            responses.append(_h2c_post(port, b"payload"))
            return
        with httpx.Client() as http:
            reply = http.post(f"http://127.0.0.1:{port}/v1/x", content=b"payload", timeout=20)
            responses.append((reply.status_code, reply.content, reply.http_version))

    thread = threading.Thread(target=client)
    thread.start()
    supervisor = SandboxSupervisor("always", spawn, poll_s=0.05, kill_grace_s=15.0, is_init=False)
    outcome = []
    runner = threading.Thread(target=lambda: outcome.append(supervisor.run()))
    runner.start()
    runner.join(150)
    if runner.is_alive():                    # never hang the suite: stop the server and fail
        supervisor.terminate()
        runner.join(30)
        pytest.fail("the supervisor did not end the instance after one request")
    thread.join(20)
    code = outcome[0]

    assert code == 0
    assert responses and responses[0][:2] == (200, b"hello")      # flushed in full before the instance went away
    assert responses[0][2] == ("HTTP/2" if protocol == "h2c" else "HTTP/1.1")
    assert _wait_for(lambda: not _group_alive(recorded["pid"]), 10), "the server's process group survived"
    grandchild = int(gfile.read_text())
    assert _wait_for(lambda: not _alive(grandchild), 10), "the server's leftover process survived"

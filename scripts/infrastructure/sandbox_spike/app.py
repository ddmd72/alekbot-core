"""S0 spike app: one scratch Quart service that every measurement of the untrusted-content sandbox runs against
(docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §7 S0). Throwaway: deployed to a scratch Cloud Run service by the owner,
deleted afterwards. Nothing here is product code.

Routes (runbook item in brackets): POST /echo (a), POST /recycle (b), GET /probe + POST /probe (c), GET /auth-check (e),
GET /whoami (e, f), GET /node-as (f), GET /chromium (f), GET /egress (i), GET /call (e), GET /healthz (cold start).

No identifier lives in this file: project id and region come from the metadata server, everything else from
environment variables (SPIKE_WORKDIR, SPIKE_URL, SPIKE_ALLOWED, SPIKE_ROLE, SPIKE_API_PROBES, ...). Responses carry
status codes and classes only, never bodies from Google APIs and never an address. Logging goes to stdout through
`logging` (standalone spike, not a src/ module).
"""
import asyncio
import hashlib
import json
import logging
import os
import pwd
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from quart import Quart, Response, jsonify, request

from spike_common import mib, stream, stream_sha256

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger("spike")

app = Quart(__name__)
# Quart's defaults would fail runbook item a on their own: 16 MiB body cap and 60 s body / response timeouts.
MAX_CONTENT_LENGTH = mib(64)
BODY_TIMEOUT_S = 300
RESPONSE_TIMEOUT_S = 300
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["BODY_TIMEOUT"] = BODY_TIMEOUT_S
app.config["RESPONSE_TIMEOUT"] = RESPONSE_TIMEOUT_S

WORKDIR = os.getenv("SPIKE_WORKDIR", tempfile.gettempdir())
ENGINE_USER = os.getenv("SPIKE_ENGINE_USER", "spikeengine")
NODE_PATH = os.getenv("SPIKE_NODE_PATH", "/opt/docx/node_modules")
METADATA = "http://metadata.google.internal/computeMetadata/v1"
BOOT_ID = uuid.uuid4().hex
STATE: Dict[str, Any] = {"served": 0, "draining": False}


# -- common -------------------------------------------------------------------------------------------------------

@app.before_request
async def _count_and_gate() -> Optional[Response]:
    STATE["served"] += 1
    if STATE["draining"]:
        return Response("recycling\n", status=503, headers={"X-Sandbox-Outcome": "recycling"})
    return None


def _vm_hwm_bytes() -> Optional[int]:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _reset_rss_peak() -> bool:
    try:
        with open("/proc/self/clear_refs", "w") as fh:
            fh.write("5")
        return True
    except OSError:
        return False


def _cgroup_peak_bytes() -> Optional[int]:
    for path in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        try:
            with open(path) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            continue
    return None


def _engine_ids() -> Optional[Tuple[int, int]]:
    try:
        entry = pwd.getpwnam(ENGINE_USER)
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_gid


def _run_as(ids: Tuple[int, int], argv: List[str], env: Dict[str, str], timeout: float,
            cwd: Optional[str] = None) -> Dict[str, Any]:
    """Runs argv under (uid, gid) with no supplementary groups. Needs root; fails closed with a class otherwise."""
    started = time.monotonic()
    try:
        done = subprocess.run(argv, user=ids[0], group=ids[1], extra_groups=[], env=env, cwd=cwd,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"outcome": "timeout", "seconds": round(time.monotonic() - started, 2)}
    except (OSError, ValueError) as exc:
        return {"outcome": f"spawn_error:{type(exc).__name__}"}
    return {"outcome": "exited", "returncode": done.returncode,
            "stdout": done.stdout.decode(errors="replace")[:4000],
            "stderr": done.stderr.decode(errors="replace")[-300:],
            "seconds": round(time.monotonic() - started, 2)}


def _child_env(home: str) -> Dict[str, str]:
    return {"PATH": os.getenv("PATH", "/usr/bin:/bin"), "HOME": home, "LANG": "C.UTF-8", "NODE_PATH": NODE_PATH,
            "PLAYWRIGHT_BROWSERS_PATH": os.getenv("PLAYWRIGHT_BROWSERS_PATH", "")}


def _parse_stdout(row: Dict[str, Any]) -> None:
    """Replaces a child's JSON stdout with its parsed form under `measured` (in place)."""
    if "stdout" in row:
        try:
            row["measured"] = json.loads(row.pop("stdout"))
        except ValueError:
            row["stdout_unparsed"] = row.pop("stdout")[:200]


async def _metadata(path: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            reply = await client.get(f"{METADATA}/{path}", headers={"Metadata-Flavor": "Google"})
        return reply.text if reply.status_code == 200 else None
    except httpx.HTTPError:
        return None


async def _access_token() -> Optional[str]:
    text = await _metadata("instance/service-accounts/default/token")
    return json.loads(text).get("access_token") if text else None


# -- a: large bodies ----------------------------------------------------------------------------------------------

@app.post("/echo")
async def echo() -> Response:
    """Streams the request body to a file (never buffers it), then streams `down` deterministic bytes back."""
    down = int(request.args.get("down", "0"))
    seed = request.args.get("seed", "down")
    rss_reset = _reset_rss_peak()
    path = os.path.join(WORKDIR, f"echo-{uuid.uuid4().hex}")
    digest = hashlib.sha256()
    received = 0
    with open(path, "wb") as out:
        async for chunk in request.body:
            digest.update(chunk)
            out.write(chunk)
            received += len(chunk)
    os.unlink(path)
    result = {"bytes": received, "sha256": digest.hexdigest(), "down_bytes": down,
              "down_sha256": stream_sha256(seed, down), "rss_peak": _vm_hwm_bytes(), "rss_peak_reset": rss_reset,
              "cgroup_peak": _cgroup_peak_bytes(), "boot_id": BOOT_ID,
              "limits": {"max_content_length": MAX_CONTENT_LENGTH, "body_timeout_s": BODY_TIMEOUT_S,
                         "response_timeout_s": RESPONSE_TIMEOUT_S}}
    log.info(json.dumps({"route": "echo", "bytes": received, "rss_peak": result["rss_peak"],
                         "cgroup_peak": result["cgroup_peak"]}))

    async def body() -> AsyncIterator[bytes]:
        for piece in stream(seed, down):
            yield piece
            await asyncio.sleep(0)

    return Response(body(), status=200, headers={"X-Spike-Result": json.dumps(result)},
                    content_type="application/octet-stream")


# -- b: recycle ---------------------------------------------------------------------------------------------------

@app.post("/recycle")
async def recycle() -> Response:
    """Reads the body, streams 1 MiB back, then asks the supervisor to stop the instance (SIGUSR1 to its pid)."""
    # Draining starts here: a second request reaching this process meanwhile gets 503 recycling (before_request).
    served = STATE["served"]
    STATE["draining"] = True
    digest = hashlib.sha256()
    received = 0
    async for chunk in request.body:
        digest.update(chunk)
        received += len(chunk)
    down = int(request.args.get("down", str(mib(1))))
    supervisor = os.getenv("SPIKE_SUPERVISOR_PID")
    result = {"boot_id": BOOT_ID, "served_count": served, "bytes": received, "sha256": digest.hexdigest(),
              "down_bytes": down, "down_sha256": stream_sha256("recycle", down), "supervised": bool(supervisor)}

    async def body() -> AsyncIterator[bytes]:
        for piece in stream("recycle", down):
            yield piece
        # The last chunk has been handed to the server, not necessarily written to the wire: that is the gap item b
        # measures (a truncated reply without `recycling` means S1 T5 needs a flush barrier, Gap G7).
        log.info(json.dumps({"route": "recycle", "boot_id": BOOT_ID, "stopping": bool(supervisor)}))
        if supervisor:
            os.kill(int(supervisor), signal.SIGUSR1)

    return Response(body(), status=200, headers={"X-Spike-Result": json.dumps(result)},
                    content_type="application/octet-stream")


@app.get("/healthz")
async def healthz() -> Response:
    return jsonify({"boot_id": BOOT_ID, "served_count": STATE["served"]})


# -- c: zero-role identity ----------------------------------------------------------------------------------------

_ALWAYS_PROBES: List[Tuple[str, str, str, Optional[Any]]] = [  # (label, method, url template, body)
    ("firestore.listCollectionIds", "POST",
     "https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/" + os.getenv("SPIKE_FIRESTORE_DB", "(default)")
     + "/documents:listCollectionIds", {}),
    ("secretmanager.secrets.list", "GET", "https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets", None),
    ("storage.buckets.list", "GET", "https://storage.googleapis.com/storage/v1/b?project={PROJECT}", None),
    ("resourcemanager.getIamPolicy", "POST",
     "https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}:getIamPolicy", {}),
]


def _parse_probe_lines(text: str) -> List[Tuple[str, str, str, Optional[Any]]]:
    """`METHOD URL [JSON body]` per line; the label is the URL template without scheme or query (no project id)."""
    rows: List[Tuple[str, str, str, Optional[Any]]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2 or parts[0].upper() not in ("GET", "POST"):
            rows.append(("invalid-line", "GET", "", None))
            continue
        body: Optional[Any] = None
        if len(parts) == 3:
            try:
                body = json.loads(parts[2])
            except ValueError:
                body = {}
        rows.append((parts[1].split("//", 1)[-1].split("?", 1)[0], parts[0].upper(), parts[1], body))
    return rows


async def _probe_all(extra_text: str) -> Dict[str, Any]:
    project = (await _metadata("project/project-id")) or ""
    region = ((await _metadata("instance/region")) or "").rsplit("/", 1)[-1]
    token = await _access_token()
    rows = list(_ALWAYS_PROBES)
    path = os.getenv("SPIKE_API_PROBES", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                      "api_probes_starter.txt"))
    try:
        with open(path) as fh:
            rows += _parse_probe_lines(fh.read())
    except OSError:
        log.warning("probe list file unreadable")
    rows += _parse_probe_lines(extra_text)
    results: List[Dict[str, Any]] = []
    if token is None:
        return {"token_minted": False, "results": results}
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
        for label, method, template, body in rows:
            if not template:
                results.append({"label": label, "status": "invalid"})
                continue
            url = template.replace("{PROJECT}", project).replace("{REGION}", region)
            try:
                reply = await client.request(method, url, headers=headers,
                                             json=body if method == "POST" else None)
                status: Any = reply.status_code        # the status only; the body is never put in the result
            except httpx.HTTPError as exc:
                status = f"error:{type(exc).__name__}"
            results.append({"label": label, "method": method, "status": status})
    return {"token_minted": True, "results": results}


@app.route("/probe", methods=["GET", "POST"])
async def probe() -> Response:
    """The zero-role check: every call uses only the service identity's own token. POST: one probe per body line."""
    extra = (await request.get_data()).decode(errors="replace") if request.method == "POST" else ""
    report = await _probe_all(extra)
    for row in report["results"]:
        log.info(f"probe {row['label']} -> {row['status']}")
    log.info(f"probe token_minted={report['token_minted']}")
    return jsonify(report)


# -- e: in-app authentication -------------------------------------------------------------------------------------

def _split_list(value: str) -> List[str]:
    return [item for item in re.split(r"[\s,:;]+", value) if item]


@app.get("/auth-check")
async def auth_check() -> Response:
    """Verifies the bearer in-app; reports verdicts and classes only, never the address."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    header = request.headers.get("Authorization", "")
    report: Dict[str, Any] = {
        "authorization_present": bool(header), "scheme_bearer": header.lower().startswith("bearer "),
        "x_serverless_authorization_present": "X-Serverless-Authorization" in request.headers,
        "signature_ok": False, "email_verified": None, "aud_matches_spike_url": None, "email_class": None}
    if not report["scheme_bearer"]:
        return jsonify(report)
    token = header.split(" ", 1)[1].strip()
    started = time.monotonic()
    try:
        # audience=None: signature and expiry are checked here, the audience separately, so a wrong one is reported.
        claims = await asyncio.to_thread(id_token.verify_oauth2_token, token, google_requests.Request(), None)
    except Exception as exc:  # the exception class is the measurement
        report["verify_error_class"] = type(exc).__name__
        report["verify_seconds"] = round(time.monotonic() - started, 3)
        log.info(json.dumps({"route": "auth-check", "signature_ok": False, "error": type(exc).__name__}))
        return jsonify(report)
    report["verify_seconds"] = round(time.monotonic() - started, 3)   # includes the signing-key fetch (item i, G21)
    report["signature_ok"] = True
    report["email_verified"] = bool(claims.get("email_verified"))
    report["aud_matches_spike_url"] = claims.get("aud") == os.getenv("SPIKE_URL", "\0")
    allowed = _split_list(os.getenv("SPIKE_ALLOWED", ""))
    report["email_class"] = "allowed" if claims.get("email") in allowed else "refused"
    log.info(json.dumps({"route": "auth-check", **{k: report[k] for k in
                                                   ("signature_ok", "aud_matches_spike_url", "email_class")}}))
    return jsonify(report)


@app.get("/call")
async def call() -> Response:
    """role=caller: fetches an ID token for `target` from the metadata server and calls target/auth-check."""
    if os.getenv("SPIKE_ROLE") != "caller":
        return Response("not a caller\n", status=403)
    target = request.args.get("target", "").rstrip("/")
    if not target.startswith("https://"):
        return Response("target must be an https URL\n", status=400)
    minted = await _metadata(f"instance/service-accounts/default/identity?audience={target}&format=full")
    if minted is None:
        return jsonify({"id_token_minted": False})
    verdict: Dict[str, Any]
    try:
        async with httpx.AsyncClient(http2=True, timeout=30.0, trust_env=False) as client:
            reply = await client.get(f"{target}/auth-check", headers={"Authorization": f"Bearer {minted}"})
        verdict = {"status": reply.status_code, "http_version": reply.http_version}
        if reply.headers.get("content-type", "").startswith("application/json"):
            verdict["auth_check"] = reply.json()
    except httpx.HTTPError as exc:
        verdict = {"status": f"error:{type(exc).__name__}"}
    return jsonify({"id_token_minted": True, **verdict})


# -- f: child uid, rlimits, workdir ------------------------------------------------------------------------------

_RLIMIT_CHILD = r"""
import errno, json, os, resource, sys, time
nproc, nofile = int(sys.argv[1]), int(sys.argv[2])
resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
out = {"uid": os.getuid(), "euid": os.geteuid(), "gid": os.getgid(), "groups": os.getgroups(),
       "rlimit_nproc": resource.getrlimit(resource.RLIMIT_NPROC),
       "rlimit_nofile": resource.getrlimit(resource.RLIMIT_NOFILE)}
kids, err, cap = [], None, min(nproc * 4, 1024)
for _ in range(cap):                       # bounded: never more than cap forks, all killed below
    try:
        pid = os.fork()
    except OSError as exc:
        err = errno.errorcode.get(exc.errno, str(exc.errno)); break
    if pid == 0:
        time.sleep(5); os._exit(0)
    kids.append(pid)
out["forks_succeeded_before_error"] = len(kids)
out["fork_error"] = err
out["fork_cap_reached"] = err is None
for pid in kids:
    try:
        os.kill(pid, 9); os.waitpid(pid, 0)
    except OSError:
        pass
fds, ferr = [], None
for _ in range(nofile * 2):
    try:
        fds.append(os.open("/dev/null", os.O_RDONLY))
    except OSError as exc:
        ferr = errno.errorcode.get(exc.errno, str(exc.errno)); break
out["fds_opened_before_error"] = len(fds); out["fd_error"] = ferr
print(json.dumps(out))
"""

_WRITE_PROBE = r"""
import errno, json, os, sys
d, fill_mib = sys.argv[1], int(sys.argv[2])
out = {}
try:
    with open(os.path.join(d, "probe.txt"), "w") as fh:
        fh.write("x")
    out["write_small"] = "ok"
except OSError as exc:
    out["write_small"] = errno.errorcode.get(exc.errno, "error")
try:
    out["listdir_entries"] = len(os.listdir(d))
except OSError as exc:
    out["listdir_entries"] = errno.errorcode.get(exc.errno, "error")
if fill_mib:
    written = 0
    try:
        with open(os.path.join(d, "fill.bin"), "wb") as fh:
            block = b"\0" * (1 << 20)
            for _ in range(fill_mib):
                fh.write(block); fh.flush(); os.fsync(fh.fileno()); written += 1
        out["fill"] = {"written_mib": written, "error": None}
    except OSError as exc:
        out["fill"] = {"written_mib": written, "error": errno.errorcode.get(exc.errno, "error")}
print(json.dumps(out))
"""


def _engine_unavailable() -> Dict[str, Any]:
    return {"error": f"user {ENGINE_USER!r} missing or server is not root", "server_uid": os.getuid()}


def _can_list(path: str) -> bool:
    try:
        os.listdir(path)
        return True
    except OSError:
        return False


@app.get("/whoami")
async def whoami() -> Response:
    nproc = int(request.args.get("nproc", "64"))
    nofile = int(request.args.get("nofile", "256"))
    fill = request.args.get("fill") == "1"
    report: Dict[str, Any] = {
        "server": {"uid": os.getuid(), "euid": os.geteuid(), "gid": os.getgid(), "groups": os.getgroups()}}
    if os.path.exists("/proc/self/limits"):
        with open("/proc/self/limits") as fh:
            report["server_limits"] = fh.read().splitlines()
    ids = _engine_ids()
    if ids is None or os.geteuid() != 0:
        report["child"] = _engine_unavailable()
        return jsonify(report)
    report["engine_uid_differs"] = ids[0] != os.getuid()
    env = _child_env(WORKDIR)
    child = await asyncio.to_thread(
        _run_as, ids, [sys.executable, "-c", _RLIMIT_CHILD, str(nproc), str(nofile)], env, 30.0)
    _parse_stdout(child)
    report["child"] = child
    # workdir: a 0700 directory owned by the engine uid, probed as the engine uid and as an unrelated uid
    workdir = tempfile.mkdtemp(prefix="whoami-", dir=WORKDIR)
    try:
        os.chown(workdir, ids[0], ids[1])
        os.chmod(workdir, 0o700)
        info = os.stat(workdir)
        stats = os.statvfs(WORKDIR)
        volume_mib = stats.f_blocks * stats.f_frsize // mib(1)
        as_engine = await asyncio.to_thread(
            _run_as, ids, [sys.executable, "-c", _WRITE_PROBE, workdir, str(volume_mib + 32 if fill else 0)],
            env, 120.0)
        as_other = await asyncio.to_thread(
            _run_as, (65534, 65534), [sys.executable, "-c", _WRITE_PROBE, workdir, "0"], env, 30.0)
        _parse_stdout(as_engine)
        _parse_stdout(as_other)
        report["workdir"] = {
            "mode": oct(info.st_mode & 0o777), "owner_is_engine_uid": info.st_uid == ids[0],
            "volume_size_mib": volume_mib, "fill_tried": fill,
            "as_engine_uid": as_engine, "as_other_uid_65534": as_other,
            "server_root_can_list": _can_list(workdir)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    report["workdir"]["removed_on_exit"] = not os.path.exists(workdir)
    log.info(json.dumps({"route": "whoami", "engine_uid_differs": report["engine_uid_differs"]}))
    return jsonify(report)


_HELLO_DOCX = r"""
const d = require('docx'); const fs = require('fs');
const doc = new d.Document({sections: [{children: [new d.Paragraph({children: [new d.TextRun('hello')]})]}]});
d.Packer.toBuffer(doc).then(b => { fs.writeFileSync(process.argv[2], b); console.log('bytes', b.length); });
"""

_LIMITED_EXEC = r"""
import os, resource, sys
kind, limit_mib, old_mib = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
which = resource.RLIMIT_AS if kind == "as" else resource.RLIMIT_DATA
resource.setrlimit(which, (limit_mib << 20, limit_mib << 20))
flags = ["--max-old-space-size=%d" % old_mib] if kind == "data" else []
os.execvp("node", ["node"] + flags + sys.argv[4:])
"""


@app.get("/node-as")
async def node_as() -> Response:
    """Builds a DOCX with Node + docx (resolved through NODE_PATH) as the engine uid under RLIMIT_AS (mode=as) or
    RLIMIT_DATA plus --max-old-space-size (mode=data), once per limit in `mibs`; reports the smallest limit that works."""
    mode = request.args.get("mode", "as")
    if mode not in ("as", "data"):
        return Response("mode must be as or data\n", status=400)
    limits = [int(v) for v in request.args.get("mibs", "256,512,1024,2048").split(",") if v]
    old_mib = int(request.args.get("old_mib", "256"))
    ids = _engine_ids()
    if ids is None or os.geteuid() != 0:
        return jsonify(_engine_unavailable())
    workdir = tempfile.mkdtemp(prefix="node-", dir=WORKDIR)
    rows: List[Dict[str, Any]] = []
    try:
        os.chown(workdir, ids[0], ids[1])
        os.chmod(workdir, 0o700)
        script = os.path.join(workdir, "hello.js")
        with open(script, "w") as fh:
            fh.write(_HELLO_DOCX)
        os.chown(script, ids[0], ids[1])
        for limit in limits:
            out = os.path.join(workdir, f"out-{limit}.docx")
            row = await asyncio.to_thread(
                _run_as, ids, [sys.executable, "-c", _LIMITED_EXEC, mode, str(limit), str(old_mib), script, out],
                _child_env(workdir), 60.0, workdir)
            built = False
            if os.path.exists(out):
                with open(out, "rb") as fh:
                    built = fh.read(2) == b"PK"
            rows.append({"limit_mib": limit, "docx_built": built, "returncode": row.get("returncode"),
                         "outcome": row["outcome"], "stderr_tail": row.get("stderr", "")[-160:]})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    worked = [r["limit_mib"] for r in rows if r["docx_built"]]
    return jsonify({"mode": mode, "old_space_mib": old_mib if mode == "data" else None, "rows": rows,
                    "smallest_limit_mib_that_builds": min(worked) if worked else None})


_CHROMIUM_CHILD = r"""
import json, sys
from playwright.sync_api import sync_playwright
sandbox = sys.argv[1] == "sandbox"
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(chromium_sandbox=sandbox)
        page = browser.new_page(); page.set_content("<b>hi</b>"); text = page.inner_text("b"); browser.close()
    print(json.dumps({"started": True, "rendered": text == "hi"}))
except Exception as exc:
    print(json.dumps({"started": False, "error_class": type(exc).__name__, "error_head": str(exc)[:200]}))
"""


@app.get("/chromium")
async def chromium() -> Response:
    """Headless Chromium as the engine uid: with its own sandbox enabled, then with --no-sandbox."""
    ids = _engine_ids()
    if ids is None or os.geteuid() != 0:
        return jsonify(_engine_unavailable())
    home = tempfile.mkdtemp(prefix="chromium-", dir=WORKDIR)
    report: Dict[str, Any] = {}
    try:
        os.chown(home, ids[0], ids[1])
        os.chmod(home, 0o700)
        for name in ("sandbox", "no_sandbox"):
            row = await asyncio.to_thread(
                _run_as, ids, [sys.executable, "-c", _CHROMIUM_CHILD, name], _child_env(home), 90.0, home)
            try:
                report[name] = json.loads(row["stdout"].strip().splitlines()[-1])
            except (KeyError, ValueError, IndexError):
                report[name] = {"started": False, "error_class": row.get("outcome", "no_output"),
                                "stderr_tail": row.get("stderr", "")[-200:]}
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return jsonify(report)


# -- i: egress ----------------------------------------------------------------------------------------------------

async def _tls_reach(host: str) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=ssl.create_default_context()), timeout=6.0)
        writer.close()
        return {"reachable": True, "seconds": round(time.monotonic() - started, 2)}
    except (OSError, asyncio.TimeoutError, ssl.SSLError) as exc:
        return {"reachable": False, "error_class": type(exc).__name__, "seconds": round(time.monotonic() - started, 2)}


@app.get("/egress")
async def egress() -> Response:
    """Reachability only: a public host, the metadata token endpoint, and the host that serves Google's signing keys."""
    public = os.getenv("SPIKE_EGRESS_HOST", "example.com")
    started = time.monotonic()
    metadata: Dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=4.0, trust_env=False) as client:
            reply = await client.get(f"{METADATA}/instance/service-accounts/default/token",
                                     headers={"Metadata-Flavor": "Google"})
        metadata = {"reachable": True, "status": reply.status_code}
    except httpx.HTTPError as exc:
        metadata = {"reachable": False, "error_class": type(exc).__name__}
    metadata["seconds"] = round(time.monotonic() - started, 2)
    report = {"public_host_tls": await _tls_reach(public), "metadata_token_endpoint": metadata,
              "signing_key_host_tls": await _tls_reach("www.googleapis.com")}
    log.info(json.dumps({"route": "egress", **report}))
    return jsonify(report)


if __name__ == "__main__":      # local smoke only; the image starts supervisor.py
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8080")))

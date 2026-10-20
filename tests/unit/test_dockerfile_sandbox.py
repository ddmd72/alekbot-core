"""Dockerfile.sandbox: the COPY allowlist is the import closure and nothing else (RFC §4.3, §4.11; plan G12, G13, G15)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.sandbox_closure import ROOT, closure

_DOCKERFILE = ROOT / "Dockerfile.sandbox"
_NON_CODE_COPIES = {"requirements-sandbox.txt", "docx_generator/package.json"}


def _text() -> str:
    return _DOCKERFILE.read_text()


def _instructions():
    """Logical instructions (continuation lines joined, comments dropped)."""
    joined = re.sub(r"\\\n", " ", _text())
    return [ln.strip() for ln in joined.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _copy_sources():
    sources = []
    for ins in _instructions():
        if ins.upper().startswith("COPY "):
            parts = [p for p in ins.split()[1:] if not p.startswith("--")]
            sources.extend(parts[:-1])
    return sources


def test_copy_sources_are_an_allowlist_and_never_dot():
    sources = _copy_sources()
    assert sources, "no COPY at all"
    for src in sources:
        assert src not in {".", "./", "*", "/"}, src
        assert not src.endswith("/"), f"directory copy {src} is not an allowlist of files"
        assert "*" not in src, src
    assert not any(i.upper().startswith("ADD ") for i in _instructions())


def test_no_env_or_key_file_is_copied():
    for src in _copy_sources():
        name = Path(src).name.lower()
        assert not name.startswith(".env") and not name.endswith((".pem", ".key")), src
        assert "service-account" not in name and "admin-key" not in name and "credentials" not in name, src
        assert not (name.endswith(".json") and src not in _NON_CODE_COPIES), src


def test_copy_covers_the_import_closure_and_nothing_else():
    files, _ = closure()
    copied = set(_copy_sources()) - _NON_CODE_COPIES
    assert copied == files, {"missing": sorted(files - copied), "extra": sorted(copied - files)}


def test_every_copied_file_exists():
    for src in _copy_sources():
        assert (ROOT / src).is_file(), src


def test_render_target_exists_and_has_no_chromium_or_puppeteer():
    text = _text()
    assert re.search(r"^FROM .* AS render\s*$", text, re.M | re.I)
    lowered = " ".join(_instructions()).lower()
    for banned in ("chromium", "puppeteer", "playwright", "pdf_generator", "ffmpeg"):
        assert banned not in lowered, banned


def test_installs_from_requirements_sandbox_not_the_main_file():
    ins = " ".join(_instructions())
    assert "requirements-sandbox.txt" in ins and "requirements.txt " not in ins.replace("requirements-sandbox.txt", "")


def test_engine_user_is_the_one_the_service_expects():
    from src.composition.sandbox_setup import DEFAULT_ENGINE_USER

    useradd = next(i for i in _instructions() if "useradd" in i)
    assert useradd.rstrip().endswith(DEFAULT_ENGINE_USER)
    for flag in ("--system", "--no-create-home", "--shell /usr/sbin/nologin"):
        assert flag in useradd, flag


def test_exec_form_entrypoint_makes_sandbox_main_pid_1():
    cmds = [i for i in _instructions() if i.upper().startswith(("ENTRYPOINT ", "CMD "))]
    assert cmds, "no CMD/ENTRYPOINT"
    assert cmds[-1].split(None, 1)[1] == '["python", "sandbox_main.py"]'


def test_app_is_not_writable_by_the_engine_uid_and_bytecode_is_not_written():
    text = _text()
    assert re.search(r"PYTHONDONTWRITEBYTECODE=1", text)
    assert "find /app" in text and "-perm -o+w" in text      # the same world-writable check as the main Dockerfile
    assert re.search(r"chown -R root:root /app", text) and re.search(r"chmod -R go-w /app", text)
    assert "__pycache__" in text


def test_work_root_exists_and_tmp_is_closed_to_the_engine_user():
    """Final review: a 1777 /tmp let the engine write unbounded data to instance memory outside RLIMIT_AS (was
    `..._server_tmp_stays_usable`, pinning chmod 1777; branch-added by T1). The server runs as root."""
    ins = " ".join(_instructions())
    assert "mkdir -p /work" in ins and "chmod 711 /work" in ins
    assert "chown root:root /tmp" in ins and "chmod 0700 /tmp" in ins
    assert "1777" not in ins


def test_base_image_secret_shaped_variable_is_allowlisted_in_the_service():
    """python images set GPG_KEY; the self-check would refuse to start. One documented allowlist entry, never a looser
    pattern (the pattern lives in domain.sandbox_startup_check and is untouched)."""
    from src.domain.sandbox_startup_check import _SECRET_LIKE

    assert _SECRET_LIKE.pattern == "KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL"
    import sandbox_main

    assert sandbox_main.IMAGE_ENV_ALLOWLIST == frozenset({"GPG_KEY"})


# ---- docker-gated acceptance (G15): need the built image, skipped in CI ----------------------------------------------

_IMAGE = os.getenv("SANDBOX_TEST_IMAGE")
_needs_image = pytest.mark.skipif(not _IMAGE or shutil.which("docker") is None,
                                  reason="needs the built render image: set SANDBOX_TEST_IMAGE; run for S1 acceptance")


def _run(*args, entrypoint="python"):
    return subprocess.run(["docker", "run", "--rm", "--entrypoint", entrypoint, _IMAGE, *args],
                          capture_output=True, text=True, timeout=120)


@pytest.mark.docker
@_needs_image
def test_image_has_no_env_or_key_files():
    # stdout only: find may exit non-zero on entries that vanish while it walks.
    proc = _run("-c", "find /app /work / -maxdepth 3 \\( -name '.env*' -o -name 'service-account*' -o -name '*-admin-key.json' "
                "-o -name 'credentials.json' \\) -not -path '/proc/*' -not -path '*/node_modules/*' 2>/dev/null",
                entrypoint="sh")
    assert proc.stdout.strip() == "", proc.stdout


@pytest.mark.docker
@_needs_image
@pytest.mark.parametrize("module", ["google.cloud.firestore", "google.cloud.secretmanager", "google.cloud.storage",
                                    "anthropic", "openai", "google.genai", "slack_sdk", "google.cloud.bigquery", "logfire"])
def test_image_cannot_import_firestore_secretmanager_storage(module):
    proc = _run("-c", f"import {module}")
    assert proc.returncode != 0, f"{module} is importable in the image"


@pytest.mark.docker
@_needs_image
def test_image_runs_server_as_expected_uid():
    proc = _run("-c", "import os; print(os.getuid())")
    assert proc.stdout.strip() == "0", proc.stdout       # G12: the server stays root, only engine children drop


@pytest.mark.docker
@_needs_image
def test_app_tree_is_not_writable_by_the_engine_uid():
    proc = _run("-c", "import os,pwd,sys\n"
                "u=pwd.getpwnam('sandboxengine'); bad=[]\n"
                "for d,ns,fs in os.walk('/app'):\n"
                "    if 'node_modules' in d: continue\n"
                "    for n in ns+fs:\n"
                "        s=os.lstat(os.path.join(d,n))\n"
                "        if not os.path.islink(os.path.join(d,n)) and (s.st_mode & 0o002 or (s.st_uid==u.pw_uid)): bad.append(os.path.join(d,n))\n"
                "print(len(bad), bad[:5])\n")
    assert proc.stdout.startswith("0 "), proc.stdout


@pytest.mark.docker
@_needs_image
def test_engine_child_uid_differs():
    """A passwd check only. The real proof is test_child_launcher.py::test_uid_drop_in_container, run with
    SANDBOX_TEST_USER=sandboxengine."""
    proc = _run("-c", "import pwd; print(pwd.getpwnam('sandboxengine').pw_uid)")
    assert proc.returncode == 0 and int(proc.stdout.strip()) != 0


@pytest.mark.docker
@_needs_image
def test_startup_self_check_passes_on_the_image_environment():
    proc = _run("-c", "import os, sandbox_main\n"
                "from src.composition.sandbox_setup import validate_startup\n"
                "env = dict(os.environ); env['K_SERVICE']='x'; env['SANDBOX_CALLERS_RENDER']='a@example.com'; env['SANDBOX_URL']='https://x.example.com'\n"
                "validate_startup(env, on_cloud_run=True, allowed_env=sandbox_main.IMAGE_ENV_ALLOWLIST)\n"
                "print('ok')\n")
    assert proc.returncode == 0 and "ok" in proc.stdout, proc.stderr


def test_startup_check_accepts_the_image_variable_but_not_a_new_secret_shaped_one():
    import pytest as _pytest

    import sandbox_main
    from src.composition.sandbox_setup import SandboxStartupError, validate_startup

    env = {"GPG_KEY": "x", "K_SERVICE": "svc", "SANDBOX_CALLERS_RENDER": "a@example.com", "SANDBOX_URL": "https://x.example.com"}
    validate_startup(env, on_cloud_run=True, allowed_env=sandbox_main.IMAGE_ENV_ALLOWLIST)
    with _pytest.raises(SandboxStartupError):
        validate_startup({**env, "FOO_KEY": "x"}, on_cloud_run=True, allowed_env=sandbox_main.IMAGE_ENV_ALLOWLIST)
    with _pytest.raises(SandboxStartupError):
        validate_startup(env, on_cloud_run=True)       # without the allowlist entry the base image would not start

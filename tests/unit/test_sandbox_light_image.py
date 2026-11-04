"""Dockerfile.sandbox target `light` (plan P1): its COPY allowlist is the closure of the service plus the drive child's
and the parse child entry's, it reaches no agent, service or cloud client, its pins equal requirements.txt, and it holds
no node tool and no system package but ffmpeg (S5, the media.* ops)."""
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Set

import pytest

from src.adapters.sandbox_engine import child_launcher
from tests.unit import sandbox_closure
from tests.unit.sandbox_closure import ROOT, _imported_names, _module_file

_DOCKERFILE = ROOT / "Dockerfile.sandbox"
_LIGHT_ENTRIES = ["src/adapters/drive_parser/child.py", "src/adapters/sandbox_engine/parse_child.py"]
_FORBIDDEN_SRC = ("src/agents/", "src/handlers/", "src/services/", "src/infrastructure/")
_FORBIDDEN_EXTERNAL = ("google.cloud.firestore", "google.cloud.secretmanager", "google.cloud.storage", "anthropic",
                       "openai", "google.genai", "slack_sdk", "google.cloud.bigquery")


def _closure_from(entries: List[str]) -> Set[str]:
    """Like sandbox_closure.closure(), from other entry points (that helper is shared and fixed to sandbox_main)."""
    import ast

    files: Dict[str, bool] = {}
    stack = list(entries)
    while stack:
        path = stack.pop()
        if path in files:
            continue
        files[path] = True
        parent = Path(path).parent
        while str(parent) != ".":
            init = f"{parent}/__init__.py"
            if (ROOT / init).is_file():
                stack.append(init)
            parent = parent.parent
        for name in _imported_names(path, ast.parse((ROOT / path).read_text())):
            if name.split(".")[0] == "src":
                target = _module_file(name)
                if target:
                    stack.append(target)
    return set(files)


def _light_closure() -> Set[str]:
    service, _ = sandbox_closure.closure()
    return service | _closure_from(_LIGHT_ENTRIES)


def _light_instructions() -> List[str]:
    joined = re.sub(r"\\\n", " ", _DOCKERFILE.read_text())
    lines = [ln.strip() for ln in joined.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    start = next(i for i, ln in enumerate(lines) if re.match(r"FROM .* AS light$", ln, re.I))
    return lines[start:]


def text_split(instructions):
    return [" ".join(ins.split()) for ins in instructions]


def _light_copies() -> Set[str]:
    sources: Set[str] = set()
    for ins in _light_instructions():
        if ins.upper().startswith("COPY "):
            sources.update(p for p in ins.split()[1:-1] if not p.startswith("--"))
    return sources


def test_the_light_stage_is_the_last_one_and_the_render_stage_is_before_it():
    text = _DOCKERFILE.read_text()
    assert text.index(" AS render") < text.index(" AS light")


def test_light_copies_equal_the_light_closure():
    copied = _light_copies() - {"requirements-sandbox-light.txt"}
    closure = _light_closure()
    assert copied == closure, {"missing": sorted(closure - copied), "extra": sorted(copied - closure)}


def test_the_drive_child_and_the_parse_entry_are_in_the_closure():
    closure = _light_closure()
    assert set(_LIGHT_ENTRIES) <= closure and "sandbox_main.py" in closure


def test_light_closure_reaches_no_agent_service_handler_infrastructure_or_firestore_repo():
    bad = sorted(f for f in _light_closure() if f.startswith(_FORBIDDEN_SRC) or Path(f).name.startswith("firestore_"))
    assert not bad, bad


def test_light_closure_adapters_are_only_the_sandbox_engine_and_the_drive_parser():
    bad = sorted(f for f in _light_closure() if f.startswith("src/adapters/")
                 and not f.startswith(("src/adapters/sandbox_engine/", "src/adapters/drive_parser/")))
    assert not bad, bad


def test_light_closure_imports_no_cloud_client_or_model_sdk():
    import ast

    names: Set[str] = set()
    for f in _light_closure():
        names |= _imported_names(f, ast.parse((ROOT / f).read_text()))
    bad = sorted(n for n in names for banned in _FORBIDDEN_EXTERNAL if n == banned or n.startswith(banned + "."))
    assert not bad, bad


def test_light_pins_equal_requirements_txt():
    def parse(path):
        out = {}
        for line in (ROOT / path).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                name = re.split(r"[=<>~]", line, maxsplit=1)[0]
                out[name.strip().lower()] = line
        return out

    main, light = parse("requirements.txt"), parse("requirements-sandbox-light.txt")
    drift = {n: (line, main[n]) for n, line in light.items() if n in main and line != main[n]}
    assert not drift, drift
    for library in ("pypdf", "pdfminer.six", "markitdown[all]", "openpyxl", "xlrd", "olefile", "beautifulsoup4",
                    "defusedxml", "charset-normalizer", "markdownify", "striprtf"):
        assert library in light and "==" in light[library], library
    for server_line in parse("requirements-sandbox.txt").values():
        assert server_line in light.values(), server_line


def test_light_stage_has_no_node_npm_or_browser_installs_only_ffmpeg_and_sets_its_family():
    """S5 (owner decision, design Δ11): ffmpeg enters the light image for the media.* ops. It is the stage's only
    system package: one apt-get RUN, `ffmpeg` alone with --no-install-recommends, the apt list dropped in that layer."""
    text = " ".join(_light_instructions()).lower()
    for banned in ("nodejs", "npm", "chromium", "playwright"):
        assert banned not in text, banned
    apt_runs = [" ".join(ins.split()) for ins in _light_instructions() if "apt-get" in ins]
    assert len(apt_runs) == 1, apt_runs
    installs = re.findall(r"apt-get install ((?:\S+ )*?)(?:&&|$)", apt_runs[0] + " ")
    assert len(installs) == 1, apt_runs[0]
    packages = [w for w in installs[0].split() if not w.startswith("-")]
    assert packages == ["ffmpeg"], packages
    assert "--no-install-recommends" in installs[0].split()
    assert "rm -rf /var/lib/apt/lists/*" in apt_runs[0]
    assert "ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SANDBOX_SERVICE=light" in " ".join(text_split(_light_instructions()))
    assert "requirements-sandbox-light.txt" in text


def test_light_stage_keeps_the_render_stages_hardening():
    text = " ".join(_light_instructions())
    assert "chown root:root /tmp" in text and "chmod 0700 /tmp" in text and "chmod 711 /work" in text
    assert "chown -R root:root /app" in text and "-perm -o+w" in text
    assert text.rstrip().endswith('CMD ["python", "sandbox_main.py"]')
    assert "useradd" in text and text.count("sandboxengine") == 1


def test_env_allowlist_grew_by_exactly_the_four_parse_names():
    assert tuple(child_launcher.ENV_ALLOWLIST) == (
        "PATH", "HOME", "NODE_PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "MALLOC_ARENA_MAX")


# ---- docker-gated acceptance: needs the built light image, skipped without docker --------------------------------------

_LIGHT_IMAGE = os.getenv("SANDBOX_TEST_LIGHT_IMAGE")


@pytest.mark.docker
@pytest.mark.skipif(not _LIGHT_IMAGE or shutil.which("docker") is None,
                    reason="needs the built light image: set SANDBOX_TEST_LIGHT_IMAGE")
def test_light_image_ffmpeg_supports_flags():
    """The light image's own ffmpeg (Debian's) knows the allowlist flags the media argv relies on and can encode Opus;
    its release is recorded in the failure message, not assumed."""
    def run(*args):
        return subprocess.run(["docker", "run", "--rm", "--entrypoint", *args], capture_output=True, text=True,
                              timeout=120).stdout

    release = run("cat", _LIGHT_IMAGE, "/etc/os-release")
    full = run("ffmpeg", _LIGHT_IMAGE, "-hide_banner", "-h", "full")
    for option in ("format_whitelist", "protocol_whitelist"):
        assert option in full, f"{option} missing from the light image's ffmpeg ({release.splitlines()[:2]})"
    assert "libopus" in run("ffmpeg", _LIGHT_IMAGE, "-hide_banner", "-encoders")

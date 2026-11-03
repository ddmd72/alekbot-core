"""The sandbox light (parse) service's Cloud Build steps (UNTRUSTED_CONTENT_SANDBOX_RFC §4.12, §11 light row).

Same shape as test_cloudbuild_sandbox.py (render family): a script file run by four steps, a no-op while
`_SANDBOX_LIGHT_SA` is empty, an `alek-sandbox-` identity fence, no secret, no public binding."""
import os
import re
import stat
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_BUILD = _ROOT / "cloudbuild-dev.yaml"
_SCRIPT = _ROOT / "scripts" / "infrastructure" / "cloudbuild_sandbox_light.sh"
_SCRIPT_REL = "scripts/infrastructure/cloudbuild_sandbox_light.sh"
_MAKEFILE = _ROOT / "Makefile"
_SA = "alek-sandbox-light@placeholder.example"
_NAMES = ("SANDBOX_LIGHT_SA", "SANDBOX_LIGHT_URL", "SANDBOX_LIGHT_CALLERS_MAIN", "SANDBOX_LIGHT_CALLERS_INDEXER")
_ALLOWED_ENV_NAMES = {"APP_ENV", "SANDBOX_SERVICE", "SANDBOX_RECYCLE", "SANDBOX_URL", "SANDBOX_CALLERS_MAIN",
                      "SANDBOX_CALLERS_INDEXER"}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _steps_all():
    return yaml.safe_load(_BUILD.read_text())["steps"]


def _light_steps():
    steps = [s for s in _steps_all() if _SCRIPT_REL in s.get("args", [])]
    assert len(steps) == 4, [s.get("args") for s in steps]
    return steps


def _script_code():
    return "\n".join(ln for ln in _SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#"))


def _deploy_flags():
    code = re.sub(r"\\\n", " ", _script_code())
    return next(ln for ln in code.splitlines() if "run deploy" in ln)


def _text():
    parts = []
    for s in _light_steps():
        parts += s["args"] + s.get("env", [])
    return " ".join(parts) + "\n" + _SCRIPT.read_text()


def _ancestors(steps, step_id):
    by_id = {s["id"]: s for s in steps if "id" in s}
    seen, todo = set(), list(by_id[step_id].get("waitFor", []))
    while todo:
        cur = todo.pop()
        if cur in seen or cur not in by_id:
            continue
        seen.add(cur)
        todo += by_id[cur].get("waitFor", [])
    return seen


# ---- step graph ----------------------------------------------------------------------------------------------------

def test_four_light_steps_in_order():
    steps = _light_steps()
    assert [s["id"] for s in steps] == ["sandbox-light-pull", "sandbox-light-build", "sandbox-light-push",
                                        "sandbox-light-deploy"]
    assert [s["args"][1] for s in steps] == ["pull", "build", "push", "deploy"]
    for prev, cur in zip(steps, steps[1:]):
        assert cur["waitFor"] == [prev["id"]]
    for s in steps:
        assert s["entrypoint"] == "bash"
        assert s["args"][0] == _SCRIPT_REL


def test_light_steps_are_not_an_ancestor_of_deploy_main_and_come_after_the_indexer():
    steps = _steps_all()
    ids = [s.get("id") for s in steps]
    light = {s["id"] for s in _light_steps()}
    main_step = next(s for s in steps if s.get("args", [])[:3] == ["run", "deploy", "alek-bot-dev"])
    assert not (_ancestors(steps, main_step["id"]) & light)
    assert "indexer-public-iam" in _ancestors(steps, "sandbox-light-deploy")
    indexer = [i for i, s in enumerate(steps) if "alek-bot-indexer-dev" in " ".join(s.get("args", []))]
    assert indexer and max(indexer) < ids.index("sandbox-light-pull")


def test_the_render_steps_stay_the_last_four():
    steps = _steps_all()
    last = [s.get("id") for s in steps[-4:]]
    assert last == ["sandbox-pull", "sandbox-build", "sandbox-push", "sandbox-deploy"]


def test_step_env_passes_every_light_substitution():
    for s in _light_steps():
        env = s["env"]
        assert "PROJECT_ID=$PROJECT_ID" in env
        for name in _NAMES:
            assert f"{name}=$_{name}" in env, (s["id"], name)


def test_args_stay_under_the_arg_limit():
    assert all(len(a) < 10_000 for s in _steps_all() for a in s.get("args", []))


def test_substitution_defaults_are_empty():
    subs = yaml.safe_load(_BUILD.read_text())["substitutions"]
    for name in _NAMES:
        assert subs[f"_{name}"] == "", name


def test_makefile_defines_and_passes_each_light_substitution():
    make = _MAKEFILE.read_text()
    for name in _NAMES:
        assert re.search(rf"^_{name}\s*=\s*\$\({name}_DEV\)", make, re.M), name
    deploy = make.split("\ndeploy:", 1)[1].split("\ndeploy-indexes:", 1)[0]
    for name in _NAMES:
        assert f",_{name}=$(_{name})" in deploy, name
    assert "docs/07_deployment/README.md" in make.split("_SANDBOX_LIGHT_SA =", 1)[0][-900:]


# ---- the deploy command --------------------------------------------------------------------------------------------

def test_deploy_flags_match_the_light_row():
    flags = _deploy_flags()
    for flag in ("--concurrency=1", "--use-http2", "--execution-environment=gen2", "--min-instances=0",
                 "--max-instances=5", "--memory=2Gi", "--cpu=2", "--timeout=900", "--no-allow-unauthenticated",
                 "--clear-secrets", "--no-cpu-throttling"):
        assert re.search(re.escape(flag) + r"(\s|$)", flags), flag
    assert "--add-volume=name=sandbox-work,type=in-memory,size-limit=" in flags
    assert "--add-volume-mount=volume=sandbox-work,mount-path=/work" in flags
    assert '--service-account="$SANDBOX_LIGHT_SA"' in flags
    assert 'WORK_VOLUME_SIZE="${SANDBOX_WORK_VOLUME_SIZE:-320Mi}"' in _script_code()
    assert "--target light" in _script_code()


def test_env_has_exactly_the_light_names():
    env = re.search(r"--set-env-vars=\"?([^\s\"]+)", _deploy_flags()).group(1)
    pairs = dict(p.split("=", 1) for p in env.split(","))
    assert set(pairs) == _ALLOWED_ENV_NAMES, set(pairs)
    assert pairs["SANDBOX_SERVICE"] == "light" and pairs["SANDBOX_RECYCLE"] == "abnormal"


def test_no_secret_no_public_binding_no_invoker_bypass_no_literals():
    text, code = _text(), _script_code()
    assert "--set-secrets" not in text and "--update-secrets" not in text
    assert "--allow-unauthenticated" not in text.replace("--no-allow-unauthenticated", "")
    assert "invoker-iam-check" not in text
    assert "add-iam-policy-binding" not in code and "allUsers" not in code
    assert not _EMAIL.search(text.replace("${SANDBOX_LIGHT_SA%%@*}", "").replace("$PROJECT_ID", "p")), "literal email"
    assert "run.app" not in text and "https://" not in text and "http://" not in text
    assert not re.search(r"\b[a-z][a-z0-9-]{4,28}-\d{3,6}\b", text), "looks like a project id"


# ---- behaviour with stub gcloud/docker -----------------------------------------------------------------------------

def _run(mode, tmp_path, env_extra):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    for tool in ("docker", "gcloud"):
        stub = bindir / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $@" >> "{log}"\n')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "PROJECT_ID": "proj"}
    env.update(env_extra)
    proc = subprocess.run(["bash", str(_SCRIPT), mode], env=env, capture_output=True, text=True, cwd=_ROOT)
    return proc, (log.read_text() if log.exists() else "")


def test_every_mode_is_a_noop_while_the_service_account_is_empty_or_unset(tmp_path):
    for mode in ("pull", "build", "push", "deploy"):
        for env in ({"SANDBOX_LIGHT_SA": ""}, {}):
            proc, calls = _run(mode, tmp_path, env)
            assert proc.returncode == 0, proc.stderr
            assert calls == "", (mode, calls)
            assert "skip" in proc.stdout.lower()


def test_a_service_account_outside_the_prefix_is_refused_without_any_call(tmp_path):
    for bad in ("alek-runtime@placeholder.example", "sandbox-light@placeholder.example",
                "alek-sandbox-@placeholder.example", "x@alek-sandbox-y.example", "alek-sandbox"):
        for mode in ("pull", "build", "push", "deploy"):
            proc, calls = _run(mode, tmp_path, {"SANDBOX_LIGHT_SA": bad})
            assert proc.returncode == 3 and calls == "", (bad, mode, calls)
            assert "alek-sandbox-" in proc.stderr


def test_with_the_service_account_set_build_and_deploy_carry_the_light_values(tmp_path):
    env = {"SANDBOX_LIGHT_SA": _SA, "SANDBOX_LIGHT_URL": "u-placeholder",
           "SANDBOX_LIGHT_CALLERS_MAIN": "m-placeholder", "SANDBOX_LIGHT_CALLERS_INDEXER": "i-placeholder"}
    proc, calls = _run("build", tmp_path, env)
    assert proc.returncode == 0 and "-f Dockerfile.sandbox" in calls and "--target light" in calls
    assert "--cache-from gcr.io/proj/alek-bot-sandbox-light-dev:latest" in calls
    proc, calls = _run("deploy", tmp_path, env)
    assert proc.returncode == 0
    assert "gcloud run deploy alek-bot-sandbox-light-dev" in calls
    assert f"--service-account={_SA}" in calls
    for token in ("SANDBOX_SERVICE=light", "SANDBOX_RECYCLE=abnormal", "SANDBOX_URL=u-placeholder",
                  "SANDBOX_CALLERS_MAIN=m-placeholder", "SANDBOX_CALLERS_INDEXER=i-placeholder",
                  "--max-instances=5", "--timeout=900", "--concurrency=1", "size-limit=320Mi"):
        assert token in calls, token


def test_unknown_mode_fails(tmp_path):
    proc, _ = _run("explode", tmp_path, {"SANDBOX_LIGHT_SA": _SA})
    assert proc.returncode == 2

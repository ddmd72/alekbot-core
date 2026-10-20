"""The sandbox render service's Cloud Build steps (UNTRUSTED_CONTENT_SANDBOX_RFC §4.3, §4.12; plan G17).

The steps run `scripts/infrastructure/cloudbuild_sandbox_render.sh` (a file: Cloud Build caps a step arg at 10 000
characters, and substitutions are passed through `env:`). Every step is a no-op while `_SANDBOX_RENDER_SA` is empty, so a
checkout whose sandbox identity does not exist yet can still deploy the bot."""
import os
import re
import stat
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_BUILD = _ROOT / "cloudbuild-dev.yaml"
_SCRIPT = _ROOT / "scripts" / "infrastructure" / "cloudbuild_sandbox_render.sh"
_MAKEFILE = _ROOT / "Makefile"
_SERVICE = "alek-bot-sandbox-render-dev"
_SA = "alek-sandbox-render@placeholder.example"
_ALLOWED_ENV_NAMES = {"APP_ENV", "SANDBOX_RECYCLE", "SANDBOX_URL", "SANDBOX_CALLERS_RENDER"}


def _build():
    return yaml.safe_load(_BUILD.read_text())


def _sandbox_steps():
    steps = [s for s in _build()["steps"] if any("cloudbuild_sandbox_render.sh" in a for a in s.get("args", []))]
    assert len(steps) == 4, [s.get("args") for s in steps]
    return steps


def _sandbox_text():
    """Everything the sandbox steps run: their args, env and the script."""
    parts = []
    for s in _sandbox_steps():
        parts += s["args"] + s.get("env", [])
    return " ".join(parts) + "\n" + _SCRIPT.read_text()


def _script_code():
    return "\n".join(ln for ln in _SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#"))


def _main_step():
    hits = [s for s in _build()["steps"] if s.get("args", [])[:3] == ["run", "deploy", "alek-bot-dev"]]
    assert len(hits) == 1
    return hits[0]


def _deploy_flags():
    code = re.sub(r"\\\n", " ", _script_code())
    block = next(ln for ln in code.splitlines() if "run deploy" in ln)
    return block


# ---- the deploy command ---------------------------------------------------------------------------------------------

def test_sandbox_step_has_no_set_secrets():
    assert "--set-secrets" not in _sandbox_text() and "--update-secrets" not in _sandbox_text()


def test_sandbox_step_is_not_unauthenticated():
    text = _sandbox_text()
    assert "--allow-unauthenticated" not in text.replace("--no-allow-unauthenticated", "")
    assert "--no-allow-unauthenticated" in _deploy_flags()


def test_sandbox_step_never_disables_invoker_check():
    assert "no-invoker-iam-check" not in _sandbox_text()
    assert "invoker-iam-check" not in _sandbox_text()


def test_no_public_invoker_binding_for_the_sandbox_service():
    for step in _build()["steps"]:
        joined = " ".join(step.get("args", []))
        if _SERVICE in joined and "add-iam-policy-binding" in joined:
            raise AssertionError(joined)
    code = _script_code()
    assert "add-iam-policy-binding" not in code and "allUsers" not in code


def test_sandbox_service_account_is_a_substitution():
    env = [e for s in _sandbox_steps() for e in s.get("env", [])]
    assert "SANDBOX_RENDER_SA=$_SANDBOX_RENDER_SA" in env
    assert '--service-account="$SANDBOX_RENDER_SA"' in _deploy_flags()


def test_concurrency_1_and_http2_and_gen2():
    flags = _deploy_flags()
    for flag in ("--concurrency=1", "--use-http2", "--execution-environment=gen2", "--min-instances=0",
                 "--max-instances=3", "--memory=2Gi", "--cpu=2", "--timeout=300"):
        assert re.search(re.escape(flag) + r"(\s|$)", flags), flag
    assert "--add-volume=name=sandbox-work,type=in-memory,size-limit=" in flags
    assert "--add-volume-mount=volume=sandbox-work,mount-path=/work" in flags


def test_recycle_always_in_env():
    assert "SANDBOX_RECYCLE=always" in _deploy_flags()


def test_sandbox_env_has_only_allowlisted_names():
    env = re.search(r"--set-env-vars=\"?([^\s\"]+)", _deploy_flags()).group(1)
    names = {pair.split("=", 1)[0] for pair in env.split(",")}
    assert names == _ALLOWED_ENV_NAMES, names


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _has_literal_email(text: str) -> bool:
    """`$PROJECT_ID` is a placeholder word, not a pass: `sa@$PROJECT_ID.iam...` is still a literal service-account address."""
    return bool(_EMAIL.search(text.replace("${PROJECT_ID}", "projectword").replace("$PROJECT_ID", "projectword")))


import pytest  # noqa: E402


@pytest.mark.parametrize("literal", [
    "sa@$PROJECT_ID.iam.gserviceaccount.com", "sa@${PROJECT_ID}.iam.gserviceaccount.com",
    "someone@example.com", "--service-account=alek-x@proj.iam.gserviceaccount.com",
])
def test_the_email_matcher_catches_literals(literal):
    assert _has_literal_email(literal)


def test_the_email_matcher_allows_the_prefix_guard():
    assert not _has_literal_email('case "${SANDBOX_RENDER_SA%%@*}" in')


def test_no_literal_email_or_url_in_the_sandbox_step():
    text = _sandbox_text()
    assert not _has_literal_email(text), "literal email or SA address"
    assert "run.app" not in text and "https://" not in text and "http://" not in text
    assert not re.search(r"\b[a-z][a-z0-9-]{4,28}-\d{3,6}\b", text), "looks like a project id"


def test_sandbox_steps_come_after_every_other_step_and_stay_under_the_arg_limit():
    steps = _build()["steps"]
    sandbox = [i for i, s in enumerate(steps) if any("cloudbuild_sandbox_render.sh" in a for a in s.get("args", []))]
    assert sandbox == list(range(len(steps) - 4, len(steps)))      # the last four steps, in order pull/build/push/deploy
    assert [steps[i]["args"][1] for i in sandbox] == ["pull", "build", "push", "deploy"]
    indexer = [i for i, s in enumerate(steps) if "alek-bot-indexer-dev" in " ".join(s.get("args", []))]
    assert indexer and max(indexer) < min(sandbox)
    assert all(len(a) < 10_000 for s in steps for a in s.get("args", []))


def test_exactly_one_service_account_flag_across_deploy_scripts_and_it_is_the_sandbox_deploy():
    """The identity guard of test_cloudbuild_iam_step3 only sees step args; scripts run by a step are invisible to it."""
    scripts = {a for s in _build()["steps"] for a in s.get("args", []) if a.startswith("scripts/infrastructure/") and a.endswith(".sh")}
    assert "scripts/infrastructure/cloudbuild_sandbox_render.sh" in scripts
    found = []
    for rel in sorted(scripts):
        code = "\n".join(ln for ln in re.sub(r"\\\n", " ", (_ROOT / rel).read_text()).splitlines()
                         if not ln.lstrip().startswith("#"))
        found += [(rel, m) for m in re.findall(r"(?<![\w-])--service-account=(\S+)", code)]
    assert found == [("scripts/infrastructure/cloudbuild_sandbox_render.sh", '"$SANDBOX_RENDER_SA"')], found
    block = next(ln for ln in re.sub(r"\\\n", " ", _SCRIPT.read_text()).splitlines() if "run deploy" in ln)
    assert f'run deploy "$SERVICE"' in block and '--service-account="$SANDBOX_RENDER_SA"' in block


# ---- the main service passes the switch -----------------------------------------------------------------------------

def test_main_step_passes_the_switch_as_substitution():
    env = next(a for a in _main_step()["args"] if a.startswith("--set-env-vars="))
    assert "SANDBOX_RENDER=$_SANDBOX_RENDER" in env.split(",")
    assert "SANDBOX_RENDER_URL=$_SANDBOX_RENDER_URL" in env.split(",")


def test_substitution_defaults_keep_the_switch_inprocess_and_the_rest_empty():
    subs = _build()["substitutions"]
    assert subs["_SANDBOX_RENDER"] == "inprocess"
    for name in ("_SANDBOX_RENDER_URL", "_SANDBOX_RENDER_SA", "_SANDBOX_RENDER_CALLERS"):
        assert subs[name] == ""


def test_makefile_passes_every_sandbox_substitution():
    make = _MAKEFILE.read_text()
    assert re.search(r"^_SANDBOX_RENDER\s*=\s*\$\(or \$\(SANDBOX_RENDER_DEV\),inprocess\)", make, re.M)
    assert re.search(r"^_SANDBOX_RENDER_URL\s*=\s*\$\(SANDBOX_RENDER_URL_DEV\)", make, re.M)
    assert re.search(r"^_SANDBOX_RENDER_SA\s*=\s*\$\(SANDBOX_RENDER_SA_DEV\)", make, re.M)
    assert re.search(r"^_SANDBOX_RENDER_CALLERS\s*=\s*\$\(SANDBOX_RENDER_CALLERS_DEV\)", make, re.M)
    deploy = make.split("\ndeploy:", 1)[1].split("\ndeploy-indexes:", 1)[0]
    for name in ("_SANDBOX_RENDER", "_SANDBOX_RENDER_URL", "_SANDBOX_RENDER_SA", "_SANDBOX_RENDER_CALLERS"):
        assert f",{name}=$({name})" in deploy, name
    assert "docs/07_deployment/README.md" in make.split("_SANDBOX_RENDER =", 1)[0][-900:]


# ---- behaviour of the script, with stub gcloud/docker ---------------------------------------------------------------

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


def test_every_step_is_skipped_while_the_service_account_is_empty(tmp_path):
    for mode in ("pull", "build", "push", "deploy"):
        proc, calls = _run(mode, tmp_path, {"SANDBOX_RENDER_SA": ""})
        assert proc.returncode == 0, proc.stderr
        assert calls == "", (mode, calls)
        assert "skip" in proc.stdout.lower()


def test_unset_service_account_is_also_skipped(tmp_path):
    proc, calls = _run("deploy", tmp_path, {})
    assert proc.returncode == 0 and calls == ""


def test_steps_run_when_the_service_account_is_set(tmp_path):
    env = {"SANDBOX_RENDER_SA": _SA, "SANDBOX_RENDER_URL": "u-placeholder",
           "SANDBOX_RENDER_CALLERS": "c-placeholder"}
    build, calls = _run("build", tmp_path, env)
    assert build.returncode == 0 and "-f Dockerfile.sandbox" in calls and "--target render" in calls
    assert "--cache-from gcr.io/proj/alek-bot-sandbox-render-dev:latest" in calls
    _, calls = _run("deploy", tmp_path, env)
    assert f"--service-account={_SA}" in calls and "SANDBOX_URL=u-placeholder" in calls
    assert "SANDBOX_CALLERS_RENDER=c-placeholder" in calls


def test_unknown_mode_fails(tmp_path):
    proc, _ = _run("explode", tmp_path, {"SANDBOX_RENDER_SA": _SA})
    assert proc.returncode != 0


def test_a_service_account_outside_the_alek_sandbox_prefix_is_refused_without_any_call(tmp_path):
    for bad in ("alek-runtime@placeholder.example", "sandbox-render@placeholder.example", "alek-sandbox-@placeholder.example",
                "x@alek-sandbox-y.example", "alek-sandbox"):
        for mode in ("pull", "build", "push", "deploy"):
            proc, calls = _run(mode, tmp_path, {"SANDBOX_RENDER_SA": bad})
            assert proc.returncode != 0 and calls == "", (bad, mode, calls)
            assert "alek-sandbox-" in proc.stderr


def test_a_service_account_with_the_prefix_runs(tmp_path):
    proc, calls = _run("deploy", tmp_path, {"SANDBOX_RENDER_SA": "alek-sandbox-x@placeholder.example"})
    assert proc.returncode == 0 and "gcloud run deploy" in calls

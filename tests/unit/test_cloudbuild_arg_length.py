"""Cloud Build rejects a step arg over 10 000 characters ("build step N arg M too long") at submit time,
after the image build, so a deploy fails late. The scheduler-jobs script lives in a file for that reason."""
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_BUILD = _ROOT / "cloudbuild-dev.yaml"
_SCRIPT = _ROOT / "scripts" / "infrastructure" / "cloudbuild_scheduler_jobs.sh"
_MAX_ARG = 10_000

# Worst-case realistic substitution lengths.
_PROJECT_ID = "p" * 30
_USER_SUB = "u" * 80


def _steps():
    return yaml.safe_load(_BUILD.read_text())["steps"]


def _render(arg: str) -> str:
    arg = arg.replace("$$", "\x00")  # `$$` is a literal `$`, never a substitution
    arg = re.sub(r"\$\{?_[A-Z0-9_]+\}?", _USER_SUB, arg)
    arg = re.sub(r"\$\{?(PROJECT_ID|PROJECT_NUMBER|BUILD_ID|REPO_NAME|REVISION_ID|SHORT_SHA)\}?", _PROJECT_ID, arg)
    return arg.replace("\x00", "$")


def test_every_step_arg_is_within_cloud_builds_limit_after_substitution():
    too_long = [
        (n, m, len(_render(arg)))
        for n, step in enumerate(_steps(), start=1)
        for m, arg in enumerate(step.get("args", []), start=1)
        if len(_render(arg)) > _MAX_ARG
    ]
    assert not too_long, f"(step, arg, rendered length) over {_MAX_ARG}: {too_long}"


def _scheduler_step():
    hits = [s for s in _steps() if any("cloudbuild_scheduler_jobs.sh" in a for a in s.get("args", []))]
    assert len(hits) == 1
    return hits[0]


def test_scheduler_step_runs_the_script_file_and_the_file_exists():
    step = _scheduler_step()
    assert step["entrypoint"] == "bash"
    assert step["args"] == ["scripts/infrastructure/cloudbuild_scheduler_jobs.sh"]
    assert _SCRIPT.is_file()


def test_scheduler_step_passes_every_variable_the_script_reads():
    script = _SCRIPT.read_text()
    required = set(re.findall(r"\$\{(\w+):\?", script))
    # every other $NAME / ${NAME} use that is not a variable the script assigns itself
    assigned = set(re.findall(r"^\s*(\w+)=", script, flags=re.M))
    used = set(re.findall(r"\$\{?([A-Z_][A-Z0-9_]*)\b", script)) - assigned
    env = dict(e.split("=", 1) for e in _scheduler_step().get("env", []))
    assert required, "script must fail loudly on empty inputs"
    assert used <= set(env), f"script reads {sorted(used - set(env))} but the step does not pass them"
    assert required <= set(env)
    assert all(v.startswith("$") for v in env.values()), "values must come from substitutions"


def test_script_has_no_unexpanded_cloud_build_syntax():
    script = _SCRIPT.read_text()
    assert "$$" not in script
    assert not re.search(r"\$_[A-Z]", script), "substitutions do not apply inside files"
    code = [ln for ln in script.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert code[0] == "set -euo pipefail"

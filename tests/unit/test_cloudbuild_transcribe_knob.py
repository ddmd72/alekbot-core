"""The transcription kill switch and its cap knob survive a deploy (USER_DRIVE_INDEXING_RFC §27.13; plan Task 14B; G13,
T-R3): `--set-env-vars` replaces every variable, so both ride as substitutions fed by deploy-only .env keys
(DRIVE_TRANSCRIBE_ENABLED_DEV, DRIVE_TRANSCRIBE_USD_CAP_DEV; never the local keys). The switch defaults OFF in the
Makefile and in the `substitutions:` block; the cap, which only lowers $20, reaches the indexer AND the main service, and
so does the switch since T-R34 (the Cabinet shows the disclosure only while it is on)."""
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_BUILD = yaml.safe_load((_ROOT / "cloudbuild-dev.yaml").read_text())
_MAKEFILE = (_ROOT / "Makefile").read_text()


def _env(service: str):
    (args,) = [s["args"] for s in _BUILD["steps"] if s.get("args", [])[:3] == ["run", "deploy", service]]
    env = next(a.split("=", 1)[1] for a in args if a.startswith("--set-env-vars="))
    return dict(pair.split("=", 1) for pair in env.split(","))


def test_the_indexer_deploy_sets_the_switch_and_the_cap_from_substitutions():
    env = _env("alek-bot-indexer-dev")
    assert env["DRIVE_TRANSCRIBE_ENABLED"] == "$_DRIVE_TRANSCRIBE_ENABLED"
    assert env["DRIVE_TRANSCRIBE_USD_CAP"] == "$_DRIVE_TRANSCRIBE_USD_CAP"


def test_the_main_service_gets_the_cap_and_the_switch_for_the_cabinet():
    env = _env("alek-bot-dev")
    assert env["DRIVE_TRANSCRIBE_USD_CAP"] == "$_DRIVE_TRANSCRIBE_USD_CAP"
    # T-R34: the same substitution as the indexer's, so the Cabinet and the indexer never disagree on the switch
    assert env["DRIVE_TRANSCRIBE_ENABLED"] == "$_DRIVE_TRANSCRIBE_ENABLED"


def test_substitution_default_off():
    subs = _BUILD["substitutions"]
    assert subs["_DRIVE_TRANSCRIBE_ENABLED"] == "off"
    assert subs["_DRIVE_TRANSCRIBE_USD_CAP"] == ""   # empty = the $20 maximum


def test_knob_default_off_in_cloudbuild_and_makefile():
    assert re.search(r"^_DRIVE_TRANSCRIBE_ENABLED = \$\(or \$\(DRIVE_TRANSCRIBE_ENABLED_DEV\),off\)$", _MAKEFILE, re.M)
    assert re.search(r"^_DRIVE_TRANSCRIBE_USD_CAP = \$\(DRIVE_TRANSCRIBE_USD_CAP_DEV\)$", _MAKEFILE, re.M)
    assert "_DRIVE_TRANSCRIBE_ENABLED=$(_DRIVE_TRANSCRIBE_ENABLED)" in _MAKEFILE
    assert "_DRIVE_TRANSCRIBE_USD_CAP=$(_DRIVE_TRANSCRIBE_USD_CAP)" in _MAKEFILE
    assert _BUILD["substitutions"]["_DRIVE_TRANSCRIBE_ENABLED"] == "off"


def test_the_deploy_never_reads_the_local_keys():
    """The deploy-substitution trap: a local key reused as a substitution bakes its local value into the revision."""
    deploy_lines = [l for l in _MAKEFILE.splitlines() if l.startswith("_DRIVE_TRANSCRIBE")]
    assert deploy_lines and all("_DEV)" in l for l in deploy_lines)

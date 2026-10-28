"""The owner-notes kill switch DRIVE_NOTES_ENABLED survives a deploy and is the same on both services (DRIVE_FILE_NOTES_RFC
§3.6; final review D-I1, L-M4): the deploy steps replace every variable (`--set-env-vars`), so the switch rides as the
`_DRIVE_NOTES_ENABLED` substitution on the main service AND the indexer, default "on" (the code default), fed by the
deploy-only key DRIVE_NOTES_ENABLED_DEV (never the local key), and `make deploy` echoes it."""
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


def _main_service() -> str:
    deploys = [s["args"][2] for s in _BUILD["steps"] if s.get("args", [])[:2] == ["run", "deploy"]]
    return next(d for d in deploys if "indexer" not in d and "relay" not in d and "sandbox" not in d)


def test_both_services_set_the_switch_from_one_substitution():
    assert _env(_main_service())["DRIVE_NOTES_ENABLED"] == "$_DRIVE_NOTES_ENABLED"
    assert _env("alek-bot-indexer-dev")["DRIVE_NOTES_ENABLED"] == "$_DRIVE_NOTES_ENABLED"


def test_the_substitution_defaults_to_on():
    assert _BUILD["substitutions"]["_DRIVE_NOTES_ENABLED"] == "on"


def test_make_deploy_passes_and_echoes_it_from_the_deploy_only_key_default_on():
    assert re.search(r"^_DRIVE_NOTES_ENABLED = \$\(or \$\(DRIVE_NOTES_ENABLED_DEV\),on\)$", _MAKEFILE, re.M)
    assert "_DRIVE_NOTES_ENABLED=$(_DRIVE_NOTES_ENABLED)," in _MAKEFILE
    assert "_DRIVE_NOTES_ENABLED=$(_DRIVE_NOTES_ENABLED)\"" in _MAKEFILE   # the echo line

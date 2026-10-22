"""The indexer's emergency switch DRIVE_UPGRADE_PASS survives a deploy (USER_DRIVE_INDEXING_RFC §25.8; review XC-7):
the deploy step replaces every variable (`--set-env-vars`), so the switch rides as the `_DRIVE_UPGRADE_PASS`
substitution, default "on", fed by the deploy-only .env key DRIVE_UPGRADE_PASS_DEV (never the local key)."""
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_BUILD = yaml.safe_load((_ROOT / "cloudbuild-dev.yaml").read_text())
_MAKEFILE = (_ROOT / "Makefile").read_text()


def _indexer_env():
    (args,) = [s["args"] for s in _BUILD["steps"] if s.get("args", [])[:3] == ["run", "deploy", "alek-bot-indexer-dev"]]
    env = next(a.split("=", 1)[1] for a in args if a.startswith("--set-env-vars="))
    return dict(pair.split("=", 1) for pair in env.split(","))


def test_the_indexer_deploy_sets_the_switch_from_its_substitution():
    assert _indexer_env()["DRIVE_UPGRADE_PASS"] == "$_DRIVE_UPGRADE_PASS"


def test_the_substitution_defaults_to_on():
    assert _BUILD["substitutions"]["_DRIVE_UPGRADE_PASS"] == "on"


def test_make_deploy_passes_it_from_the_deploy_only_key_default_on():
    assert re.search(r"^_DRIVE_UPGRADE_PASS = \$\(or \$\(DRIVE_UPGRADE_PASS_DEV\),on\)$", _MAKEFILE, re.M)
    assert "_DRIVE_UPGRADE_PASS=$(_DRIVE_UPGRADE_PASS)" in _MAKEFILE

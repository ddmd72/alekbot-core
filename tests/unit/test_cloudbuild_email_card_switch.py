"""The email card online switch EMAIL_CARD_ONLINE survives a deploy (email index v2, slice 6): the main deploy step
replaces every variable (`--set-env-vars`), so the switch rides as the `_EMAIL_CARD_ONLINE` substitution on the main
service only, default "1" (on), fed by the deploy-only key EMAIL_CARD_ONLINE_DEV (never the local key), and
`make deploy` echoes it."""
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


def _deployed_services():
    return [s["args"][2] for s in _BUILD["steps"] if s.get("args", [])[:2] == ["run", "deploy"]]


def _main_service() -> str:
    return next(d for d in _deployed_services()
                if "indexer" not in d and "relay" not in d and "sandbox" not in d)


def test_the_main_service_sets_the_switch_from_the_substitution():
    assert _env(_main_service())["EMAIL_CARD_ONLINE"] == "$_EMAIL_CARD_ONLINE"


def test_no_other_service_sets_the_switch():
    others = [d for d in _deployed_services() if d != _main_service()]
    assert others
    for service in others:
        args = next(s["args"] for s in _BUILD["steps"] if s.get("args", [])[:3] == ["run", "deploy", service])
        assert not any("EMAIL_CARD_ONLINE" in a for a in args)


def test_the_substitution_defaults_to_on():
    assert _BUILD["substitutions"]["_EMAIL_CARD_ONLINE"] == "1"


def test_make_deploy_passes_and_echoes_it_from_the_deploy_only_key_default_on():
    assert re.search(r"^_EMAIL_CARD_ONLINE = \$\(or \$\(EMAIL_CARD_ONLINE_DEV\),1\)$", _MAKEFILE, re.M)
    assert "_EMAIL_CARD_ONLINE=$(_EMAIL_CARD_ONLINE)," in _MAKEFILE
    assert "_EMAIL_CARD_ONLINE=$(_EMAIL_CARD_ONLINE)\"" in _MAKEFILE   # the echo line

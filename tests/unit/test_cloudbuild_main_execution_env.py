"""The main service's deploy step pins the gen2 execution environment: the parser child's limits were measured there
(USER_FILE_PARSING_RFC §4.8), the same pin the indexer carries."""
from pathlib import Path

import yaml

_STEPS = yaml.safe_load((Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml").read_text())["steps"]


def _step(step_id):
    return next(s for s in _STEPS if s.get("id") == step_id)


def test_deploy_main_pins_gen2_exactly_once():
    assert _step("deploy-main")["args"].count("--execution-environment=gen2") == 1


def test_the_indexer_step_keeps_its_gen2_pin():
    steps = [s for s in _STEPS if "alek-bot-indexer-dev" in " ".join(s.get("args", []))
             and "--execution-environment=gen2" in s["args"]]
    assert steps

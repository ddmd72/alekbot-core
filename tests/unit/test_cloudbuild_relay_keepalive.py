"""The build-managed relay keep-alive reproduces the hand-made live job: a 180 s attempt deadline (main's warmup
handler waits up to 120 s for the relay) and a description, on both the update and the create path."""
from pathlib import Path

import yaml

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _relay_calls():
    script = next(s["args"][-1] for s in yaml.safe_load(_BUILD.read_text())["steps"]
                  if "alek-voice-relay-dev-keep-alive" in s.get("args", [""])[-1])
    calls = script.split("gcloud scheduler jobs ")
    return [c for c in calls if c.startswith(("update http alek-voice-relay-dev-keep-alive",
                                              "create http alek-voice-relay-dev-keep-alive"))]


def test_both_paths_use_the_live_jobs_deadline_and_description():
    calls = _relay_calls()
    assert len(calls) == 2
    for call in calls:
        assert "--attempt-deadline=180s" in call
        assert "--description=" in call

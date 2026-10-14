"""The live check runs only while the drive queue is paused (review E-M1, USER_DRIVE_INDEXING_RFC §22, §24.12).

The main service's fan-out enqueues a tick for every user with a stored sync state, the synthetic one included, so a
live indexer tick would race the check's own tick (and purge an index whose user does not exist). The owner pauses
`drive-sync-<suffix>` before the check and resumes it after; the script only reads the queue's state, refuses unless
it is PAUSED, and prints the resume command when it ends."""
import asyncio
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.validation.drive_eval import live_check as module
from scripts.validation.drive_eval.live_check import LiveCheckError, check_drive_queue, queue_command

_PATH = "projects/proj-x/locations/us-central1/queues/drive-sync-dev"


class _Client:
    def __init__(self, state):
        self.state = state
        self.asked = []

    def get_queue(self, name):
        self.asked.append(name)
        return SimpleNamespace(state=self.state)


def _settings(dev=True, project="proj-x"):
    return {"ENVIRONMENT_CONFIG": SimpleNamespace(is_development=dev), "GOOGLE_CLOUD_PROJECT": project}


def test_a_paused_queue_lets_the_check_run():
    from google.cloud import tasks_v2

    client = _Client(tasks_v2.Queue.State.PAUSED)
    assert check_drive_queue(_settings(), client) == _PATH
    assert client.asked == [_PATH]


@pytest.mark.parametrize("state", ["RUNNING", "DISABLED", "STATE_UNSPECIFIED"])
def test_any_other_state_is_refused_with_the_pause_command(state):
    from google.cloud import tasks_v2

    with pytest.raises(LiveCheckError, match=f"is {state}, not PAUSED") as err:
        check_drive_queue(_settings(), _Client(getattr(tasks_v2.Queue.State, state)))
    assert "gcloud tasks queues pause drive-sync-dev --location=us-central1 --project=proj-x" in str(err.value)


def test_the_queue_name_follows_the_composition_rule():
    from google.cloud import tasks_v2

    client = _Client(tasks_v2.Queue.State.PAUSED)
    assert check_drive_queue(_settings(dev=False), client).endswith("/queues/drive-sync-prod")


def test_no_project_is_refused_before_any_api_call():
    client = _Client("PAUSED")
    with pytest.raises(LiveCheckError, match="GOOGLE_CLOUD_PROJECT"):
        check_drive_queue(_settings(project=""), client)
    assert client.asked == []


def test_queue_commands():
    assert queue_command("resume", _PATH) == (
        "gcloud tasks queues resume drive-sync-dev --location=us-central1 --project=proj-x")


def _patch_main(monkeypatch, state, firestore_client):
    import google.cloud.firestore as firestore_mod
    import src.config.settings as settings_mod
    from google.cloud import tasks_v2

    monkeypatch.setattr(settings_mod, "load_settings", lambda: _settings())
    monkeypatch.setattr(tasks_v2, "CloudTasksClient", lambda: _Client(state))
    monkeypatch.setattr(firestore_mod, "AsyncClient", firestore_client)


def _args():
    return Namespace(folder=Path("unused"), user="zz-synthetic-x", query=[], report=None)


def test_main_refuses_a_running_queue_before_touching_firestore(monkeypatch):
    from google.cloud import tasks_v2

    def no_firestore(**kwargs):
        raise AssertionError("Firestore must not be opened while the queue runs")

    _patch_main(monkeypatch, tasks_v2.Queue.State.RUNNING, no_firestore)
    with pytest.raises(LiveCheckError, match="not PAUSED"):
        asyncio.run(module._main(_args()))


def test_main_prints_the_resume_command_when_it_ends_even_on_failure(monkeypatch, capsys):
    from google.cloud import tasks_v2

    def broken_firestore(**kwargs):
        raise RuntimeError("no credentials")

    _patch_main(monkeypatch, tasks_v2.Queue.State.PAUSED, broken_firestore)
    with pytest.raises(RuntimeError, match="no credentials"):
        asyncio.run(module._main(_args()))
    assert "gcloud tasks queues resume drive-sync-dev --location=us-central1 --project=proj-x" in capsys.readouterr().out

"""The connect wiring (MULTI_DRIVE_RFC §5.1, §5.2, slice 1a): one edge per realm from the client settings, and the
activation of each integration after a consent, through a real connect round trip (`tests/unit/web/connect_world.py`):

- MAIL: R1's `consented(MAIL)`;
- TASKS: To Do → `consented(TASKS)`, then the `setup_microsoft_todo` worker task; Google Tasks → nothing;
- DRIVE: the drive id of the new grant onto the stored credential, then `consented(DRIVE)`.
A step that fails is logged and never stops the next one or the connect."""
import pytest

from src.composition.oauth_connect_setup import build_oauth_edges
from tests.support.firestore_eval import eval_transactions
from tests.unit.web.connect_world import ALEX, APPROOT, ConnectWorld


@pytest.fixture
def world(monkeypatch):
    with eval_transactions():
        yield ConnectWorld(monkeypatch)


def test_one_edge_per_realm_and_microsoft_only_with_its_client():
    full = build_oauth_edges({"GOOGLE_OAUTH_CLIENT_ID": "g", "GOOGLE_OAUTH_CLIENT_SECRET": "gs",
                              "MICROSOFT_TODO_CLIENT_ID": "m", "MICROSOFT_TODO_CLIENT_SECRET": "ms"})
    assert [edge.realm for edge in full] == ["google", "microsoft"]
    assert [e.realm for e in build_oauth_edges({"GOOGLE_OAUTH_CLIENT_ID": "g", "GOOGLE_OAUTH_CLIENT_SECRET": "gs",
                                                "MICROSOFT_TODO_CLIENT_ID": "m"})] == ["google"]


async def test_the_mail_activation_clears_the_reconnect_state(world):
    assert (await world.connect("gmail")).headers["Location"] == "/cabinet?connected=gmail"
    assert world.events == [("consented", ALEX, "mail")]
    assert world.worker_tasks() == []


async def test_the_todo_activation_clears_the_state_then_enqueues_the_setup(world):
    assert (await world.connect("microsoft_todo")).headers["Location"] == "/cabinet?connected=microsoft_todo"
    assert world.events == [("consented", ALEX, "tasks")]
    [(task_type, body)] = world.worker_tasks()
    assert task_type == "setup_microsoft_todo" and body["user_id"] == ALEX


async def test_a_google_tasks_consent_runs_nothing(world):
    assert (await world.connect("google_tasks")).headers["Location"] == "/cabinet?connected=google_tasks"
    assert world.events == [] and world.worker_tasks() == []
    assert len(await world.held("google_tasks")) == 1


async def test_a_failed_clear_does_not_stop_the_todo_setup(world):
    async def failing(user_id, integration):
        raise RuntimeError("feature store down")

    world.reconnect.consented.side_effect = failing
    assert (await world.connect("microsoft_todo")).headers["Location"] == "/cabinet?connected=microsoft_todo"
    assert [t for t, _ in world.worker_tasks()] == ["setup_microsoft_todo"]


async def test_the_drive_activation_stores_the_drive_id_then_clears_the_state(world):
    assert (await world.connect("microsoft_onedrive")).headers["Location"] == "/cabinet?connected=microsoft_onedrive"
    (held,) = await world.held("microsoft_onedrive")
    assert held.drive_id == "drive-b1"
    assert world.events == [("consented", ALEX, "drive")]
    [read] = world.http.requests_to("GET", APPROOT)
    assert read.headers == {"Authorization": "Bearer at-microsoft_onedrive"}


async def test_an_unread_drive_id_still_connects_without_one(world):
    world.http.route("GET", APPROOT, 503, {"error": {"code": "serviceNotAvailable"}})
    assert (await world.connect("microsoft_onedrive")).headers["Location"] == "/cabinet?connected=microsoft_onedrive"
    (held,) = await world.held("microsoft_onedrive")
    assert held.drive_id == ""
    assert world.events == [("consented", ALEX, "drive")]


async def test_a_drive_reconnect_to_another_drive_takes_the_new_id(world):
    await world.connect("microsoft_onedrive")
    world.http.route("GET", APPROOT, body={"id": "approot-2", "parentReference": {"driveId": "drive-b2"}})
    await world.connect("microsoft_onedrive")                    # same account: the record is updated in place
    (held,) = await world.held("microsoft_onedrive")
    assert held.drive_id == "drive-b2"

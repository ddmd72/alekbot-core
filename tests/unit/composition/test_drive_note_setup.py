"""Part C's owner-notes wiring (DRIVE_FILE_NOTES_RFC §3.6, §4.8, §6.4, §7): with the switch off the indexer gets no
lifecycle; the deleted-files source joins the search whatever the switch; the indexer passes the lifecycle to the
tick; the `$drive-notes` command gets the container's service only with the switch on, through both adapter factories
and both main.py sites (review I2: without it the erase codes could never be redeemed). The kill switch's parser and
the store builders are part A's (their tests are A's)."""
import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import indexer_main
from src.composition.drive_note_setup import (
    add_drive_note_deleted_source, build_drive_note_lifecycle, command_drive_note_service,
)
from src.services.drive_note_deleted_search import DriveNoteDeletedSearch
from src.services.drive_note_lifecycle import DriveNoteLifecycle
from src.services.drive_search_service import DriveSearchService

from tests.unit.composition.test_memory_fact_adapter_factories import _slack, _telegram
from tests.unit.test_indexer_main import wired  # noqa: F401  (the indexer wiring fixture)

_ROOT = Path(__file__).resolve().parents[3]


def test_no_lifecycle_with_the_switch_off_or_no_store():
    assert build_drive_note_lifecycle(enabled=False, notes=MagicMock(), index=MagicMock(), drive=MagicMock()) is None
    assert build_drive_note_lifecycle(enabled=True, notes=None, index=MagicMock(), drive=MagicMock()) is None
    assert isinstance(build_drive_note_lifecycle(enabled=True, notes=MagicMock(), index=MagicMock(),
                                                 drive=MagicMock()), DriveNoteLifecycle)


def test_the_deleted_source_joins_the_search():
    search = DriveSearchService(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), embedding_space="s/8")
    source = add_drive_note_deleted_source(search, AsyncMock(), AsyncMock(), embedding_space="s/8",
                                             connections=AsyncMock())
    assert isinstance(source, DriveNoteDeletedSearch) and search._deleted_sources[-1] is source
    assert len(search._deleted_sources) == 2


async def test_the_indexer_passes_the_lifecycle_to_the_tick(wired, monkeypatch):  # noqa: F811
    monkeypatch.delenv("DRIVE_NOTES_ENABLED", raising=False)
    indexer_main.build_app()
    assert isinstance(wired["note_lifecycle"], DriveNoteLifecycle)


async def test_the_indexer_has_no_lifecycle_with_the_switch_off(wired, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DRIVE_NOTES_ENABLED", "off")
    indexer_main.build_app()
    assert wired["note_lifecycle"] is None


def test_the_command_gets_the_service_only_with_the_switch_on():
    service = MagicMock(name="drive_note_service")
    assert command_drive_note_service(service, True) is service
    assert command_drive_note_service(service, False) is None
    assert command_drive_note_service(None, True) is None


@pytest.mark.parametrize("build", [_slack, _telegram])
def test_both_factories_forward_the_service(build):
    sentinel = MagicMock(name="drive_note_service")
    assert build(drive_note_service=sentinel)["drive_note_service"] is sentinel


@pytest.mark.parametrize("build", [_slack, _telegram])
def test_both_factories_default_to_none(build):
    assert build()["drive_note_service"] is None


def test_both_main_sites_pass_the_gated_service():
    """The application graph builds the Slack and the Telegram adapter (main.py did until `build_application_graph`):
    each gets the container's service through the switch, from the one `ChatServices` both factories take."""
    source = (_ROOT / "src" / "composition" / "application_graph.py").read_text()
    tree = ast.parse(source)
    [built] = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
               and ast.unparse(n.value.func) == "ChatServices"]
    keywords = {k.arg: ast.unparse(k.value) for k in built.value.keywords}
    assert keywords["drive_note_service"] == ("command_drive_note_service(container.drive_note_service, "
                                              "container.drive_notes_enabled)")
    assert keywords["memory_fact_service"] == "container.memory_fact_service"
    [name] = [ast.unparse(t) for t in built.targets]
    factories = {ast.unparse(n.func): {k.arg: ast.unparse(k.value) for k in n.keywords} for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("AdapterFactory.create_adapter")}
    assert set(factories) == {"SlackAdapterFactory.create_adapter", "TelegramAdapterFactory.create_adapter"}
    assert all(kw.get("chat") == name for kw in factories.values()), factories
    # no other site builds the command's service, gated or not
    assert source.count("command_drive_note_service(") == 1
    assert source.count("container.drive_note_service") == 1

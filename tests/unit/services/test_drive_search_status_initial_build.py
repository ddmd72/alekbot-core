"""`DriveSearchService.indexing_status` reads the initial build of the connection's provider account (MULTI_DRIVE_RFC §10,
D8), as the indexer does; a connection without an account is logged at ERROR and reports no initial build."""
import logging
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from scripts.validation.drive_eval.eval_connection import EvalDriveConnections, eval_connection
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveSyncState
from src.domain.provider_account import ProviderAccount
from src.services.drive_search_service import DriveSearchService

from .drive_index_fakes import FakeEmbeddings

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
USER = "u-status"
ACCOUNT = ProviderAccount(realm="microsoft", subject="subject-1", label="")


async def _service(connection):
    repo = InMemoryDriveIndexRepository()
    scope = ConnectionScope(USER, connection.connection_id)
    await repo.save_sync_state(DriveSyncState(user_id=USER, connection_id=connection.connection_id, epoch=1,
                                              root_item_id="root", created_at=NOW))
    service = DriveSearchService(repo, FakeEmbeddings(), object(), EvalDriveConnections(connection),
                                 clock=lambda: NOW, embedding_space="fake/8")
    return repo, scope, service


async def test_the_status_reports_the_initial_build_of_the_provider_account():
    connection = replace(eval_connection(USER), provider_account=ACCOUNT)
    repo, _, service = await _service(connection)
    await repo.spend_initial_build(USER, ACCOUNT.realm, ACCOUNT.subject, 0.75, NOW)
    other = ProviderAccount(realm="microsoft", subject="subject-2", label="")
    await repo.spend_initial_build(USER, other.realm, other.subject, 5.0, NOW)   # another account's build: not this one
    status = await service.indexing_status(USER)
    assert status.initial_build_spent_usd == pytest.approx(0.75) and not status.initial_build_closed


async def test_a_connection_without_an_account_logs_an_error_and_reports_a_closed_build(caplog):
    _, _, service = await _service(eval_connection(USER))
    with caplog.at_level(logging.ERROR):
        status = await service.indexing_status(USER)
    assert status.initial_build_spent_usd == 0 and status.initial_build_closed
    assert any("no provider account" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)

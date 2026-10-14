"""The initial build's share of a month's class dollars (USER_DRIVE_INDEXING_RFC §25.4.5, D27; review rv_sv2_p5 M1):
both adapters keep `<cls>_initial_usd` beside `<cls>_usd`, the Firestore one as a server-side increment written only
when there is a share, so the month's cap counts only its own dollars."""
import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.adapters.firestore_drive_index_repo import usage_from_doc
from src.domain.drive_enrichment import ModelUsageMonth

from tests.unit.adapters.firestore_drive_eval_fakes import USAGE, EvalDb, eval_transactions, make_eval_repo


@pytest.fixture
def repos(mock_env_config):
    db = EvalDb()
    with eval_transactions():
        yield db, InMemoryDriveIndexRepository(), make_eval_repo(db, mock_env_config)


async def test_both_adapters_keep_the_initial_share_per_class(repos):
    _, mem, fs = repos
    for repo in (mem, fs):
        await repo.add_model_usage("u", "2026-10", "2026-10-13", "profile", 1.0, 0.0, 1, covered_usd=0.75)
        await repo.add_model_usage("u", "2026-10", "2026-10-13", "profile", 0.5, 0.0, 1)
        await repo.add_model_usage("u", "2026-10", "2026-10-13", "passage", 0.25, 0.0, 1, covered_usd=0.25)
    got = [await repo.get_model_usage("u", "2026-10") for repo in (mem, fs)]
    assert got[0] == got[1]
    usage = got[0]
    assert (usage.profile_usd, usage.profile_initial_usd, usage.passage_initial_usd) == (1.5, 0.75, 0.25)
    assert (usage.cap_usd("profile"), usage.cap_usd("passage"), usage.cap_usd("reader")) == (0.75, 0.0, 0.0)


async def test_the_share_is_an_increment_written_only_when_there_is_one(repos):
    db, _, fs = repos
    await fs.add_model_usage("u", "2026-10", "2026-10-13", "reader", 0.5, 0.0, 1)
    assert "reader_initial_usd" not in db.cols[USAGE].docs["u:2026-10"]
    await fs.add_model_usage("u", "2026-10", "2026-10-13", "reader", 0.5, 0.0, 1, covered_usd=0.5)
    assert db.cols[USAGE].docs["u:2026-10"]["reader_initial_usd"] == 0.5


def test_a_document_without_the_share_reads_as_none_paid_by_the_build():
    usage = usage_from_doc({"profile_usd": 2.0})
    assert usage == ModelUsageMonth(profile_usd=2.0) and usage.cap_usd("profile") == 2.0

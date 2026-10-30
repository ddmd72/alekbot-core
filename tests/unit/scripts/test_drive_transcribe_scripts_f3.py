"""Final review F3: the requeue's cap source and the sweep's no-reserved_month count (N2)."""
import importlib.util
from pathlib import Path

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_transcript import TranscriptBatch

from src.domain.connection_scope import ConnectionScope

from tests.unit.services.drive_index_fakes import T0

SCOPE = ConnectionScope("u", "u-c")   # the connection the fixtures belong to (MULTI_DRIVE_RFC §8)
_DIR = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cap_prefers_the_flag_then_the_dev_key_then_the_default():
    requeue = _load("drive_transcribe_requeue")
    assert requeue.resolve_cap(5.0, "10") == (5.0, "--cap-usd")
    assert requeue.resolve_cap(None, "10") == (10.0, "DRIVE_TRANSCRIBE_USD_CAP_DEV")
    cap, source = requeue.resolve_cap(None, "")
    assert cap == 20.0 and source.startswith("default")
    assert requeue.resolve_cap(50.0, None)[0] == 20.0    # the knob only lowers


def test_the_cli_has_a_cap_flag():
    assert "--cap-usd" in (_DIR / "drive_transcribe_requeue.py").read_text()


def test_the_local_cap_key_is_not_read():
    text = (_DIR / "drive_transcribe_requeue.py").read_text()
    assert 'getenv("DRIVE_TRANSCRIBE_USD_CAP")' not in text


async def test_sweep_counts_documents_holding_money_with_no_month():
    sweep = _load("drive_transcript_sweep")
    repo = InMemoryDriveIndexRepository()
    await repo.create_batch(TranscriptBatch(name="batches/b", user_id="u", connection_id="u-c", request_keys=("i:0",), reserved_usd=0.5,
                                            reserved_month="", next_poll_at=T0))
    out = await sweep.settle_reservations(repo, "u", ["2026-10"], scopes=[SCOPE], apply=False, now=T0)
    assert out.no_month == 1
    assert "1 documents hold reserved money with no reserved_month" in sweep._no_month_note(out)
    assert sweep._no_month_note(sweep.Settlement()) == ""

"""Search v2 cross-cutting review fixes on the indexer (USER_DRIVE_INDEXING_RFC §25.4.5, §25.5.6): the reader's
fallback dollars reach the fallback alert (XC-5), and the tick stores the coverage line's counts so a search reads
them instead of counting (XC-3, XC-9)."""
import pytest

from src.domain.drive_enrich_plan import COVERAGE_COUNTERS, GAP_WAITING
from src.domain.drive_enrichment_calls import ModelReplyStatus
from src.domain.drive_extraction import READER, DriveModelUnavailable, ExtractionOutcome, ExtractionStatus
from src.domain.billing import calculate_cost
from src.domain.exceptions import LLMUnavailableError

from tests.unit.agents.test_drive_agents_gemini_fallback import _read, _reader
from tests.unit.agents.drive_model_fakes import reply
from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_enrich_world import EnrichWorld

pytestmark = pytest.mark.usefixtures("admitted")


def doc_text(word: str = "Acme", n: int = 30) -> str:
    return f"{word} Rentals. " + prose(n)


# -- XC-5: the reader agent, the runner and the tick -----------------------------------------------------------------

async def test_the_reader_agent_reports_the_fallbacks_share():
    agent, _, _ = _reader([LLMUnavailableError("503", http_status=503)], [reply({"text": "x", "brief": None})])
    out = await _read(agent)
    assert out.fallback_cost == pytest.approx(out.cost) and out.cost == pytest.approx(
        calculate_cost("gemini-3.8-flash", 1000, 200))


async def test_a_primary_read_has_no_fallback_share():
    agent, _, _ = _reader([reply({"text": "x", "brief": None})], [])
    out = await _read(agent)
    assert out.fallback_cost == 0.0 and out.cost > 0


async def test_the_runner_carries_the_fallback_share_into_the_outcome(tmp_path):
    from src.agents.drive_document_reader_agent import ReaderReply
    from tests.unit.composition.test_drive_reader_runner_halving import _request, _runner

    runner, _, _ = _runner(lambda request: None, pages_total=25)   # 25 pages: several windows

    class _Agent:
        async def read(self, **kw):
            return ReaderReply(text="page text", cost=0.01, fallback_cost=0.004, calls=1)

    async def agent(user_id):
        return _Agent()

    runner._agent = agent
    outcome = await runner.extract(_request(tmp_path))
    windows = outcome.model_calls
    assert windows > 1
    assert outcome.model_cost == pytest.approx(0.01 * windows)
    assert outcome.model_fallback_cost == pytest.approx(0.004 * windows)


async def test_the_runner_carries_the_fallback_share_into_an_outage(tmp_path):
    from tests.unit.composition.test_drive_reader_runner_halving import _request, _runner

    runner, _, _ = _runner(lambda request: None, pages_total=1)

    class _Agent:
        async def read(self, **kw):
            raise DriveModelUnavailable("down", model_calls=1, model_cost=0.02, model_fallback_cost=0.02)

    async def agent(user_id):
        return _Agent()

    runner._agent = agent
    with pytest.raises(DriveModelUnavailable) as caught:
        await runner.extract(_request(tmp_path))
    assert (caught.value.model_cost, caught.value.model_fallback_cost) == (0.02, 0.02)


async def test_the_reader_outage_exception_carries_a_fallback_share():
    exc = DriveModelUnavailable("x", model_cost=0.3, model_fallback_cost=0.2)
    assert (exc.model_cost, exc.model_fallback_cost) == (0.3, 0.2)
    assert DriveModelUnavailable("x").model_fallback_cost == 0.0


async def test_the_readers_fallback_dollars_reach_the_usage_documents_fallback_share():
    w = EnrichWorld()
    w.reader.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=doc_text(),
                                                      model_calls=1, model_cost=0.003, model_fallback_cost=0.002,
                                                      pages_sent=2, pages_total=2, extracted_by=f"{READER}@1")
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2)
    w.add_doc("scan.pdf", "x")
    await w.settle()
    usage = w.repo.usage[("u", "2026-10")]
    assert usage.reader_usd == pytest.approx(0.003)
    assert usage.fallback_usd >= 0.002 - 1e-9
    day_total, day_fallback = next(iter(usage.daily.values()))
    assert day_fallback >= 0.002 - 1e-9


async def test_a_reader_outage_bills_its_fallback_share_too():
    w = EnrichWorld()

    async def down(request):
        w.reader.requests.append(request)
        raise DriveModelUnavailable("down", model_calls=1, model_cost=0.004, model_fallback_cost=0.004,
                                    charges_file=False)

    w.reader.extract = down
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2)
    w.add_doc("scan.pdf", "x")
    await w.settle()
    assert w.reader.requests
    usage = w.repo.usage[("u", "2026-10")]
    assert usage.reader_usd == pytest.approx(0.004) and usage.fallback_usd == pytest.approx(0.004)


# -- XC-3 / XC-9: the tick stores the coverage counts ----------------------------------------------------------------

async def test_the_tick_stores_the_coverage_counts_from_the_records():
    w = EnrichWorld()
    w.profiler.script = [ModelReplyStatus.UNAVAILABLE] * 50
    w.add_doc("lease.md", doc_text())
    await w.settle()
    rec = next(r for r in w.repo.records.values() if r.name == "lease.md")
    counters = w.repo.states["u"].counters
    assert rec.describe_gap == GAP_WAITING
    assert counters.get(COVERAGE_COUNTERS[GAP_WAITING]) == 1


async def test_a_described_file_leaves_no_coverage_count():
    w = EnrichWorld()
    w.add_doc("lease.md", doc_text())
    await w.settle()
    counters = w.repo.states["u"].counters
    assert not any(k.startswith("coverage_") for k in counters)
    rec = next(r for r in w.repo.records.values() if r.name == "lease.md")
    assert rec.describe_gap == ""

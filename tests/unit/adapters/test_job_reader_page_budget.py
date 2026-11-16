"""JobReaderPageBudget (email index v2, slice 5): the scripts' in-memory page budget, with an optional dollar gate."""
import asyncio

from src.adapters.job_reader_page_budget import JobReaderPageBudget
from src.ports.reader_page_budget import ReaderPageBudget


def test_it_implements_the_port():
    assert issubclass(JobReaderPageBudget, ReaderPageBudget)


async def test_pages_are_booked_up_to_the_cap():
    budget = JobReaderPageBudget(5)
    assert [await budget.reserve("u1", 3), await budget.reserve("u1", 3), await budget.reserve("u1", 2)] == [True, False, True]
    assert budget.used == 5


async def test_a_gate_refusal_books_nothing_but_is_counted_as_asked():
    seen = []

    async def gate(pages: int) -> bool:
        seen.append(pages)
        return False

    budget = JobReaderPageBudget(10, gate=gate)
    assert await budget.reserve("u1", 3) is False
    assert (budget.used, budget.asked, budget.asked_pages, seen) == (0, 1, 3, [3])


async def test_a_zero_budget_refuses_and_still_counts_what_it_was_asked():
    budget = JobReaderPageBudget(0)
    await budget.reserve("u1", 3)
    await budget.reserve("u1", 2)
    assert (budget.used, budget.asked, budget.asked_pages) == (0, 2, 5)


async def test_the_gate_is_not_asked_when_the_page_cap_already_refuses():
    calls = []

    async def gate(pages: int) -> bool:
        calls.append(pages)
        return True

    budget = JobReaderPageBudget(2, gate=gate)
    assert await budget.reserve("u1", 3) is False and calls == []


async def test_concurrent_bookings_never_overbook():
    budget = JobReaderPageBudget(5)
    results = await asyncio.gather(*(budget.reserve("u1", 3) for _ in range(4)))
    assert results.count(True) == 1 and budget.used == 3

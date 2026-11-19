"""The cloud kits' retry of "index currently building" (tests/support/kit_backend.py `BuildingIndexRetry`), offline: a
stand-in GAPIC API scripts the answers; the waits are recorded, never slept."""
import pytest
from google.api_core.exceptions import FailedPrecondition, ServiceUnavailable

from tests.support import kit_backend
from tests.support.kit_backend import BuildingIndexRetry

BUILDING = FailedPrecondition("The query requires an index. That index is currently building and cannot be used yet.")
MISSING = FailedPrecondition("The query requires an index. You can create it here: ...")


class _Api:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def run_query(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    run_aggregation_query = run_query

    def commit(self, **kwargs):
        return ("commit", kwargs)


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def _retrying(api, clock, run=None):
    """A fresh run state unless one is given: the module's RUN_STATE is the session's and must not leak between tests."""
    return BuildingIndexRetry(api, sleep=clock.sleep, clock=clock, run={"expired": False} if run is None else run)


@pytest.mark.parametrize("method", BuildingIndexRetry.RETRIED)
async def test_a_building_index_is_retried_with_backoff_until_it_serves(method):
    api, clock = _Api(BUILDING, BUILDING, BUILDING, "rows"), _Clock()
    with pytest.warns(UserWarning, match="3 time"):
        assert await getattr(_retrying(api, clock), method)(request="q") == "rows"
    assert clock.waits == [5.0, 10.0, 20.0] and api.calls == [{"request": "q"}] * 4


async def test_the_wait_is_capped_and_the_deadline_raises_the_last_refusal():
    api, clock = _Api(*[BUILDING] * 30), _Clock()
    with pytest.raises(FailedPrecondition, match="currently building"):
        await _retrying(api, clock).run_query(request="q")
    assert max(clock.waits) == 60.0 and sum(clock.waits) <= kit_backend.BUILDING_RETRY_S + 60


@pytest.mark.parametrize("answer", [MISSING, ServiceUnavailable("down")])
async def test_any_other_answer_is_raised_at_once(answer):
    api, clock = _Api(answer, "rows"), _Clock()
    with pytest.raises(type(answer)):
        await _retrying(api, clock).run_query(request="q")
    assert clock.waits == [] and len(api.calls) == 1


def test_every_other_call_is_the_apis_own():
    api = _Api()
    assert _retrying(api, _Clock()).commit(request="c") == ("commit", {"request": "c"})


async def test_once_one_call_passed_the_deadline_every_later_call_raises_at_once():
    run, clock = {"expired": False}, _Clock()
    with pytest.raises(FailedPrecondition, match="currently building"):
        await _retrying(_Api(*[BUILDING] * 30), clock, run).run_query(request="q")
    assert run["expired"]
    waited = list(clock.waits)
    later = _Api(BUILDING, "rows")                       # another test's client, same run
    with pytest.raises(FailedPrecondition, match="currently building"):
        await _retrying(later, clock, run).run_aggregation_query(request="q")
    assert clock.waits == waited and len(later.calls) == 1
    assert await _retrying(_Api("rows"), clock, run).run_query(request="q") == "rows"     # a served query still is


def test_the_default_run_state_is_the_modules():
    assert BuildingIndexRetry(_Api())._run is kit_backend.RUN_STATE

"""FactProfilerPort contract (FACT_PROFILE_RFC §4.1): an ABC with one abstract async method, `profile`, that takes the
account, the fact's text and its metadata and returns a FactProfilerReply; a minimal implementation works and a class
that leaves `profile` out cannot be instantiated."""
import inspect
from abc import ABC

import pytest

from src.domain.fact_profiler_reply import FactProfilerReply, ProfilerStatus
from src.ports.fact_profiler_port import FactProfilerPort


def test_it_is_an_abc_that_cannot_be_instantiated():
    assert issubclass(FactProfilerPort, ABC)
    with pytest.raises(TypeError):
        FactProfilerPort()  # type: ignore[abstract]


def test_profile_is_its_only_abstract_method_and_is_async():
    assert FactProfilerPort.__abstractmethods__ == frozenset({"profile"})
    assert inspect.iscoroutinefunction(FactProfilerPort.profile)


def test_profile_signature():
    params = list(inspect.signature(FactProfilerPort.profile).parameters)
    assert params == ["self", "account_id", "text", "metadata", "people"]


async def test_a_minimal_implementation_works():
    class Profiler(FactProfilerPort):
        async def profile(self, account_id, text, metadata):
            return FactProfilerReply.unavailable("m")

    reply = await Profiler().profile("acc-1", "text", None)
    assert reply.status is ProfilerStatus.UNAVAILABLE and reply.answer is None


def test_an_implementation_without_profile_cannot_be_instantiated():
    class Incomplete(FactProfilerPort):
        pass

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]

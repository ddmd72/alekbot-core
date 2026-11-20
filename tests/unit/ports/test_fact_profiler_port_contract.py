"""FactProfilerPort contract (FACT_PROFILE_RFC §4.1): a minimal implementation answers with a FactProfilerReply."""


from src.domain.fact_profiler_reply import FactProfilerReply, ProfilerStatus
from src.ports.fact_profiler_port import FactProfilerPort


async def test_a_minimal_implementation_works():
    class Profiler(FactProfilerPort):
        async def profile(self, account_id, text, metadata):
            return FactProfilerReply.unavailable("m")

    reply = await Profiler().profile("acc-1", "text", None)
    assert reply.status is ProfilerStatus.UNAVAILABLE and reply.answer is None

"""The fingerprint probe end to end on a scripted model: no network, no Firestore, no paid call. A real Smart runs the
case's scripted first turn, the production handler answers it, the one "paid" call goes to the scripted LLM, and the
probe writes its files. Cases: R2 (daily review through the real notify path) and R1.3 (memory proposal rejected)."""
import json
from typing import Any, List

import pytest

from scripts.validation import fingerprint_cases as cases
from scripts.validation import fingerprint_probe as probe
from scripts.validation.drive_eval.cost_meter import Meter
from src.domain.llm import LLMResponse, UsageMetadata
from tests.support.scripted_llm import ScriptedLLM, text
from tests.support.tool_e2e import StubPromptBuilder, smart_rig


class _PromptWithBlocks(StubPromptBuilder):
    """The stub prompt plus the static blocks Smart hands the builder (the daily review's email_for_triage)."""

    async def build_for_agent(self, **kwargs: Any) -> str:  # type: ignore[override]
        await super().build_for_agent(**kwargs)
        return "SYSTEM\n" + "\n".join(kwargs.get("extra_static_blocks") or [])


class _Memory:
    async def propose(self, user_id: str, origin_session_id: Any, draft: Any) -> str:
        return "never reached: the proposal is rejected by validation"


class _Coordinator:
    """route_message goes to Smart; handle_delegation is patched by the probe."""

    def __init__(self, smart: Any) -> None:
        self.smart = smart

    async def route_message(self, message: Any) -> Any:
        return await self.smart.execute(message)


def _answer(prompt_tokens: int = 1000) -> LLMResponse:
    return LLMResponse(text="Done.", tool_calls=[], usage_metadata=UsageMetadata(
        prompt_tokens=prompt_tokens, completion_tokens=10, total_tokens=prompt_tokens + 10))


async def _run(case_id: str, tmp_path: Any, **rig: Any):
    llm = ScriptedLLM([_answer()])          # the ONE paid call; the first turn is scripted by the probe, not by this list
    smart = smart_rig(llm, user_id="u1", **rig)
    meter = Meter(max_usd=1.0)
    uninstall = meter.install()
    try:
        rt = probe.Runtime(agents={"smart_response": smart}, coordinator=_Coordinator(smart), user_id="u1",
                           account_id="a1", meter=meter, out_dir=tmp_path)
        (case,) = cases.select([case_id])
        result = await probe.run_case(case, rt)
    finally:
        uninstall()
    return result, llm, meter


async def test_r2_daily_review_request_carries_the_invented_emails_and_the_forged_tag(tmp_path):
    result, llm, meter = await _run("R2", tmp_path, prompt_builder=_PromptWithBlocks())

    assert result["error"] == "" and result["real_calls"] == 1 and llm.unanswered == 0
    request = result["request_text"]
    assert "[DAILY EMAIL REVIEW]" in request and "Tom Baker" in request and "FA-0137" in request
    assert "external_content nonce=" in request                      # the forged closing tag, inside the JSON body
    assert "Rome" in request and "email_for_triage" in request
    written = json.loads((tmp_path / "R2.json").read_text(encoding="utf-8"))
    assert written["response_text"] == "Done." and written["cost"]["calls"][0]["input"] == 1000
    assert result["outcome"]["delivered"] is True
    assert "send_long_text" in {c["method"] for c in result["outcome"]["channel_calls"]}   # recorded, never sent


async def test_r1_3_the_model_reads_the_real_rejection_and_a_summary_line_is_written(tmp_path):
    result, llm, meter = await _run("R1.3", tmp_path, memory_fact_service=_Memory())

    assert result["error"] == "" and result["real_calls"] == 1
    request = result["request_text"]
    assert "Remember these two facts from Tom's plan." in request
    assert "propose_memory_facts" in request and "proposal rejected" in request   # the real handler's answer
    assert "one non-empty line" in request
    assert (tmp_path / "R1.3.json").is_file()
    line = probe.summary_line(result)
    assert line.startswith("R1.3: real_calls=1") and "cache-read 0 tok" in line and "cache-write 0 tok" in line
    assert meter.calls[0]["input"] == 1000       # the scripted first turn was never metered: one call only


async def test_the_case_stops_after_its_real_call_cap(tmp_path):
    llm = ScriptedLLM([_answer(), _answer()])
    smart = smart_rig(llm, user_id="u1", memory_fact_service=_Memory())
    meter = Meter(max_usd=1.0)
    uninstall = meter.install()
    try:
        rt = probe.Runtime(agents={"smart_response": smart}, coordinator=_Coordinator(smart), user_id="u1",
                           account_id="a1", meter=meter, out_dir=tmp_path)
        result = await probe.run_case(cases.select(["R1.3"])[0], rt)
    finally:
        uninstall()
    assert result["real_calls"] == 1 and llm.pending() == 1          # the second scripted answer was never asked for


def test_case_selection_and_synthetic_data_stay_inside_the_invented_universe():
    ids = [c.id for c in cases.select(["R1"])]
    assert ids == [f"R1.{n}" for n in range(1, 9)]
    assert [c.id for c in cases.select(["R5"])] == ["R5.1"]
    with pytest.raises(ValueError):
        cases.select(["R9"])
    blob = json.dumps([cases.invented_emails()]) + "".join(c.text for c in cases.ALL_CASES)
    for needed in ("Tom Baker", "FA-0137", "R&D/Tom's plan.docx"):
        assert needed in blob


async def test_the_write_guard_records_a_firestore_write_and_restores_the_methods():
    from google.cloud.firestore_v1.async_document import AsyncDocumentReference
    original = AsyncDocumentReference.set
    uninstall, blocked = probe.install_write_guard()
    try:
        class _Ref:
            path = "development_facts/x"
        assert await AsyncDocumentReference.set(_Ref(), {"a": 1}) == []
        assert blocked == ["AsyncDocumentReference.set development_facts/x"]
    finally:
        uninstall()
    assert AsyncDocumentReference.set is original

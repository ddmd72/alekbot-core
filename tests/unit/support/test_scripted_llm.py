"""The scripted model (P14 card A): answers in order, an exhausted script is loud and never an `AssertionError`, and
`tool_results` reads what each agent told the model in each request."""
import pytest

from src.domain.llm import LLMRequest, Message, MessagePart, ToolCall, build_tool_turn
from tests.support.scripted_llm import (
    RoutedLLM, ScriptExhausted, ScriptedLLM, execution_context, first_user_text, text, tool_results, tools,
)


def _request(*messages: Message) -> LLMRequest:
    return LLMRequest(model_name="scripted-model", messages=list(messages))


def _user(t: str) -> Message:
    return Message(role="user", parts=[MessagePart(text=t)])


async def test_scripted_answers_in_order_and_records_each_request():
    llm = ScriptedLLM([text("one"), tools(("count_words", {"text": "a b"})), text("three")])

    first = await llm.generate_content(_request(_user("q1")))
    second = await llm.generate_content(_request(_user("q2")))

    assert first.text == "one"
    assert [(c.name, c.args) for c in second.tool_calls] == [("count_words", {"text": "a b"})]
    assert [r.messages[0].parts[0].text for r in llm.requests] == ["q1", "q2"]
    assert llm.pending() == 1 and llm.unanswered == 0


async def test_a_scripted_exception_is_raised():
    llm = ScriptedLLM([TimeoutError("provider timed out")])
    with pytest.raises(TimeoutError):
        await llm.generate_content(_request(_user("q")))
    assert llm.pending() == 0 and llm.unanswered == 0


async def test_an_exhausted_script_raises_script_exhausted_and_counts_it():
    llm = ScriptedLLM([text("only")])
    await llm.generate_content(_request(_user("q1")))

    with pytest.raises(ScriptExhausted):
        await llm.generate_content(_request(_user("q2")))

    assert issubclass(ScriptExhausted, AssertionError) is False
    assert issubclass(ScriptExhausted, RuntimeError)
    assert llm.unanswered == 1 and llm.pending() == 0 and len(llm.requests) == 2


async def test_routed_answers_by_content_and_none_is_an_exhausted_script():
    llm = RoutedLLM(lambda r: text("yes") if r.messages[0].parts[0].text == "known" else None)

    assert (await llm.generate_content(_request(_user("known")))).text == "yes"
    with pytest.raises(ScriptExhausted):
        await llm.generate_content(_request(_user("unknown")))

    assert llm.unanswered == 1 and llm.pending() == 0 and llm.peak == 1


async def test_routed_handler_raising_script_exhausted_is_counted_and_pending_is_the_tests():
    queue = [text("a")]

    async def handler(request):
        if not queue:
            raise ScriptExhausted("queue empty")
        return queue.pop(0)

    llm = RoutedLLM(handler, pending=lambda: len(queue))
    assert llm.pending() == 1
    await llm.generate_content(_request(_user("q1")))
    with pytest.raises(ScriptExhausted):
        await llm.generate_content(_request(_user("q2")))

    assert llm.unanswered == 1 and llm.pending() == 0


async def test_routed_returned_exception_is_raised_not_counted():
    llm = RoutedLLM(lambda r: ConnectionError("down"))
    with pytest.raises(ConnectionError):
        await llm.generate_content(_request(_user("q")))
    assert llm.unanswered == 0


def test_execution_context_puts_the_fake_behind_a_real_breaker():
    llm = ScriptedLLM([])
    ctx = execution_context(llm, "notes", provider_name="openai")
    assert ctx.provider is llm and ctx.agent_type == "notes" and ctx.provider_name == "openai"
    assert ctx.resilience_port.is_provider_open("openai") is False
    assert execution_context(llm, "smart").provider_name == ""


def test_tool_results_reads_the_three_shapes_of_the_last_message():
    consolidation = _request(_user("batch"), Message(role="user", parts=[MessagePart(tool_response={
        "name": "create_fact", "response": {"result": '{"status": "created", "fact_id": "f1"}'}})]))
    notes = _request(_user("remind"), Message(role="user", parts=[MessagePart(tool_response={
        "name": "create_self_reminder", "response": {"result": str({"status": "created", "note_id": "1"})}})]))
    tasks = _request(_user("task"), Message(role="user", parts=[MessagePart(tool_response={
        "name": "create_task", "response": {"title": "Car service", "ref": "ab12cd34"}})]))
    llm = ScriptedLLM([])
    llm.requests = [consolidation, notes, tasks]

    assert tool_results(llm) == [
        ("create_fact", {"status": "created", "fact_id": "f1"}),        # JSON string decoded
        ("create_self_reminder", "{'status': 'created', 'note_id': '1'}"),  # Python repr kept as is
        ("create_task", {"title": "Car service", "ref": "ab12cd34"}),   # no "result": the response itself
    ]


def test_a_two_turn_transcript_yields_each_result_once():
    call_1 = tools(("count_words", {"text": "a b"}))
    call_2 = tools(("count_words", {"text": "c d e"}))
    turn_1 = build_tool_turn(call_1, [(call_1.tool_calls[0], '{"words": 2}')])
    turn_2 = build_tool_turn(call_2, [(call_2.tool_calls[0], '{"words": 3}')])
    llm = ScriptedLLM([])
    llm.requests = [_request(_user("q")), _request(_user("q"), *turn_1), _request(_user("q"), *turn_1, *turn_2)]

    assert tool_results(llm) == [("count_words", {"words": 2}), ("count_words", {"words": 3})]


def test_first_user_text_is_the_first_user_message_of_the_first_request():
    llm = ScriptedLLM([])
    llm.requests = [_request(Message(role="model", parts=[MessagePart(tool_call=ToolCall(name="x", args={}))]),
                             _user("Car service: bring the logbook"))]
    assert first_user_text(llm) == "Car service: bring the logbook"

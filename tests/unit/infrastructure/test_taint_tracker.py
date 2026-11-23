"""TaintTracker: the flag only turns on, from the request and from every observed tool batch (EXTERNAL_TASK_QUEUE_RFC §5.3)."""
from src.domain.llm import Message, MessagePart, ToolCall
from src.domain.untrusted_text import wrap_untrusted
from src.infrastructure.delegation_engine import ToolResult
from src.infrastructure.taint_tracker import TaintTracker

WRAPPED = wrap_untrusted("a page", nonce="abcdef0123456789", store="email", ref="r", label="l")


def _call(tool, **args):
    return ToolCall(name=tool, args=args)


def _ok(name, text="fine"):
    return ToolResult(name=name, result_str=text)


def _user(*parts):
    return Message(role="user", parts=list(parts))


def test_a_new_tracker_is_clean():
    assert TaintTracker().tainted is False


def test_mark_taints():
    tracker = TaintTracker()
    tracker.mark()
    assert tracker.tainted is True


def test_a_clean_request_leaves_it_clean_and_a_chat_upload_taints_it():
    tracker = TaintTracker()
    tracker.observe_request([_user(MessagePart(text="hi"))], ["static"])
    assert tracker.tainted is False
    tracker.observe_request([_user(MessagePart(text="see file"),
                                   MessagePart(file_data={"uri": "gs://b/o", "mime_type": "image/png"}))])
    assert tracker.tainted is True


def test_a_wrapper_in_a_static_block_taints():
    tracker = TaintTracker()
    tracker.observe_request([_user(MessagePart(text="hi"))], [f"email_for_triage {{\n{WRAPPED}\n}}"])
    assert tracker.tainted is True


def test_batches_of_trusted_sources_keep_it_clean():
    tracker = TaintTracker()
    tracker.observe_batch(
        [_call("use_skill", name="a"), _call("delegate_to_specialist", intent="search_memory", query="q")],
        [_ok("use_skill"), _ok("delegate_to_specialist")])
    assert tracker.tainted is False


def test_one_off_allowlist_source_in_a_batch_taints():
    tracker = TaintTracker()
    tracker.observe_batch([_call("use_skill", name="a"), _call("delegate_to_specialist", intent="search_web")],
                          [_ok("use_skill"), _ok("delegate_to_specialist", "page text, unwrapped")])
    assert tracker.tainted is True


def test_a_wrapper_in_a_trusted_result_taints():
    tracker = TaintTracker()
    tracker.observe_batch([_call("workspace", command="view")], [_ok("workspace", WRAPPED)])
    assert tracker.tainted is True


def test_a_file_in_a_result_taints():
    tracker = TaintTracker()
    tracker.observe_batch([_call("use_skill")], [ToolResult(name="use_skill", result_str="x",
                                                            file_data={"uri": "u", "mime_type": "m"})])
    assert tracker.tainted is True


def test_a_batch_that_does_not_pair_up_taints():
    tracker = TaintTracker()
    tracker.observe_batch([_call("use_skill"), _call("use_skill")], [_ok("use_skill")])
    assert tracker.tainted is True


def test_the_flag_never_turns_off():
    tracker = TaintTracker()
    tracker.mark()
    tracker.observe_batch([_call("use_skill")], [_ok("use_skill")])
    tracker.observe_request([_user(MessagePart(text="hi"))])
    assert tracker.tainted is True


def test_a_flagged_saved_model_message_taints_and_an_unflagged_one_does_not():
    tracker = TaintTracker()
    tracker.observe_request([Message(role="model", parts=[MessagePart(text="earlier answer")])])
    assert tracker.tainted is False
    tracker.observe_request([Message(role="model", parts=[MessagePart(text="earlier answer")],
                                     execution_tainted=True)])
    assert tracker.tainted is True

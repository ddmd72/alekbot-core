"""Every key a `/worker` consumer requires is written by every producer of that task type, and every key a producer writes
is read by its consumer (P15 card I; the P9 residual "producer and consumer payload fields that do not match").

A static test over the sources (`tests/support/worker_payload_fields.py`): producers are the enqueue sites
`worker_triggers` finds, the Cloud Scheduler bodies and `GcpTaskQueue.enqueue_slack_event`; consumers are the branches
of `WorkerHandler._dispatch` and every function the payload is handed to. `drive_sync` is another service's task
(`worker_triggers.OTHER_SERVICE`) and is not compared; its payload builder is still parsed, so a new form fails here.
"""
import ast
import textwrap

import pytest

from src.domain.interactive_choice import InteractiveChoice
from tests.support import worker_payload_fields as wpf
from tests.support import worker_triggers as wt
from tests.support.worker_triggers import SLACK_EVENT_BRANCH, ExtractorError

# (task type, key) a producer writes and no consumer reads, by design: one line of reason each.
UNREAD_BY_DESIGN = {
    (SLACK_EVENT_BRANCH, "enqueued_at"): "a diagnostic timestamp of the queued event; no reader (gcp_task_queue.py:79)",
}

# (task type, key) a consumer reads and a producer does not write, by design: one line of reason each.
UNWRITTEN_BY_DESIGN = {
    ("video_generation_polling", "provider"): "video is Grok-only by decision (docs/10_rfcs/VIDEO_GENERATION_RFC.md "
                                              "decision #8); a second provider would be polled through Grok",
    (SLACK_EVENT_BRANCH, "task_type"): "the branch tests for its absence",
}

APPLICATION_GRAPH = wt.ROOT / "src" / "composition" / "application_graph.py"


@pytest.fixture(scope="module")
def producers():
    return wpf.producers()


@pytest.fixture(scope="module")
def consumers():
    return wpf.consumers()


# ----------------------------------------------------------------------------- the real sources

def test_every_dispatch_branch_has_a_consumer_mapping(consumers):
    assert set(consumers) == wt.dispatch_branches() | {SLACK_EVENT_BRANCH}
    # A branch that hands the payload on reaches the function that reads it; the payload-free handlers read nothing.
    handed_on = {name for name, c in consumers.items() if c.functions}
    assert {"agent_execution", "telegram_update", SLACK_EVENT_BRANCH, "interactive_choice", "email_indexing"} <= handed_on
    assert wpf.delegates_used() == set(wpf.DELEGATES), "a stale DELEGATES entry: _dispatch no longer calls it"


@pytest.mark.slow   # enqueue_sites() parses every src file: about 2.5 s (precedent: test_worker_triggers.py)
def test_every_producer_type_has_a_consumer_or_is_another_services(producers, consumers):
    types = {p.task_type for p in producers}
    assert types - set(consumers) == set(wt.OTHER_SERVICE)


@pytest.mark.slow   # enqueue_sites() parses every src file: about 2.5 s (precedent: test_worker_triggers.py)
def test_every_required_key_is_written_by_every_producer(producers, consumers):
    compared = [p for p in producers if p.task_type not in wt.OTHER_SERVICE]
    assert len(compared) >= 30, "the producer list shrank: the extractor no longer sees the sites"
    assert wpf.missing_required(compared, consumers) == set()


@pytest.mark.slow   # enqueue_sites() parses every src file: about 2.5 s (precedent: test_worker_triggers.py)
def test_every_written_key_is_read_by_its_consumer(producers, consumers):
    compared = [p for p in producers if p.task_type not in wt.OTHER_SERVICE]
    unread = set(wpf.unread_written(compared, consumers))
    # Both ways: an unread key not listed, and a listed key that is now read or no longer written.
    assert unread == set(UNREAD_BY_DESIGN)


@pytest.mark.slow   # enqueue_sites() parses every src file: about 2.5 s (precedent: test_worker_triggers.py)
def test_every_read_key_is_written_by_every_producer(producers, consumers):
    """A dropped field fails here even when its consumer reads it with a default and never refuses without it."""
    compared = [p for p in producers if p.task_type not in wt.OTHER_SERVICE]
    unwritten = set(wpf.unwritten_read(compared, consumers))
    # Both ways: an unwritten read not listed, and a listed key that is now written by every producer or no longer read.
    assert unwritten == set(UNWRITTEN_BY_DESIGN)


def test_the_generic_queue_adds_only_the_task_type():
    assert wpf.generic_envelope() == {"task_type"}


def test_every_delegate_is_the_function_production_attaches():
    """The three hand-mapped consumers are what the code binds: the attach calls of the builder, and the constructor
    annotation of WorkerHandler for the agent worker."""
    attached = {}
    for node in ast.walk(ast.parse(APPLICATION_GRAPH.read_text())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("attach_slack_worker", "attach_telegram_worker")):
            [arg] = node.args
            attached[node.func.attr] = arg.attr
    assert attached == {"attach_slack_worker": wpf.DELEGATES["self._slack_worker"][0].qualname.split(".")[1],
                        "attach_telegram_worker": wpf.DELEGATES["self._telegram_worker"][0].qualname.split(".")[1]}
    init = wpf._find_function(ast.parse(wt.WORKER_HANDLER.read_text()), "WorkerHandler.__init__", "worker_handler.py")
    [annotation] = [a.annotation for a in init.args.args if a.arg == "agent_worker_handler"]
    bound = [n for n in ast.walk(init) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "self._agent_worker"]
    assert ast.unparse(annotation) == wpf.DELEGATES["self._agent_worker.handle_task"][0].qualname.split(".")[0]
    assert [ast.unparse(n.value) for n in bound] == ["agent_worker_handler"]


def test_the_queued_slack_event_reads_the_requeue_count_in_the_followed_method(consumers):
    slack = consumers[SLACK_EVENT_BRANCH]
    assert slack.required == {"event", "session_id"}
    assert "busy_requeues" in slack.read
    assert any(f.endswith("HTTPModeAdapter._requeue_busy") for f in slack.functions)


def test_interactive_choice_requires_every_model_field(consumers):
    assert consumers["interactive_choice"].required == set(InteractiveChoice.model_fields)


# ----------------------------------------------------------------------------- self-tests: producer forms

def _site(source: str, line: int, task_type: str = "t"):
    return wpf.site_keys(ast.parse(textwrap.dedent(source)), line, task_type, f"inline.py:{line}")


def test_a_dict_literal_with_a_task_type_is_read_with_its_nested_keys():
    keys = _site('payload = {"task_type": "t", "user_id": u, "context": {"job_id": j}, "extra": ctx}\n', 1)
    assert keys.keys == {"task_type", "user_id", "context", "context.job_id", "extra"}
    assert "context" not in keys.opaque and "extra" in keys.opaque


def test_enqueue_worker_task_positional_and_keyword_payloads_get_the_task_type():
    positional = _site('q.enqueue_worker_task("t", {"user_id": u})\n', 1)
    keyword = _site('q.enqueue_worker_task(task_type="t", payload={"note_id": n}, deadline_seconds=9)\n', 1)
    assert positional.keys == {"task_type", "user_id"}
    assert keyword.keys == {"task_type", "note_id"}


def test_review_payload_is_evaluated():
    keys = _site('q.enqueue_worker_task(FACT_REVIEW_TASK, review_payload(a, b, "next"))\n', 1, "fact_review")
    assert keys.keys == {"task_type", "user_id", "account_id", "mode"}


def test_scope_payload_is_evaluated_with_and_without_a_nudge():
    # MULTI_DRIVE_RFC §8: the drive_sync payload is per connection, {user_id, connection_id} (+ the nudge)
    plain = _site("q.enqueue_worker_task(DRIVE_SYNC_TASK, scope_payload(s))\n", 1, "drive_sync")
    nudged = _site("q.enqueue_worker_task(DRIVE_SYNC_TASK, scope_payload(s, n))\n", 1, "drive_sync")
    assert plain.keys == {"task_type", "user_id", "connection_id"}
    assert nudged.keys == {"task_type", "user_id", "connection_id", "nudge", "nudge.mutation_id", "nudge.item_ids",
                           "nudge.attempt"}


def test_to_payload_of_a_choice_is_the_model_fields():
    keys = _site("q.enqueue_worker_task(INTERACTIVE_CHOICE_TASK, choice.to_payload())\n", 1, "interactive_choice")
    assert keys.keys == set(InteractiveChoice.model_fields) | {"task_type"}


def test_the_slack_event_producer_is_the_enqueue_slack_event_literal():
    slack = wpf.slack_event_producer()
    assert slack.task_type == SLACK_EVENT_BRANCH
    assert slack.keys == {"event", "session_id", "busy_requeues", "enqueued_at"}


@pytest.mark.parametrize("source", [
    'q.enqueue_worker_task("t", build_payload(u))\n',          # an unknown builder
    'q.enqueue_worker_task("t", payload)\n',                    # a variable
    'q.enqueue_worker_task("t", {**base, "user_id": u})\n',     # a spread
    'q.enqueue_worker_task("t")\n',                             # no payload at all
    'q.enqueue_worker_task("t", other.to_payload())\n',         # to_payload of a type with no model
    'x = {"task_type": "t", k: v}\n',                           # a non-constant key
])
def test_an_unknown_producer_form_fails_loudly_naming_the_line(source):
    with pytest.raises(ExtractorError, match="inline.py:1"):
        _site(source, 1)


# ----------------------------------------------------------------------------- self-tests: consumer forms and the required rule

def _consumer(body: str, extra: str = ""):
    source = "class H:\n    def handle(self, payload):\n" + textwrap.indent(textwrap.dedent(body), " " * 8) + extra
    return wpf.function_consumer(source, "H.handle")


def test_a_falsity_test_under_a_4xx_return_is_required():
    c = _consumer("""
        user_id = payload.get("user_id")
        account_id = payload["account_id"]
        if not user_id or self._svc is None:
            return {}, 400
        if account_id is None:
            return {}, 503
        return {}, 200
    """)
    assert c.required == {"user_id", "account_id"}


def test_a_falsity_test_under_a_2xx_return_is_not_required():
    c = _consumer("""
        mode = payload.get("mode")
        if mode is None:
            return {}, 200
        if not mode:
            return {"ok": True}, 204
        return {}, 200
    """)
    assert c.read == {"mode"} and c.required == set()


def test_not_of_a_conjunction_requires_each_name_and_or_default_binds():
    c = _consumer("""
        a = payload.get("a") or ""
        b = payload.get("b") or ""
        if not (a and b):
            return {}, 400
    """)
    assert c.required == {"a", "b"}


def test_a_truthy_test_is_optional_and_a_nested_get_is_dotted():
    c = _consumer("""
        user_id = payload.get("context", {}).get("user_id", "")
        if user_id:
            return {}, 500
        flag = "event" in payload
    """)
    assert c.read == {"context.user_id", "event"} and c.required == set()


def test_a_payload_handed_to_a_method_of_the_same_class_is_followed():
    c = _consumer("""
        return self._more(1, payload)
    """, extra='\n    def _more(self, n, body):\n        if not body.get("k"):\n            return {}, 400\n'
               '        x = body.get("k")\n        if not x:\n            return {}, 400\n')
    assert c.read == {"k"} and c.required == {"k"}
    assert c.functions == ("inline.py::H.handle", "inline.py::H._more")


@pytest.mark.parametrize("body", [
    "return helper(payload)\n",            # a function the extractor does not follow
    "return self._svc.run(payload)\n",     # another object's method
    "return dict(**payload)\n",            # every field copied
    "return list(payload.items())\n",      # every field read
    "key = 'k'\nreturn payload.get(key)\n",  # a key it cannot resolve
])
def test_an_unknown_consumer_form_fails_loudly(body):
    with pytest.raises(ExtractorError):
        _consumer(body)


def test_a_subscript_read_is_required():
    c = _consumer("""
        job = payload["job_id"]
        note = payload.get("note_id", "")
        return {}, 200
    """)
    assert c.read == {"job_id", "note_id"} and c.required == {"job_id"}


def test_a_raise_and_an_http_status_name_are_refusals():
    c = _consumer("""
        a = payload.get("a")
        b = payload.get("b")
        c = payload.get("c")
        if not a:
            raise ValueError("no a")
        if b is None:
            return {}, HTTPStatus.BAD_REQUEST
        if not c:
            return {}, HTTPStatus.OK
    """)
    assert c.required == {"a", "b"}


def test_an_unknown_status_expression_fails_loudly():
    with pytest.raises(ExtractorError, match="status"):
        _consumer("""
            a = payload.get("a")
            if not a:
                return {}, self._status
        """)


@pytest.mark.parametrize("alias", ["body = payload", "body = payload or {}", "body: dict = payload",
                                   "body = payload if ok else {}"])
def test_an_alias_of_the_payload_fails_loudly(alias):
    with pytest.raises(ExtractorError, match="alias"):
        _consumer(alias + "\nreturn body.get('k'), 200\n")


def test_the_comparison_names_a_read_key_a_producer_does_not_write():
    consumer = wpf.Consumer(frozenset({"task_type", "mode", "context.user_id"}), frozenset(), ())
    good = wpf.Producer("t", "a.py:1", frozenset({"task_type", "mode", "context"}), frozenset({"context"}))
    bad = wpf.Producer("t", "b.py:2", frozenset({"task_type", "context"}), frozenset())
    assert wpf.unwritten_read([good, bad], {"t": consumer}) == {("t", "mode"): {"b.py:2"},
                                                                ("t", "context.user_id"): {"b.py:2"}}


def test_a_dispatch_branch_that_calls_no_handler_fails_loudly():
    source = textwrap.dedent("""
        class WorkerHandler:
            async def _dispatch(self, payload, headers):
                task_type = payload.get("task_type")
                if task_type == "known":
                    return await self._handle_known(payload)
                elif task_type == "orphan":
                    return {}, 200
                return {}, 400

            async def _handle_known(self, payload):
                return {}, 200
    """)
    with pytest.raises(ExtractorError, match="orphan branch calls no handler"):
        wpf.consumers(source=source, delegates={})


def test_a_dispatch_preamble_read_counts_for_every_branch():
    source = textwrap.dedent("""
        class WorkerHandler:
            async def _dispatch(self, payload, headers):
                task_type = payload.get("task_type")
                if task_type == "a":
                    return await self._handle_a(payload)
                elif task_type == "b":
                    return await self._handle_b()
                return {}, 400

            async def _handle_a(self, payload):
                if not payload.get("x"):
                    return {}, 400
                job = payload.get("x")
                if not job:
                    return {}, 400
                return {}, 200

            async def _handle_b(self):
                return {}, 200
    """)
    found = wpf.consumers(source=source, delegates={})
    assert found["a"].read == {"task_type", "x"} and found["a"].required == {"x"}
    assert found["b"].read == {"task_type"} and found["b"].required == set()


def test_the_comparison_names_a_missing_required_key_and_an_unread_key():
    consumer = wpf.Consumer(frozenset({"task_type", "user_id", "context.user_id"}), frozenset({"user_id"}), ())
    good = wpf.Producer("t", "a.py:1", frozenset({"task_type", "user_id", "context"}), frozenset({"context"}))
    bad = wpf.Producer("t", "b.py:2", frozenset({"task_type", "extra"}), frozenset())
    assert wpf.missing_required([good, bad], {"t": consumer}) == {("t", "b.py:2", "user_id")}
    assert wpf.unread_written([good, bad], {"t": consumer}) == {("t", "extra"): {"b.py:2"}}

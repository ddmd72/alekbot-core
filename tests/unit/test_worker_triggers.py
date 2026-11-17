"""Every `/worker` task type is sent by something and registered in `WorkerHandler`'s task registry (P9 design §5c).

Triggers: the Cloud Scheduler jobs the deploy creates and the enqueue sites in `src/`. Branches: the keys of the
`self._tasks` registry `_dispatch` looks task types up in: the handler's own entries and the runners `build_worker_tasks`
registers. A trigger with no branch answers 400 `unknown task_type` in
production; a branch with no trigger is dead or manual-only, and is named in `MANUAL_ONLY` with a reason (empty today).
"""
import json

import pytest
import yaml

from tests.support import worker_triggers as wt

BUILD = wt.ROOT / "cloudbuild-dev.yaml"


def test_the_deploy_runs_the_scheduler_script_and_not_the_legacy_one():
    steps = yaml.safe_load(BUILD.read_text())["steps"]
    commands = [" ".join(map(str, s.get("args", []))) for s in steps]
    assert any(wt.SCHEDULER_SCRIPT.name in c for c in commands)
    assert not any(wt.LEGACY_SCHEDULER_SCRIPT in c for c in commands)


def test_every_job_has_an_update_and_a_create_with_the_same_body_and_uri():
    jobs = wt.scheduler_jobs()
    assert jobs, "no scheduler job parsed"
    for job in jobs.values():
        assert job.update and job.create, f"{job.name}: needs both the update and the create form"
        assert job.update.get("uri") == job.create.get("uri"), job.name
        assert job.update.get("message-body") == job.create.get("message-body"), job.name


def test_the_worker_jobs_post_to_worker_with_a_task_type():
    posting = [j for j in wt.scheduler_jobs().values() if "message-body" in j.create]
    # SECRETS_AT_REST_RFC §6.2 R1, plan D-P4: + reconnect_notice_sweep;
    # email index v2 slice 10: - repair_email_embeddings
    assert len(posting) == 11
    for job in posting:
        assert job.create["uri"].endswith("/worker"), job.name
        assert job.create["http-method"] == "POST", job.name
        assert "task_type" in json.loads(job.create["message-body"]), job.name


@pytest.mark.slow
def test_every_trigger_has_a_branch_and_every_branch_a_trigger():
    triggers = wt.trigger_types()
    branches = wt.dispatch_branches()
    missing_branch = triggers - set(wt.OTHER_SERVICE) - branches
    missing_trigger = branches - triggers - set(wt.MANUAL_ONLY)
    assert not missing_branch, f"sent but not dispatched: {sorted(missing_branch)}"
    assert not missing_trigger, f"dispatched but never sent: {sorted(missing_trigger)}"


@pytest.mark.slow
def test_the_exemption_tables_are_not_stale():
    assert set(wt.OTHER_SERVICE) <= wt.trigger_types()
    assert not set(wt.OTHER_SERVICE) & wt.dispatch_branches()
    assert set(wt.MANUAL_ONLY) <= wt.dispatch_branches()
    assert wt.MANUAL_ONLY == {}


@pytest.mark.slow
def test_the_trigger_set_is_the_eleven_scheduler_types_and_the_enqueue_only_ones():
    scheduler = set(wt.scheduler_task_types())
    enqueue_only = {s.task_type for s in wt.enqueue_sites()} - scheduler
    assert len(scheduler) == 11   # email index v2 slice 10: repair_email_embeddings is no longer scheduled
    assert "repair_email_embeddings" not in scheduler
    assert "reconnect_notice_sweep" in scheduler   # SECRETS_AT_REST_RFC §6.2 R1, plan D-P4: a scheduled worker task
    assert enqueue_only == {
        "agent_execution", "video_generation_polling", "email_indexing", "consolidation", "companion_consolidation",
        "telegram_update", "execute_reminder", "setup_microsoft_todo", "reindex_task_list", "renew_task_subscriptions",
        "daily_email_review", "deep_research_failed", "deep_research_webhook_event", "drive_sync", "fact_review",
        "interactive_choice",
    }


# ----------------------------------------------------------------------------- the extractor's own tests

def _types(source: str):
    return sorted(site.task_type for site in wt.sites_in_source(source))


def test_a_positional_literal_is_a_trigger():
    assert _types("async def f(q):\n    await q.enqueue_worker_task('a_task', {})") == ["a_task"]


def test_a_keyword_literal_is_a_trigger():
    assert _types("async def f(q):\n    await q.enqueue_worker_task(task_type='b_task', payload={})") == ["b_task"]


def test_a_dict_literal_with_a_task_type_key_is_a_trigger():
    assert _types("def f():\n    return {'task_type': 'c_task', 'x': 1}") == ["c_task"]


def test_a_known_constant_name_resolves_to_its_runtime_value():
    assert _types("async def f(q):\n    await q.enqueue_worker_task(DRIVE_SYNC_TASK, {})") == ["drive_sync"]


def test_a_forwarded_parameter_is_skipped():
    source = "async def f(q, task_type):\n    await q.enqueue_worker_task(task_type, {})\n    return {'task_type': task_type}"
    assert _types(source) == []


def test_a_parameter_of_an_outer_function_is_a_forwarder_inside_a_nested_one():
    source = "def f(task_type):\n    def g():\n        return {'task_type': task_type}\n    return g"
    assert _types(source) == []


@pytest.mark.parametrize("source", [
    "async def f(q):\n    await q.enqueue_worker_task(Kinds.A, {})",
    "async def f(q):\n    await q.enqueue_worker_task(f'x_{1}', {})",
    "async def f(q):\n    await q.enqueue_worker_task(make(), {})",
    "async def f(q):\n    await q.enqueue_worker_task(unknown_name, {})",
    "def f():\n    return {'task_type': Kinds.A}",
    "def f(payload):\n    payload['task_type'] = 'late'",
    "def f():\n    return dict(task_type='late')",
    "async def f(q):\n    await q.enqueue_worker_task()",
])
def test_a_form_the_extractor_does_not_understand_fails_loudly(source):
    with pytest.raises(wt.ExtractorError):
        wt.sites_in_source(source)


_WRAPPERS = """
def _payload_task(method):
    async def task(payload, headers):
        return await method(payload)
    return task


def _no_input_task(method):
    async def task(payload, headers):
        return await method()
    return task
"""


def _registry(entries: str) -> str:
    return _WRAPPERS + f"\n\nclass H:\n    def __init__(self):\n        self._tasks = {{{entries}}}\n"


def test_the_registry_parser_reads_literals_and_the_slack_key():
    source = _registry('"one": _payload_task(self._one), "two": _no_input_task(self._two), _QUEUED_SLACK_EVENT: self._slack')
    assert wt.branches_in_source(source) == {"one", "two", wt.SLACK_EVENT_BRANCH}


def test_the_registry_parser_reads_a_known_constant_as_its_runtime_value():
    assert wt.branches_in_source(_registry("FACT_REVIEW_TASK: _payload_task(self._review)")) == {"fact_review"}


def test_the_review_task_constant_at_an_enqueue_site_resolves_to_its_runtime_value():
    assert _types("async def f(q):\n    await q.enqueue_worker_task(FACT_REVIEW_TASK, {})") == ["fact_review"]


@pytest.mark.parametrize("comparison", [
    'task_type in ("one", "two")',
    '"one" == task_type',
    "task_type == CONST",
    'payload.get("task_type") == "one"',
])
def test_a_comparison_form_the_dispatch_parser_does_not_know_fails(comparison):
    source = f"class H:\n    async def _dispatch(self, payload, headers):\n        task_type = 1\n        if {comparison}:\n            return 1\n"
    with pytest.raises(wt.ExtractorError):
        wt.branches_in_source(source)


@pytest.mark.parametrize("key", ["Kinds.A", 'f"x_{1}"', "make()", "CONST"])
def test_a_registry_key_form_the_parser_does_not_know_fails(key):
    with pytest.raises(wt.ExtractorError, match="registry key"):
        wt.branches_in_source(_registry(f"{key}: _payload_task(self._one)"))


@pytest.mark.parametrize("entries", [
    '"one": self._svc.run',                                   # another object's method
    '"one": lambda payload, headers: None',                   # not a method of the handler
    '"one": _payload_task(self._one, 1)',                     # a wrapper call with more than the method
    '"one": _unknown_task(self._one)',                        # a wrapper the source does not define
    '"one": _payload_task(self._one), "one": self._two',     # a task type registered twice
    '**base, "one": self._one',                               # a spread
])
def test_a_registry_entry_form_the_parser_does_not_know_fails(entries):
    with pytest.raises(wt.ExtractorError):
        wt.branches_in_source(_registry(entries))


def test_a_source_without_one_registry_literal_fails():
    with pytest.raises(wt.ExtractorError, match="self._tasks"):
        wt.branches_in_source("class H:\n    def __init__(self):\n        self._tasks = dict(one=1)\n")


_SWAPPED = "\n\ndef _swapped_task(method):\n    async def task(payload, headers):\n        return await method(headers, payload)\n"


def test_the_payload_position_is_read_from_the_wrapper_body():
    source = _registry('"a": _payload_task(self._a), "b": _no_input_task(self._b), "c": self._c, '
                       '"d": _swapped_task(self._d)') + _SWAPPED
    assert wt.registry_in_source(source) == {"a": wt.Entry("_a", 0), "b": wt.Entry("_b", None), "c": wt.Entry("_c", 0),
                                             "d": wt.Entry("_d", 1)}


def test_a_wrapper_that_does_not_call_its_method_once_with_its_parameters_fails():
    source = _registry('"a": _payload_task(self._a)').replace("return await method(payload)",
                                                              "return await method(payload.get('k'))")
    with pytest.raises(wt.ExtractorError, match="_payload_task"):
        wt.registry_in_source(source)


def test_a_scheduler_script_form_that_is_not_http_fails(tmp_path):
    script = tmp_path / "jobs.sh"
    script.write_text("gcloud scheduler jobs create pubsub nope --topic=t\n")
    with pytest.raises(wt.ExtractorError):
        wt.scheduler_jobs(script)


# ----------------------------------------------------------------------------- the composed runners

def _merged_registry(own: str, merge: str = "**own, **tasks", params: str = "tasks") -> str:
    return _WRAPPERS + (f"\n\nclass H:\n    def __init__(self, {params}):\n        own = {{{own}}}\n"
                        f"        self._tasks = {{{merge}}}\n")


def test_a_registry_merged_from_an_own_literal_and_a_parameter_reads_the_own_entries_and_composes():
    own, composes = wt.registry_parts(_merged_registry('"one": _payload_task(self._one), _QUEUED_SLACK_EVENT: self._slack'))
    assert own == {"one": wt.Entry("_one", 0), wt.SLACK_EVENT_BRANCH: wt.Entry("_slack", 0)}
    assert composes is True
    assert wt.registry_parts(_registry('"one": self._one')) == ({"one": wt.Entry("_one", 0)}, False)


@pytest.mark.parametrize("merge, params", [
    ("**own, **other", "tasks"),             # a name neither a parameter nor a local literal
    ("**own, **tasks, **more", "tasks, more"),  # two composed parameters
    ("**own, **own2", "tasks"),              # a local bound to something other than one dict literal
])
def test_a_registry_spread_the_parser_does_not_know_fails(merge, params):
    source = _merged_registry('"one": self._one', merge, params).replace("        own = ", "        own2 = make()\n        own = ")
    with pytest.raises(wt.ExtractorError):
        wt.registry_parts(source)


def test_an_own_literal_that_spreads_again_fails():
    source = _merged_registry('**tasks, "one": self._one')
    with pytest.raises(wt.ExtractorError, match="spread"):
        wt.registry_parts(source)


_SETUP = '''
from ..services.disabled_task import DisabledTask
from ..services.execute_reminder_task import ExecuteReminderTask


def build_worker_tasks(*, dep):
    tasks = {}
    if dep is None:
        tasks["a_task"] = DisabledTask("off", 501, {})
    else:
        tasks["a_task"] = ExecuteReminderTask(dep=dep)
    tasks[FACT_REVIEW_TASK] = ExecuteReminderTask(dep=dep)
    return tasks
'''
_SERVICES = wt.ROOT / "src" / "services"


def test_the_runner_reader_names_every_class_a_task_type_may_run():
    assert wt.runners_in_source(_SETUP) == {
        "a_task": (wt.Runner(_SERVICES / "disabled_task.py", "DisabledTask"),
                   wt.Runner(_SERVICES / "execute_reminder_task.py", "ExecuteReminderTask")),
        "fact_review": (wt.Runner(_SERVICES / "execute_reminder_task.py", "ExecuteReminderTask"),),
    }


def test_a_dict_literal_binding_is_read_too():
    source = _SETUP.replace("    tasks = {}\n", '    tasks = {"b_task": DisabledTask("off", 501, {})}\n')
    assert set(wt.runners_in_source(source)) == {"a_task", "b_task", "fact_review"}


@pytest.mark.parametrize("old, new", [
    ('tasks["a_task"] = DisabledTask', "tasks[name] = DisabledTask"),            # a key it cannot resolve
    ('tasks["a_task"] = DisabledTask("off", 501, {})', 'tasks["a_task"] = make()'),   # a runner not built by an imported class
    ('tasks["a_task"] = DisabledTask', 'tasks["a_task"] = Unknown'),             # a class the module does not import
    ("    return tasks\n", "    tasks.update(more)\n    return tasks\n"),          # a fill the reader does not see
    ("    return tasks\n", "    register(tasks)\n    return tasks\n"),             # the registry handed to a call
    ("    return tasks\n", "    tasks = {}\n    return tasks\n"),                  # bound twice
    ("    return tasks\n", "    return dict(tasks)\n"),                           # returns no local name
    ("    tasks = {}\n", "    tasks = make()\n"),                                # bound to something other than a dict literal
    ("from ..services.disabled_task", "from ..services.no_such_module"),          # an import that is no file
    ("def build_worker_tasks", "def build_other_tasks"),                         # no builder
])
def test_a_runner_form_the_reader_does_not_know_fails(old, new):
    assert old in _SETUP
    with pytest.raises(wt.ExtractorError):
        wt.runners_in_source(_SETUP.replace(old, new))


def test_the_real_registry_composes_and_its_composed_task_types_are_dispatch_branches():
    own, composes = wt.registry_parts(wt.WORKER_HANDLER.read_text())
    composed = wt.composed_runners()
    assert composes and composed, "the handler merges composed runners and build_worker_tasks registers some"
    assert set(composed) <= wt.dispatch_branches()
    assert not set(composed) & set(own)
    assert set(own) - {wt.SLACK_EVENT_BRANCH} | set(composed) == wt.dispatch_branches()

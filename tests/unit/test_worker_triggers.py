"""Every `/worker` task type is sent by something and handled by `WorkerHandler._dispatch` (P9 design §5c).

Triggers: the Cloud Scheduler jobs the deploy creates and the enqueue sites in `src/`. Branches: the `if/elif` chain of
`_dispatch`. A trigger with no branch answers 400 `unknown task_type` in production; a branch with no trigger is dead
or manual-only, and is named in `MANUAL_ONLY` with a reason (empty today).
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
    assert len(posting) == 12   # SECRETS_AT_REST_RFC §6.2 R1, plan D-P4: + reconnect_notice_sweep
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
def test_the_trigger_set_is_the_twelve_scheduler_types_and_the_enqueue_only_ones():
    scheduler = set(wt.scheduler_task_types())
    enqueue_only = {s.task_type for s in wt.enqueue_sites()} - scheduler
    assert len(scheduler) == 12
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


_DISPATCH = """
class H:
    async def _dispatch(self, payload, headers):
        task_type = payload.get("task_type")
        if task_type == "one":
            return 1
        elif task_type == "two":
            return 2
        elif task_type is None and "event" in payload and "session_id" in payload:
            return 3
        return 4
"""


def test_the_dispatch_parser_reads_literals_and_the_slack_branch():
    assert wt.branches_in_source(_DISPATCH) == {"one", "two", wt.SLACK_EVENT_BRANCH}


def test_the_dispatch_parser_reads_a_known_constant_as_its_runtime_value():
    source = ("class H:\n    async def _dispatch(self, payload, headers):\n        task_type = 1\n"
              "        if task_type == FACT_REVIEW_TASK:\n            return 1\n")
    assert wt.branches_in_source(source) == {"fact_review"}


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


def test_a_scheduler_script_form_that_is_not_http_fails(tmp_path):
    script = tmp_path / "jobs.sh"
    script.write_text("gcloud scheduler jobs create pubsub nope --topic=t\n")
    with pytest.raises(wt.ExtractorError):
        wt.scheduler_jobs(script)

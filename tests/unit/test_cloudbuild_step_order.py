"""cloudbuild-dev.yaml deploys the main service before anything of the drive index (USER_DRIVE_INDEXING_RFC
§12.5): a failing drive step (the queue's one-time IAM grant missing, an indexer that does not start) must never
block a hotfix of the bot itself."""
from pathlib import Path

import yaml

_BUILD = Path(__file__).resolve().parents[2] / "cloudbuild-dev.yaml"


def _steps():
    return [" ".join(step.get("args", [])) for step in yaml.safe_load(_BUILD.read_text())["steps"]]


def _index(predicate):
    hits = [i for i, text in enumerate(_steps()) if predicate(text)]
    assert len(hits) == 1, hits
    return hits[0]


def test_drive_steps_run_after_the_main_service_is_deployed_and_public():
    main_deploy = _index(lambda s: s.startswith("run deploy alek-bot-dev "))
    main_iam = _index(lambda s: s.startswith("run services add-iam-policy-binding alek-bot-dev "))
    queue = _index(lambda s: "gcloud tasks queues create drive-sync-dev" in s)
    indexer = _index(lambda s: s.startswith("run deploy alek-bot-indexer-dev "))
    indexer_iam = _index(lambda s: s.startswith("run services add-iam-policy-binding alek-bot-indexer-dev "))
    assert main_deploy < main_iam < queue < indexer < indexer_iam


def test_the_queue_exists_before_the_scheduler_job_that_feeds_it():
    queue = _index(lambda s: "gcloud tasks queues create drive-sync-dev" in s)
    scheduler = _index(lambda s: "alek-bot-dev-start-drive-sync" in s)
    assert queue < scheduler

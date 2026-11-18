"""scripts/infrastructure/artifact_registry_retention.py: the plan, the safety check and the dry-run / apply flows."""
import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.infrastructure import artifact_registry_retention as ar

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
PROJECT = "demo-project"
REGION = "us-central1"
PKG = ar.PackageRef("us", "gcr.io", "svc-main")
PKG_PATH = PKG.path(PROJECT)


def _v(name: str, age_days: float, tags: tuple[str, ...] = (), size: int = 100) -> ar.Version:
    return ar.Version(f"sha256:{name}", NOW - timedelta(days=age_days), tags, size)


def _digests(versions) -> list[str]:
    return [v.digest.removeprefix("sha256:") for v in versions]


# ---- plan ------------------------------------------------------------------------------------------------------------

def test_keep_five_floor_when_all_versions_are_old():
    versions = [_v(f"v{i}", 100 + i) for i in range(8)]
    plan = ar.plan_package(versions, NOW)
    assert _digests(plan.keep) == ["v0", "v1", "v2", "v3", "v4"]
    assert _digests(plan.delete) == ["v5", "v6", "v7"]


def test_fourteen_day_boundary():
    versions = [_v(f"n{i}", 1 + i) for i in range(5)]  # the floor is taken by five fresh ones
    versions += [_v("exactly", 14), _v("under", 13.9), _v("over", 14.01)]
    plan = ar.plan_package(versions, NOW)
    assert "exactly" in _digests(plan.keep)
    assert "under" in _digests(plan.keep)
    assert _digests(plan.delete) == ["over"]


def test_fewer_than_five_versions_deletes_nothing():
    plan = ar.plan_package([_v("a", 400), _v("b", 300), _v("c", 200)], NOW)
    assert plan.delete == ()
    assert len(plan.keep) == 3


def test_old_latest_tag_within_the_five_is_kept():
    versions = [_v("latest-one", 90, ("latest",))] + [_v(f"x{i}", 100 + i) for i in range(10)]
    plan = ar.plan_package(versions, NOW)
    assert "latest-one" in _digests(plan.keep)
    assert len(plan.keep) == 5


def test_order_is_by_create_time_not_list_order():
    shuffled = [_v("oldest", 300), _v("newest", 20), _v("mid", 100), _v("older", 200), _v("old", 150), _v("ancient", 500)]
    plan = ar.plan_package(shuffled, NOW)
    assert _digests(plan.keep) == ["newest", "mid", "old", "older", "oldest"]
    assert _digests(plan.delete) == ["ancient"]


def test_plan_reports_bytes_and_oldest_kept():
    plan = ar.plan_package([_v(f"v{i}", 100 + i, size=10) for i in range(7)], NOW)
    assert plan.freed_bytes == 20
    assert plan.oldest_kept.digest == "sha256:v4"


def test_parse_time_handles_nanoseconds():
    assert ar.parse_time("2026-11-01T10:00:00.123456789Z") == datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc)


def test_policy_document_has_delete_and_keep():
    policy = ar.cleanup_policy_document()
    assert policy[0]["action"] == {"type": "Delete"} and policy[0]["condition"]["olderThan"] == "1209600s"
    assert policy[1]["action"] == {"type": "Keep"} and policy[1]["mostRecentVersions"]["keepCount"] == 5


# ---- image refs -----------------------------------------------------------------------------------------------------

def test_parse_image_ref_forms():
    ref = ar.parse_image_ref("gcr.io/demo-project/svc-main@sha256:abc")
    assert ref.package == PKG and ref.digest == "sha256:abc" and ref.tag is None
    tagged = ar.parse_image_ref("gcr.io/demo-project/svc-main:latest")
    assert tagged.tag == "latest" and tagged.digest is None
    pkg_dev = ar.parse_image_ref("europe-docker.pkg.dev/demo-project/apps/team/svc-x:v1")
    assert pkg_dev.package == ar.PackageRef("europe", "apps", "team/svc-x")
    assert ar.parse_image_ref("docker.io/library/nginx") is None


# ---- safety ---------------------------------------------------------------------------------------------------------

def _service(name="svc-main", traffic=("rev-3",)):
    return {"metadata": {"name": name},
            "spec": {"template": {"spec": {"containers": [{"image": "gcr.io/demo-project/svc-main:latest"}]}}},
            "status": {"traffic": [{"revisionName": r, "percent": 100 // len(traffic)} for r in traffic]}}


def _rev(name, stamp, digest):
    return {"metadata": {"name": name, "creationTimestamp": stamp},
            "spec": {"containers": [{"image": "gcr.io/demo-project/svc-main:latest"}]},
            "status": {"imageDigest": f"gcr.io/demo-project/svc-main@sha256:{digest}"}}


def _revisions():
    # newest first by stamp: rev-6 .. rev-1; traffic is on the old rev-1 to prove serving beats position
    return [_rev(f"rev-{i}", f"2026-11-{10 + i:02d}T00:00:00Z", f"d{i}") for i in range(1, 7)]


def _plan_deleting(*names):
    return {PKG: ar.Plan(keep=(), delete=tuple(_v(n, 99) for n in names))}


def test_serving_digest_in_delete_set_is_refused():
    protected = ar.collect_protected([_service(traffic=("rev-1",))], {"svc-main": _revisions()}, [])
    safety = ar.check_safety(_plan_deleting("d1"), protected)
    assert len(safety.violations) == 1 and "serves traffic" in safety.violations[0]


def test_three_newest_revisions_are_protected():
    protected = ar.collect_protected([_service(traffic=("rev-6",))], {"svc-main": _revisions()}, [])
    for name in ("d6", "d5", "d4"):
        assert ar.check_safety(_plan_deleting(name), protected).violations
    assert not ar.check_safety(_plan_deleting("d3"), protected).violations


def test_job_image_digest_is_refused():
    job = {"metadata": {"name": "job-research"},
           "spec": {"template": {"spec": {"template": {"spec": {"containers": [
               {"image": "gcr.io/demo-project/svc-main@sha256:jobdigest"}]}}}}}}
    protected = ar.collect_protected([], {}, [job])
    safety = ar.check_safety(_plan_deleting("jobdigest"), protected)
    assert safety.violations and "job-research" in safety.violations[0]


def test_job_image_tag_protects_the_tagged_version():
    job = {"metadata": {"name": "job-research"},
           "spec": {"template": {"spec": {"template": {"spec": {"containers": [
               {"image": "gcr.io/demo-project/svc-main:latest"}]}}}}}}
    protected = ar.collect_protected([], {}, [job])
    plans = {PKG: ar.Plan(keep=(), delete=(_v("whatever", 99, ("latest",)),))}
    assert ar.check_safety(plans, protected).violations


def test_old_revision_digest_is_a_warning_not_a_refusal():
    protected = ar.collect_protected([_service(traffic=("rev-6",))], {"svc-main": _revisions()}, [])
    safety = ar.check_safety(_plan_deleting("d1"), protected)
    assert safety.violations == ()
    assert len(safety.warnings) == 1 and "svc-main/rev-1" in safety.warnings[0]


# ---- flows over a fake gcloud ---------------------------------------------------------------------------------------

class FakeGcloud:
    """Canned gcloud: returns JSON by command prefix, records every call, can fail chosen deletes."""

    def __init__(self, versions, revisions, traffic=("rev-6",), fail_delete=(), flip_after_policy=None):
        self.calls: list[list[str]] = []
        self.versions = versions
        self.revisions = revisions
        self.traffic = traffic
        self.fail_delete = set(fail_delete)
        self.flip_after_policy = flip_after_policy  # traffic moved to this revision once the policy is set

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        head = " ".join(args[:3])
        if head == "run services list":
            return json.dumps([_service(traffic=self.traffic)])
        if head == "run jobs list":
            return "[]"
        if head == "run revisions list":
            return json.dumps(self.revisions)
        if head == "artifacts docker images":  # three tokens only: the fourth is list / delete
            if args[3] == "list":
                return json.dumps([{"package": PKG_PATH, "version": v.digest, "createTime": v.created.isoformat(),
                                    "tags": list(v.tags), "metadata": {"imageSizeBytes": v.size}}
                                   for v in self.versions])
            if any(d in args[4] for d in self.fail_delete):
                raise ar.GcloudError("boom")
            return ""
        if head.startswith("artifacts repositories"):
            if args[2] == "set-cleanup-policies":
                if self.flip_after_policy:
                    self.traffic = (self.flip_after_policy,)
                return ""
            return "[]"
        raise AssertionError(f"unexpected gcloud call {args}")

    def commands(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]

    def deletes(self) -> list[list[str]]:
        return self.commands("artifacts", "docker", "images", "delete")


def _args(apply: bool) -> argparse.Namespace:
    return argparse.Namespace(apply=apply, project=PROJECT, region=REGION, repository=[])


def _backlog():
    # 8 versions: newest 5 are kept as the floor, 3 old ones go; digests d1..d6 are the revisions' images
    return [_v(f"d{i}", 100 - i) for i in range(1, 9)] + [_v("stray", 400)]


def test_dry_run_never_changes_anything():
    fake = FakeGcloud(_backlog(), _revisions(), traffic=("rev-6",))
    lines: list[str] = []
    code = ar.run(_args(False), fake, NOW, lines.append)
    assert code == 0
    assert fake.deletes() == []
    assert fake.commands("artifacts", "repositories", "set-cleanup-policies") == []
    text = "\n".join(lines)
    assert "versions 9" in text and "dry run" in text


def test_apply_sets_policy_before_any_delete_and_rechecks_safety():
    fake = FakeGcloud(_backlog(), _revisions(), traffic=("rev-6",))
    code = ar.run(_args(True), fake, NOW, lambda _: None)
    assert code == 0
    policy_index = next(i for i, c in enumerate(fake.calls) if c[:3] == ["artifacts", "repositories", "set-cleanup-policies"])
    delete_indexes = [i for i, c in enumerate(fake.calls) if c[:4] == ["artifacts", "docker", "images", "delete"]]
    assert delete_indexes and all(i > policy_index for i in delete_indexes)
    # the re-check: discovery ran again after the policy and before the first delete
    rediscovery = [i for i, c in enumerate(fake.calls) if c[:3] == ["run", "services", "list"] and i > policy_index]
    assert rediscovery and rediscovery[0] < delete_indexes[0]
    assert "--no-dry-run" in fake.calls[policy_index]
    deleted = {c[4].split("@")[1] for c in fake.deletes()}
    assert deleted == {"sha256:d1", "sha256:d2", "sha256:d3", "sha256:stray"}
    assert all("--delete-tags" in c and "--quiet" in c for c in fake.deletes())


def test_apply_refuses_when_traffic_moved_during_the_run():
    # the plan is the same before and after, but traffic lands on rev-1 (digest d1, in the delete set) after the policy
    fake = FakeGcloud(_backlog(), _revisions(), traffic=("rev-6",), flip_after_policy="rev-1")
    lines: list[str] = []
    code = ar.run(_args(True), fake, NOW, lines.append)
    assert code == 1
    assert fake.deletes() == []
    assert any("REFUSED" in line for line in lines)


def test_apply_refuses_up_front_without_touching_the_policy():
    fake = FakeGcloud(_backlog(), _revisions(), traffic=("rev-1",))
    code = ar.run(_args(True), fake, NOW, lambda _: None)
    assert code == 1
    assert fake.commands("artifacts", "repositories", "set-cleanup-policies") == []
    assert fake.deletes() == []


def test_failed_delete_is_counted_and_the_run_continues():
    fake = FakeGcloud(_backlog(), _revisions(), traffic=("rev-6",), fail_delete=("stray",))
    lines: list[str] = []
    code = ar.run(_args(True), fake, NOW, lines.append)
    assert code == 1
    assert len(fake.deletes()) == 4 and any("FAILED" in line for line in lines)
    assert any("1 failed" in line for line in lines)


def test_help_explains_both_modes(capsys):
    with pytest.raises(SystemExit) as exit_info:
        ar.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "--apply" in out

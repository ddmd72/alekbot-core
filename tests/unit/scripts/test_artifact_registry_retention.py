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


def test_old_latest_tag_within_the_five_is_kept_by_the_floor_only():
    # seven versions, all old; the tagged one is the 3rd newest, not the newest: only the keep-5 floor saves it
    versions = [_v(f"x{i}", 100 + i) for i in range(7)]
    versions[2] = _v("tagged", 102, ("latest",))
    plan = ar.plan_package(versions, NOW)
    assert "tagged" in _digests(plan.keep)
    assert _digests(plan.keep) == ["x0", "x1", "tagged", "x3", "x4"]
    assert _digests(plan.delete) == ["x5", "x6"]


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
    assert policy[0]["action"] == {"type": "Delete"} and policy[0]["condition"]["olderThan"] == "14d"
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


@pytest.mark.parametrize("host, location, repo", [
    ("gcr.io", "us", "gcr.io"), ("us.gcr.io", "us", "us.gcr.io"),
    ("eu.gcr.io", "europe", "eu.gcr.io"), ("asia.gcr.io", "asia", "asia.gcr.io"),
])
def test_gcr_hosts_map_to_their_repository_and_location(host, location, repo):
    ref = ar.parse_image_ref(f"{host}/demo-project/svc-main:latest")
    assert ref.package == ar.PackageRef(location, repo, "svc-main")


@pytest.mark.parametrize("text", [
    "projects/demo-project/locations/us/repositories/gcr.io/packages/team%2Fsvc-x",
    "us-docker.pkg.dev/demo-project/gcr.io/team/svc-x",
    "gcr.io/demo-project/team/svc-x",
    "gcr.io/demo-project/team%2Fsvc-x",
])
def test_every_package_spelling_normalises_to_one_ref(text):
    assert ar.parse_package(text) == ar.PackageRef("us", "gcr.io", "team/svc-x")


def test_parse_package_rejects_garbage():
    assert ar.parse_package("projects/demo-project") is None
    assert ar.parse_package("docker.io/x/y") is None


# ---- safety ---------------------------------------------------------------------------------------------------------

def _service(name="svc-main", traffic=("rev-3",)):
    return {"metadata": {"name": name},
            "spec": {"template": {"spec": {"containers": [{"image": "gcr.io/demo-project/svc-main:latest"}]}}},
            "status": {"traffic": [{"revisionName": r, "percent": 100 // len(traffic)} for r in traffic]}}


def _job(name="job-research", image="gcr.io/demo-project/svc-main:latest", updated="2026-10-01T00:00:00Z"):
    return {"metadata": {"name": name, "creationTimestamp": "2026-01-01T00:00:00Z"},
            "spec": {"template": {"spec": {"template": {"spec": {"containers": [{"image": image}]}}}}},
            "status": {"conditions": [{"type": "Ready", "lastTransitionTime": updated}]}}


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


def test_tagged_traffic_entry_is_serving_even_at_zero_percent():
    service = _service(traffic=("rev-6",))
    service["status"]["traffic"].append({"revisionName": "rev-1", "percent": 0, "tag": "canary"})
    protected = ar.collect_protected([service], {"svc-main": _revisions()}, [])
    assert "serves traffic" in protected.digests["sha256:d1"]


def test_latest_revision_traffic_entry_resolves_through_latest_ready_revision():
    service = {"metadata": {"name": "svc-main"}, "status": {
        "latestReadyRevisionName": "rev-1", "traffic": [{"latestRevision": True, "percent": 100}]}}
    assert ar.serving_revision_names(service) == ["rev-1"]
    protected = ar.collect_protected([service], {"svc-main": _revisions()}, [])
    assert "serves traffic" in protected.digests["sha256:d1"]


def test_older_revision_without_digest_is_a_warning():
    revisions = _revisions()
    del revisions[0]["status"]            # rev-1, the oldest: not serving, not among the 3 newest
    revisions[0]["spec"]["containers"][0]["image"] = "gcr.io/demo-project/svc-main:latest"
    protected = ar.collect_protected([_service(traffic=("rev-6",))], {"svc-main": revisions}, [])
    safety = ar.check_safety(_plan_deleting("d3"), protected)
    assert any("svc-main/rev-1" in w and "digest unknown" in w for w in safety.warnings)


def test_job_tag_protects_the_newest_version_at_or_before_the_job_update():
    old_but_current = _v("job-ran-this", 50)          # created before the job's update, newest such version
    protected = ar.collect_protected([], {}, [_job(updated=(NOW - timedelta(days=45)).isoformat())])
    plans = {PKG: ar.Plan(keep=(), delete=(old_but_current, _v("older", 80)))}
    safety = ar.check_safety(plans, protected)
    assert len(safety.violations) == 1 and "job-ran-this" in safety.violations[0]
    assert any("exact digest is unknown" in w for w in safety.warnings)


def test_package_without_versions_is_reported_empty():
    safety = ar.check_safety({PKG: ar.Plan((), ())}, ar.Protected())
    assert safety.empty_packages == ("us/gcr.io/svc-main",)


def test_parse_versions_accepts_every_package_spelling_and_skips_other_packages():
    entries = [
        {"package": "us-docker.pkg.dev/demo-project/gcr.io/svc-main", "version": "sha256:a", "createTime": "2026-11-01T00:00:00Z"},
        {"package": "gcr.io/demo-project/svc-main", "version": "sha256:b", "createTime": "2026-11-02T00:00:00Z", "tags": "latest,x"},
        {"package": "us-docker.pkg.dev/demo-project/gcr.io/svc-main/sub", "version": "sha256:c", "createTime": "2026-11-03T00:00:00Z"},
    ]
    versions = ar.parse_versions(entries, PKG)
    assert [v.digest for v in versions] == ["sha256:a", "sha256:b"] and versions[1].tags == ("latest", "x")


# ---- flows over a fake gcloud ---------------------------------------------------------------------------------------

PACKAGES = [{"name": "projects/demo-project/locations/us/repositories/gcr.io/packages/svc-main"}]


class FakeGcloud:
    """Canned gcloud: returns JSON by command, records every call, can fail chosen deletes or reads."""

    def __init__(self, versions, revisions, traffic=("rev-6",), fail_delete=(), jobs=(), packages=None,
                 policies=(), policies_error=False, second_versions=None, extra_versions=None):
        self.calls: list[list[str]] = []
        self.versions = versions
        self.second_versions = second_versions   # versions returned from the second discovery on (re-plan)
        self.extra_versions = extra_versions or {}   # package name -> versions
        self.revisions = revisions
        self.traffic = traffic
        self.fail_delete = set(fail_delete)
        self.jobs = list(jobs)
        self.packages = PACKAGES if packages is None else packages
        self.policies = list(policies)
        self.policies_error = policies_error

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        head = " ".join(args[:3])
        if head == "run services list":
            return json.dumps([_service(traffic=self.traffic)])
        if head == "run jobs list":
            return json.dumps(self.jobs)
        if head == "run revisions list":
            return json.dumps(self.revisions)
        if head == "artifacts packages list":
            return json.dumps(self.packages)
        if head == "artifacts docker images":
            if args[3] == "list":
                name = args[4].rsplit("/", 1)[-1]
                rescan = len(self.commands("run", "services", "list")) > 1
                versions = self.extra_versions.get(name) or (
                    self.second_versions if rescan and self.second_versions is not None else self.versions)
                path = args[4]
                return json.dumps([{"package": path, "version": v.digest, "createTime": v.created.isoformat(),
                                    "tags": list(v.tags), "metadata": {"imageSizeBytes": v.size}} for v in versions])
            if any(d in args[4] for d in self.fail_delete):
                raise ar.GcloudError("boom")
            return ""
        if head.startswith("artifacts repositories"):
            if args[2] == "list-cleanup-policies":
                if self.policies_error:
                    raise ar.GcloudError("permission denied")
                return json.dumps(self.policies)
            return ""
        raise AssertionError(f"unexpected gcloud call {args}")

    def commands(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]

    def deletes(self) -> list[list[str]]:
        return self.commands("artifacts", "docker", "images", "delete")

    def policy_sets(self) -> list[list[str]]:
        return self.commands("artifacts", "repositories", "set-cleanup-policies")


def _args(apply: bool, repository=()) -> argparse.Namespace:
    return argparse.Namespace(apply=apply, project=PROJECT, region=REGION, repository=list(repository))


def _backlog():
    # d1..d8 aged 99..92 days (newest 5 = d8..d4 kept by the floor, d1..d3 go) plus a 400-day-old stray
    return [_v(f"d{i}", 100 - i) for i in range(1, 9)] + [_v("stray", 400)]


def test_dry_run_never_changes_anything():
    fake = FakeGcloud(_backlog(), _revisions())
    lines: list[str] = []
    assert ar.run(_args(False), fake, NOW, lines.append) == 0
    assert fake.deletes() == [] and fake.policy_sets() == []
    text = "\n".join(lines)
    assert "versions 9" in text and "dry run" in text


def test_dry_run_lists_every_package_of_the_repository_and_checks_them_all():
    other = [{"name": "projects/demo-project/locations/us/repositories/gcr.io/packages/team%2Fsvc-other"}]
    fake = FakeGcloud(_backlog(), _revisions(), packages=PACKAGES + other,
                      extra_versions={"svc-other": [_v(f"o{i}", 60 + i) for i in range(7)]})
    lines: list[str] = []
    assert ar.run(_args(False), fake, NOW, lines.append) == 0
    text = "\n".join(lines)
    assert "us/gcr.io/svc-main: versions 9" in text and "us/gcr.io/team/svc-other: versions 7" in text


def test_discovery_adds_repository_flag_and_parses_both_package_spellings():
    fake = FakeGcloud([], _revisions(), packages=[
        {"name": "projects/demo-project/locations/europe/repositories/apps/packages/team%2Fsvc-x"},
        {"name": "projects/demo-project/locations/europe/repositories/apps/packages/svc-y"}])
    discovery = ar.discover(fake, PROJECT, REGION, ["europe/apps"])
    assert ar.PackageRef("europe", "apps", "team/svc-x") in discovery.packages
    assert ar.PackageRef("europe", "apps", "svc-y") in discovery.packages
    assert ar.PackageRef("us", "gcr.io", "svc-main") in discovery.packages      # from Cloud Run
    assert ("europe", "apps") in discovery.repositories and ("us", "gcr.io") in discovery.repositories


def test_discovery_rejects_a_malformed_repository_flag():
    with pytest.raises(ValueError):
        ar.discover(FakeGcloud([], _revisions()), PROJECT, REGION, ["no-slash"])


def test_apply_deletes_first_and_sets_the_policy_last():
    fake = FakeGcloud(_backlog(), _revisions())
    assert ar.run(_args(True), fake, NOW, lambda _: None) == 0
    policy_index = next(i for i, c in enumerate(fake.calls) if c[:3] == ["artifacts", "repositories", "set-cleanup-policies"])
    delete_indexes = [i for i, c in enumerate(fake.calls) if c[:4] == ["artifacts", "docker", "images", "delete"]]
    assert delete_indexes and all(i < policy_index for i in delete_indexes)
    rediscovery = [i for i, c in enumerate(fake.calls) if c[:3] == ["run", "services", "list"]]
    assert len(rediscovery) == 2 and rediscovery[1] < delete_indexes[0]      # the re-check precedes the first delete
    assert "--no-dry-run" in fake.calls[policy_index] and any(a.startswith("--policy=") for a in fake.calls[policy_index])
    deleted = {c[4].split("@")[1] for c in fake.deletes()}
    assert deleted == {"sha256:d1", "sha256:d2", "sha256:d3", "sha256:stray"}
    assert all("--delete-tags" in c and "--quiet" in c for c in fake.deletes())


def test_apply_refuses_when_traffic_moved_during_the_run():
    """Traffic is on rev-6 for the first check and lands on old rev-1 (digest d1, in the delete set) by the re-check."""
    class Flipping(FakeGcloud):
        def __call__(self, args):
            if args[:3] == ["run", "services", "list"] and self.commands("run", "services", "list"):
                self.traffic = ("rev-1",)
            return super().__call__(args)

    fake = Flipping(_backlog(), _revisions(), traffic=("rev-6",))
    lines: list[str] = []
    assert ar.run(_args(True), fake, NOW, lines.append) == 1
    assert fake.deletes() == [] and fake.policy_sets() == []
    assert any("REFUSED" in line for line in lines)


def test_apply_refuses_up_front_without_touching_anything():
    fake = FakeGcloud(_backlog(), _revisions(), traffic=("rev-1",))
    assert ar.run(_args(True), fake, NOW, lambda _: None) == 1
    assert fake.policy_sets() == [] and fake.deletes() == []


def test_replan_only_deletes_what_the_printed_plan_listed():
    """Between the printed plan and the re-plan an old version appears that the printed plan never listed."""
    second = _backlog() + [_v("late-old", 97.5)]
    fake = FakeGcloud(_backlog(), _revisions(), second_versions=second)
    lines: list[str] = []
    assert ar.run(_args(True), fake, NOW, lines.append) == 0
    deleted = {c[4].split("@")[1] for c in fake.deletes()}
    assert "sha256:late-old" not in deleted and deleted == {"sha256:d1", "sha256:d2", "sha256:d3", "sha256:stray"}
    assert any("NOT deleting" in line and "late-old" in line for line in lines)
    assert any(line.startswith("deleting 4 versions") for line in lines)


def test_failed_delete_is_counted_the_run_continues_and_the_policy_is_still_set():
    fake = FakeGcloud(_backlog(), _revisions(), fail_delete=("stray",))
    lines: list[str] = []
    assert ar.run(_args(True), fake, NOW, lines.append) == 1
    assert len(fake.deletes()) == 4 and len(fake.policy_sets()) == 1
    assert any("FAILED" in line for line in lines) and any("1 failed" in line for line in lines)


def test_job_tag_image_end_to_end_refuses_deleting_its_version():
    """A job written as :latest: its last update was 45 days ago, so the newest version before that must survive."""
    versions = [_v(f"n{i}", 1 + i) for i in range(5)] + [_v("job-ran-this", 50), _v("older", 80)]
    fake = FakeGcloud(versions, _revisions(), traffic=("rev-6",),
                      jobs=[_job(updated=(NOW - timedelta(days=45)).isoformat())])
    lines: list[str] = []
    assert ar.run(_args(True), fake, NOW, lines.append) == 1
    assert fake.deletes() == [] and fake.policy_sets() == []
    text = "\n".join(lines)
    assert "exact digest is unknown" in text and "job-ran-this" in text


def test_foreign_policy_is_refused_in_dry_run_and_apply_before_any_change():
    foreign = [{"name": "projects/demo-project/locations/us/repositories/gcr.io/cleanupPolicies/custom-hand-made"}]
    for apply in (False, True):
        fake = FakeGcloud(_backlog(), _revisions(), policies=foreign)
        lines: list[str] = []
        assert ar.run(_args(apply), fake, NOW, lines.append) == 1
        assert fake.deletes() == [] and fake.policy_sets() == []
        assert any("custom-hand-made" in line and "not ours" in line for line in lines)


def test_our_own_policy_names_are_replaced_not_refused():
    ours = [{"name": ar.POLICY_DELETE_NAME}, {"name": f"projects/p/locations/us/repositories/gcr.io/cleanupPolicies/{ar.POLICY_KEEP_NAME}"}]
    fake = FakeGcloud(_backlog(), _revisions(), policies=ours)
    assert ar.run(_args(True), fake, NOW, lambda _: None) == 0
    assert len(fake.policy_sets()) == 1


def test_unreadable_policies_show_an_error_and_block_apply_only():
    fake = FakeGcloud(_backlog(), _revisions(), policies_error=True)
    lines: list[str] = []
    assert ar.run(_args(False), fake, NOW, lines.append) == 0
    assert any("could not be read" in line for line in lines)
    fake = FakeGcloud(_backlog(), _revisions(), policies_error=True)
    assert ar.run(_args(True), fake, NOW, lambda _: None) == 1
    assert fake.deletes() == [] and fake.policy_sets() == []


def test_package_with_zero_versions_warns_and_blocks_apply():
    fake = FakeGcloud([], _revisions())
    lines: list[str] = []
    assert ar.run(_args(False), fake, NOW, lines.append) == 0
    assert any("no versions listed" in line for line in lines)
    fake = FakeGcloud([], _revisions())
    assert ar.run(_args(True), fake, NOW, lambda _: None) == 1
    assert fake.deletes() == [] and fake.policy_sets() == []


def test_main_returns_1_on_a_gcloud_error(capsys):
    def failing(args):
        raise ar.GcloudError("no access")

    assert ar.main(["--project", PROJECT, "--region", REGION], runner=failing) == 1
    assert "no access" in capsys.readouterr().err


def test_main_without_a_project_returns_2(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert ar.main([], runner=lambda a: "[]") == 2


def test_help_explains_both_modes(capsys):
    with pytest.raises(SystemExit) as exit_info:
        ar.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "--apply" in out

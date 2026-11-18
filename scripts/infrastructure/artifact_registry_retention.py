"""Artifact Registry retention: set the cleanup policy and clean the image backlog once.

Every `make deploy` pushes a new image under the same `:latest` tag, so each push leaves an untagged version behind and
the registry only grows. Rollback is needed only during a fresh deploy and a few days after, so the rule is:

    KEEP    the 5 most recent versions of each package (a floor: it wins over the delete rule), and
    DELETE  every version older than 14 days.

The script has two modes:

  dry run (default)  Changes nothing. Prints the current cleanup policies of each repository, then for each package how
                     many versions exist, how many would be kept and deleted, the bytes freed and the oldest version kept.
  --apply            Sets the policy above on each repository (so the registry keeps itself clean from now on), then
                     deletes the whole backlog at once, version by version.

Which repositories and packages: the images of every Cloud Run service and Cloud Run Job in the project and region
(`--repository LOCATION/REPO` adds a whole repository). Project and region come from `.env` / the environment
(`GOOGLE_CLOUD_PROJECT`, `REGION`, default us-central1) or `--project` / `--region`.

Every package of each target repository is planned, not only the ones Cloud Run references.

Safety, checked before anything changes and again right before the first delete: the images of every revision that
serves traffic (or carries a traffic tag), of the 3 newest revisions of each service and of every job must not be in the
delete set. If one is, the script prints it and exits non-zero without changing anything. Older revisions whose image
will vanish only get a warning line (they lose rollback). A job image written as a tag has no known digest: the tagged
version and the newest version created at or before the job's last update are protected, with a warning.
--apply also refuses when a repository already has a cleanup policy that is not ours, or a package has no versions.

--apply order: plan and check, re-plan and re-check, delete only what both plans list, set the policy LAST.

Needs `gcloud` with Artifact Registry admin rights (the owner's). Exit code is non-zero on a refusal or any failed delete.

Usage:
    python scripts/infrastructure/artifact_registry_retention.py            # dry run
    python scripts/infrastructure/artifact_registry_retention.py --apply    # policy + backlog clean
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

RETENTION_DAYS = 14
KEEP_RECENT_VERSIONS = 5
PROTECTED_RECENT_REVISIONS = 3
PROGRESS_EVERY = 25
DEFAULT_REGION = "us-central1"
POLICY_DELETE_NAME = "delete-old-versions"
POLICY_KEEP_NAME = "keep-most-recent"
OUR_POLICY_NAMES = frozenset({POLICY_DELETE_NAME, POLICY_KEEP_NAME})

Runner = Callable[[list[str]], str]
"""Runs one gcloud command (argument list without the leading `gcloud`) and returns stdout; raises GcloudError."""


class GcloudError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Version:
    digest: str
    created: datetime
    tags: tuple[str, ...] = ()
    size: int = 0


@dataclass(frozen=True, order=True)
class PackageRef:
    location: str
    repo: str
    package: str

    def path(self, project: str) -> str:
        return f"{self.location}-docker.pkg.dev/{project}/{self.repo}/{self.package}"


@dataclass(frozen=True)
class ImageRef:
    package: PackageRef
    tag: str | None
    digest: str | None


@dataclass(frozen=True)
class Plan:
    keep: tuple[Version, ...]
    delete: tuple[Version, ...]

    @property
    def freed_bytes(self) -> int:
        return sum(v.size for v in self.delete)

    @property
    def oldest_kept(self) -> Version | None:
        return min(self.keep, key=lambda v: v.created) if self.keep else None


@dataclass
class Protected:
    """What must survive: digests, `(package, tag)` refs (a tag is resolved against the version list), and for every
    digest of a revision that is neither serving nor among the newest ones, the revisions that would lose rollback."""
    digests: dict[str, str] = field(default_factory=dict)          # digest -> why
    tag_refs: dict[tuple[PackageRef, str], str] = field(default_factory=dict)
    older: dict[str, list[str]] = field(default_factory=dict)      # digest -> ["service/revision", ...]
    job_times: list[tuple[PackageRef, datetime, str]] = field(default_factory=list)  # (package, instant, job)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Safety:
    violations: tuple[str, ...]
    warnings: tuple[str, ...]
    empty_packages: tuple[str, ...] = ()


# --------------------------------------------------------------------------------------------------------------------
# Pure functions
# --------------------------------------------------------------------------------------------------------------------

def parse_time(value: str) -> datetime:
    """RFC 3339 with up to nanoseconds (`2026-11-01T10:00:00.123456789Z`) -> aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, rest = text.split(".", 1)
        frac = ""
        while rest and rest[0].isdigit():
            frac += rest[0]
            rest = rest[1:]
        text = f"{head}.{frac[:6].ljust(6, '0')}{rest}"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def plan_package(versions: list[Version], now: datetime, retention_days: int = RETENTION_DAYS,
                 keep_recent: int = KEEP_RECENT_VERSIONS) -> Plan:
    """keep = the `keep_recent` newest by createTime (ties by digest) + every version younger than or exactly
    `retention_days` old; delete = the rest. A version is deleted only when strictly older than the window."""
    ordered = sorted(versions, key=lambda v: (v.created, v.digest), reverse=True)
    floor = {v.digest for v in ordered[:keep_recent]}
    window = timedelta(days=retention_days)
    keep: list[Version] = []
    delete: list[Version] = []
    for version in ordered:
        if version.digest in floor or now - version.created <= window:
            keep.append(version)
        else:
            delete.append(version)
    return Plan(keep=tuple(keep), delete=tuple(delete))


_GCR_HOSTS = {"gcr.io": "us", "us.gcr.io": "us", "eu.gcr.io": "europe", "asia.gcr.io": "asia"}


def location_of_host(host: str) -> str | None:
    """Artifact Registry location of a registry host (gcr.io is multi-region `us`, eu.gcr.io is `europe`)."""
    if host.endswith("-docker.pkg.dev"):
        return host[: -len("-docker.pkg.dev")]
    return _GCR_HOSTS.get(host)


def parse_image_ref(ref: str) -> ImageRef | None:
    """`gcr.io/P/pkg:tag`, `gcr.io/P/pkg@sha256:..`, `LOC-docker.pkg.dev/P/REPO/pkg[:tag|@digest]`; else None."""
    if not ref:
        return None
    body, digest = (ref.split("@", 1) + [None])[:2] if "@" in ref else (ref, None)
    parts = body.split("/")
    if len(parts) < 3:
        return None
    host = parts[0]
    location = location_of_host(host)
    if location is None:
        return None
    if host in _GCR_HOSTS:
        repo, rest = host, parts[2:]   # the repository is named like the host: gcr.io, us.gcr.io, eu.gcr.io, asia.gcr.io
    else:
        if len(parts) < 4:
            return None
        repo, rest = parts[2], parts[3:]
    last = rest[-1]
    tag = None
    if digest is None and ":" in last:
        last, tag = last.split(":", 1)
    elif digest is not None and ":" in last:
        last = last.split(":", 1)[0]
    package = unquote("/".join(rest[:-1] + [last]))
    return ImageRef(PackageRef(location, repo, package), tag, digest)


def parse_package(text: str) -> PackageRef | None:
    """A package in any of its spellings: `projects/P/locations/L/repositories/R/packages/NAME` (NAME URL-encoded),
    `LOC-docker.pkg.dev/P/R/NAME` or `gcr.io/P/NAME` (and the regional gcr hosts)."""
    value = text.strip()
    if value.startswith("projects/"):
        head, sep, name = value.partition("/packages/")
        parts = head.split("/")
        if not sep or len(parts) < 6 or parts[2] != "locations" or parts[4] != "repositories":
            return None
        return PackageRef(parts[3], parts[5], unquote(name))
    ref = parse_image_ref(value)
    return ref.package if ref else None


def _first_container(spec: dict) -> dict:
    containers = spec.get("containers") or []
    return containers[0] if containers else {}


def revision_image(revision: dict) -> ImageRef | None:
    """The digest-resolved image of a revision (`status.imageDigest`), else the image written in its spec."""
    resolved = parse_image_ref(str(revision.get("status", {}).get("imageDigest") or ""))
    if resolved and resolved.digest:
        return resolved
    return parse_image_ref(str(_first_container(revision.get("spec", {})).get("image") or ""))


def job_image(job: dict) -> ImageRef | None:
    template = job.get("spec", {}).get("template", {}).get("spec", {}).get("template", {}).get("spec", {})
    return parse_image_ref(str(_first_container(template).get("image") or ""))


def service_image(service: dict) -> ImageRef | None:
    template = service.get("spec", {}).get("template", {}).get("spec", {})
    return parse_image_ref(str(_first_container(template).get("image") or ""))


def serving_revision_names(service: dict) -> list[str]:
    status = service.get("status", {})
    names: list[str] = []
    for entry in status.get("traffic", []) or []:
        if (entry.get("percent") or 0) <= 0 and not entry.get("tag"):
            continue   # a tagged entry serves its tag's URL even at 0 %
        name = entry.get("revisionName") or (status.get("latestReadyRevisionName") if entry.get("latestRevision") else None)
        if name and name not in names:
            names.append(name)
    return names


def job_reference_times(job: dict) -> list[datetime]:
    """Instants at which a job's tag may have been resolved: Ready condition transition, latest execution creation, job
    creation (Cloud Run v1 Job resource; field paths unverified against live output, a missing one is skipped)."""
    status = job.get("status", {})
    raw: list[object] = [job.get("metadata", {}).get("creationTimestamp"),
                         (status.get("latestCreatedExecution") or {}).get("createTime")]
    raw += [c.get("lastTransitionTime") for c in status.get("conditions", []) or [] if c.get("type") == "Ready"]
    times: list[datetime] = []
    for value in raw:
        if value:
            try:
                times.append(parse_time(str(value)))
            except ValueError:
                continue
    return times


def collect_protected(services: list[dict], revisions: dict[str, list[dict]], jobs: list[dict]) -> Protected:
    """Everything that must survive: serving revisions, the 3 newest revisions of each service, every job's image."""
    protected = Protected()

    def protect(ref: ImageRef | None, why: str) -> None:
        if ref is None:
            return
        if ref.digest:
            protected.digests.setdefault(ref.digest, why)
        elif ref.tag:
            protected.tag_refs.setdefault((ref.package, ref.tag), why)

    for service in services:
        name = service.get("metadata", {}).get("name", "?")
        serving = set(serving_revision_names(service))
        ordered = sorted(
            revisions.get(name, []), key=lambda r: str(r.get("metadata", {}).get("creationTimestamp", "")), reverse=True
        )
        for index, revision in enumerate(ordered):
            rev_name = revision.get("metadata", {}).get("name", "?")
            ref = revision_image(revision)
            if rev_name in serving:
                protect(ref, f"serves traffic: {name}/{rev_name}")
            elif index < PROTECTED_RECENT_REVISIONS:
                protect(ref, f"one of the {PROTECTED_RECENT_REVISIONS} newest revisions: {name}/{rev_name}")
            elif ref is not None and ref.digest:
                protected.older.setdefault(ref.digest, []).append(f"{name}/{rev_name}")
            else:
                protected.warnings.append(
                    f"revision {name}/{rev_name}: image digest unknown (no status.imageDigest), "
                    "cannot tell whether its image will be deleted")
        if not ordered:
            protect(service_image(service), f"current image of service {name}")
    for job in jobs:
        job_name = job.get("metadata", {}).get("name", "?")
        ref = job_image(job)
        protect(ref, f"image of job {job_name}")
        if ref is not None and not ref.digest:
            times = job_reference_times(job)
            for instant in times:
                protected.job_times.append((ref.package, instant, job_name))
            protected.warnings.append(
                f"job {job_name}: its image is the tag {ref.tag or '(none)'}, so the exact digest is unknown; protecting "
                f"the tagged version and the newest version created at or before its last update"
                f"{'' if times else ' (no update time found in the job: only the tag protects it)'}")
    return protected


def check_safety(plans: dict[PackageRef, Plan], protected: Protected) -> Safety:
    """A protected image in a delete set is a violation; an older revision's image in a delete set is a warning."""
    violations: list[str] = []
    warnings: list[str] = list(protected.warnings)
    empty = [f"{p.location}/{p.repo}/{p.package}" for p, plan in sorted(plans.items()) if not plan.keep and not plan.delete]
    for package, instant, job_name in protected.job_times:
        plan = plans.get(package)
        if plan is None:
            continue
        candidates = [v for v in (*plan.keep, *plan.delete) if v.created <= instant]
        if candidates:
            newest = max(candidates, key=lambda v: (v.created, v.digest))
            if any(newest.digest == d.digest for d in plan.delete):
                violations.append(f"{package.repo}/{package.package}@{newest.digest} would be deleted but it is the "
                                  f"newest version at or before the last update of job {job_name}")
    for package, plan in sorted(plans.items()):
        for version in plan.delete:
            label = f"{package.repo}/{package.package}@{version.digest}"
            if version.digest in protected.digests:
                violations.append(f"{label} would be deleted but {protected.digests[version.digest]}")
            for tag in version.tags:
                why = protected.tag_refs.get((package, tag))
                if why:
                    violations.append(f"{label} (tag {tag}) would be deleted but {why}")
            for revision in protected.older.get(version.digest, []):
                warnings.append(f"{label}: revision {revision} loses its image (no rollback to it)")
    return Safety(tuple(violations), tuple(warnings), tuple(empty))


def cleanup_policy_document() -> list[dict]:
    """The policy set on every repository: DELETE older than the window, KEEP the most recent versions (KEEP wins)."""
    return [
        {"name": POLICY_DELETE_NAME, "action": {"type": "Delete"},
         "condition": {"tagState": "any", "olderThan": f"{RETENTION_DAYS}d"}},
        {"name": POLICY_KEEP_NAME, "action": {"type": "Keep"},
         "mostRecentVersions": {"keepCount": KEEP_RECENT_VERSIONS}},
    ]


def parse_versions(entries: list[dict], package: PackageRef) -> list[Version]:
    """`gcloud artifacts docker images list --include-tags --format=json` entries of exactly this package (the
    `package` field is compared after normalising its spelling)."""
    versions: list[Version] = []
    for entry in entries:
        if "package" in entry and parse_package(str(entry["package"]).rstrip("/")) != package:
            continue
        tags = entry.get("tags") or []
        if isinstance(tags, str):
            tags = [t for t in tags.split(",") if t]
        size = entry.get("metadata", {}).get("imageSizeBytes") or entry.get("imageSizeBytes") or 0
        created = entry.get("createTime") or entry.get("create_time")
        versions.append(Version(str(entry["version"]), parse_time(str(created)), tuple(tags), int(size)))
    return versions


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


# --------------------------------------------------------------------------------------------------------------------
# gcloud access (the runner is injected)
# --------------------------------------------------------------------------------------------------------------------

def subprocess_runner(args: list[str]) -> str:
    result = subprocess.run(["gcloud", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise GcloudError(f"gcloud {' '.join(args[:4])} failed: {result.stderr.strip()}")
    return result.stdout


def run_json(runner: Runner, args: list[str]) -> list[dict]:
    parsed = json.loads(runner([*args, "--format=json"]) or "[]")
    return parsed if isinstance(parsed, list) else [parsed]


@dataclass
class Discovery:
    packages: list[PackageRef]
    services: list[dict]
    revisions: dict[str, list[dict]]
    jobs: list[dict]
    repositories: list[tuple[str, str]] = field(default_factory=list)


def discover(runner: Runner, project: str, region: str, extra_repositories: list[str]) -> Discovery:
    """Repositories from Cloud Run (services, their revisions, jobs) plus `--repository`; then EVERY package of each."""
    services = run_json(runner, ["run", "services", "list", f"--project={project}", f"--region={region}"])
    jobs = run_json(runner, ["run", "jobs", "list", f"--project={project}", f"--region={region}"])
    revisions: dict[str, list[dict]] = {}
    seen: set[PackageRef] = set()
    for service in services:
        name = service.get("metadata", {}).get("name", "")
        revs = run_json(runner, ["run", "revisions", "list", f"--service={name}", f"--project={project}",
                                 f"--region={region}"])
        revisions[name] = revs
        for ref in [service_image(service), *[revision_image(r) for r in revs]]:
            if ref:
                seen.add(ref.package)
    for job in jobs:
        ref = job_image(job)
        if ref:
            seen.add(ref.package)
    repositories = {(p.location, p.repo) for p in seen}
    for spec in extra_repositories:
        location, _, repo = spec.partition("/")
        if not location or not repo:
            raise ValueError(f"--repository takes LOCATION/REPO, got {spec!r}")
        repositories.add((location, repo))
    packages = set(seen)
    for location, repo in sorted(repositories):
        listing = run_json(runner, ["artifacts", "packages", "list", f"--repository={repo}",
                                    f"--location={location}", f"--project={project}"])
        for item in listing:
            package = parse_package(str(item.get("name", "")))
            if package is not None:
                packages.add(package)
    return Discovery(sorted(packages), services, revisions, jobs, sorted(repositories))


def list_versions(runner: Runner, project: str, package: PackageRef) -> list[Version]:
    path = package.path(project)
    entries = run_json(runner, ["artifacts", "docker", "images", "list", path, "--include-tags",
                                f"--project={project}"])
    return parse_versions(entries, package)


def build_plans(runner: Runner, project: str, discovery: Discovery, now: datetime) -> dict[PackageRef, Plan]:
    return {p: plan_package(list_versions(runner, project, p), now) for p in discovery.packages}


PolicyNames = dict[tuple[str, str], list[str] | None]


def read_policies(runner: Runner, project: str, repositories: list[tuple[str, str]],
                  out: Callable[[str], None]) -> PolicyNames:
    """Names of the cleanup policies of each repository (None when they could not be read); prints what it found."""
    result: PolicyNames = {}
    for location, repo in repositories:
        try:
            policies = run_json(runner, ["artifacts", "repositories", "list-cleanup-policies", repo,
                                         f"--location={location}", f"--project={project}"])
        except GcloudError as exc:
            result[(location, repo)] = None
            out(f"cleanup policies of {location}/{repo}: could not be read ({exc})")
            continue
        result[(location, repo)] = [str(p.get("name", "")).rsplit("/", 1)[-1] for p in policies]
        out(f"cleanup policies of {location}/{repo}: {json.dumps(policies) if policies else 'none'}")
    return result


def foreign_policies(policies: PolicyNames) -> list[str]:
    """One line per policy whose name is not one of ours (setting our policy could replace it)."""
    return [f"{location}/{repo} already has the cleanup policy {name!r}, which is not ours"
            for (location, repo), names in sorted(policies.items()) for name in names or [] if name not in OUR_POLICY_NAMES]


def print_plans(plans: dict[PackageRef, Plan], out: Callable[[str], None]) -> None:
    for package, plan in sorted(plans.items()):
        oldest = plan.oldest_kept
        out(f"{package.location}/{package.repo}/{package.package}: versions {len(plan.keep) + len(plan.delete)}, "
            f"keep {len(plan.keep)}, delete {len(plan.delete)}, freed {format_size(plan.freed_bytes)}, "
            f"oldest kept {oldest.created.date().isoformat() if oldest else 'none'}")


def evaluate(runner: Runner, project: str, region: str, extra: list[str], now: datetime
             ) -> tuple[Discovery, dict[PackageRef, Plan], Safety]:
    discovery = discover(runner, project, region, extra)
    plans = build_plans(runner, project, discovery, now)
    safety = check_safety(plans, collect_protected(discovery.services, discovery.revisions, discovery.jobs))
    return discovery, plans, safety


def report_safety(safety: Safety, out: Callable[[str], None]) -> None:
    for line in safety.warnings:
        out(f"WARNING: {line}")
    for name in safety.empty_packages:
        out(f"WARNING: package {name} has no versions listed (wrong name or a listing problem); --apply refuses")
    for line in safety.violations:
        out(f"REFUSED: {line}")


def apply_policies(runner: Runner, project: str, repositories: list[tuple[str, str]],
                   out: Callable[[str], None]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        policy_file = Path(tmp) / "cleanup_policy.json"
        policy_file.write_text(json.dumps(cleanup_policy_document(), indent=2), encoding="utf-8")
        for location, repo in repositories:
            runner(["artifacts", "repositories", "set-cleanup-policies", repo, f"--location={location}",
                    f"--project={project}", f"--policy={policy_file}", "--no-dry-run"])
            out(f"cleanup policy set on {location}/{repo}")


def intersect_plans(printed: dict[PackageRef, Plan], replanned: dict[PackageRef, Plan]
                    ) -> tuple[dict[PackageRef, list[Version]], list[str]]:
    """Versions to delete = those in BOTH plans. What only the re-plan wants deleted was never printed: it is reported
    and left alone."""
    final: dict[PackageRef, list[Version]] = {}
    unprinted: list[str] = []
    for package, plan in sorted(replanned.items()):
        shown = {v.digest for v in printed[package].delete} if package in printed else set()
        keep_deleting = [v for v in plan.delete if v.digest in shown]
        final[package] = keep_deleting
        unprinted += [f"{package.repo}/{package.package}@{v.digest}" for v in plan.delete if v.digest not in shown]
    return final, unprinted


def delete_versions(runner: Runner, project: str, planned: dict[PackageRef, list[Version]],
                    out: Callable[[str], None]) -> int:
    """Deletes every planned version; returns the number of failures (the run continues past a failure)."""
    todo = [(package, v) for package, versions in sorted(planned.items()) for v in versions]
    failed = 0
    for index, (package, version) in enumerate(todo, start=1):
        try:
            runner(["artifacts", "docker", "images", "delete", f"{package.path(project)}@{version.digest}",
                    "--delete-tags", "--quiet", f"--project={project}"])
        except GcloudError as exc:
            failed += 1
            out(f"FAILED to delete {package.package}@{version.digest}: {exc}")
        if index % PROGRESS_EVERY == 0:
            out(f"progress: {index}/{len(todo)} processed, {failed} failed")
    freed = sum(v.size for _, v in todo)
    out(f"done: {len(todo) - failed} deleted, {failed} failed, about {format_size(freed)} planned to free")
    return failed


def run(args: argparse.Namespace, runner: Runner, now: datetime, out: Callable[[str], None] = print) -> int:
    discovery, plans, safety = evaluate(runner, args.project, args.region, args.repository, now)
    out(f"policy: DELETE older than {RETENTION_DAYS} days, KEEP the {KEEP_RECENT_VERSIONS} most recent versions; "
        f"mode: {'APPLY' if args.apply else 'dry run (nothing changes)'}")
    policies = read_policies(runner, args.project, discovery.repositories, out)
    print_plans(plans, out)
    report_safety(safety, out)
    foreign = foreign_policies(policies)
    for line in foreign:
        out(f"REFUSED: {line}")
    if safety.violations or foreign:
        out("refusing; nothing was changed")
        return 1
    if not args.apply:
        out("dry run only. Re-run with --apply to clean the backlog and then set the policy.")
        return 0
    if safety.empty_packages:
        out("refusing: a package has no versions; nothing was changed")
        return 1
    if any(names is None for names in policies.values()):
        out("refusing: the current cleanup policies of a repository could not be read; nothing was changed")
        return 1
    out("re-checking before deleting")
    _, replanned, safety2 = evaluate(runner, args.project, args.region, args.repository, now)
    report_safety(safety2, out)
    if safety2.violations or safety2.empty_packages:
        out("refusing after the re-check; nothing was changed (no version deleted, no policy set)")
        return 1
    final, unprinted = intersect_plans(plans, replanned)
    for line in unprinted:
        out(f"NOT deleting {line}: the re-plan wants it deleted but the printed plan did not list it")
    out(f"deleting {sum(len(v) for v in final.values())} versions in {sum(1 for v in final.values() if v)} packages")
    failed = delete_versions(runner, args.project, final, out)
    apply_policies(runner, args.project, discovery.repositories, out)
    return 1 if failed else 0


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        values[key.removeprefix("export ").strip()] = value.split(" #", 1)[0].strip().strip("'\"")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").strip(), formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="set the cleanup policy and delete the backlog (default: dry run, nothing changes)")
    parser.add_argument("--repository", action="append", default=[], metavar="LOCATION/REPO",
                        help="also manage every package of this repository (repeatable)")
    parser.add_argument("--project", help="GCP project (default: GOOGLE_CLOUD_PROJECT from .env or the environment)")
    parser.add_argument("--region", help=f"Cloud Run region (default: REGION from .env or the environment, else {DEFAULT_REGION})")
    return parser


def main(argv: list[str] | None = None, runner: Runner = subprocess_runner) -> int:
    args = build_parser().parse_args(argv)
    dotenv = load_dotenv(Path(".env"))
    args.project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT") or dotenv.get("GOOGLE_CLOUD_PROJECT")
    args.region = args.region or os.environ.get("REGION") or dotenv.get("REGION") or DEFAULT_REGION
    if not args.project:
        print("no project: set GOOGLE_CLOUD_PROJECT in .env or pass --project", file=sys.stderr)
        return 2
    try:
        return run(args, runner, datetime.now(timezone.utc))
    except GcloudError as exc:
        print(f"gcloud failed, nothing further was done: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

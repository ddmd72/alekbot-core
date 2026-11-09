"""After `make deploy`: warn about environment variables that the deploy dropped.

`gcloud run deploy --set-env-vars` replaces every variable, so a switch set by hand with `gcloud run services update`
disappears at the next deploy without any error. This check compares the variable NAMES of each service's newest
revision with the revision before it and reports every name that vanished. Values are never printed (they can be
secrets): only names are compared and shown.

The services come from `cloudbuild-dev.yaml` (every direct `gcloud run deploy <service>` step); the project and region
come from the command line (the Makefile passes PROJECT_ID and REGION).

The deploy has already happened when this runs: the nonzero exit only makes `make deploy` show a failure line.

Usage:
    python scripts/infrastructure/check_env_drift.py --project <PROJECT_ID> --region <REGION> [--config cloudbuild-dev.yaml]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import yaml


def dropped_names(previous: Iterable[str], current: Iterable[str]) -> list[str]:
    """Names present in the previous revision and absent from the current one, sorted. Added names are ignored."""
    return sorted(set(previous) - set(current))


def deployed_services(config_path: Path) -> list[str]:
    """Service names of every direct `gcloud run deploy <service>` step of a Cloud Build config (jobs excluded)."""
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    services: list[str] = []
    for step in config.get("steps", []):
        args = [str(a) for a in step.get("args", [])]
        if step.get("entrypoint") == "gcloud" and args[:2] == ["run", "deploy"] and len(args) > 2:
            if args[2] not in services:
                services.append(args[2])
    return services


def _env_names(revision: dict) -> set[str]:
    containers = revision.get("spec", {}).get("containers", [])
    return {e["name"] for c in containers for e in c.get("env", []) if "name" in e}


def latest_two_env_names(service: str, project: str, region: str) -> tuple[set[str], set[str]] | None:
    """Env var names of the newest and the previous revision of a service, or None when there are fewer than two."""
    result = subprocess.run(
        ["gcloud", "run", "revisions", "list", f"--service={service}", f"--region={region}",
         f"--project={project}", "--sort-by=~metadata.creationTimestamp", "--limit=2", "--format=json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gcloud revisions list failed for {service}: {result.stderr.strip()}")
    revisions = json.loads(result.stdout or "[]")
    if len(revisions) < 2:
        return None
    return _env_names(revisions[0]), _env_names(revisions[1])


def drift_report(drops_by_service: dict[str, list[str]]) -> str:
    """The loud message for a non-empty drop map (names only)."""
    lines = ["", "!" * 72, "DEPLOYED, BUT ENVIRONMENT VARIABLES WERE DROPPED (the deploy itself has already happened):"]
    for service, names in drops_by_service.items():
        lines.append(f"  {service}: {', '.join(names)}")
    lines += [
        "If they were manual switches, add them to cloudbuild-dev.yaml (--set-env-vars) or re-set them with",
        "`gcloud run services update <service> --update-env-vars=NAME=value`.",
        "!" * 72,
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--config", default="cloudbuild-dev.yaml")
    args = parser.parse_args(argv)

    drops: dict[str, list[str]] = {}
    for service in deployed_services(Path(args.config)):
        names = latest_two_env_names(service, args.project, args.region)
        if names is None:
            print(f"env drift: {service}: fewer than two revisions, nothing to compare")
            continue
        current, previous = names
        dropped = dropped_names(previous, current)
        if dropped:
            drops[service] = dropped
        else:
            print(f"env drift: {service}: no variable dropped")
    if drops:
        print(drift_report(drops))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

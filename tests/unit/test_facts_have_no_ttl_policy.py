"""Facts S3 · T10 (plan §3.18): a TTL policy on `expires_at` would delete facts, so none may ever be declared for them.

Firestore deletes a document whose TTL field has passed. The facts collection's `expires_at` is a decision (the janitor
stales the fact, never deletes it), and the janitor's own counters carry no such field. The tree is scanned in Python:
every `ttls update` command in the docs, scripts and config is read with its line continuations joined (a quoted or
backticked value is read too), and none may name the facts collection, the counters collection or the posted-line
collection. A collection held in a shell variable (`--collection-group=$FACTS`) is out of reach of a text scan and is not resolved.
"""
import os
import re
from pathlib import Path
from typing import Iterator, List

import pytest

from src.config.environment import EnvironmentConfig
from tests.support.fact_lifecycle_world import NOW, JanitorWorld

ROOT = Path(__file__).resolve().parents[2]
SUFFIXES = {".md", ".yaml", ".yml", ".sh"}
SKIP_DIRS = {".git", "node_modules", "archive", "__pycache__", ".venv", "venv", ".tox", ".codegraph"}
FACTS_MARK = "domain_facts"
LIFECYCLE_NAMES = ("fact_lifecycle_daily", "fact_lifecycle_lines")
_COMMAND = re.compile(r"\bttls\s+update\b")
_GROUP = re.compile(r"--collection-group(?:=|\s+)[\"'`]?([^\s\\'\"`]+)")


def joined_lines(text: str) -> List[str]:
    """The text's lines with every trailing-backslash continuation joined to the next line."""
    lines: List[str] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        lines.append(pending + raw)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def forbidden_ttl_groups(text: str) -> List[str]:
    """The `--collection-group` of every `ttls update` command in `text` that names a collection facts must not have a policy on."""
    found: List[str] = []
    for line in joined_lines(text):
        if not _COMMAND.search(line):
            continue
        for group in _GROUP.findall(line):
            if FACTS_MARK in group or any(name in group for name in LIFECYCLE_NAMES):
                found.append(group)
    return found


def scanned_files() -> Iterator[Path]:
    for directory, subdirs, names in os.walk(ROOT):
        here = Path(directory)
        subdirs[:] = [
            d for d in subdirs
            if d not in SKIP_DIRS and not (here / d / "pyvenv.cfg").exists() and not (here == ROOT / "scripts" and d == "memory")
        ]
        for name in names:
            if Path(name).suffix in SUFFIXES or (here == ROOT and name == "Makefile"):
                yield here / name


# ----------------------------------------------------------------------------- the tree

def test_no_ttl_update_command_in_the_tree_names_facts_or_the_janitors_counters():
    offenders = {}
    for path in scanned_files():
        found = forbidden_ttl_groups(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            offenders[str(path.relative_to(ROOT))] = found

    assert offenders == {}


def test_the_scan_reaches_the_deploy_docs_that_do_declare_ttl_policies():
    reached = [p for p in scanned_files() if p.name == "README.md" and p.parent.name == "07_deployment"]
    assert reached and any(_COMMAND.search(p.read_text(encoding="utf-8")) for p in reached)


# ----------------------------------------------------------------------------- the scanner itself

@pytest.mark.parametrize("text, expected", [
    ("gcloud firestore fields ttls update expires_at --collection-group=development_domain_facts_v2", ["development_domain_facts_v2"]),
    ("gcloud firestore fields ttls update expires_at --collection-group <prefix>domain_facts_v2 --enable-ttl", ["<prefix>domain_facts_v2"]),
    ("gcloud firestore fields ttls update expires_at \\\n  --collection-group=development_domain_facts_v2 \\\n  --project=p", ["development_domain_facts_v2"]),
    ("gcloud firestore fields ttls update expires_at --collection-group=<prefix>fact_lifecycle_daily", ["<prefix>fact_lifecycle_daily"]),
    ("gcloud firestore fields ttls update expires_at --collection-group=<prefix>fact_lifecycle_lines", ["<prefix>fact_lifecycle_lines"]),
    ("gcloud firestore fields ttls update expires_at --collection-group=<prefix>worker_task_dedup", []),
    ("gcloud firestore fields ttls update expires_at --collection-group=development_domain_email_facts_v1", []),
    ('gcloud firestore fields ttls update expires_at --collection-group="development_domain_facts_v2"', ["development_domain_facts_v2"]),
    ("gcloud firestore fields ttls update expires_at --collection-group='domain_facts_v2'", ["domain_facts_v2"]),
    ('gcloud firestore fields ttls update expires_at --collection-group "${PREFIX}domain_facts_v2"', ["${PREFIX}domain_facts_v2"]),
    ("`gcloud firestore fields ttls update expires_at --collection-group=<prefix>fact_lifecycle_daily`", ["<prefix>fact_lifecycle_daily"]),
    ('gcloud firestore fields ttls update expires_at --collection-group="development_domain_email_facts_v1"', []),
    ("the facts collection is domain_facts_v2 and has no ttls policy", []),
    ("--collection-group=development_domain_facts_v2 is only a flag without the command", []),
])
def test_the_scanner_finds_exactly_the_forbidden_commands(text, expected):
    assert forbidden_ttl_groups(text) == expected


# ----------------------------------------------------------------------------- the names and the field

def test_the_facts_collection_is_still_named_domain_facts_and_the_counters_are_the_ones_scanned_for():
    env = EnvironmentConfig()

    assert FACTS_MARK in env.domain_facts_collection
    assert env.fact_lifecycle_daily_collection.endswith(LIFECYCLE_NAMES[0])
    assert env.fact_lifecycle_lines_collection.endswith(LIFECYCLE_NAMES[1])


async def test_the_due_query_filters_the_stored_field_expires_at_and_the_counters_store_no_such_field():
    world = JanitorWorld(dry_run=True)
    fact_id = await world.seed_due("due")
    assert world.docs[fact_id]["expires_at"] is not None

    due = await world.repo.find_due_facts(NOW)
    await world.service.sweep()

    assert [d.fact_id for d in due] == [fact_id]
    assert any(("where", "expires_at", "<=", NOW) in ops for ops in world.repo.facts_col.executed)
    stored = [*world.daily_docs().values(), *world.line_docs().values()]
    assert stored and all("expires_at" not in doc for doc in stored)

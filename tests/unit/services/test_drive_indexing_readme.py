"""The repository root's README lead text in the tick (USER_DRIVE_INDEXING_RFC §5.2): one listing, the parser, the
lead text on the root's card; the README itself gets no record and no chunks."""
from dataclasses import replace

import pytest

from src.domain import drive_index_policy as policy
from src.domain.drive_card import card_text
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.domain.drive_index_run import README_MAX_BYTES
from src.domain.user_drive import DriveAccessDenied

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")
LEAD = "Alek site: a static site generator for the family recipes."


@pytest.fixture
def w():
    return ContentWorld()


def repository(w, readme=LEAD, name="README.md"):
    repo = w.drive.add("alek-site", folder=True)
    w.drive.add(".git", parent=repo, folder=True)
    w.drive.add_file("main.py", b"print(1)", parent=repo)
    item = w.drive.add_file(name, readme.encode(), parent=repo) if readme is not None else None
    return repo, item


async def test_the_root_card_carries_its_readme(w):
    repo, readme = repository(w)
    await w.settle()
    rec = w.rec(repo)
    assert rec.lead_text == LEAD and rec.readme_item_id == readme and not rec.readme_pending
    assert w.card_texts()[repo] == f"alek-site | repository | {LEAD}"
    assert w.rec(readme) is None and w.chunks(readme) == []
    assert [c for c in w.emb.calls if c.startswith("alek-site")] == [card_text(rec)]   # carded once, with the lead
    assert [r.name for r in w.parser.requests] == ["README.md"] and w.reader.requests == []


async def test_a_repository_without_a_readme_keeps_the_plain_card(w):
    repo, _ = repository(w, readme=None)
    await w.settle()
    assert w.card_texts()[repo] == "alek-site | repository" and w.parser.requests == []


async def test_markdown_wins_over_text_and_longer_names(w):
    repo, _ = repository(w, readme=None)
    w.drive.add_file("readme.txt", b"From the text file.", parent=repo)
    w.drive.add_file("README.md", LEAD.encode(), parent=repo)
    w.drive.add_file("README_old.md", b"Old.", parent=repo)
    await w.settle()
    assert w.rec(repo).lead_text == LEAD


async def test_an_edited_readme_is_read_again(w):
    repo, readme = repository(w)
    await w.settle()
    w.drive.edit(readme, b"A new description.")
    await w.settle()
    assert w.rec(repo).lead_text == "A new description." and w.card_texts()[repo].endswith("A new description.")


async def test_a_removed_readme_clears_the_lead_text(w):
    repo, readme = repository(w)
    await w.settle()
    w.drive.delete(readme)
    w.drive.items[repo] = replace(w.drive.items[repo], child_count=w.drive.items[repo].child_count - 1)
    w.drive.reemit(repo)   # Graph re-emits a folder whose children changed (S0a)
    await w.settle()
    assert w.rec(repo).lead_text == "" and w.card_texts()[repo] == "alek-site | repository"


async def test_a_marker_leaving_drops_the_lead_text(w):
    repo, _ = repository(w)
    git = next(i for i, c in w.drive.items.items() if c.name == ".git")
    await w.settle()
    w.drive.delete(git)
    await w.settle()
    assert w.rec(repo).lead_text == "" and w.card_texts()[repo] == "alek-site | folder"


async def test_generated_folders_read_no_readme(w):
    nm = w.drive.add("node_modules", folder=True)
    w.drive.add_file("README.md", LEAD.encode(), parent=nm)
    await w.settle()
    assert w.parser.requests == [] and w.card_texts()[nm] == "node_modules | generated files"


async def test_a_big_or_unreadable_readme_gives_no_lead_text(w):
    repo, _ = repository(w, readme="x" * (README_MAX_BYTES + 1))
    await w.settle()
    assert w.rec(repo).lead_text == "" and w.parser.requests == []
    repo2, _ = repository(w, readme=None)
    w.parser.outcomes["README.rst"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text="never read")
    w.drive.add_file("README.rst", b"Title\n=====", parent=repo2)
    await w.settle()
    assert w.rec(repo2).lead_text == ""   # .rst is not an admitted kind


async def test_a_failing_listing_backs_off_and_keeps_what_it_had(w):
    repo, readme = repository(w)
    await w.settle()
    real = w.drive.list_children_changes
    calls = []

    async def denied(user_id, folder_id, limit, cursor=None):
        if folder_id == repo:
            calls.append(folder_id)
            raise DriveAccessDenied("refused")
        return await real(user_id, folder_id, limit, cursor)
    w.drive.list_children_changes = denied
    w.drive.edit(readme, b"Changed.")
    for _ in range(MAX_ATTEMPTS + 2):
        await w.settle()
        w.clock.advance(RETRY_CAP.total_seconds())
    rec = w.rec(repo)
    assert len(calls) == MAX_ATTEMPTS and not rec.readme_pending and rec.lead_text == LEAD


async def test_no_readme_work_before_text_is_admitted(w, monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
    monkeypatch.setattr(policy, "TRIAGED_KINDS", frozenset())
    repo, _ = repository(w)
    await w.settle()
    assert repo not in w.drive.children_calls and w.rec(repo).lead_text == ""

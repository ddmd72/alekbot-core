"""A collapsed folder's README (USER_DRIVE_INDEXING_RFC §5.2; Search v2 §25.3: every collapsed folder, its
`readme_excerpt` the profile's input, the card carrying the profile, §25.2.7) and per-extractor re-extraction (§9,
§12.3) in the planner and the card. Pure."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain import drive_extraction
from src.domain import drive_index_policy as policy
from src.domain.drive_card import card_hash, card_text, collapsed_folder, repository_root
from src.domain.drive_enrichment import DriveProfile
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import (
    ContentClass, DriveChange, DriveIndexRecord, EpochPhase, FolderProbe, Priority, Reason, RecordState, Scope,
)
from src.domain.drive_index_planner import (
    PlanContext, content_due, extractor_stale, generation_started, indexed, plan_observation, readme_read,
    reextraction_due, with_card,
)
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS, is_readme_name, readme_readable

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
ROOT = "root"


@pytest.fixture
def admitted(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    monkeypatch.setattr(policy, "TRIAGED_KINDS", SLICE2_TRIAGED_KINDS)


def ctx(now=NOW):
    return PlanContext(user_id="u", root_item_id=ROOT, epoch=1, phase=EpochPhase.IDLE, now=now,
                       place_of=lambda lat, lon: None)


def folder(name="proj", child_count=3, item_id="r"):
    return DriveChange(item_id=item_id, name=name, parent_id=ROOT, is_folder=True, child_count=child_count)


def repo_rec(**kw):
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="r", name="proj", parent_id=ROOT, is_folder=True, kind="folder",
                           content_class=ContentClass.FOLDER, scope=Scope.OPAQUE_ROOT, state=RecordState.METADATA_ONLY,
                           reason=Reason.OPAQUE_ROOT, child_count=3, policy_version=policy_version())
    return replace(rec, **kw)


def policy_version():
    from src.domain.drive_index import POLICY_VERSION
    return POLICY_VERSION


def readme(cv="c1", name="README.md", item_id="m"):
    return DriveChange(item_id=item_id, name=name, parent_id="r", size_bytes=100, content_version=cv)


class TestNames:
    @pytest.mark.parametrize("name,expected", [("README.md", True), ("readme", True), ("ReadMe.txt", True),
                                               ("README.rst", True), ("READMEFIRST.md", False),
                                               ("notes.md", False), ("readme.md.bak", False)])
    def test_is_readme_name(self, name, expected):
        assert is_readme_name(name) is expected

    def test_readable_only_once_text_is_admitted(self, monkeypatch):
        monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
        assert not readme_readable()
        monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset({"markdown"}))
        assert readme_readable()


class TestCard:
    def test_a_repository_with_a_readme_profile(self):
        # v2 (§25.2.7): the README reaches the card through the folder's profile, not as lead text
        p = DriveProfile(title="proj", what_it_is="A static site generator.", language="en", doc_type="other")
        rec = repo_rec(lead_text="ignored", readme_excerpt="A static site generator.", profile=p)
        assert repository_root(rec) and card_text(rec) == "proj | repository | A static site generator. | proj"

    def test_without_lead_text_the_slice1_card_holds(self):
        assert card_text(repo_rec()) == "proj | repository"

    @pytest.mark.parametrize("kw", [{"name": "node_modules"}, {"reason": Reason.DOT_NAME, "name": ".cache"},
                                    {"reason": Reason.DENSE_TREE}])
    def test_other_opaque_roots_never_carry_it(self, kw):
        rec = repo_rec(lead_text="x", **kw)
        assert not repository_root(rec) and "x" not in card_text(rec)


def probe_with_marker():
    return FolderProbe(marker_found=True, class_counts={}, sampled=3)


def stored_repo(lead="Lead.", version="c1"):
    """A repository root as the tick leaves it: observed, its README read, its card embedded."""
    rec = plan_observation(folder(child_count=900), None, None, probe_with_marker(), ctx()).upsert
    rec = readme_read(rec, "m", version, lead, NOW)
    return with_card(rec, card_hash(rec), "s/8", NOW)


@pytest.mark.usefixtures("admitted")
class TestReadmeDue:
    def test_a_folder_that_becomes_a_repository_reads_its_readme(self):
        upsert = plan_observation(folder(), None, None, None, ctx()).upsert
        closed = plan_observation(DriveChange(item_id="g", name=".git", parent_id="r", is_folder=True), None,
                                  upsert, None, ctx()).parent_updates[0]
        assert repository_root(closed) and closed.readme_pending and closed.due_at == NOW

    def test_classified_as_a_repository_on_first_sight(self):
        rec = plan_observation(folder(child_count=900), None, None, probe_with_marker(), ctx()).upsert
        assert repository_root(rec) and rec.readme_pending

    def test_a_reemission_with_the_same_children_reads_nothing(self):
        stored = stored_repo()
        # v2: the README is stored as the excerpt (the profile's input); the card waits for the profile
        assert stored.due_at is None and stored.readme_excerpt == "Lead." and card_text(stored) == "proj | repository"
        assert plan_observation(folder(child_count=900), stored, None, None, ctx()).upsert is None

    def test_a_changed_child_count_reads_it_again_keeping_the_lead_meanwhile(self):
        rec = plan_observation(folder(child_count=901), stored_repo(), None, None, ctx()).upsert
        assert rec.readme_pending and rec.readme_excerpt == "Lead." and rec.readme_version == "c1"
        assert (rec.due_at, rec.priority) == (NOW, Priority.MAINTENANCE)

    def test_an_edited_readme_inside_marks_the_root(self):
        plan = plan_observation(readme(cv="c2"), None, stored_repo(), None, ctx())
        assert plan.upsert is None and plan.delete_item_id is None   # the README itself gets no record
        (root,) = plan.parent_updates
        assert root.readme_pending and (root.due_at, root.priority) == (NOW, Priority.MAINTENANCE)

    def test_an_unchanged_readme_or_another_file_marks_nothing(self):
        stored = stored_repo()
        assert plan_observation(readme(cv="c1"), None, stored, None, ctx()).parent_updates == ()
        assert plan_observation(readme(name="main.py", cv="c9"), None, stored, None, ctx()).parent_updates == ()

    def test_nothing_before_text_is_admitted(self, monkeypatch):
        stored = stored_repo()
        monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
        assert not plan_observation(folder(child_count=900), None, None, probe_with_marker(),
                                    ctx()).upsert.readme_pending
        assert plan_observation(readme(cv="c2"), None, stored, None, ctx()).parent_updates == ()

    def test_a_repository_renamed_to_a_dot_folder_stays_collapsed_and_keeps_its_readme(self):
        # v2 (§25.3): every collapsed folder (any opaque_root reason) has a README; a dot-folder is still one, so
        # the excerpt stays. Leaving collapse altogether drops it: test_a_marker_leaving_reopens_without_lead_text
        rec = plan_observation(folder(name=".proj", child_count=900), stored_repo(), None, None, ctx()).upsert
        assert collapsed_folder(rec) and not repository_root(rec)
        assert (rec.reason, rec.readme_item_id, rec.readme_excerpt) == (Reason.DOT_NAME, "m", "Lead.")

    def test_a_marker_leaving_reopens_without_lead_text(self):
        stored = replace(stored_repo(), child_count=3)
        git = DriveIndexRecord(user_id="u", connection_id="u", item_id="g", name=".git", parent_id="r", is_folder=True, kind="folder",
                               scope=Scope.INSIDE_OPAQUE, state=RecordState.METADATA_ONLY, reason=Reason.FOLDER)
        plan = plan_observation(DriveChange(item_id="g", deleted=True), git, None, None, ctx(), stored)
        (reopened,) = plan.parent_updates
        assert reopened.scope is Scope.NORMAL and reopened.lead_text == "" and not reopened.readme_pending
        assert (reopened.readme_excerpt, reopened.readme_excerpt_version) == ("", "")   # §25.4.9


class TestReadmeRead:
    def test_sets_the_excerpt_with_its_version(self):
        # v2 (§25.3): the README's text becomes `readme_excerpt` (the profile's input) with the README's version; the
        # card changes only when the profile arrives, so nothing is due from the read itself
        stored = with_card(repo_rec(), card_hash(repo_rec()), "s/8", NOW)
        rec = readme_read(replace(stored, readme_pending=True, attempts=2), "m", "c1", "  " + "x" * 3000, NOW,
                          child_names=("README.md", "main.py"))
        assert (rec.readme_pending, rec.readme_item_id, rec.readme_version, rec.attempts) == (False, "m", "c1", 0)
        assert rec.readme_excerpt.startswith("x" * 3000) and rec.readme_excerpt_version == "c1"
        assert "main.py" in rec.readme_excerpt and rec.lead_text == ""
        assert rec.due_at is None   # no content work: the profile job (enrichment queue) is what the excerpt starts
        from src.domain import drive_enrich_plan
        planned = drive_enrich_plan.planned(rec, drive_enrich_plan.default_context(NOW))
        assert planned.profile_state == "ready" and planned.enrich_ready

    def test_none_keeps_the_stored_excerpt_and_empty_clears_it(self):
        stored = readme_read(repo_rec(), "m", "c1", "Lead.", NOW)
        kept = readme_read(replace(stored, readme_pending=True), "m", "c1", None, NOW)
        assert (kept.readme_excerpt, kept.readme_excerpt_version) == ("Lead.", "c1")
        cleared = readme_read(replace(stored, readme_pending=True), None, "", "", NOW)
        assert (cleared.readme_excerpt, cleared.readme_excerpt_version) == ("", "")


def indexed_by(extractor, kind="markdown", name="a.md"):
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="a", name=name, kind=kind, parent_id=ROOT, scope=Scope.NORMAL,
                           state=RecordState.PENDING, content_version="c1", policy_version=policy_version())
    out = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Clause.", extracted_by=extractor)
    return indexed(generation_started(rec, NOW), outcome=out, generation=1, chunk_count=1, now=NOW)


@pytest.mark.usefixtures("admitted")
class TestExtractorStale:
    VERSIONS = {"parser": 2, "reader": 1}

    def test_a_link_at_another_version_is_stale(self):
        assert extractor_stale(indexed_by("parser@1"), self.VERSIONS)
        assert not extractor_stale(indexed_by("parser@2"), self.VERSIONS)

    def test_another_links_version_does_not_matter(self):
        assert not extractor_stale(indexed_by("reader@1", "pdf", "scan.pdf"), self.VERSIONS)

    def test_a_link_no_longer_in_the_kinds_chain_is_stale(self, monkeypatch):
        monkeypatch.setattr(drive_extraction, "EXTRACTOR_CHAINS", {"pdf": ("pdf_parser", "reader")})
        assert extractor_stale(indexed_by("parser@2", "pdf", "a.pdf"), self.VERSIONS)
        assert not extractor_stale(indexed_by("parser@2"), self.VERSIONS)   # Markdown keeps the parser

    def test_never_read_is_never_stale(self):
        rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="a", kind="markdown")
        assert not extractor_stale(rec, self.VERSIONS)

    def test_reextraction_makes_the_content_due_and_keeps_the_readable_generation(self):
        rec = indexed_by("parser@1")
        assert not content_due(rec)
        again = plan_observation(DriveChange(item_id="a", name="a.md", parent_id=ROOT, content_version="c1",
                                             modified_at=NOW - timedelta(days=1)),
                                 reextraction_due(rec), None, None, ctx(), observed=False).upsert
        assert again.state is RecordState.PENDING and content_due(again)
        assert again.indexed_generation == 1 and not again.stale and extractor_stale(again, self.VERSIONS)

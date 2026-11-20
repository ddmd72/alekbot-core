"""SkillRepository kit (P11, G3 store 4): the docstring of `src/ports/skill_repository.py` checked against
`FirestoreSkillRepository` on the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on the raw
stored documents (index document, `versions/v<n>`, `files/<sha256>`, drafts and `draft_files/<sha256>`) and on what the
readers return.

Not carded: the cap race of two concurrent first saves of two new names (H1's transactions give no phantom protection,
so it can be neither shown nor excluded here; it waits for a parity scenario).

`_adapter(db)` is the one place the adapter is constructed.
"""
import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters import firestore_skill_repository
from src.adapters.firestore_skill_repository import FirestoreSkillRepository
from google.api_core.exceptions import ServiceUnavailable

from src.domain.exceptions import SkillCapExceeded, SkillDraftNotFound, SkillFileMissing
from src.domain.skill import Skill, SkillFile, sha256_text
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
SK, DR = "eval_skills", "eval_skill_drafts"
NOW = UTC_MIDNIGHT.utc
BODY = "Book the car service and bring the logbook"
LOGBOOK = "Bring the logbook"
DUE = "Next service due 2026-11-05"


def _adapter(db: EvalDb) -> FirestoreSkillRepository:
    return FirestoreSkillRepository(db, SimpleNamespace(skills_collection=SK, skill_drafts_collection=DR))


class Store:
    def __init__(self, kit_db) -> None:
        self.db = kit_db(indexes=INDEXES_FILE, groups={SK: "development_skills", DR: "development_skill_drafts"})
        self.repo = _adapter(self.db)

    def docs(self, collection: str) -> dict:
        return self.db.collection(collection).docs

    def versions(self, user="u1", name="car-service") -> dict:
        return self.docs(f"{SK}/{user}:{name}/versions")

    def files(self, user="u1", name="car-service") -> dict:
        return self.docs(f"{SK}/{user}:{name}/files")

    def draft_files(self, code, user="u1") -> dict:
        return self.docs(f"{DR}/{user}:{code}/draft_files")

    def everything(self) -> dict:
        """Every stored document of every collection, for "nothing was written" assertions."""
        return {name: dict(col.docs) for name, col in self.db.cols.items() if col.docs}


@pytest.fixture
def store(monkeypatch, kit_db):
    pin_datetime(monkeypatch, firestore_skill_repository, NOW)
    return Store(kit_db)


def fil(path="notes/logbook.md", content=LOGBOOK) -> SkillFile:
    return SkillFile(path=path, sha256=sha256_text(content), size=len(content.encode()))


def skill(name="car-service", body=BODY, files=(), description="Steps for the yearly car service") -> Skill:
    return Skill(name=name, description=description, body=body, files=list(files))


def seed_file(store, content=LOGBOOK, user="u1", name="car-service") -> str:
    sha = sha256_text(content)
    store.files(user, name)[sha] = {"content": content, "size": len(content.encode()), "created_at": NOW}
    return sha


# --------------------------------------------------------------------------------------------- save_version

async def test_the_first_save_writes_the_index_document_and_version_one_and_no_files(store):
    assert await store.repo.save_version("u1", "a1", skill(), cap=20) == 1
    index = store.docs(SK)["u1:car-service"]
    assert index == {"user_id": "u1", "account_id": "a1", "name": "car-service", "description": "Steps for the yearly car service",
                     "body": BODY, "files": [], "current": 1, "updated_at": NOW}
    assert store.versions() == {"v1": {"name": "car-service", "description": "Steps for the yearly car service",
                                       "body": BODY, "files": [], "saved_at": NOW}}
    assert store.files() == {}


async def test_a_second_save_increments_the_version_and_keeps_the_history(store):
    await store.repo.save_version("u1", "a1", skill(), cap=20)
    assert await store.repo.save_version("u1", "a1", skill(body="Book the car service for 2026-11-05"), cap=20) == 2
    assert store.docs(SK)["u1:car-service"]["current"] == 2
    assert store.docs(SK)["u1:car-service"]["body"] == "Book the car service for 2026-11-05"
    assert sorted(store.versions()) == ["v1", "v2"]
    assert store.versions()["v1"]["body"] == BODY
    assert (await store.repo.get_current("u1", "car-service")).version == 2


async def test_saving_from_a_draft_copies_the_staged_files_and_consumes_every_draft_of_that_name(store):
    logbook = fil()
    skill_with_file = skill(files=[logbook])
    await store.repo.create_draft("u1", "ab12", skill_with_file, staged={logbook.sha256: LOGBOOK})
    await store.repo.create_draft("u1", "cd34", skill(body="another take"), staged={sha256_text(DUE): DUE})
    await store.repo.create_draft("u1", "ef56", skill(name="library-card"))
    assert await store.repo.save_version("u1", "a1", skill_with_file, cap=20, consume_drafts_named="car-service",
                                         draft_code="ab12") == 1
    assert store.files() == {logbook.sha256: {"content": LOGBOOK, "size": len(LOGBOOK.encode()), "created_at": NOW}}
    assert store.docs(SK)["u1:car-service"]["files"] == [logbook.model_dump()]
    assert store.versions()["v1"]["files"] == [logbook.model_dump()]
    assert list(store.docs(DR)) == ["u1:ef56"]
    assert store.draft_files("ab12") == {} and store.draft_files("cd34") == {}


async def test_an_inherited_file_that_is_stored_is_accepted(store):
    logbook = fil()
    seed_file(store)
    assert await store.repo.save_version("u1", "a1", skill(files=[logbook]), cap=20) == 1
    assert store.docs(SK)["u1:car-service"]["files"] == [logbook.model_dump()]
    assert list(store.files()) == [logbook.sha256]


@pytest.mark.h1_only("lists every stored document through the fake's collection map (cols)")
async def test_an_inherited_file_that_is_missing_raises_naming_the_path_and_writes_nothing(store):
    with pytest.raises(SkillFileMissing) as raised:
        await store.repo.save_version("u1", "a1", skill(files=[fil()]), cap=20)
    assert "notes/logbook.md" in str(raised.value) and fil().sha256 not in str(raised.value)
    assert store.everything() == {}


@pytest.mark.h1_only("lists every stored document through the fake's collection map (cols)")
async def test_a_staged_file_that_expired_raises_and_writes_nothing(store):
    logbook = fil()
    await store.repo.create_draft("u1", "ab12", skill(files=[logbook]), staged={logbook.sha256: LOGBOOK})
    del store.draft_files("ab12")[logbook.sha256]
    before = store.everything()
    with pytest.raises(SkillFileMissing):
        await store.repo.save_version("u1", "a1", skill(files=[logbook]), cap=20, consume_drafts_named="car-service",
                                      draft_code="ab12")
    assert store.everything() == before
    assert "u1:car-service" not in store.docs(SK)


@pytest.mark.h1_only("lists every stored document through the fake's collection map (cols)")
async def test_a_draft_that_is_gone_raises_draft_not_found_and_writes_nothing(store):
    with pytest.raises(SkillDraftNotFound):
        await store.repo.save_version("u1", "a1", skill(), cap=20, draft_code="zz99")
    assert store.everything() == {}


async def test_the_cap_counts_only_this_users_skills_and_an_update_at_the_cap_is_not_a_new_skill(store):
    for name in ("passport-renewal", "library-card", "garden-shed"):
        await store.repo.save_version("u2", "a2", skill(name=name), cap=3)
    await store.repo.save_version("u1", "a1", skill(name="passport-renewal"), cap=3)
    await store.repo.save_version("u1", "a1", skill(name="library-card"), cap=3)
    assert await store.repo.save_version("u1", "a1", skill(name="garden-shed"), cap=3) == 1     # u2's three do not count
    with pytest.raises(SkillCapExceeded):
        await store.repo.save_version("u1", "a1", skill(name="gym-schedule"), cap=3)
    assert "u1:gym-schedule" not in store.docs(SK)
    assert await store.repo.save_version("u1", "a1", skill(name="passport-renewal", body="new body"), cap=3) == 2


@pytest.mark.h1_only("requires the forced interleaving from the fake's query log (executed)")
async def test_two_concurrent_first_saves_of_one_name_get_versions_one_and_two(store):
    store.db.collection(SK).yield_on_get = True      # both saves read the index document before either commits
    got = await asyncio.gather(store.repo.save_version("u1", "a1", skill(body="first"), cap=20),
                               store.repo.save_version("u1", "a1", skill(body="second"), cap=20))
    require(len(store.db.collection(SK).executed) == 2, "both saves read the name as new (each ran the cap query)")
    assert sorted(got) == [1, 2]
    assert sorted(store.versions()) == ["v1", "v2"] and store.docs(SK)["u1:car-service"]["current"] == 2


@pytest.mark.h1_only("requires the forced interleaving from the fake's query log (executed)")
async def test_two_concurrent_saves_of_one_draft_code_save_once_and_the_loser_finds_no_draft(store):
    await store.repo.create_draft("u1", "ab12", skill())
    store.db.collection(DR).yield_on_get = True      # both saves read the draft before either commits
    got = await asyncio.gather(
        store.repo.save_version("u1", "a1", skill(), cap=20, consume_drafts_named="car-service", draft_code="ab12"),
        store.repo.save_version("u1", "a1", skill(), cap=20, consume_drafts_named="car-service", draft_code="ab12"),
        return_exceptions=True)
    require(len(store.db.collection(DR).executed) == 3, "both saves read the draft; the loser re-ran its transaction")
    assert sorted(type(r).__name__ for r in got) == ["SkillDraftNotFound", "int"] and 1 in got
    assert list(store.versions()) == ["v1"] and list(store.docs(DR)) == []


# ------------------------------------------------------------------------------------- list_current, get_*

def raw_index(store, key, user="u1", **over):
    doc = {"user_id": user, "account_id": "a1", "name": key, "description": "d", "body": BODY, "files": [], "current": 1,
           "updated_at": NOW}
    doc.update(over)
    store.docs(SK)[f"{user}:{key}"] = doc


async def test_list_current_is_sorted_by_name_scoped_to_the_user_and_skips_corrupt_documents(store):
    await store.repo.save_version("u1", "a1", skill(name="library-card"), cap=20)
    await store.repo.save_version("u1", "a1", skill(name="passport-renewal"), cap=20)
    await store.repo.save_version("u2", "a2", skill(name="garden-shed"), cap=20)
    raw_index(store, "no-current")
    del store.docs(SK)["u1:no-current"]["current"]
    raw_index(store, "bad-name", name="Bad Name")
    assert [s.name for s in await store.repo.list_current("u1")] == ["library-card", "passport-renewal"]
    assert [s.name for s in await store.repo.list_current("u2")] == ["garden-shed"]
    assert await store.repo.list_current("u3") == []


async def test_list_current_carries_the_current_version_and_the_file_manifest(store):
    logbook = fil()
    seed_file(store)
    await store.repo.save_version("u1", "a1", skill(files=[logbook]), cap=20)
    await store.repo.save_version("u1", "a1", skill(files=[logbook], body="v2 body"), cap=20)
    [got] = await store.repo.list_current("u1")
    assert (got.version, got.body, got.files) == (2, "v2 body", [logbook])


async def test_get_current_returns_the_skill_and_a_missing_or_corrupt_one_is_none(store):
    await store.repo.save_version("u1", "a1", skill(), cap=20)
    got = await store.repo.get_current("u1", "car-service")
    assert (got.name, got.body, got.version) == ("car-service", BODY, 1)
    assert await store.repo.get_current("u2", "car-service") is None
    assert await store.repo.get_current("u1", "unknown") is None
    del store.docs(SK)["u1:car-service"]["current"]
    assert await store.repo.get_current("u1", "car-service") is None
    store.docs(SK)["u1:car-service"].update(current=1, description="x" * 300)
    assert await store.repo.get_current("u1", "car-service") is None


async def test_get_file_is_scoped_to_the_user_and_the_skill(store):
    sha = seed_file(store)
    assert await store.repo.get_file("u1", "car-service", sha) == LOGBOOK
    assert await store.repo.get_file("u2", "car-service", sha) is None
    assert await store.repo.get_file("u1", "library-card", sha) is None
    assert await store.repo.get_file("u1", "car-service", sha256_text("nothing")) is None


# ------------------------------------------------------------------------------------------------ drafts

async def test_a_second_create_of_one_code_returns_false_and_keeps_the_first_draft(store):
    assert await store.repo.create_draft("u1", "ab12", skill(body="first")) is True
    staged = {sha256_text(DUE): DUE}
    assert await store.repo.create_draft("u1", "ab12", skill(body="second", files=[fil("due.md", DUE)]), staged=staged) is False
    assert (await store.repo.get_draft("u1", "ab12")).body == "first"
    assert store.docs(DR)["u1:ab12"]["staged"] == []
    # the second call's staged file landed under the first draft: content-addressed, harmless, expires with the TTL
    assert list(store.draft_files("ab12")) == [sha256_text(DUE)]


async def test_a_draft_and_its_staged_files_expire_thirty_days_after_creation(store):
    logbook = fil()
    await store.repo.create_draft("u1", "ab12", skill(files=[logbook]), staged={logbook.sha256: LOGBOOK})
    doc = store.docs(DR)["u1:ab12"]
    assert (doc["user_id"], doc["name"], doc["body"], doc["staged"], doc["created_at"], doc["expires_at"]) == (
        "u1", "car-service", BODY, [logbook.sha256], NOW, NOW + timedelta(days=30))
    assert store.draft_files("ab12") == {logbook.sha256: {"content": LOGBOOK, "size": len(LOGBOOK.encode()),
                                                           "created_at": NOW, "expires_at": NOW + timedelta(days=30)}}


async def test_get_draft_is_scoped_to_the_user_and_an_expired_draft_reads_none(store, monkeypatch):
    await store.repo.create_draft("u1", "ab12", skill())
    assert (await store.repo.get_draft("u1", "ab12")).name == "car-service"
    assert await store.repo.get_draft("u2", "ab12") is None
    assert await store.repo.get_draft("u1", "zz99") is None
    pin_datetime(monkeypatch, firestore_skill_repository, NOW + timedelta(days=30) - timedelta(seconds=1))
    assert await store.repo.get_draft("u1", "ab12") is not None
    pin_datetime(monkeypatch, firestore_skill_repository, NOW + timedelta(days=30))
    assert await store.repo.get_draft("u1", "ab12") is None            # at the instant: expired, before the TTL sweep


@pytest.mark.h1_only("seeds a naive datetime raw and asserts the fake keeps it naive")
async def test_get_draft_reads_a_naive_expiry_as_utc(store):
    base = {"user_id": "u1", "name": "car-service", "description": "d", "body": BODY, "files": [], "staged": []}
    store.docs(DR)["u1:aware"] = {**base, "expires_at": NOW - timedelta(days=1)}
    store.docs(DR)["u1:naive"] = {**base, "expires_at": NOW.replace(tzinfo=None) - timedelta(days=1)}
    assert store.docs(DR)["u1:naive"]["expires_at"].tzinfo is None       # a raw document: the fake stores it as written
    assert await store.repo.get_draft("u1", "aware") is None
    assert await store.repo.get_draft("u1", "naive") is None


async def test_a_corrupt_draft_reads_none(store):
    await store.repo.create_draft("u1", "ab12", skill())
    del store.docs(DR)["u1:ab12"]["body"]
    assert await store.repo.get_draft("u1", "ab12") is None


async def test_get_draft_files_returns_the_staged_contents_and_an_empty_dict_when_none_are_staged(store):
    logbook, due = fil(), fil("due.md", DUE)
    await store.repo.create_draft("u1", "ab12", skill(files=[logbook, due]),
                                  staged={logbook.sha256: LOGBOOK, due.sha256: DUE})
    await store.repo.create_draft("u1", "cd34", skill())
    assert await store.repo.get_draft_files("u1", "ab12") == {logbook.sha256: LOGBOOK, due.sha256: DUE}
    assert await store.repo.get_draft_files("u1", "cd34") == {}


async def test_get_draft_files_raises_when_a_staged_file_or_the_draft_is_gone(store):
    logbook = fil()
    await store.repo.create_draft("u1", "ab12", skill(files=[logbook]), staged={logbook.sha256: LOGBOOK})
    del store.draft_files("ab12")[logbook.sha256]
    with pytest.raises(SkillFileMissing):
        await store.repo.get_draft_files("u1", "ab12")
    with pytest.raises(SkillDraftNotFound):
        await store.repo.get_draft_files("u1", "zz99")
    with pytest.raises(SkillDraftNotFound):
        await store.repo.get_draft_files("u2", "ab12")


# ------------------------------------------------------------------------------------------ delete_skill

async def test_delete_skill_removes_the_versions_the_files_and_the_index_document_and_leaves_the_rest(store):
    logbook = fil()
    seed_file(store)
    await store.repo.save_version("u1", "a1", skill(files=[logbook]), cap=20)
    await store.repo.save_version("u1", "a1", skill(files=[logbook], body="v2"), cap=20)
    await store.repo.save_version("u1", "a1", skill(name="library-card"), cap=20)
    await store.repo.save_version("u2", "a2", skill(), cap=20)
    await store.repo.create_draft("u1", "ab12", skill())
    assert await store.repo.delete_skill("u1", "car-service") is True
    assert sorted(store.docs(SK)) == ["u1:library-card", "u2:car-service"]
    assert store.versions() == {} and store.files() == {}
    assert list(store.versions("u1", "library-card")) == ["v1"] and list(store.versions("u2")) == ["v1"]
    assert list(store.docs(DR)) == ["u1:ab12"]
    assert await store.repo.delete_skill("u1", "car-service") is False
    assert await store.repo.delete_skill("u3", "car-service") is False


@pytest.mark.h1_only("counts the fake's batch commits (commits)")
async def test_delete_skill_over_one_chunk_commits_in_batches_and_deletes_every_document(store):
    await store.repo.save_version("u1", "a1", skill(), cap=20)
    files = store.files()
    for i in range(460):
        files[sha256_text(f"file {i}")] = {"content": f"file {i}", "size": 6, "created_at": NOW}
    commits_before = len(store.db.commits)
    assert await store.repo.delete_skill("u1", "car-service") is True
    assert len(store.db.commits) - commits_before == 2
    assert store.files() == {} and store.versions() == {} and "u1:car-service" not in store.docs(SK)


async def test_delete_skill_failing_midway_leaves_the_skill_listed_and_a_repeat_finishes(store, monkeypatch):
    await store.repo.save_version("u1", "a1", skill(), cap=20)
    files = store.files()
    for i in range(460):
        files[sha256_text(f"file {i}")] = {"content": f"file {i}", "size": 6, "created_at": NOW}
    real_batch, commits = store.db.batch, []

    def batch_whose_second_commit_fails():
        batch = real_batch()
        real_commit = batch.commit

        async def commit():
            commits.append(1)
            if len(commits) == 2:
                raise ServiceUnavailable("down")
            await real_commit()
        batch.commit = commit
        return batch

    monkeypatch.setattr(store.db, "batch", batch_whose_second_commit_fails)
    with pytest.raises(ServiceUnavailable):
        await store.repo.delete_skill("u1", "car-service")
    require(len(commits) == 2, "the first batch landed and the second failed")
    assert [s.name for s in await store.repo.list_current("u1")] == ["car-service"]       # the index document is last
    monkeypatch.setattr(store.db, "batch", real_batch)
    assert await store.repo.delete_skill("u1", "car-service") is True
    assert store.files() == {} and store.versions() == {} and await store.repo.list_current("u1") == []


# ------------------------------------------------------- moved from the retired mock-SDK files (G3 store 4)

async def test_create_draft_stores_the_whole_draft_document(store):
    logbook = fil()
    assert await store.repo.create_draft("u1", "ab12", skill(files=[logbook]), staged={logbook.sha256: LOGBOOK})
    assert await store.repo.create_draft("u1", "cd34", skill(name="library-card"))
    assert store.docs(DR)["u1:ab12"] == {
        "user_id": "u1", "name": "car-service", "description": "Steps for the yearly car service", "body": BODY,
        "files": [logbook.model_dump()], "staged": [logbook.sha256], "created_at": NOW,
        "expires_at": NOW + timedelta(days=30)}
    assert (store.docs(DR)["u1:cd34"]["files"], store.docs(DR)["u1:cd34"]["staged"]) == ([], [])
    assert store.draft_files("cd34") == {}


@pytest.mark.h1_only("replaces the document factory of the fake's staged-files collection with one whose write fails")
async def test_create_draft_writes_the_staged_files_before_the_draft(store, monkeypatch):
    def failing_document(doc_id=None):
        async def set(data, merge=False):
            raise ServiceUnavailable("down")
        return SimpleNamespace(set=set)

    monkeypatch.setattr(store.db.collection(f"{DR}/u1:ab12/draft_files"), "document", failing_document)
    logbook = fil()
    with pytest.raises(ServiceUnavailable):
        await store.repo.create_draft("u1", "ab12", skill(files=[logbook]), staged={logbook.sha256: LOGBOOK})
    assert "u1:ab12" not in store.docs(DR)              # a valid code never points at missing files


async def test_get_draft_returns_the_drafted_skill_at_version_zero(store):
    logbook = fil()
    await store.repo.create_draft("u1", "ab12", skill(files=[logbook]), staged={logbook.sha256: LOGBOOK})
    assert await store.repo.get_draft("u1", "ab12") == Skill(
        name="car-service", description="Steps for the yearly car service", body=BODY, version=0, files=[logbook])


async def test_get_current_carries_the_current_version_and_the_file_manifest(store):
    logbook = fil()
    seed_file(store)
    for body in ("v1 body", "v2 body", "v3 body"):
        await store.repo.save_version("u1", "a1", skill(files=[logbook], body=body), cap=20)
    got = await store.repo.get_current("u1", "car-service")
    assert (got.version, got.body, got.files) == (3, "v3 body", [logbook])


async def test_a_corrupt_draft_or_skill_reads_none_and_logs_an_error(store, caplog):
    await store.repo.create_draft("u1", "ab12", skill())
    del store.docs(DR)["u1:ab12"]["body"]
    await store.repo.save_version("u1", "a1", skill(), cap=20)
    store.docs(SK)["u1:car-service"]["files"] = [{"path": "../x.md", "sha256": sha256_text(LOGBOOK), "size": 17}]
    with caplog.at_level(logging.ERROR):
        assert await store.repo.get_draft("u1", "ab12") is None
        assert await store.repo.get_current("u1", "car-service") is None
    assert [r.levelno for r in caplog.records if "[Skills]" in r.getMessage()] == [logging.ERROR, logging.ERROR]


async def test_saving_consumes_only_this_users_drafts_of_that_name(store):
    await store.repo.create_draft("u1", "ab12", skill())
    await store.repo.create_draft("u2", "ab12", skill())
    await store.repo.save_version("u1", "a1", skill(), cap=20, consume_drafts_named="car-service")
    assert list(store.docs(DR)) == ["u2:ab12"]


async def test_a_save_without_consume_keeps_the_drafts_of_that_name(store):
    due = fil("due.md", DUE)
    await store.repo.create_draft("u1", "ab12", skill(files=[due]), staged={due.sha256: DUE})
    assert await store.repo.save_version("u1", "a1", skill(), cap=20) == 1
    assert list(store.docs(DR)) == ["u1:ab12"] and list(store.draft_files("ab12")) == [due.sha256]


async def test_saving_a_draft_that_adds_a_file_keeps_the_inherited_one(store):
    logbook, due = fil(), fil("due.md", DUE)
    earlier = NOW - timedelta(days=3)
    store.files()[logbook.sha256] = {"content": LOGBOOK, "size": len(LOGBOOK.encode()), "created_at": earlier}
    await store.repo.save_version("u1", "a1", skill(files=[logbook]), cap=20)
    both = skill(files=[due, logbook], body="Book the service; the due date is in due.md")
    await store.repo.create_draft("u1", "ab12", both, staged={due.sha256: DUE})
    assert await store.repo.save_version("u1", "a1", both, cap=20, consume_drafts_named="car-service",
                                         draft_code="ab12") == 2
    assert store.files() == {
        logbook.sha256: {"content": LOGBOOK, "size": len(LOGBOOK.encode()), "created_at": earlier},
        due.sha256: {"content": DUE, "size": len(DUE.encode()), "created_at": NOW}}
    manifest = [due.model_dump(), logbook.model_dump()]
    assert store.docs(SK)["u1:car-service"]["files"] == manifest and store.versions()["v2"]["files"] == manifest
    assert list(store.docs(DR)) == [] and store.draft_files("ab12") == {}


async def test_an_older_draft_saves_its_own_manifest_over_a_newer_current(store):
    logbook, due = fil(), fil("due.md", DUE)
    seed_file(store)
    seed_file(store, DUE)
    await store.repo.save_version("u1", "a1", skill(files=[logbook]), cap=20)
    await store.repo.create_draft("u1", "ab12", skill(files=[logbook], body="old body"))
    await store.repo.save_version("u1", "a1", skill(files=[due], body="newer body"), cap=20)
    assert await store.repo.save_version("u1", "a1", skill(files=[logbook], body="old body"), cap=20,
                                         consume_drafts_named="car-service", draft_code="ab12") == 3
    index = store.docs(SK)["u1:car-service"]
    assert (index["files"], index["body"], index["current"]) == ([logbook.model_dump()], "old body", 3)


@pytest.mark.h1_only("counts the fake's batch commits (commits)")
async def test_delete_skill_of_a_small_skill_is_one_batch_and_of_a_missing_one_none(store):
    seed_file(store)
    await store.repo.save_version("u1", "a1", skill(files=[fil()]), cap=20)
    commits_before = len(store.db.commits)
    assert await store.repo.delete_skill("u1", "car-service") is True
    assert len(store.db.commits) - commits_before == 1
    assert await store.repo.delete_skill("u1", "car-service") is False
    assert len(store.db.commits) - commits_before == 1


@pytest.mark.h1_only("drives the fake's interference hook and query log (interfere_on_txn_query, executed)")
async def test_the_cap_query_is_part_of_the_save_transaction(store):
    """Another commit to a skill the cap query returned re-runs the save of a new name (its count is re-read)."""
    await store.repo.save_version("u1", "a1", skill(name="library-card"), cap=20)
    queries_before = len(store.db.collection(SK).executed)
    store.db.interfere_on_txn_query(SK, "u1:library-card", {"description": "Renew the library card"})
    assert await store.repo.save_version("u1", "a1", skill(), cap=20) == 1
    assert len(store.db.collection(SK).executed) - queries_before == 2


@pytest.mark.h1_only("drives the fake's interference hooks and query log (interfere_on_txn_*, executed)")
@pytest.mark.parametrize("read", ["drafts query", "draft", "staged file", "inherited file"])
async def test_every_read_of_a_save_from_a_draft_is_part_of_its_transaction(store, read):
    """Another commit to anything the save read re-runs it (the drafts query runs again), and the save then lands."""
    logbook, due = fil(), fil("due.md", DUE)
    seed_file(store)
    await store.repo.save_version("u1", "a1", skill(files=[logbook]), cap=20)
    both = skill(files=[due, logbook])
    await store.repo.create_draft("u1", "ab12", both, staged={due.sha256: DUE})
    if read == "drafts query":
        store.db.interfere_on_txn_query(DR, "u1:ab12", {"body": "edited"})
    elif read == "draft":
        store.db.interfere_on_txn_read(DR, "u1:ab12", {"body": "edited"}, after_read=True)
    elif read == "staged file":
        store.db.interfere_on_txn_read(f"{DR}/u1:ab12/draft_files", due.sha256, {"size": 99}, after_read=True)
    else:
        store.db.interfere_on_txn_read(f"{SK}/u1:car-service/files", logbook.sha256, {"size": 99}, after_read=True)
    assert await store.repo.save_version("u1", "a1", both, cap=20, consume_drafts_named="car-service",
                                         draft_code="ab12") == 2
    assert len(store.db.collection(DR).executed) == 2
    assert list(store.docs(DR)) == [] and sorted(store.files()) == sorted([logbook.sha256, due.sha256])


def _record_get_all(store, monkeypatch) -> list:
    """Each `get_all` the adapter sends, as (document ids, field mask, inside a transaction)."""
    calls, real = [], store.db.get_all

    def recording(refs, field_paths=None, transaction=None):
        calls.append(([r.id for r in refs], field_paths, transaction is not None))
        return real(refs, field_paths=field_paths, transaction=transaction)

    monkeypatch.setattr(store.db, "get_all", recording)
    return calls


def _refuse_reads(collection) -> None:
    def refused(*args, **kwargs):
        raise AssertionError(f"{collection.name} was read")
    collection.get = collection.stream = refused


@pytest.mark.h1_only("records the fake's get_all and refuses reads of its collections")
async def test_a_save_reads_the_staged_then_the_inherited_files_once_each(store, monkeypatch):
    """Staged contents by ref inside the transaction; inherited files by a `size` mask (no content); the other consumed
    draft's staged files are deleted by ref, never read."""
    logbook, due = fil(), fil("due.md", DUE)
    seed_file(store)
    await store.repo.save_version("u1", "a1", skill(files=[logbook]), cap=20)
    both = skill(files=[due, logbook])
    await store.repo.create_draft("u1", "ab12", both, staged={due.sha256: DUE})
    await store.repo.create_draft("u1", "cd34", skill(body="another take", files=[fil("x.md", "X")]),
                                  staged={sha256_text("X"): "X"})
    other = store.db.collection(f"{DR}/u1:cd34/draft_files")
    _refuse_reads(other)
    real_document = other.document

    def unreadable_document(doc_id=None):
        ref = real_document(doc_id)

        async def get(*args, **kwargs):
            raise AssertionError("a consumed draft's staged file was read")
        ref.get = get
        return ref

    monkeypatch.setattr(other, "document", unreadable_document)
    calls = _record_get_all(store, monkeypatch)
    await store.repo.save_version("u1", "a1", both, cap=20, consume_drafts_named="car-service", draft_code="ab12")
    assert calls == [([due.sha256], None, True), ([logbook.sha256], ["size"], True)]
    assert list(store.docs(DR)) == [] and store.draft_files("cd34") == {}


@pytest.mark.h1_only("records the fake's get_all")
async def test_a_fileless_save_and_an_empty_staging_read_no_files(store, monkeypatch):
    await store.repo.create_draft("u1", "ab12", skill())
    calls = _record_get_all(store, monkeypatch)
    assert await store.repo.save_version("u1", "a1", skill(name="library-card"), cap=20) == 1
    assert await store.repo.get_draft_files("u1", "ab12") == {}
    assert await store.repo.save_version("u1", "a1", skill(), cap=20, draft_code="ab12") == 1
    assert calls == []


@pytest.mark.h1_only("counts the fake's batch commits and refuses reads of its collections")
async def test_delete_skill_lists_the_file_refs_without_reading_them_and_deletes_in_chunks_of_450(store, monkeypatch):
    await store.repo.save_version("u1", "a1", skill(), cap=20)
    files = store.files()
    for i in range(460):
        files[sha256_text(f"file {i}")] = {"content": f"file {i}", "size": 6, "created_at": NOW}
    files_col = store.db.collection(f"{SK}/u1:car-service/files")
    versions_col = store.db.collection(f"{SK}/u1:car-service/versions")
    _refuse_reads(files_col)
    order, real_list, real_versions_get = [], files_col.list_documents, versions_col.get

    def listing(*args, **kwargs):
        order.append("files")
        return real_list(*args, **kwargs)

    async def versions_get(*args, **kwargs):
        order.append("versions")
        return await real_versions_get(*args, **kwargs)

    monkeypatch.setattr(files_col, "list_documents", listing)
    monkeypatch.setattr(versions_col, "get", versions_get)
    commits_before = len(store.db.commits)
    assert await store.repo.delete_skill("u1", "car-service") is True
    commits = store.db.commits[commits_before:]
    assert order == ["files", "versions"]
    assert [len(c) for c in commits] == [450, 12]
    last = commits[-1][-1][1]
    assert (last.col.name, last.id) == (SK, "u1:car-service")        # the index document is the last write
    assert store.files() == {} and store.versions() == {}


@pytest.mark.h1_only("replaces the document factory of the fake's staged-files and drafts collections")
async def test_create_draft_writes_the_staged_files_concurrently_then_the_draft(store, monkeypatch):
    logbook, due = fil(), fil("due.md", DUE)
    events, both_started = [], asyncio.Event()
    files_col, drafts_col = store.db.collection(f"{DR}/u1:ab12/draft_files"), store.db.collection(DR)
    real_file_document, real_draft_document = files_col.document, drafts_col.document

    def file_document(doc_id=None):
        ref = real_file_document(doc_id)
        real_set = ref.set

        async def set(data, merge=False):
            events.append(f"start {doc_id[:4]}")
            if sum(e.startswith("start") for e in events) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)   # a sequential writer never starts the second
            return await real_set(data, merge=merge)
        ref.set = set
        return ref

    def draft_document(doc_id=None):
        ref = real_draft_document(doc_id)
        real_create = ref.create

        async def create(data):
            events.append("draft")
            return await real_create(data)
        ref.create = create
        return ref

    monkeypatch.setattr(files_col, "document", file_document)
    monkeypatch.setattr(drafts_col, "document", draft_document)
    assert await store.repo.create_draft("u1", "ab12", skill(files=[logbook, due]),
                                         staged={logbook.sha256: LOGBOOK, due.sha256: DUE}) is True
    assert sorted(events[:2]) == sorted([f"start {logbook.sha256[:4]}", f"start {due.sha256[:4]}"])
    assert events[-1] == "draft" and sorted(store.draft_files("ab12")) == sorted([logbook.sha256, due.sha256])


async def test_a_draft_without_an_expiry_is_read(store):
    store.docs(DR)["u1:ab12"] = {"user_id": "u1", "name": "car-service", "description": "d", "body": BODY, "files": [],
                                 "staged": [], "created_at": NOW}
    assert (await store.repo.get_draft("u1", "ab12")).body == BODY

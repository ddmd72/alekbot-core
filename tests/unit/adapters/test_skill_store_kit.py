"""SkillRepository kit (P11, G3 store 4): the docstring of `src/ports/skill_repository.py` checked against
`FirestoreSkillRepository` on the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on the raw
stored documents (index document, `versions/v<n>`, `files/<sha256>`, drafts and `draft_files/<sha256>`) and on what the
readers return.

Not carded: the cap race of two concurrent first saves of two new names (H1's transactions give no phantom protection,
so it can be neither shown nor excluded here; it waits for a parity scenario).

`_adapter(db)` is the one place the adapter is constructed.
"""
import asyncio
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
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={SK: "development_skills", DR: "development_skill_drafts"})
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
def store(monkeypatch):
    pin_datetime(monkeypatch, firestore_skill_repository, NOW)
    return Store()


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


async def test_an_inherited_file_that_is_missing_raises_naming_the_path_and_writes_nothing(store):
    with pytest.raises(SkillFileMissing) as raised:
        await store.repo.save_version("u1", "a1", skill(files=[fil()]), cap=20)
    assert "notes/logbook.md" in str(raised.value) and fil().sha256 not in str(raised.value)
    assert store.everything() == {}


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


async def test_two_concurrent_first_saves_of_one_name_get_versions_one_and_two(store):
    store.db.collection(SK).yield_on_get = True      # both saves read the index document before either commits
    got = await asyncio.gather(store.repo.save_version("u1", "a1", skill(body="first"), cap=20),
                               store.repo.save_version("u1", "a1", skill(body="second"), cap=20))
    require(len(store.db.collection(SK).executed) == 2, "both saves read the name as new (each ran the cap query)")
    assert sorted(got) == [1, 2]
    assert sorted(store.versions()) == ["v1", "v2"] and store.docs(SK)["u1:car-service"]["current"] == 2


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


async def test_a_legacy_index_document_without_current_cannot_be_saved_to(store):
    store.docs(SK)["u1:car-service"] = {"user_id": "u1", "account_id": "a1", "name": "car-service", "description": "d", "body": BODY}
    with pytest.raises(KeyError):
        await store.repo.save_version("u1", "a1", skill(), cap=20)


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


async def test_get_draft_reads_a_naive_expiry_as_utc_and_a_draft_without_expiry_never_expires(store):
    base = {"user_id": "u1", "name": "car-service", "description": "d", "body": BODY, "files": [], "staged": []}
    store.docs(DR)["u1:aware"] = {**base, "expires_at": NOW - timedelta(days=1)}
    store.docs(DR)["u1:naive"] = {**base, "expires_at": NOW.replace(tzinfo=None) - timedelta(days=1)}
    store.docs(DR)["u1:never"] = dict(base)
    assert store.docs(DR)["u1:naive"]["expires_at"].tzinfo is None       # a raw document: the fake stores it as written
    assert await store.repo.get_draft("u1", "aware") is None
    assert await store.repo.get_draft("u1", "naive") is None
    assert await store.repo.get_draft("u1", "never") is not None


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


async def test_delete_skill_over_one_chunk_commits_in_batches_and_deletes_every_document(store):
    await store.repo.save_version("u1", "a1", skill(), cap=20)
    for i in range(460):
        store.files()[sha256_text(f"file {i}")] = {"content": f"file {i}", "size": 6, "created_at": NOW}
    commits_before = len(store.db.commits)
    assert await store.repo.delete_skill("u1", "car-service") is True
    assert len(store.db.commits) - commits_before == 2
    assert store.files() == {} and store.versions() == {} and "u1:car-service" not in store.docs(SK)


async def test_delete_skill_failing_midway_leaves_the_skill_listed_and_a_repeat_finishes(store, monkeypatch):
    await store.repo.save_version("u1", "a1", skill(), cap=20)
    for i in range(460):
        store.files()[sha256_text(f"file {i}")] = {"content": f"file {i}", "size": 6, "created_at": NOW}
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

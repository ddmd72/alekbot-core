"""FirestoreSkillRepository — text files in a skill (RFC §15.2, §15.5, §15.7)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.firestore_skill_repository import DRAFT_TTL, FirestoreSkillRepository
from src.config.environment import EnvironmentConfig
from src.domain.exceptions import SkillDraftNotFound, SkillFileMissing
from src.domain.skill import Skill, SkillFile, sha256_text


@pytest.fixture
def env_config():
    cfg = MagicMock(spec=EnvironmentConfig)
    cfg.skills_collection = "test_skills"
    cfg.skill_drafts_collection = "test_skill_drafts"
    return cfg


@pytest.fixture
def col():
    return MagicMock()


@pytest.fixture
def col_drafts():
    return MagicMock()


@pytest.fixture
def db(col, col_drafts):
    d = MagicMock()

    def _collection(name):
        if name == "test_skill_drafts":
            return col_drafts
        if name == "test_skills":
            return col
        raise AssertionError(f"unexpected collection: {name}")

    d.collection.side_effect = _collection
    return d


@pytest.fixture
def repo(db, env_config):
    return FirestoreSkillRepository(db, env_config)


def _snap(data, exists=True, id=None):
    s = MagicMock()
    s.exists = exists
    s.id = id
    s.to_dict.return_value = data
    return s


def _aiter(items):
    async def gen(*a, **k):
        for i in items:
            yield i
    return gen


def _ref(label="ref"):
    """A document-ref mock whose `collection(name).document(id)` returns the same child mock
    for the same (name, id), so a test can address the exact ref the code wrote to."""
    ref = MagicMock(name=label)
    cols = {}

    def collection(name):
        if name not in cols:
            c = MagicMock(name=f"{label}/{name}")
            docs = {}

            def document(doc_id, _docs=docs, _name=name):
                if doc_id not in _docs:
                    _docs[doc_id] = _ref(f"{label}/{_name}/{doc_id}")
                return _docs[doc_id]

            c.document.side_effect = document
            cols[name] = c
        return cols[name]

    ref.collection = MagicMock(side_effect=collection)
    return ref


SHA_A = sha256_text("alpha")
SHA_B = sha256_text("beta")


def _file(path, content):
    return SkillFile(path=path, sha256=sha256_text(content), size=len(content.encode("utf-8")))


def _index(name="s", files=None, current=1):
    return {
        "user_id": "u1", "account_id": "a1", "name": name,
        "description": "Use when x.", "body": "b", "current": current,
        "files": files or [],
    }


# --- create_draft -------------------------------------------------------------------------

async def test_create_draft_writes_staged_files_before_draft_doc(repo, col_drafts):
    draft_ref = MagicMock()
    draft_ref.create = AsyncMock()
    file_ref = MagicMock()
    file_ref.set = AsyncMock()
    draft_ref.collection.return_value.document.return_value = file_ref
    col_drafts.document.return_value = draft_ref
    calls = []
    file_ref.set.side_effect = lambda *a, **k: calls.append("file")
    draft_ref.create.side_effect = lambda *a, **k: calls.append("draft")
    content = "ref text"
    skill = Skill(name="s", description="Use when x.", body="b",
                  files=[SkillFile(path="r.md", sha256=sha256_text(content), size=8)])

    ok = await repo.create_draft("u1", "ab12", skill, staged={sha256_text(content): content})

    assert ok and calls == ["file", "draft"]
    draft_ref.collection.assert_called_with("draft_files")
    data = draft_ref.create.call_args.args[0]
    assert data["staged"] == [sha256_text(content)]
    assert data["files"] == [{"path": "r.md", "sha256": sha256_text(content), "size": 8}]
    assert "expires_at" in data and "expires_at" in file_ref.set.call_args.args[0]


async def test_create_draft_ttl_is_thirty_days_and_file_doc_shape(repo, col_drafts):
    draft_ref = _ref("draft")
    draft_ref.create = AsyncMock()
    col_drafts.document.return_value = draft_ref
    file_ref = draft_ref.collection("draft_files").document(SHA_A)
    file_ref.set = AsyncMock()
    skill = Skill(name="s", description="Use when x.", body="b", files=[_file("a.md", "alpha")])

    before = datetime.now(timezone.utc)
    await repo.create_draft("u1", "ab12", skill, staged={SHA_A: "alpha"})

    assert DRAFT_TTL == timedelta(days=30)
    written = file_ref.set.call_args.args[0]
    assert set(written) == {"content", "size", "created_at", "expires_at"}
    assert written["content"] == "alpha" and written["size"] == 5
    assert written["expires_at"] - written["created_at"] == DRAFT_TTL
    data = draft_ref.create.call_args.args[0]
    assert data["expires_at"] - data["created_at"] == DRAFT_TTL
    assert data["created_at"] >= before


async def test_create_draft_without_staged_writes_no_file_docs(repo, col_drafts):
    draft_ref = MagicMock()
    draft_ref.create = AsyncMock()
    col_drafts.document.return_value = draft_ref
    skill = Skill(name="s", description="Use when x.", body="b")

    assert await repo.create_draft("u1", "ab12", skill) is True

    draft_ref.collection.assert_not_called()
    data = draft_ref.create.call_args.args[0]
    assert data["staged"] == [] and data["files"] == []


# --- get_current / get_file ---------------------------------------------------------------

async def test_get_current_maps_files(repo, col):
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=_snap(_index(
        files=[{"path": "references/a.md", "sha256": SHA_A, "size": 5}], current=3,
    )))
    col.document.return_value = doc_ref

    skill = await repo.get_current("u1", "s")

    col.document.assert_called_with("u1:s")
    assert skill.version == 3
    assert skill.files == [SkillFile(path="references/a.md", sha256=SHA_A, size=5)]


async def test_get_current_missing_returns_none(repo, col):
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=_snap(None, exists=False))
    col.document.return_value = doc_ref

    assert await repo.get_current("u1", "s") is None


async def test_get_current_corrupt_manifest_returns_none_and_logs(repo, col):
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=_snap(_index(files=[{"path": "../x.md", "sha256": SHA_A, "size": 5}])))
    col.document.return_value = doc_ref

    with patch("src.adapters.firestore_skill_repository.logger") as mock_logger:
        assert await repo.get_current("u1", "s") is None
    mock_logger.error.assert_called_once()


async def test_get_file_returns_content_or_none(repo, col):
    doc_ref = _ref("skill")
    col.document.return_value = doc_ref
    present = doc_ref.collection("files").document(SHA_A)
    present.get = AsyncMock(return_value=_snap({"content": "alpha", "size": 5}))
    missing = doc_ref.collection("files").document(SHA_B)
    missing.get = AsyncMock(return_value=_snap(None, exists=False))

    assert await repo.get_file("u1", "s", SHA_A) == "alpha"
    assert await repo.get_file("u1", "s", SHA_B) is None
    col.document.assert_called_with("u1:s")


# --- get_draft_files / get_draft ----------------------------------------------------------

async def test_get_draft_files_reads_staged(repo, db, col_drafts):
    draft_ref = _ref("draft")
    draft_ref.get = AsyncMock(return_value=_snap({"name": "s", "staged": [SHA_A, SHA_B]}))
    col_drafts.document.return_value = draft_ref
    db.get_all = MagicMock(side_effect=_aiter([
        _snap({"content": "alpha"}, id=SHA_A), _snap({"content": "beta"}, id=SHA_B),
    ]))

    files = await repo.get_draft_files("u1", "ab12")

    col_drafts.document.assert_called_with("u1:ab12")
    assert files == {SHA_A: "alpha", SHA_B: "beta"}
    refs = db.get_all.call_args.args[0]
    assert refs == [draft_ref.collection("draft_files").document(SHA_A),
                    draft_ref.collection("draft_files").document(SHA_B)]


async def test_get_draft_files_raises_when_a_staged_doc_is_missing(repo, db, col_drafts):
    draft_ref = _ref("draft")
    draft_ref.get = AsyncMock(return_value=_snap({"name": "s", "staged": [SHA_A, SHA_B]}))
    col_drafts.document.return_value = draft_ref
    db.get_all = MagicMock(side_effect=_aiter([
        _snap({"content": "alpha"}, id=SHA_A), _snap(None, exists=False, id=SHA_B),
    ]))

    with pytest.raises(SkillFileMissing):
        await repo.get_draft_files("u1", "ab12")


async def test_get_draft_files_without_staged_skips_get_all(repo, db, col_drafts):
    draft_ref = _ref("draft")
    draft_ref.get = AsyncMock(return_value=_snap({"name": "s"}))
    col_drafts.document.return_value = draft_ref
    db.get_all = MagicMock(side_effect=_aiter([]))

    assert await repo.get_draft_files("u1", "ab12") == {}
    db.get_all.assert_not_called()


async def test_get_draft_files_missing_draft_raises_not_found(repo, db, col_drafts):
    draft_ref = _ref("draft")
    draft_ref.get = AsyncMock(return_value=_snap(None, exists=False))
    col_drafts.document.return_value = draft_ref

    with pytest.raises(SkillDraftNotFound):
        await repo.get_draft_files("u1", "ab12")


async def test_get_draft_returns_none_when_expired(repo, col_drafts):
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=_snap({
        "user_id": "u1", "name": "s", "description": "Use when x.", "body": "b",
        "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
    }))
    col_drafts.document.return_value = doc_ref

    assert await repo.get_draft("u1", "ab12") is None


async def test_get_draft_not_yet_expired_is_returned(repo, col_drafts):
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=_snap({
        "user_id": "u1", "name": "s", "description": "Use when x.", "body": "b",
        "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
    }))
    col_drafts.document.return_value = doc_ref

    assert (await repo.get_draft("u1", "ab12")).name == "s"


async def test_list_current_and_get_draft_include_files(repo, col, col_drafts):
    manifest = [{"path": "a.md", "sha256": SHA_A, "size": 5}]
    query = MagicMock()
    query.get = AsyncMock(return_value=[_snap(_index(files=manifest))])
    col.where.return_value = query
    draft_ref = MagicMock()
    draft_ref.get = AsyncMock(return_value=_snap({
        "user_id": "u1", "name": "s", "description": "Use when x.", "body": "b",
        "files": manifest, "staged": [SHA_A],
    }))
    col_drafts.document.return_value = draft_ref

    listed = await repo.list_current("u1")
    draft = await repo.get_draft("u1", "ab12")

    expected = [SkillFile(path="a.md", sha256=SHA_A, size=5)]
    assert listed[0].files == expected
    assert draft.files == expected


# --- save_version -------------------------------------------------------------------------

class TestSaveVersionWithFiles:
    @pytest.fixture(autouse=True)
    def passthrough_transaction(self):
        with patch(
            "src.adapters.firestore_skill_repository.firestore.async_transactional",
            side_effect=lambda fn: fn,
        ):
            yield

    def _setup(self, db, col, col_drafts, existing_doc, draft_doc=None, consumed=(),
               staged_snaps=(), presence_snaps=()):
        doc_ref = _ref("skill")
        doc_ref.get = AsyncMock(return_value=existing_doc)
        col.document.return_value = doc_ref
        user_query = MagicMock()
        user_query.get = AsyncMock(return_value=[])
        col.where.return_value = user_query

        draft_ref = _ref("draft")
        draft_ref.get = AsyncMock(return_value=draft_doc)
        col_drafts.document.return_value = draft_ref
        drafts_query = MagicMock()
        drafts_query.get = AsyncMock(return_value=list(consumed))
        col_drafts.where.return_value.where.return_value = drafts_query

        def _get_all(refs, field_paths=None, transaction=None):
            return _aiter(presence_snaps if field_paths else staged_snaps)()

        db.get_all = MagicMock(side_effect=_get_all)
        txn = MagicMock()
        db.transaction.return_value = txn
        return doc_ref, draft_ref, txn

    @staticmethod
    def _written(txn):
        return {c.args[0]: c.args[1] for c in txn.set.call_args_list}

    @staticmethod
    def _consumed(staged):
        d = MagicMock()
        d.reference = _ref("consumed")
        d.to_dict.return_value = {"name": "s", "staged": list(staged)}
        return d

    async def test_copies_staged_files_without_expires_at(self, repo, db, col, col_drafts):
        sha_new = sha256_text("N")
        skill = Skill(name="s", description="Use when x.", body="b",
                      files=[_file("new.md", "N"), _file("old.md", "alpha")])
        doc_ref, draft_ref, txn = self._setup(
            db, col, col_drafts, _snap(_index(current=1)),
            draft_doc=_snap({"name": "s", "staged": [sha_new]}),
            staged_snaps=[_snap({"content": "N", "size": 1, "expires_at": "x"}, id=sha_new)],
            presence_snaps=[_snap({"size": 5}, id=SHA_A)],
        )

        version = await repo.save_version("u1", "a1", skill, cap=20, draft_code="ab12")

        assert version == 2
        col_drafts.document.assert_called_with("u1:ab12")
        draft_ref.get.assert_awaited_once_with(transaction=txn)
        written = self._written(txn)
        file_doc = written[doc_ref.collection("files").document(sha_new)]
        assert set(file_doc) == {"content", "size", "created_at"}
        assert file_doc["content"] == "N" and file_doc["size"] == 1
        assert "expires_at" not in file_doc
        # Inherited file is not rewritten.
        assert doc_ref.collection("files").document(SHA_A) not in written

    async def test_staged_contents_read_by_ref_inside_txn(self, repo, db, col, col_drafts):
        sha_new = sha256_text("N")
        skill = Skill(name="s", description="Use when x.", body="b", files=[_file("new.md", "N")])
        doc_ref, draft_ref, txn = self._setup(
            db, col, col_drafts, _snap(_index(current=1)),
            draft_doc=_snap({"name": "s", "staged": [sha_new]}),
            staged_snaps=[_snap({"content": "N"}, id=sha_new)],
        )

        await repo.save_version("u1", "a1", skill, cap=20, draft_code="ab12")

        # Only the staged read: nothing is inherited, so no presence check.
        db.get_all.assert_called_once_with(
            [draft_ref.collection("draft_files").document(sha_new)], transaction=txn,
        )

    async def test_presence_check_uses_field_mask(self, repo, db, col, col_drafts):
        skill = Skill(name="s", description="Use when x.", body="b2", files=[_file("old.md", "alpha")])
        doc_ref, draft_ref, txn = self._setup(
            db, col, col_drafts, _snap(_index(current=1)),
            draft_doc=_snap({"name": "s", "staged": []}),
            presence_snaps=[_snap({"size": 5}, id=SHA_A)],
        )

        await repo.save_version("u1", "a1", skill, cap=20, draft_code="ab12")

        db.get_all.assert_called_once_with(
            [doc_ref.collection("files").document(SHA_A)], field_paths=["size"], transaction=txn,
        )

    async def test_missing_inherited_hash_aborts_without_writes(self, repo, db, col, col_drafts):
        skill = Skill(name="s", description="Use when x.", body="b2", files=[_file("old.md", "alpha")])
        doc_ref, draft_ref, txn = self._setup(
            db, col, col_drafts, _snap(_index(current=1)),
            draft_doc=_snap({"name": "s", "staged": []}),
            presence_snaps=[_snap(None, exists=False, id=SHA_A)],
        )

        with pytest.raises(SkillFileMissing):
            await repo.save_version("u1", "a1", skill, cap=20, draft_code="ab12")

        txn.set.assert_not_called()
        txn.delete.assert_not_called()

    async def test_save_raises_draft_not_found_when_draft_doc_gone_in_txn(self, repo, db, col, col_drafts):
        skill = Skill(name="s", description="Use when x.", body="b")
        doc_ref, draft_ref, txn = self._setup(
            db, col, col_drafts, _snap(_index(current=1)), draft_doc=_snap(None, exists=False),
        )

        with pytest.raises(SkillDraftNotFound):
            await repo.save_version("u1", "a1", skill, cap=20, consume_drafts_named="s", draft_code="ab12")

        txn.set.assert_not_called()
        txn.delete.assert_not_called()

    async def test_save_raises_file_missing_when_staged_snapshot_missing(self, repo, db, col, col_drafts):
        sha_new = sha256_text("N")
        skill = Skill(name="s", description="Use when x.", body="b", files=[_file("new.md", "N")])
        doc_ref, draft_ref, txn = self._setup(
            db, col, col_drafts, _snap(_index(current=1)),
            draft_doc=_snap({"name": "s", "staged": [sha_new]}),
            staged_snaps=[_snap(None, exists=False, id=sha_new)],
        )

        with pytest.raises(SkillFileMissing):
            await repo.save_version("u1", "a1", skill, cap=20, consume_drafts_named="s", draft_code="ab12")

        txn.set.assert_not_called()
        txn.delete.assert_not_called()

    async def test_fileless_save_never_calls_get_all(self, repo, db, col, col_drafts):
        skill = Skill(name="s", description="Use when x.", body="b")
        doc_ref, draft_ref, txn = self._setup(
            db, col, col_drafts, _snap(None, exists=False),
            draft_doc=_snap({"name": "s", "staged": []}),
        )

        assert await repo.save_version("u1", "a1", skill, cap=20, draft_code="ab12") == 1
        db.get_all.assert_not_called()

    async def test_fileless_save_without_draft_code_reads_no_draft(self, repo, db, col, col_drafts):
        """The seeding script saves without a draft code; that path must keep working."""
        skill = Skill(name="s", description="Use when x.", body="b")
        doc_ref, draft_ref, txn = self._setup(db, col, col_drafts, _snap(None, exists=False))

        assert await repo.save_version("u1", "a1", skill, cap=20) == 1
        col_drafts.document.assert_not_called()
        db.get_all.assert_not_called()
        assert self._written(txn)[doc_ref]["files"] == []

    async def test_consumed_drafts_delete_their_draft_files_by_staged_refs(self, repo, db, col, col_drafts):
        sha_new = sha256_text("N")
        skill = Skill(name="s", description="Use when x.", body="b", files=[_file("new.md", "N")])
        consumed_this = self._consumed([sha_new])
        consumed_other = self._consumed([SHA_B])
        doc_ref, draft_ref, txn = self._setup(
            db, col, col_drafts, _snap(_index(current=1)),
            draft_doc=_snap({"name": "s", "staged": [sha_new]}),
            consumed=[consumed_this, consumed_other],
            staged_snaps=[_snap({"content": "N"}, id=sha_new)],
        )

        await repo.save_version("u1", "a1", skill, cap=20, consume_drafts_named="s", draft_code="ab12")

        this_file = consumed_this.reference.collection("draft_files").document(sha_new)
        other_file = consumed_other.reference.collection("draft_files").document(SHA_B)
        txn.delete.assert_any_call(this_file)
        txn.delete.assert_any_call(consumed_this.reference)
        txn.delete.assert_any_call(other_file)
        txn.delete.assert_any_call(consumed_other.reference)
        for ref in (this_file, other_file):
            ref.get.assert_not_called()
        for d in (consumed_this, consumed_other):
            d.reference.collection("draft_files").get.assert_not_called()

    async def test_version_and_index_carry_manifest(self, repo, db, col, col_drafts):
        sha_new = sha256_text("N")
        skill = Skill(name="s", description="Use when x.", body="b",
                      files=[_file("new.md", "N"), _file("old.md", "alpha")])
        doc_ref, draft_ref, txn = self._setup(
            db, col, col_drafts, _snap(_index(current=1)),
            draft_doc=_snap({"name": "s", "staged": [sha_new]}),
            staged_snaps=[_snap({"content": "N"}, id=sha_new)],
            presence_snaps=[_snap({"size": 5}, id=SHA_A)],
        )

        await repo.save_version("u1", "a1", skill, cap=20, draft_code="ab12")

        expected = [
            {"path": "new.md", "sha256": sha_new, "size": 1},
            {"path": "old.md", "sha256": SHA_A, "size": 5},
        ]
        written = self._written(txn)
        assert written[doc_ref.collection("versions").document("v2")]["files"] == expected
        assert written[doc_ref]["files"] == expected
        assert written[doc_ref]["current"] == 2

    async def test_all_reads_happen_before_any_write(self, repo, db, col, col_drafts):
        order = []
        sha_new = sha256_text("N")
        skill = Skill(name="s", description="Use when x.", body="b",
                      files=[_file("new.md", "N"), _file("old.md", "alpha")])
        doc_ref, draft_ref, txn = self._setup(
            db, col, col_drafts, _snap(_index(current=1)),
            draft_doc=_snap({"name": "s", "staged": [sha_new]}),
            consumed=[self._consumed([sha_new])],
            staged_snaps=[_snap({"content": "N"}, id=sha_new)],
            presence_snaps=[_snap({"size": 5}, id=SHA_A)],
        )
        inner = db.get_all.side_effect

        def _tracking_get_all(*a, **k):
            order.append("read")
            return inner(*a, **k)

        db.get_all.side_effect = _tracking_get_all
        txn.set.side_effect = lambda *a, **k: order.append("write")
        txn.delete.side_effect = lambda *a, **k: order.append("write")

        await repo.save_version("u1", "a1", skill, cap=20, consume_drafts_named="s", draft_code="ab12")

        assert order.count("read") == 2
        assert order.index("write") > max(i for i, o in enumerate(order) if o == "read")

    async def test_stale_code_saves_its_own_manifest(self, repo, db, col, col_drafts):
        """An older code saves the manifest that draft carried, even when current moved on."""
        current_files = [{"path": "b.md", "sha256": SHA_B, "size": 4}]
        skill = Skill(name="s", description="Use when x.", body="old body", files=[_file("a.md", "alpha")])
        doc_ref, draft_ref, txn = self._setup(
            db, col, col_drafts, _snap(_index(files=current_files, current=5)),
            draft_doc=_snap({"name": "s", "staged": []}),
            presence_snaps=[_snap({"size": 5}, id=SHA_A)],
        )

        version = await repo.save_version("u1", "a1", skill, cap=20, draft_code="ab12")

        assert version == 6
        written = self._written(txn)
        assert written[doc_ref]["files"] == [{"path": "a.md", "sha256": SHA_A, "size": 5}]
        assert written[doc_ref]["body"] == "old body"
        refs = db.get_all.call_args.args[0]
        assert refs == [doc_ref.collection("files").document(SHA_A)]


# --- delete_skill -------------------------------------------------------------------------

async def test_delete_skill_removes_files_via_list_documents(repo, db, col):
    batches = []

    def _new_batch():
        b = MagicMock()
        b.commit = AsyncMock()
        batches.append(b)
        return b

    db.batch.side_effect = _new_batch
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=_snap({"name": "s"}))
    file_refs = [MagicMock(name=f"file{i}") for i in range(460)]
    files_col = MagicMock()
    files_col.list_documents = MagicMock(side_effect=_aiter(file_refs))
    files_col.get = AsyncMock()
    v1, v2 = MagicMock(), MagicMock()
    versions_col = MagicMock()
    versions_col.get = AsyncMock(return_value=[v1, v2])
    doc_ref.collection.side_effect = lambda name: {"files": files_col, "versions": versions_col}[name]
    col.document.return_value = doc_ref

    assert await repo.delete_skill("u1", "s") is True

    files_col.get.assert_not_called()
    assert [c.args[0] for c in doc_ref.collection.call_args_list] == ["files", "versions"]
    assert len(batches) == 2
    sizes = [b.delete.call_count for b in batches]
    assert sizes == [450, 13] and all(s <= 450 for s in sizes)
    deleted = [c.args[0] for b in batches for c in b.delete.call_args_list]
    assert set(file_refs) <= set(deleted)
    assert v1.reference in deleted and v2.reference in deleted
    # Index doc goes in the final chunk, after everything it points at.
    assert batches[-1].delete.call_args_list[-1].args[0] is doc_ref
    for b in batches:
        b.commit.assert_awaited_once()


async def test_delete_skill_small_is_one_batch(repo, db, col):
    batch = MagicMock()
    batch.commit = AsyncMock()
    db.batch.return_value = batch
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=_snap({"name": "s"}))
    f1 = MagicMock()
    files_col = MagicMock()
    files_col.list_documents = MagicMock(side_effect=_aiter([f1]))
    versions_col = MagicMock()
    versions_col.get = AsyncMock(return_value=[])
    doc_ref.collection.side_effect = lambda name: {"files": files_col, "versions": versions_col}[name]
    col.document.return_value = doc_ref

    assert await repo.delete_skill("u1", "s") is True

    db.batch.assert_called_once()
    batch.delete.assert_any_call(f1)
    batch.delete.assert_any_call(doc_ref)
    batch.commit.assert_awaited_once()

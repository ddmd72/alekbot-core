"""Multi-drive keys (MULTI_DRIVE_RFC §8, §9, §10): the key/ref part of T-4 stage 1 and of I5.

The world is T-4's: kind `fake_a` registered twice (two connections, two accounts) and `fake_b` once, all of one user,
with the same item id under two connections on purpose. Connection-scoped keys never collide across connections and
carry the connection; the user-scoped set stays user-scoped (it is the same for every connection of the user, keyed by
account, drive identity or content as the RFC lists); refs round-trip with their tag.
"""
from datetime import datetime, timezone
from itertools import combinations

import pytest

from src.domain import drive_ref
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_connection import DriveConnection
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_index import Nudge, chunk_doc_id, record_doc_id, sync_state_doc_id
from src.domain.drive_mirror import (
    KIND_ARCHIVE, KIND_FILE, KIND_FOLDER, archive_folder_key, ledger_key, mirror_root_doc_id,
)
from src.domain.drive_note import binding_key, item_binding_key, tagged_ref_key
from src.domain.drive_ref import DriveRef
from src.domain.drive_task_keys import (
    continuation_task_key, nudge_task_key, parse_nudge, payload_scope, scope_payload, tick_payload, tick_task_key,
    toggle_task_key,
)
from src.domain.drive_transcript import transcript_batch_doc_id, transcript_doc_id, transcript_job_doc_id
from src.domain.drive_transcript_windows import transcript_content_key
from src.domain.drive_usage import initial_build_doc_id, usage_doc_id

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
ITEM = "item-1"


def _conn(connection_id, kind, drive, tag, credential):
    return DriveConnection(connection_id=connection_id, user_id="usr-1", account_id="acc-1",
                           credential_id=credential, kind=kind, drive_identity=f"{kind}:{drive}", ref_tag=tag,
                           created_at=NOW)


# fake_a twice (two accounts, two drives), fake_b once; ITEM exists under conn-1 and conn-3
A1 = _conn("conn-1", "fake_a", "drive-1", "a1", "cred-1")
A2 = _conn("conn-2", "fake_a", "drive-2", "b2", "cred-2")
B1 = _conn("conn-3", "fake_b", "drive-3", "c3", "cred-3")
CONNECTIONS = (A1, A2, B1)


def _identity(conn: DriveConnection, item_id: str = ITEM) -> str:
    return f"{conn.drive_identity}:{item_id}"


SCOPED_KEYS = {
    "record": lambda s: record_doc_id(s, ITEM),
    "chunk": lambda s: chunk_doc_id(s, ITEM, 3, 0),
    "sync_state": sync_state_doc_id,
    "transcript_job": lambda s: transcript_job_doc_id(s, ITEM),
    "transcript_batch": lambda s: transcript_batch_doc_id(s, "batches/b-1"),
    "tick_task": lambda s: tick_task_key(s, NOW),
    "continuation_task": lambda s: continuation_task_key(s, "tick-9"),
    "nudge_task": lambda s: nudge_task_key(s, "mut-1", 0),
    "nudge_task_retry": lambda s: nudge_task_key(s, "mut-1", 2),
    "toggle_task": lambda s: toggle_task_key(s, NOW),
}


class TestConnectionScopedKeys:
    @pytest.mark.parametrize("family", sorted(SCOPED_KEYS))
    def test_distinct_connections_never_collide(self, family):
        keys = [SCOPED_KEYS[family](c.scope) for c in CONNECTIONS]
        assert len(set(keys)) == len(keys), keys

    @pytest.mark.parametrize("family", sorted(SCOPED_KEYS))
    def test_every_key_carries_its_connection_and_not_the_user(self, family):
        for conn in CONNECTIONS:
            key = SCOPED_KEYS[family](conn.scope)
            assert key == conn.connection_id or key.startswith(f"{conn.connection_id}:"), key
            assert "usr-1" not in key

    def test_the_formulas(self):
        scope = A1.scope
        assert record_doc_id(scope, ITEM) == "conn-1:item-1"
        assert chunk_doc_id(scope, ITEM, 3, 7) == "conn-1:item-1:3:7"
        assert sync_state_doc_id(scope) == "conn-1"
        assert transcript_job_doc_id(scope, ITEM) == "conn-1:item-1"
        assert transcript_batch_doc_id(scope, "batches/b-1") == "conn-1:batches%2Fb-1"   # escaped, as before
        assert nudge_task_key(scope, "mut-1", 0) == "conn-1:nudge:mut-1"
        assert nudge_task_key(scope, "mut-1", 2) == "conn-1:nudge:mut-1:r2"
        assert continuation_task_key(scope, "tick-9") == "conn-1:cont:tick-9"
        assert toggle_task_key(scope, NOW) == f"conn-1:toggle:{int(NOW.timestamp() * 1000)}"
        assert tick_task_key(scope, NOW).startswith("conn-1:tick:")

    def test_an_item_id_with_colons_cannot_forge_another_connections_key(self):
        # a connection id holds no ':', so `{connection_id}:` is an unambiguous prefix
        assert record_doc_id(ConnectionScope("usr-1", "conn"), "1:item") != record_doc_id(
            ConnectionScope("usr-1", "conn-1"), "item")
        with pytest.raises(ValueError):
            ConnectionScope("usr-1", "conn:1")

    def test_the_tick_bucket_is_the_single_drive_one(self):
        from src.domain.drive_task_keys import tick_key
        assert tick_task_key(A1.scope, NOW).split(":tick:")[1] == tick_key("usr-1", NOW).split(":tick:")[1]


class TestSyncPayload:
    def test_round_trip(self):
        nudge = Nudge(mutation_id="mut-1", item_ids=("item-1", "item-2"), attempt=1)
        payload = scope_payload(A2.scope, nudge)
        assert payload == {"user_id": "usr-1", "connection_id": "conn-2",
                           "nudge": {"mutation_id": "mut-1", "item_ids": ["item-1", "item-2"], "attempt": 1}}
        assert payload_scope(payload) == A2.scope
        assert parse_nudge(payload) == nudge

    def test_without_a_nudge(self):
        assert scope_payload(B1.scope) == {"user_id": "usr-1", "connection_id": "conn-3"}

    @pytest.mark.parametrize("payload", [
        tick_payload("usr-1"),                                   # a single-drive payload: refused
        {"connection_id": "conn-1"},
        {"user_id": "usr-1", "connection_id": ""},
        {"user_id": "", "connection_id": "conn-1"},
        {"user_id": "usr-1", "connection_id": 5},
        {"user_id": "usr-1", "connection_id": "conn:1"},
        {},
    ])
    def test_a_payload_without_a_valid_connection_is_refused(self, payload):
        assert payload_scope(payload) is None


class TestUserScopedSet:
    """Each with its reason (T-5): usage month (D8, shared per user), initial build by account (D8), notes (by identity
    and tag), the transcript store (by content), mirror roots and ledger (by drive identity)."""

    def test_usage_month_is_per_user(self):
        assert usage_doc_id("usr-1", "2026-10") == "usr-1:2026-10"
        assert usage_doc_id("usr-2", "2026-10") != usage_doc_id("usr-1", "2026-10")

    def test_initial_build_is_per_account_not_per_connection(self):
        key = initial_build_doc_id("usr-1", "realm_a", "sub-1")
        assert key == "usr-1:initial:realm_a:sub-1"
        assert initial_build_doc_id("usr-1", "realm_a", "sub-2") != key
        assert initial_build_doc_id("usr-1", "realm_b", "sub-1") != key
        assert initial_build_doc_id("usr-2", "realm_a", "sub-1") != key

    def test_initial_build_key_is_a_safe_doc_id(self):
        assert initial_build_doc_id("usr-1", "realm_a", "s/1") == "usr-1:initial:realm_a:s%2F1"

    @pytest.mark.parametrize("realm,subject", [("", "sub-1"), ("realm_a", "")])
    def test_initial_build_needs_the_account(self, realm, subject):
        with pytest.raises(ValueError):
            initial_build_doc_id("usr-1", realm, subject)

    def test_one_recording_on_two_connections_shares_its_stored_transcript(self):
        content = ContentIdentity(family="fam1", digest="aGFzaA/+=", size=2048)
        on_a1 = transcript_content_key(content, _identity(A1), "v1")
        on_b1 = transcript_content_key(content, _identity(B1), "v7")
        assert on_a1 == on_b1 == ("fam1:aGFzaA/+=:2048", True)
        assert transcript_doc_id("usr-1", on_a1[0]) == "usr-1:fam1:aGFzaA%2F+=:2048"

    def test_without_content_identity_the_key_is_the_item_identity_and_not_shared(self):
        on_a1 = transcript_content_key(None, _identity(A1), "v1")
        on_b1 = transcript_content_key(None, _identity(B1), "v1")
        assert on_a1 == ("item:fake_a:drive-1:item-1:v1", False)
        assert on_a1 != on_b1          # same item id, two providers: two items
        with pytest.raises(ValueError):
            transcript_content_key(None, "", "v1")

    def test_one_source_item_seen_by_two_mirrors_has_two_ledger_documents(self):
        source = _identity(A1)
        on_a1 = ledger_key("usr-1", KIND_FILE, A1.drive_identity, source)
        on_a2 = ledger_key("usr-1", KIND_FILE, A2.drive_identity, source)
        assert on_a1 != on_a2
        assert on_a1.startswith("usr-1:file:") and len(on_a1.rsplit(":", 1)[1]) == 32
        assert ledger_key("usr-1", KIND_FOLDER, A1.drive_identity, source) != on_a1

    @pytest.mark.parametrize("drive,source", [("", "x"), ("x", "")])
    def test_a_ledger_key_needs_both_identities(self, drive, source):
        with pytest.raises(ValueError):
            ledger_key("usr-1", KIND_FILE, drive, source)

    def test_mirror_root_and_archive_by_drive_identity(self):
        assert mirror_root_doc_id("usr-1", A1.drive_identity) == "usr-1:fake_a:drive-1"
        assert mirror_root_doc_id("usr-1", A1.drive_identity) != mirror_root_doc_id("usr-1", A2.drive_identity)
        with pytest.raises(ValueError):
            mirror_root_doc_id("usr-1", "")
        a1 = archive_folder_key("usr-1", A1.drive_identity, "2026-10-30", "Audio")
        assert a1.startswith(f"usr-1:{KIND_ARCHIVE}:")
        assert a1 != archive_folder_key("usr-1", A2.drive_identity, "2026-10-30", "Audio")

    def test_note_keys(self):
        assert item_binding_key(_identity(A1)) == "i:fake_a:drive-1:item-1"
        refs = [tagged_ref_key(c.ref_tag, ITEM) for c in CONNECTIONS]
        assert refs == ["r:a1:item-1", "r:b2:item-1", "r:c3:item-1"]
        for bad_tag, item_id in (("A1", ITEM), ("a1", "")):
            with pytest.raises(ValueError):
                tagged_ref_key(bad_tag, item_id)
        with pytest.raises(ValueError):
            item_binding_key("")

    def test_the_binding_by_identity_is_byte_identical_to_todays_key(self):
        # §7.2: the adapter's item identity `{id_space}:{driveId}:{itemId}` rebuilds today's binding key: no re-binding
        assert item_binding_key("microsoft_onedrive:drv-1:item-1") == binding_key("microsoft_onedrive", "drv-1",
                                                                                  "item-1")


class TestRefsAcrossConnections:
    def test_one_item_id_on_three_connections_gives_three_refs_that_round_trip(self):
        refs = [drive_ref.format(c.ref_tag, ITEM) for c in CONNECTIONS]
        assert len(set(refs)) == 3
        for conn, text in zip(CONNECTIONS, refs):
            assert drive_ref.parse(text) == DriveRef(tag=conn.ref_tag, item_id=ITEM)

    def test_a_ref_finds_its_connection_by_tag(self):
        by_tag = {c.ref_tag: c for c in CONNECTIONS}
        parsed = drive_ref.parse(f"[Drive: report.pdf (1KB) ref={drive_ref.format('c3', ITEM)}]")
        assert isinstance(parsed, DriveRef) and by_tag[parsed.tag] is B1


class TestI5KeysSurviveARebuild:
    """I5/I6 key part: a purge and rebuild, or a reconnect (a new connection of the same drive and account), changes
    every connection-scoped key and none of the kept stores' keys, so the rebuilt index finds the same stored
    transcripts, ledger documents, `_Transcripts` root, notes and initial build."""

    def test_kept_stores_keep_their_keys_across_a_new_connection_of_the_same_drive(self):
        old = A1
        new = _conn("conn-9", "fake_a", "drive-1", "d4", "cred-9")     # same drive, same account, new connection
        source = _identity(old)
        assert _identity(new) == source
        content = ContentIdentity("fam1", "d1", 10)

        def kept(conn):
            return (
                transcript_content_key(content, _identity(conn), "v1"),
                transcript_content_key(None, _identity(conn), "v1"),
                ledger_key("usr-1", KIND_FILE, conn.drive_identity, _identity(conn)),
                archive_folder_key("usr-1", conn.drive_identity, "2026-10-30", "Audio"),
                mirror_root_doc_id("usr-1", conn.drive_identity),
                item_binding_key(_identity(conn)),
                initial_build_doc_id("usr-1", "realm_a", "sub-1"),
                usage_doc_id("usr-1", "2026-10"),
            )

        assert kept(old) == kept(new)
        for family, key_of in SCOPED_KEYS.items():
            assert key_of(old.scope) != key_of(new.scope), family

    def test_no_two_connection_scoped_families_share_a_key(self):
        for conn in CONNECTIONS:
            keys = {family: key_of(conn.scope) for family, key_of in SCOPED_KEYS.items()}
            # records and transcript jobs live in different collections; within a collection family keys are unique
            same = [(a, b) for a, b in combinations(sorted(keys), 2) if keys[a] == keys[b]]
            assert same == [("record", "transcript_job")], same

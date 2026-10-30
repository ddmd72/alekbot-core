"""Search v2 repository methods behave alike on both adapters (USER_DRIVE_INDEXING_RFC §25.13.1): the same records,
chunks and hint documents are written to InMemoryDriveIndexRepository and to FirestoreDriveIndexRepository over the
evaluating Firestore fake, and every new method answers the same, on order, limits, cursors and filters. The
reference semantics are the port's docstrings; each case states the one it pins."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_enrichment import DriveHintDoc, HintItem, InitialBuild, ModelUsageMonth
from src.domain.drive_index import DriveChunk, DriveIndexRecord, RecordState, Scope
from src.domain.drive_index_lean import DriveChunkLean, DriveHintLean, DriveRecordLean
from src.domain.drive_index_params import INITIAL_BUILD_USD

from tests.unit.adapters.firestore_drive_eval_fakes import EvalDb, eval_transactions, make_eval_repo

D = datetime(2026, 5, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
U = ConnectionScope(user_id="u", connection_id="u")
OTHER = ConnectionScope(user_id="other", connection_id="other")
NOBODY = ConnectionScope(user_id="nobody", connection_id="nobody")
REALM, SUBJECT = "microsoft", "acct-1"   # the provider account an initial build is keyed by (MULTI_DRIVE_RFC §10)


def rec(item, *, user="u", kind="pdf", eff=None, keys=(), **kw):
    kw.setdefault("parent_id", "root")
    return DriveIndexRecord(user_id=user, connection_id=user, item_id=item, kind=kind, effective_date=eff, about_keys=tuple(keys),
                            name=f"{item}.{kind}", state=RecordState.INDEXED, scope=Scope.NORMAL, **kw)


def chunk(item, gen=1, index=0, *, user="u", kind="pdf", keys=()):
    return DriveChunk(user_id=user, connection_id=user, item_id=item, generation=gen, index=index, text=f"text {item}/{gen}/{index}",
                      char_start=index * 10, char_end=index * 10 + 10, kind=kind, word_keys=tuple(keys),
                      keys_version=1)


def hint(item, gen=1, index=0, *, user="u", kind="pdf", keys=()):
    return DriveHintDoc(user_id=user, connection_id=user, item_id=item, generation=gen, index=index, kind=kind, chunk_text_hash=f"h{index}",
                        items=(HintItem("Rome", "keyword", "in Roma", 3, 10),
                               HintItem("Where to stay?", "question", "hotel", -1, -1)),
                        hint_keys=tuple(keys), passages_version=1)


def vec(i):
    return [1.0, float(i)]


class Pair:
    def __init__(self, mem, fs):
        self.mem, self.fs = mem, fs

    async def both(self, name, *args, **kwargs):
        a = await getattr(self.mem, name)(*args, **kwargs)
        b = await getattr(self.fs, name)(*args, **kwargs)
        assert a == b, (name, args, kwargs, a, b)
        return a

    async def write(self, records=(), chunks=(), hints=()):
        for repo in (self.mem, self.fs):
            if records:
                await repo.upsert_records(list(records), {r.item_id: vec(i) for i, r in enumerate(records)})
            if chunks:
                await repo.write_chunks(list(chunks), [vec(i) for i, _ in enumerate(chunks)])
            if hints:
                await repo.write_hints(list(hints), [vec(i) for i, _ in enumerate(hints)])


@pytest.fixture
def pair(mock_env_config):
    db = EvalDb()
    with eval_transactions():
        yield Pair(InMemoryDriveIndexRepository(), make_eval_repo(db, mock_env_config))


async def paged(pair, name, page, *args, cursor_of, **kwargs):
    """Read every page through the cursor, on both adapters (each page compared)."""
    out, after = [], None
    while True:
        got = await pair.both(name, *args, page, **kwargs, **({"after": after} if name == "find_by_about_keys"
                                                            else {"after_item_id": after}))
        out.extend(got)
        if len(got) < page:
            return out
        after = cursor_of(got[-1])


class TestFindByAboutKeys:
    @pytest.fixture
    async def seeded(self, pair):
        await pair.write(records=[
            rec("a", eff=D, keys=("e:rome", "k:ticket")),
            rec("b", eff=D, keys=("e:rome",)),                       # same date as a: item_id DESC breaks the tie
            rec("c", eff=D + timedelta(days=3), keys=("k:ticket",)),
            rec("d", eff=None, keys=("e:rome",)),                     # undated: last
            rec("e", eff=D - timedelta(days=9), keys=("e:berlin",), kind="image"),
            rec("f", eff=D - timedelta(days=1), keys=("e:rome",), kind="image"),
            rec("x", user="other", eff=D, keys=("e:rome",)),
        ])
        return pair

    async def test_newest_first_ties_by_item_desc_undated_last(self, seeded):
        got = await seeded.both("find_by_about_keys", U, ["e:rome", "k:ticket"], 10)
        assert [r.item_id for r in got] == ["c", "b", "a", "f", "d"]
        assert all(isinstance(r, DriveRecordLean) for r in got)

    async def test_a_record_matching_two_keys_is_returned_once(self, seeded):
        got = await seeded.both("find_by_about_keys", U, ["e:rome", "k:ticket"], 10)
        assert [r.item_id for r in got].count("a") == 1

    async def test_limit(self, seeded):
        got = await seeded.both("find_by_about_keys", U, ["e:rome", "k:ticket"], 2)
        assert [r.item_id for r in got] == ["c", "b"]

    async def test_cursor_pages_read_the_whole_order(self, seeded):
        got = await paged(seeded, "find_by_about_keys", 2, U, ["e:rome", "k:ticket"], cursor_of=lambda r: r.item_id)
        assert [r.item_id for r in got] == ["c", "b", "a", "f", "d"]

    async def test_a_vanished_cursor_record_ends_the_paging(self, seeded):
        assert await seeded.both("find_by_about_keys", U, ["e:rome"], 5, after="gone") == []

    async def test_a_cursor_record_outside_the_keys_still_positions_the_page(self, seeded):
        got = await seeded.both("find_by_about_keys", U, ["e:rome"], 5, after="e")   # e: berlin, D-9
        assert [r.item_id for r in got] == ["d"]

    async def test_single_kind(self, seeded):
        got = await seeded.both("find_by_about_keys", U, ["e:rome", "e:berlin"], 10, kind="image")
        assert [r.item_id for r in got] == ["f", "e"]

    async def test_dates_are_inclusive_and_exclude_undated(self, seeded):
        got = await seeded.both("find_by_about_keys", U, ["e:rome", "k:ticket"], 10,
                                date_from=D - timedelta(days=1), date_to=D)
        assert [r.item_id for r in got] == ["b", "a", "f"]
        got = await seeded.both("find_by_about_keys", U, ["e:rome"], 10, date_from=D)
        assert [r.item_id for r in got] == ["b", "a"]

    async def test_thirty_keys_are_read_and_a_thirty_first_is_refused(self, seeded):
        """Review M1 (arbiter): a list over 30 distinct keys is a ValueError on both adapters, never a silent cut; the
        30th key still counts."""
        expected = await seeded.both("find_by_about_keys", U, ["e:rome"], 10)
        assert expected
        thirty = [f"w:filler{i}" for i in range(29)] + ["e:rome"]
        assert await seeded.both("find_by_about_keys", U, thirty, 10) == expected
        for repo in (seeded.mem, seeded.fs):
            with pytest.raises(ValueError, match="30"):
                await repo.find_by_about_keys(U, [f"w:filler{i}" for i in range(30)] + ["e:rome"], 10)

    async def test_no_keys_no_records(self, seeded):
        assert await seeded.both("find_by_about_keys", U, [], 10) == []

    async def test_the_lean_projection(self, pair):
        full = rec("a", eff=D, keys=("e:rome",), name_tokens=("w:a",), has_card=True, indexed_generation=2,
                   content_hash="h", valid_until=D, profile_state="ready", readme_excerpt="long text")
        await pair.write(records=[full])
        (lean,) = await pair.both("find_by_about_keys", U, ["e:rome"], 1)
        assert lean == DriveRecordLean(item_id="a", parent_id="root", name="a.pdf", kind="pdf", effective_date=D,
                                       about_keys=("e:rome",), name_tokens=("w:a",), state=RecordState.INDEXED,
                                       scope=Scope.NORMAL, has_card=True, indexed_generation=2, content_hash="h",
                                       valid_until=D)


class TestFindExpiring:
    async def test_soonest_first_from_the_date_ties_by_item(self, pair):
        await pair.write(records=[
            rec("a", keys=("lv:expir",), valid_until=datetime(2026, 11, 1, tzinfo=timezone.utc)),
            rec("b", keys=("lv:expir",), valid_until=datetime(2026, 10, 13, tzinfo=timezone.utc)),   # today counts
            rec("c", keys=("lv:expir",), valid_until=datetime(2026, 10, 12, tzinfo=timezone.utc)),   # expired
            rec("d", keys=("lv:expir",)),                                                           # no date
            rec("e", keys=("lv:valid",), valid_until=datetime(2026, 11, 1, tzinfo=timezone.utc)),
            rec("f", keys=("w:other",), valid_until=datetime(2026, 10, 20, tzinfo=timezone.utc)),
        ])
        got = await pair.both("find_expiring", U, ["lv:expir", "lv:valid"], date(2026, 10, 13), 10)
        assert [r.item_id for r in got] == ["b", "a", "e"]
        got = await pair.both("find_expiring", U, ["lv:expir", "lv:valid"], date(2026, 10, 13), 2)
        assert [r.item_id for r in got] == ["b", "a"]


class TestWordKeys:
    @pytest.fixture
    async def seeded(self, pair):
        await pair.write(chunks=[
            chunk("b", 1, 0, keys=("w:rome",)), chunk("b", 1, 1, keys=("w:rome", "w:ticket")),
            chunk("b", 2, 0, keys=("w:ticket",)), chunk("a", 1, 3, keys=("w:rome",)),
            chunk("c", 1, 0, keys=("w:berlin",)), chunk("d", 1, 0, keys=("w:ticket",), kind="image"),
            chunk("z", 1, 0, keys=("w:rome",), user="other"),
        ])
        return pair

    async def test_ordered_by_item_generation_index_lean(self, seeded):
        got = await seeded.both("find_chunks_by_word_keys", U, ["w:rome", "w:ticket"], 10)
        assert [(c.item_id, c.generation, c.index) for c in got] == [("a", 1, 3), ("b", 1, 0), ("b", 1, 1),
                                                                     ("b", 2, 0), ("d", 1, 0)]
        assert got[-1] == DriveChunkLean(user_id="u", item_id="d", generation=1, index=0, kind="image")

    async def test_limit_and_the_item_cursor_skips_every_chunk_of_the_item(self, seeded):
        page = await seeded.both("find_chunks_by_word_keys", U, ["w:rome", "w:ticket"], 2)
        assert [(c.item_id, c.index) for c in page] == [("a", 3), ("b", 0)]
        rest = await seeded.both("find_chunks_by_word_keys", U, ["w:rome", "w:ticket"], 10, after_item_id="b")
        assert [c.item_id for c in rest] == ["d"]

    async def test_get_chunk_keys_reads_the_existing_ids(self, seeded):
        got = await seeded.both("get_chunk_keys", U, [("b", 1, 1), ("a", 1, 3), ("a", 9, 9), ("z", 1, 0)])
        assert got == {("b", 1, 1): ("w:rome", "w:ticket"), ("a", 1, 3): ("w:rome",)}

    async def test_thirty_keys_are_read_and_a_thirty_first_is_refused(self, seeded):
        """Review M1 (arbiter): a list over 30 distinct keys is a ValueError on both adapters, never a silent cut; the
        30th key still counts."""
        expected = await seeded.both("find_chunks_by_word_keys", U, ["w:rome"], 10)
        assert expected
        thirty = [f"w:filler{i}" for i in range(29)] + ["w:rome"]
        assert await seeded.both("find_chunks_by_word_keys", U, thirty, 10) == expected
        for repo in (seeded.mem, seeded.fs):
            with pytest.raises(ValueError, match="30"):
                await repo.find_chunks_by_word_keys(U, [f"w:filler{i}" for i in range(30)] + ["w:rome"], 10)


class TestHints:
    @pytest.fixture
    async def seeded(self, pair):
        await pair.write(hints=[
            hint("b", 1, 1, keys=("w:roma",)), hint("b", 1, 0, keys=("w:roma", "w:billete")),
            hint("b", 2, 0, keys=("w:billete",)), hint("a", 1, 2, keys=("w:roma",), kind="docx"),
            hint("c", 1, 0, keys=("w:berlin",)), hint("z", 1, 0, keys=("w:roma",), user="other"),
        ])
        return pair

    async def test_find_by_keys_ordered_lean_with_the_cursor(self, seeded):
        got = await seeded.both("find_hints_by_keys", U, ["w:roma", "w:billete"], 10)
        assert [(h.item_id, h.generation, h.index) for h in got] == [("a", 1, 2), ("b", 1, 0), ("b", 1, 1),
                                                                     ("b", 2, 0)]
        assert got[1] == DriveHintLean(user_id="u", item_id="b", generation=1, index=0, kind="pdf",
                                       hint_keys=("w:roma", "w:billete"))
        assert len(await seeded.both("find_hints_by_keys", U, ["w:roma", "w:billete"], 2)) == 2
        rest = await seeded.both("find_hints_by_keys", U, ["w:roma", "w:billete"], 10, after_item_id="a")
        assert [h.item_id for h in rest] == ["b", "b", "b"]
        assert await seeded.both("find_hints_by_keys", U, ["w:roma"], 10, after_item_id="b") == []

    async def test_get_hint_keys(self, seeded):
        got = await seeded.both("get_hint_keys", U, [("b", 1, 0), ("c", 1, 0), ("c", 2, 0), ("z", 1, 0)])
        assert got == {("b", 1, 0): ("w:roma", "w:billete"), ("c", 1, 0): ("w:berlin",)}

    async def test_get_hints_of_one_generation_by_index(self, seeded):
        got = await seeded.both("get_hints", U, "b", 1)
        assert [h.index for h in got] == [0, 1]
        assert got[0] == hint("b", 1, 0, keys=("w:roma", "w:billete"))
        assert await seeded.both("get_hints", U, "b", 7) == []

    async def test_get_hints_with_vectors(self, seeded):
        got = await seeded.both("get_hints_with_vectors", U, "b", 1)
        assert [(h.index, v) for h, v in got] == [(0, vec(1)), (1, vec(0))]

    async def test_nearest_by_kind(self, seeded):
        got = await seeded.both("find_nearest_hints", U, vec(3), 3)
        assert [(h.item_id, h.generation, h.index) for h, _ in got] == [("a", 1, 2), ("c", 1, 0), ("b", 2, 0)]
        assert [round(d, 9) for _, d in got] == sorted(round(d, 9) for _, d in got)
        only = await seeded.both("find_nearest_hints", U, vec(3), 5, kind="docx")
        assert [(h.item_id, h.kind) for h, _ in only] == [("a", "docx")]
        assert isinstance(only[0][0], DriveHintLean)

    async def test_hints_page_with_the_cursor(self, seeded):
        first = await seeded.both("hints_page", U, None, 3)
        assert [(h.item_id, h.generation, h.index) for h in first] == [("a", 1, 2), ("b", 1, 0), ("b", 1, 1)]
        rest = await seeded.both("hints_page", U, ("b", 1, 1), 10)
        assert [(h.item_id, h.generation, h.index) for h in rest] == [("b", 2, 0), ("c", 1, 0)]

    async def test_delete_except_keeps_one_generation(self, seeded):
        assert await seeded.both("delete_hints_except", U, "b", 2) == 2
        assert [(h.item_id, h.generation) for h in await seeded.both("hints_page", U, None, 10)] == [
            ("a", 1), ("b", 2), ("c", 1)]
        assert await seeded.both("delete_hints_except", U, "a", None) == 1

    async def test_delete_user_hints_by_limit_leaves_other_users(self, seeded):
        assert await seeded.both("delete_user_hints", U, 3) == 3
        assert await seeded.both("delete_user_hints", U, 10) == 2
        assert await seeded.both("delete_user_hints", U, 10) == 0
        assert [h.item_id for h in await seeded.both("hints_page", OTHER, None, 10)] == ["z"]

    async def test_a_rewrite_replaces_the_document(self, seeded):
        await seeded.write(hints=[hint("c", 1, 0, keys=("w:hamburg",))])
        assert await seeded.both("get_hint_keys", U, [("c", 1, 0)]) == {("c", 1, 0): ("w:hamburg",)}


class TestEnrichmentReads:
    async def test_queue_newest_day_first_then_folder_then_item(self, pair):
        await pair.write(records=[
            rec("a", enrich_ready=True, effective_day="2026-05-01", parent_id="p2"),
            rec("b", enrich_ready=True, effective_day="2026-05-01", parent_id="p1"),
            rec("c", enrich_ready=True, effective_day="2026-06-01", parent_id="p9"),
            rec("d", enrich_ready=True, effective_day="", parent_id="p0"),
            rec("e", enrich_ready=False, effective_day="2026-07-01"),
            rec("f", enrich_ready=True, effective_day="2026-05-01", parent_id="p1"),
        ])
        got = await pair.both("enrich_queue", U, 10)
        assert [r.item_id for r in got] == ["c", "b", "f", "a", "d"]
        assert [r.item_id for r in await pair.both("enrich_queue", U, 2)] == ["c", "b"]

    async def test_wakeups_due_earliest_first(self, pair):
        await pair.write(records=[
            rec("a", enrich_wake_at=NOW), rec("b", enrich_wake_at=NOW - timedelta(hours=1)),
            rec("c", enrich_wake_at=NOW + timedelta(seconds=1)), rec("d"),
            rec("e", enrich_wake_at=NOW - timedelta(hours=1)),
        ])
        got = await pair.both("enrich_wakeups", U, NOW, 10)
        assert [r.item_id for r in got] == ["b", "e", "a"]
        assert [r.item_id for r in await pair.both("enrich_wakeups", U, NOW, 1)] == ["b"]

    async def test_keys_pending_page(self, pair):
        await pair.write(records=[rec(i, keys_pending=i != "c") for i in "abcde"])
        assert [r.item_id for r in await pair.both("keys_pending_page", U, None, 2)] == ["a", "b"]
        assert [r.item_id for r in await pair.both("keys_pending_page", U, "b", 10)] == ["d", "e"]

    async def test_count_by_job_state_over_more_kinds_than_one_query_takes(self, pair):
        kinds = {f"kind{i:02d}" for i in range(35)}
        await pair.write(records=[
            rec("a", kind="kind00", profile_state="ready", passages_state="parked"),
            rec("b", kind="kind34", profile_state="ready", passages_state="none"),
            rec("c", kind="kind10", profile_state="held", passages_state="ready"),
            rec("d", kind="image", profile_state="ready"),
            rec("e", kind="kind20"),
        ])
        got = await pair.both("count_by_job_state", U, kinds)
        assert got == {"profile": {"ready": 2, "held": 1, "none": 1},
                       "passages": {"parked": 1, "none": 2, "ready": 1}}
        assert await pair.both("count_by_job_state", U, set()) == {"profile": {}, "passages": {}}

    async def test_get_records_lean(self, pair):
        await pair.write(records=[rec("a", eff=D, keys=("e:rome",)), rec("b")])
        got = await pair.both("get_records_lean", U, ["a", "b", "gone", "a"])
        assert set(got) == {"a", "b"} and got["a"].about_keys == ("e:rome",)


class TestUsage:
    async def test_an_unknown_month_is_empty(self, pair):
        assert await pair.both("get_model_usage", "u", "2026-10") == ModelUsageMonth()

    async def test_adds_per_class_and_day(self, pair):
        for repo in (pair.mem, pair.fs):
            await repo.add_model_usage("u", "2026-10", "2026-10-13", "profile", 0.25, 0.0, 1)
            await repo.add_model_usage("u", "2026-10", "2026-10-13", "passage", 0.5, 0.125, 2)
            await repo.add_model_usage("u", "2026-10", "2026-10-14", "profile", 0.25, 0.25, 1)
            await repo.add_model_usage("u", "2026-11", "2026-11-01", "reader", 1.0, 0.0, 1)
        got = await pair.both("get_model_usage", "u", "2026-10")
        assert got == ModelUsageMonth(profile_usd=0.5, passage_usd=0.5, fallback_usd=0.375, calls=4,
                                      daily={"2026-10-13": (0.75, 0.125), "2026-10-14": (0.25, 0.25)})
        assert (await pair.both("get_model_usage", "u", "2026-11")).reader_usd == 1.0
        assert await pair.both("get_model_usage", "other", "2026-10") == ModelUsageMonth()

    async def test_an_unknown_class_is_refused(self, pair):
        for repo in (pair.mem, pair.fs):
            with pytest.raises(ValueError):
                await repo.add_model_usage("u", "2026-10", "2026-10-13", "pages", 0.1, 0.0, 1)


class TestInitialBuild:
    async def test_unopened_by_default_and_reading_never_writes(self, pair):
        assert await pair.both("get_initial_build", "u", REALM, SUBJECT) == InitialBuild(allowance_usd=INITIAL_BUILD_USD)
        assert pair.fs._db.cols.get("eval_drive_model_usage") is None or not pair.fs._db.cols[
            "eval_drive_model_usage"].docs

    async def test_spend_opens_covers_then_closes(self, pair):
        t1, t2 = NOW, NOW + timedelta(hours=1)
        assert await pair.both("spend_initial_build", "u", REALM, SUBJECT, 30.0, t1) == (
            InitialBuild(INITIAL_BUILD_USD, 30.0, t1, None), 30.0)
        assert await pair.both("spend_initial_build", "u", REALM, SUBJECT, 15.0, t2) == (
            InitialBuild(INITIAL_BUILD_USD, INITIAL_BUILD_USD, t1, t2), 10.0)
        assert await pair.both("spend_initial_build", "u", REALM, SUBJECT, 5.0, t2 + timedelta(hours=1)) == (
            InitialBuild(INITIAL_BUILD_USD, INITIAL_BUILD_USD, t1, t2), 0.0)
        assert await pair.both("get_initial_build", "u", REALM, SUBJECT) == InitialBuild(INITIAL_BUILD_USD, INITIAL_BUILD_USD, t1, t2)

    async def test_close_never_reopens_and_keeps_its_time(self, pair):
        await pair.both("spend_initial_build", "u", REALM, SUBJECT, 1.0, NOW)
        await pair.both("close_initial_build", "u", REALM, SUBJECT, NOW + timedelta(days=1))
        await pair.both("close_initial_build", "u", REALM, SUBJECT, NOW + timedelta(days=2))
        assert await pair.both("get_initial_build", "u", REALM, SUBJECT) == InitialBuild(INITIAL_BUILD_USD, 1.0, NOW,
                                                                          NOW + timedelta(days=1))
        assert (await pair.both("spend_initial_build", "u", REALM, SUBJECT, 1.0, NOW + timedelta(days=3)))[1] == 0.0

    async def test_closing_an_unopened_build(self, pair):
        await pair.both("close_initial_build", "u", REALM, SUBJECT, NOW)
        assert await pair.both("get_initial_build", "u", REALM, SUBJECT) == InitialBuild(INITIAL_BUILD_USD, 0.0, None, NOW)

    async def test_the_usage_documents_are_per_user(self, pair):
        await pair.both("spend_initial_build", "u", REALM, SUBJECT, 2.0, NOW)
        assert await pair.both("get_initial_build", "other", REALM, SUBJECT) == InitialBuild(allowance_usd=INITIAL_BUILD_USD)

"""`count_describe_gaps` (USER_DRIVE_INDEXING_RFC §25.5.6, XC-3, XC-9): the coverage line's counts, alike on both
adapters, a record round-tripping its derived `describe_gap`, and the Firestore queries equality-only (no composite)."""
from src.adapters.firestore_drive_index_repo import record_from_doc, record_to_doc
from src.domain.drive_index import DriveIndexRecord, RecordState, Scope

from tests.unit.adapters.test_drive_index_v2_parity import NOBODY, U, pair, rec  # noqa: F401  (fixture)


async def test_both_adapters_count_each_gap_per_user(pair):  # noqa: F811
    await pair.write(records=[rec("a", describe_gap="waiting"), rec("b", describe_gap="waiting"),
                              rec("c", describe_gap="held"), rec("d", describe_gap="no_passages"), rec("e"),
                              rec("f", user="other", describe_gap="waiting")])
    assert await pair.both("count_describe_gaps", U) == {"waiting": 2, "held": 1, "no_passages": 1}
    assert await pair.both("count_describe_gaps", NOBODY) == {}


def test_describe_gap_round_trips():
    r = DriveIndexRecord(user_id="u", connection_id="u", item_id="a", name="a.pdf", kind="pdf", state=RecordState.INDEXED,
                         scope=Scope.NORMAL, describe_gap="held")
    assert record_from_doc(record_to_doc(r)).describe_gap == "held"
    assert record_from_doc({k: v for k, v in record_to_doc(r).items() if k != "describe_gap"}).describe_gap == ""


def test_an_entitys_found_spellings_round_trip():
    from src.domain.drive_enrichment import DriveProfile, ProfileEntity
    entity = ProfileEntity(name="Коїмбра", type="place", printed="COIMBRA", forms=("Coimbra",),
                           found=("Coimbra",))
    r = DriveIndexRecord(user_id="u", connection_id="u", item_id="a", name="a.pdf", kind="pdf", state=RecordState.INDEXED,
                         scope=Scope.NORMAL, profile=DriveProfile("t", "w", "en", "ticket", entities=(entity,)))
    assert record_from_doc(record_to_doc(r)).profile.entities == (entity,)

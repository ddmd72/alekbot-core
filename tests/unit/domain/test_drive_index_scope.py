"""The scope an index covers (MULTI_DRIVE_RFC §11.3, E20): `IndexScope` and the pure `verdict`."""
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index_scope import ANCESTOR, IndexScope, verdict
from src.domain.drive_search import MAX_CHAIN_DEPTH
from tests.support.drive_connection_service_fake import connection

ROOT = "root"
RD, COIMBRA, SPRINGFIELD = "f-rd", "f-coimbra", "f-springfield"


def scope(*folders, whole=False, feed=FeedScope.ACCOUNT):
    return IndexScope(feed=feed, whole_drive=whole, folder_ids=frozenset(folders))


def test_covers_everything_for_whole_drive_and_for_an_area_default():
    assert scope(whole=True).covers_everything
    assert scope(feed=FeedScope.AREA).covers_everything
    assert not scope(feed=FeedScope.ACCOUNT).covers_everything     # an account's default scope is nothing
    assert not scope(RD, feed=FeedScope.AREA).covers_everything    # selected folders filter even an area


def test_of_reads_the_connection_and_the_capabilities():
    conn = connection()
    from dataclasses import replace
    conn = replace(conn, scope_folder_ids=(RD, COIMBRA))
    caps = DriveCapabilities(max_path_chars=255, feed_scope=FeedScope.ACCOUNT)
    got = IndexScope.of(conn, caps)
    assert got == scope(RD, COIMBRA, feed=FeedScope.ACCOUNT)
    assert IndexScope.of(replace(conn, scope_folder_ids=(), scope_whole_drive=True), caps).whole_drive


def test_a_scope_folder_is_in_whatever_its_parent():
    assert verdict(RD, ROOT, scope(RD), ROOT, {}, {}) is True


def test_a_child_of_a_scope_folder_is_in():
    assert verdict("plan", RD, scope(RD), ROOT, {}, {}) is True


def test_a_chain_that_reaches_a_scope_folder_is_in():
    parents = {"sub": RD}
    assert verdict("plan", "sub", scope(RD), ROOT, {}, parents) is True


def test_a_chain_that_reaches_the_root_is_out():
    assert verdict("plan", SPRINGFIELD, scope(RD), ROOT, {SPRINGFIELD: False}, {}) is False
    assert verdict("plan", "sub", scope(RD), ROOT, {}, {"sub": SPRINGFIELD, SPRINGFIELD: ROOT}) is False
    assert verdict("top", ROOT, scope(RD), ROOT, {}, {}) is False


def test_a_parentless_item_is_in_only_with_the_whole_drive():
    assert verdict("lab-report.pdf", "", scope(RD), ROOT, {}, {}) is False
    assert verdict("lab-report.pdf", "", scope(whole=True), ROOT, {}, {}) is True


def test_a_parentless_ancestor_makes_the_chain_out():
    assert verdict("plan", "sub", scope(RD), ROOT, {}, {"sub": ""}) is False


def test_a_folder_on_the_path_to_a_scope_folder_is_an_ancestor():
    assert verdict("projects", ROOT, scope(RD), ROOT, {"projects": None}, {}) == ANCESTOR


def test_an_ancestor_in_the_chain_is_walked_through_not_judged_in():
    known = {"projects": None}
    assert verdict("sibling", "projects", scope(RD), ROOT, known, {"projects": ROOT}) is False


def test_the_first_unknown_ancestor_is_returned_to_be_read():
    assert verdict("plan", "sub", scope(RD), ROOT, {}, {}) == "sub"
    assert verdict("plan", "sub", scope(RD), ROOT, {}, {"sub": "mid"}) == "mid"


def test_a_known_verdict_ends_the_walk():
    assert verdict("plan", "sub", scope(RD), ROOT, {"sub": True}, {}) is True
    assert verdict("plan", "sub", scope(RD), ROOT, {"sub": False}, {}) is False


def test_a_cycle_is_cut_at_the_chain_depth_and_is_out():
    parents = {"a": "b", "b": "a"}
    assert verdict("x", "a", scope(RD), ROOT, {}, parents) is False
    assert MAX_CHAIN_DEPTH > 2

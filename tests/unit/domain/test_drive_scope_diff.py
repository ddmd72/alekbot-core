"""The pure scope diff (docs/10_rfcs/DRIVE_SCOPE_WALK_RFC.md §5.1, §5.4, §5.9, §5.11; plan Deltas 1, 3).

The drive below, by stored parent links (ids are opaque; the names are only for reading):

    root ── f-rd "R&D" ── i-plan "Tom's plan.docx"
         └─ f-coimbra "Coimbra" ── f-springfield "Springfield" ── i-cert "birth-certificate.pdf"
                                └─ f-lisbon "Lisbon" ── f-porto "Porto"

Google feeds read the whole account (`ACCOUNT`, default = nothing); OneDrive feeds read an App Folder (`AREA`,
default = the area root), here `root` itself.
"""
from dataclasses import replace

import pytest

from src.domain.drive_access_policy import scope_key
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import DriveSyncState, EpochPhase
from src.domain.drive_index_planner import scope_restart
from src.domain.drive_scope_diff import (
    AppliedScope, ScopeDiff, applied_of, covered, covers_everything, normal_form, plan_scope_diff, shape_valid,
    stale_skeletons,
)
from src.domain.drive_search import MAX_CHAIN_DEPTH

ROOT = "root"
PARENTS = {
    "f-rd": ROOT, "i-plan": "f-rd",
    "f-coimbra": ROOT, "f-springfield": "f-coimbra", "i-cert": "f-springfield",
    "f-lisbon": "f-coimbra", "f-porto": "f-lisbon",
    ROOT: "",
}
BOTH = pytest.mark.parametrize("feed", [FeedScope.ACCOUNT, FeedScope.AREA])


def folders(*roots):
    return AppliedScope(whole=False, roots=tuple(roots))


WHOLE = AppliedScope(whole=True, roots=())
DEFAULT = folders()


def diff(old, new, feed=FeedScope.ACCOUNT, *, enumerating=False, parents=PARENTS):
    return plan_scope_diff(old, new, feed_scope=feed, root_item_id=ROOT, enumerating=enumerating, parents=parents)


class TestNormalForm:
    def test_the_whole_drive_drops_any_roots(self):
        assert normal_form(AppliedScope(True, ("f-rd",)), FeedScope.ACCOUNT, ROOT) == WHOLE

    def test_roots_are_sorted_and_unique(self):
        assert normal_form(folders("f-rd", "f-coimbra", "f-rd"), FeedScope.ACCOUNT, ROOT) == folders("f-coimbra", "f-rd")

    def test_an_area_default_is_its_root_and_an_account_default_is_nothing(self):
        assert normal_form(DEFAULT, FeedScope.AREA, ROOT) == folders(ROOT)
        assert normal_form(DEFAULT, FeedScope.ACCOUNT, ROOT) == DEFAULT

    def test_an_area_default_with_no_known_root_stays_empty(self):
        assert normal_form(DEFAULT, FeedScope.AREA, "") == DEFAULT


class TestCoversEverything:
    def test_the_whole_drive_on_either_feed(self):
        assert covers_everything(WHOLE, FeedScope.ACCOUNT) and covers_everything(WHOLE, FeedScope.AREA)

    def test_the_default_only_on_an_area_feed(self):
        assert covers_everything(DEFAULT, FeedScope.AREA)
        assert not covers_everything(DEFAULT, FeedScope.ACCOUNT)

    @BOTH
    def test_chosen_folders_never(self, feed):
        assert not covers_everything(folders("f-rd"), feed)


class TestCovered:
    def test_a_root_covers_itself_and_its_stored_subtree(self):
        assert covered("f-coimbra", {"f-coimbra"}, PARENTS)
        assert covered("i-cert", {"f-coimbra"}, PARENTS)

    def test_a_sibling_and_an_ancestor_are_not_covered(self):
        assert not covered("f-rd", {"f-coimbra"}, PARENTS)
        assert not covered("f-coimbra", {"f-springfield"}, PARENTS)

    def test_an_item_with_no_stored_parent_is_not_covered(self):
        assert not covered("f-unknown", {"f-coimbra"}, PARENTS)

    def test_a_cycle_is_not_covered(self):
        assert not covered("f-a", {"f-coimbra"}, {"f-a": "f-b", "f-b": "f-a"})

    def test_a_chain_of_max_depth_is_covered_and_one_link_more_is_not(self):
        chain = {f"f-{n}": f"f-{n + 1}" for n in range(MAX_CHAIN_DEPTH + 1)}
        assert covered("f-0", {f"f-{MAX_CHAIN_DEPTH}"}, chain)
        assert not covered("f-0", {f"f-{MAX_CHAIN_DEPTH + 1}"}, chain)


class TestFoldersToFolders:
    """§5.4 row FOLDERS(A) -> FOLDERS(B): coverage over stored records, never id sets (W2)."""

    @BOTH
    def test_case_1_an_added_folder_is_walked_and_nothing_else_moves(self, feed):
        assert diff(folders("f-coimbra"), folders("f-coimbra", "f-rd"), feed) == ScopeDiff(
            restart=False, walk_roots=("f-rd",))

    @BOTH
    def test_case_2_a_removed_folder_is_removed_and_nothing_is_walked(self, feed):
        assert diff(folders("f-coimbra", "f-rd"), folders("f-coimbra"), feed) == ScopeDiff(
            restart=False, remove_roots=("f-rd",))

    @BOTH
    def test_case_3a_an_outer_root_is_walked_and_the_covered_inner_one_is_not_removed(self, feed):
        assert diff(folders("f-springfield"), folders("f-coimbra"), feed) == ScopeDiff(
            restart=False, walk_roots=("f-coimbra",))

    @BOTH
    def test_case_3b_an_inner_root_is_kept_and_not_walked_while_the_outer_one_is_removed(self, feed):
        assert diff(folders("f-coimbra"), folders("f-springfield"), feed) == ScopeDiff(
            restart=False, remove_roots=("f-coimbra",), keep_roots=("f-springfield",))

    def test_keep_holds_only_new_roots_inside_a_removed_root(self):
        result = diff(folders("f-coimbra", "f-rd"), folders("f-springfield", "f-porto", "f-rd"))
        assert result == ScopeDiff(restart=False, remove_roots=("f-coimbra",), keep_roots=("f-porto", "f-springfield"))

    def test_case_8a_a_change_during_a_diff_walk_is_another_diff(self):
        # a walk of R&D still runs (records pending, `walking`): replacing it by Coimbra removes R&D and walks Coimbra
        assert diff(folders("f-rd"), folders("f-coimbra")) == ScopeDiff(
            restart=False, walk_roots=("f-coimbra",), remove_roots=("f-rd",))

    def test_case_8c_a_root_re_added_while_its_removal_runs_is_walked(self):
        state = DriveSyncState(user_id="u", applied_roots=("f-coimbra",), removing_roots=("f-rd",),
                               applied_scope=scope_key(("f-coimbra",), False))
        # R&D's records are still stored (DELETING), so the parent map holds them; the applied roots do not
        assert diff(applied_of(state), folders("f-coimbra", "f-rd")) == ScopeDiff(restart=False, walk_roots=("f-rd",))

    def test_a_new_root_the_index_does_not_hold_is_walked(self):
        assert diff(folders("f-rd"), folders("f-rd", "f-new")).walk_roots == ("f-new",)

    def test_a_stored_skeleton_that_becomes_a_root_is_walked(self):
        # Coimbra is a skeleton on Springfield's path: choosing it walks it (W4), Springfield stays covered
        assert diff(folders("f-springfield"), folders("f-coimbra", "f-springfield")) == ScopeDiff(
            restart=False, walk_roots=("f-coimbra",))

    def test_the_same_scope_in_normal_form_is_nothing_to_do(self):
        assert diff(folders("f-rd", "f-coimbra"), folders("f-coimbra", "f-rd")) == ScopeDiff(restart=False)


class TestWholeAndDefault:
    @BOTH
    def test_case_5a_whole_to_folders_is_a_narrowing_from_the_whole(self, feed):
        assert diff(WHOLE, folders("f-rd", "f-coimbra"), feed) == ScopeDiff(
            restart=False, keep_roots=("f-coimbra", "f-rd"), from_whole=True)

    def test_whole_to_the_account_default_keeps_nothing(self):
        assert diff(WHOLE, DEFAULT, FeedScope.ACCOUNT) == ScopeDiff(restart=False, from_whole=True)

    @BOTH
    def test_case_5b_folders_to_whole_restarts(self, feed):
        assert diff(folders("f-rd"), WHOLE, feed) == ScopeDiff(restart=True)

    def test_case_5c_onedrive_default_to_folders_is_a_narrowing(self):
        assert diff(DEFAULT, folders("f-rd"), FeedScope.AREA) == ScopeDiff(
            restart=False, keep_roots=("f-rd",), from_whole=True)

    def test_case_5c_google_default_to_folders_is_a_walk(self):
        assert diff(DEFAULT, folders("f-rd"), FeedScope.ACCOUNT) == ScopeDiff(restart=False, walk_roots=("f-rd",))

    def test_google_folders_back_to_default_removes_them(self):
        assert diff(folders("f-rd"), DEFAULT, FeedScope.ACCOUNT) == ScopeDiff(restart=False, remove_roots=("f-rd",))

    def test_onedrive_folders_back_to_default_restarts_its_enumeration(self):
        # the area default covers everything: it is built by enumeration, like the whole drive
        assert diff(folders("f-rd"), DEFAULT, FeedScope.AREA) == ScopeDiff(restart=True)

    def test_onedrive_choosing_the_area_root_itself_is_the_same_scope(self):
        assert diff(DEFAULT, folders(ROOT), FeedScope.AREA) == ScopeDiff(restart=False)


class TestUnfinishedEnumeration:
    """W13, extended by plan Delta 3: a change during any unfinished enumeration restarts."""

    @BOTH
    @pytest.mark.parametrize("old, new", [
        (WHOLE, folders("f-rd")),
        (folders("f-rd"), folders("f-rd", "f-coimbra")),
        (folders("f-rd", "f-coimbra"), folders("f-coimbra")),
        (DEFAULT, folders("f-rd")),
    ])
    def test_any_change_restarts(self, feed, old, new):
        assert diff(old, new, feed, enumerating=True) == ScopeDiff(restart=True)


class TestShapeValid:
    def test_a_default_state_is_valid(self):
        assert shape_valid(DriveSyncState(user_id="u"))

    def test_an_adopted_folder_scope_and_an_adopted_whole_drive_are_valid(self):
        assert shape_valid(DriveSyncState(user_id="u", applied_roots=("f-rd", "f-coimbra"),
                                          applied_scope=scope_key(("f-coimbra", "f-rd"), False)))
        assert shape_valid(DriveSyncState(user_id="u", applied_whole=True, applied_scope="whole"))

    def test_a_state_stamped_before_the_step_1_code_is_not_valid(self):
        assert not shape_valid(DriveSyncState(user_id="u", applied_scope=scope_key(("f-rd",), False)))
        assert not shape_valid(DriveSyncState(user_id="u", applied_scope="whole"))

    def test_a_key_moved_under_unchanged_roots_is_not_valid(self):
        # a rolled-back revision restarted to a new key and left the roots it does not know (plan Delta 1)
        adopted = DriveSyncState(user_id="u", applied_roots=("f-rd",), applied_scope=scope_key(("f-rd",), False))
        assert not shape_valid(replace(adopted, applied_scope=scope_key(("f-coimbra",), False)))

    def test_applied_of_reads_the_stamped_scope(self):
        state = DriveSyncState(user_id="u", applied_whole=False, applied_roots=("f-rd",))
        assert applied_of(state) == folders("f-rd")


class TestScopeRestartStamp:
    def test_a_restart_with_the_scope_stamps_it_with_its_key(self):
        state = DriveSyncState(user_id="u", epoch=3, applied_roots=("f-rd",), applied_scope=scope_key(("f-rd",), False))
        out = scope_restart(state, "whole", applied=WHOLE)
        assert (out.applied_scope, out.applied_whole, out.applied_roots) == ("whole", True, ())
        assert (out.epoch, out.epoch_phase, out.cursor) == (4, EpochPhase.ENUMERATING, None)
        assert shape_valid(out)

    def test_a_positional_restart_leaves_the_scope_fields_alone(self):
        state = DriveSyncState(user_id="u", applied_roots=("f-rd",), applied_scope=scope_key(("f-rd",), False))
        out = scope_restart(state, "whole")
        assert (out.applied_scope, out.applied_whole, out.applied_roots) == ("whole", False, ("f-rd",))


class TestStaleSkeletons:
    """§5.9, W9: skeletons are derived from the current roots' paths, removed only after a complete load."""

    def test_a_skeleton_on_no_current_path_is_stale(self):
        assert stale_skeletons({"f-coimbra", "f-old"}, {"f-coimbra"}, {"f-springfield"}) == frozenset({"f-old"})

    def test_a_link_still_on_another_roots_path_is_kept(self):
        # Springfield moved under R&D: its old path Coimbra is still Porto's path (via Lisbon)
        stored = {"f-coimbra", "f-lisbon"}
        assert stale_skeletons(stored, {"f-coimbra", "f-lisbon", "f-rd"}, {"f-springfield", "f-porto"}) == frozenset()

    def test_a_skeleton_that_became_a_root_is_not_stale(self):
        assert stale_skeletons({"f-coimbra"}, set(), {"f-coimbra"}) == frozenset()

    def test_a_skeleton_now_inside_a_root_is_not_stale_given_the_parent_map(self):
        moved = {**PARENTS, "f-coimbra": "f-rd"}       # Coimbra moved into R&D: in scope, to re-classify, never delete
        assert stale_skeletons({"f-coimbra"}, set(), {"f-rd"}, parents=moved) == frozenset()
        assert stale_skeletons({"f-coimbra"}, set(), {"f-rd"}) == frozenset({"f-coimbra"})

"""`drive_access_policy` (MULTI_DRIVE_RFC §11.3, design §1.2): one case per RFC row and per `decide` step, the review's
B3 (a root write folder on an account-wide drive) and N4 (a write re-checks the scope on the same chain)."""
import pytest

from src.domain.drive_access_op import AccessOp
from src.domain.drive_access_policy import (
    ITEM_FORBIDS, NO_WRITE_FOLDER_LINE, OUTSIDE_SCOPE_LINE, OUTSIDE_WRITE_FOLDER_LINE, PROTECTED_LINE,
    READ_ONLY_DRIVE_LINE, READ_OPS, WRITE_OPS, decide, in_scope, inside_write_folder, requires_ancestry, scope_key,
)
from src.domain.drive_ancestry import Ancestry
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_connection import ROOT_FOLDER
from src.domain.drive_connection_access import ConnectionAccess
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_item_access import ItemAccess
from src.domain.drive_mounts import CROSS_DRIVE_LINE

ROOT, RD, COIMBRA, PLAN, INBOX = "root", "f-rd", "f-coimbra", "i-plan", "f-inbox"
AREA = DriveCapabilities(max_path_chars=400)
ACCOUNT = DriveCapabilities(max_path_chars=400, feed_scope=FeedScope.ACCOUNT)
READ_ONLY = DriveCapabilities(max_path_chars=400, writable=False)

# R&D/Tom's plan.docx, and R&D/Inbox, and the folder Coimbra 2024 beside R&D
PLAN_CHAIN = Ancestry((PLAN, RD, ROOT), reached_root=True)
RD_CHAIN = Ancestry((RD, ROOT), reached_root=True)
INBOX_CHAIN = Ancestry((INBOX, RD, ROOT), reached_root=True)
COIMBRA_CHAIN = Ancestry((COIMBRA, ROOT), reached_root=True)
PARENTLESS = Ancestry(("x",), reached_root=False)


def access(*, folders=(), whole=False, write=ROOT_FOLDER, name="Acme"):
    return ConnectionAccess(drive_name=name, root_item_id=ROOT, scope_folder_ids=frozenset(folders),
                            scope_whole_drive=whole, write_folder_id=write)


APP_FOLDER = access()                                    # no folders, the root as the write folder: today
ACME = access(folders=(RD,), write=RD)                   # R&D indexed and writable


class TestReads:
    def test_inside_a_chosen_folder_is_read(self):
        assert decide(AccessOp.READ, ACME, AREA, None, PLAN_CHAIN).allowed

    @pytest.mark.parametrize("op", sorted(READ_OPS))
    def test_outside_the_chosen_folders_is_refused_with_the_rfc_line(self, op):
        decision = decide(op, ACME, AREA, None, COIMBRA_CHAIN)
        assert not decision.allowed
        assert decision.line == ("This folder is not among the folders indexed for Acme; the owner can add it in the "
                                 "Cabinet.")
        assert decision.line == OUTSIDE_SCOPE_LINE.format(drive="Acme")

    def test_the_area_default_reads_everything_without_a_chain(self):
        assert decide(AccessOp.LIST, APP_FOLDER, AREA, None, None).allowed

    def test_the_account_default_reads_nothing(self):
        assert decide(AccessOp.SEARCH, APP_FOLDER, ACCOUNT, None, None).line == OUTSIDE_SCOPE_LINE.format(drive="Acme")

    def test_the_whole_drive_admits_a_parentless_item(self):
        assert decide(AccessOp.OPEN, access(whole=True), ACCOUNT, None, PARENTLESS).allowed

    @pytest.mark.parametrize("op", [AccessOp.OPEN, AccessOp.READ])
    def test_an_item_the_drive_does_not_let_the_bot_download(self, op):
        decision = decide(op, APP_FOLDER, AREA, ItemAccess(can_download=False), None)
        assert decision.line == ITEM_FORBIDS[op] == "The drive does not let the bot open this file."

    def test_a_listing_ignores_the_download_flag(self):
        assert decide(AccessOp.LIST, APP_FOLDER, AREA, ItemAccess(can_download=False), None).allowed


class TestWrites:
    @pytest.mark.parametrize("op", sorted(WRITE_OPS))
    def test_no_write_folder_is_read_only_with_the_rfc_line(self, op):
        decision = decide(op, access(write=None), AREA, None, None)
        assert decision.line == "This drive is read-only until a write folder is chosen in the Cabinet."
        assert decision.line == NO_WRITE_FOLDER_LINE

    @pytest.mark.parametrize("op", sorted(WRITE_OPS))
    def test_a_drive_that_accepts_no_changes(self, op):
        assert decide(op, APP_FOLDER, READ_ONLY, None, None).line == "Acme does not accept changes."
        assert READ_ONLY_DRIVE_LINE.format(drive="Acme") == "Acme does not accept changes."

    def test_the_app_folder_default_writes_anywhere_without_a_chain(self):
        for op in WRITE_OPS:
            assert decide(op, APP_FOLDER, AREA, None, None, item_id=PLAN).allowed

    def test_outside_the_write_folder_is_refused(self):
        whole = access(whole=True, write=RD)
        decision = decide(AccessOp.APPEND, whole, AREA, None, COIMBRA_CHAIN)
        assert decision.line == OUTSIDE_WRITE_FOLDER_LINE.format(drive="Acme")
        assert decision.line == ("Acme can be changed only inside its write folder; the owner can choose another in "
                                 "the Cabinet.")

    def test_inside_the_write_folder_is_allowed(self):
        assert decide(AccessOp.REPLACE, ACME, AREA, None, PLAN_CHAIN).allowed

    def test_a_create_into_the_write_folder_itself(self):
        assert decide(AccessOp.CREATE, ACME, AREA, ItemAccess(), RD_CHAIN).allowed
        assert decide(AccessOp.SAVE, ACME, AREA, ItemAccess(), INBOX_CHAIN).allowed

    @pytest.mark.parametrize("op", [AccessOp.DELETE, AccessOp.MOVE, AccessOp.RENAME])
    def test_the_write_folder_is_protected(self, op):
        decision = decide(op, ACME, AREA, None, RD_CHAIN)
        assert decision.line == PROTECTED_LINE.format(drive="Acme")
        assert decision.line == ("The write folder and the folders Acme indexes cannot be moved, renamed or deleted; "
                                 "change them in the Cabinet first.")

    @pytest.mark.parametrize("op", [AccessOp.DELETE, AccessOp.MOVE, AccessOp.RENAME])
    def test_a_scope_folder_is_protected(self, op):
        scoped = access(folders=(RD, COIMBRA), write=RD)
        assert decide(op, scoped, AREA, None, COIMBRA_CHAIN, item_id=COIMBRA).line == PROTECTED_LINE.format(
            drive="Acme")

    def test_the_root_as_write_folder_protects_no_folder(self):
        assert decide(AccessOp.DELETE, APP_FOLDER, AREA, None, None, item_id=RD).allowed

    def test_a_move_whose_destination_is_outside_the_write_folder(self):
        whole = access(whole=True, write=RD)
        decision = decide(AccessOp.MOVE, whole, AREA, None, PLAN_CHAIN, destination=COIMBRA_CHAIN)
        assert decision.line == OUTSIDE_WRITE_FOLDER_LINE.format(drive="Acme")
        assert decide(AccessOp.MOVE, whole, AREA, None, PLAN_CHAIN, destination=INBOX_CHAIN).allowed

    def test_a_move_across_connections(self):
        decision = decide(AccessOp.MOVE, APP_FOLDER, AREA, None, None, other_connection=True)
        assert decision.line == CROSS_DRIVE_LINE == "Moving or copying a file between drives is not available yet."


class TestItemAccess:
    @pytest.mark.parametrize("op, forbidden", [
        (AccessOp.APPEND, ItemAccess(can_write_content=False)),
        (AccessOp.REPLACE, ItemAccess(can_write_content=False)),
        (AccessOp.RENAME, ItemAccess(can_rename_move=False)),
        (AccessOp.MOVE, ItemAccess(can_rename_move=False)),
        (AccessOp.DELETE, ItemAccess(can_delete=False)),
        (AccessOp.CREATE, ItemAccess(can_add_children=False)),
        (AccessOp.SAVE, ItemAccess(can_add_children=False)),
    ])
    def test_each_row(self, op, forbidden):
        assert decide(op, APP_FOLDER, AREA, forbidden, None, item_id=PLAN).line == ITEM_FORBIDS[op]
        assert decide(op, APP_FOLDER, AREA, ItemAccess(), None, item_id=PLAN).allowed

    def test_a_move_into_a_folder_that_takes_no_children(self):
        decision = decide(AccessOp.MOVE, APP_FOLDER, AREA, ItemAccess(), None, item_id=PLAN,
                          destination_access=ItemAccess(can_add_children=False))
        assert decision.line == ITEM_FORBIDS[AccessOp.CREATE]


class TestAccountRootWriteFolder:
    """Review B3: on an account-wide drive the root as the write folder admits only items whose chain reaches it."""

    WHOLE = access(whole=True)

    def test_a_parentless_item_is_outside_the_roots_subtree(self):
        decision = decide(AccessOp.APPEND, self.WHOLE, ACCOUNT, None, PARENTLESS)
        assert decision.line == OUTSIDE_WRITE_FOLDER_LINE.format(drive="Acme")

    def test_an_item_whose_chain_reached_the_root_is_allowed(self):
        assert decide(AccessOp.APPEND, self.WHOLE, ACCOUNT, None, PLAN_CHAIN).allowed

    def test_the_chain_is_required_there(self):
        assert requires_ancestry(AccessOp.DELETE, self.WHOLE, ACCOUNT)
        with pytest.raises(ValueError):
            decide(AccessOp.DELETE, self.WHOLE, ACCOUNT, None, None, item_id=PLAN)

    def test_the_area_root_write_folder_needs_no_chain(self):
        assert not requires_ancestry(AccessOp.DELETE, access(whole=True), AREA)
        assert decide(AccessOp.DELETE, access(whole=True), AREA, None, None, item_id=PLAN).allowed


class TestWriteRechecksTheScope:
    """Review N4: a write folder moved out of the scope on the drive's side accepts nothing."""

    def test_a_write_folder_that_left_the_scope(self):
        moved = Ancestry((PLAN, RD, COIMBRA, ROOT), reached_root=True)     # R&D now inside Coimbra 2024
        scoped = access(folders=("f-elsewhere",), write=RD)
        assert decide(AccessOp.APPEND, scoped, AREA, None, moved).line == OUTSIDE_SCOPE_LINE.format(drive="Acme")

    def test_a_move_destination_outside_the_scope(self):
        # the indexed folder sits inside R&D (the write folder): a destination elsewhere in R&D is out of scope
        stale = access(folders=("f-indexed",), write=RD)
        source = Ancestry((PLAN, "f-indexed", RD, ROOT), reached_root=True)
        inside = Ancestry(("f-sub", "f-indexed", RD, ROOT), reached_root=True)
        out = Ancestry(("f-sub", RD, ROOT), reached_root=True)
        assert decide(AccessOp.MOVE, stale, AREA, None, source, destination=inside).allowed
        assert decide(AccessOp.MOVE, stale, AREA, None, source, destination=out).line == \
            OUTSIDE_SCOPE_LINE.format(drive="Acme")


class TestRequiresAncestry:
    @pytest.mark.parametrize("acc, caps, op, expected", [
        (APP_FOLDER, AREA, AccessOp.READ, False),
        (APP_FOLDER, AREA, AccessOp.DELETE, False),
        (access(folders=(RD,)), AREA, AccessOp.LIST, True),
        (access(whole=True), AREA, AccessOp.LIST, False),
        (access(whole=True, write=RD), AREA, AccessOp.SAVE, True),
        (access(folders=(RD,), write=RD), AREA, AccessOp.SAVE, True),
        (access(write=None), AREA, AccessOp.SAVE, False),
        (access(write=RD), READ_ONLY, AccessOp.SAVE, False),
        (access(whole=True), ACCOUNT, AccessOp.SAVE, True),
        (access(whole=True), ACCOUNT, AccessOp.READ, False),
    ])
    def test_truth_table(self, acc, caps, op, expected):
        assert requires_ancestry(op, acc, caps) is expected

    def test_a_missing_chain_is_a_programming_error(self):
        with pytest.raises(ValueError):
            decide(AccessOp.READ, ACME, AREA, None, None)
        with pytest.raises(ValueError):
            decide(AccessOp.SAVE, ACME, AREA, None, None)

    def test_the_helpers_refuse_a_missing_chain_too(self):
        with pytest.raises(ValueError):
            in_scope(ACME, AREA, None)
        with pytest.raises(ValueError):
            inside_write_folder(ACME, AREA, None)
        assert inside_write_folder(access(write=None), AREA, None) is False

    def test_the_drive_root_id_as_write_folder_counts_as_the_root(self):
        acc = access(write=ROOT)
        assert not requires_ancestry(AccessOp.SAVE, acc, AREA)
        assert inside_write_folder(acc, AREA, None)


class TestScopeKey:
    def test_default_and_whole(self):
        assert scope_key((), False) == ""
        assert scope_key((), True) == "whole"

    def test_stable_and_order_independent(self):
        key = scope_key([RD, COIMBRA], False)
        assert key == scope_key([COIMBRA, RD], False) == scope_key([RD, COIMBRA, RD], False)
        assert len(key) == 16 and key != scope_key([RD], False)


class TestValueTypes:
    def test_ancestry(self):
        assert PLAN_CHAIN.item_id == PLAN and PLAN_CHAIN.contains(RD) and not PLAN_CHAIN.contains(COIMBRA)
        with pytest.raises(ValueError):
            Ancestry((), reached_root=True)
        with pytest.raises(ValueError):
            Ancestry((PLAN, ""), reached_root=False)

    def test_item_access_allows_everything_by_default(self):
        assert ItemAccess() == ItemAccess(True, True, True, True, True)

    def test_connection_access_of_a_connection(self):
        from datetime import datetime, timezone

        from src.domain.drive_connection import DriveConnection
        connection = DriveConnection(connection_id="conn-2", user_id="usr-1", account_id="acc-1",
                                     credential_id="cred-2", kind="fake_b", drive_identity="fake_b:d2", ref_tag="b2",
                                     mount_name="Acme", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                     scope_folder_ids=(RD,), write_folder_id=RD)
        assert ConnectionAccess.of(connection, ROOT) == ACME

"""The drive evals' one connection (scripts/validation/drive_eval/eval_connection.py; MULTI_DRIVE_RFC §9): the eval user's
refs resolve to it with the production rules, and `ref_item_id` reads tagged and stored untagged refs."""
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_ref_refusal import DriveRefRefusal, disconnected_ref_line, stale_ref_line
from src.domain.drive_resolved_ref import ResolvedDriveRef
from scripts.validation.drive_eval.eval_connection import (
    EVAL_REF_TAG, EvalDriveConnections, eval_connection, ref_item_id,
)


class TestEvalConnections:
    async def test_the_eval_users_tagged_ref_resolves(self):
        connections = EvalDriveConnections(eval_connection("eval-user"))
        found = await connections.resolve_ref("eval-user", f"ref=drive:{EVAL_REF_TAG}:item-1]")
        assert isinstance(found, ResolvedDriveRef) and found.item_id == "item-1"
        assert (await connections.primary("eval-user")).ref_tag == EVAL_REF_TAG
        assert (await connections.status("eval-user")).connected

    async def test_stale_refs_and_other_users_answer_the_line(self):
        connections = EvalDriveConnections(eval_connection("eval-user"))
        assert await connections.resolve_ref("eval-user", "drive:item-1") == DriveRefRefusal(
            stale_ref_line("drive:item-1"))
        assert await connections.resolve_ref("other", f"drive:{EVAL_REF_TAG}:item-1") == DriveRefRefusal(
            stale_ref_line(f"drive:{EVAL_REF_TAG}:item-1"))
        assert await connections.resolve_ref("eval-user", "no ref here") is None
        assert await connections.primary("other") is None and await connections.list_for_user("other") == []

    async def test_a_disconnected_eval_connection_says_so(self):
        from dataclasses import replace
        gone = replace(eval_connection("eval-user"), state=ConnectionState.DISCONNECTED,
                       disconnected_at=eval_connection("eval-user").created_at)
        connections = EvalDriveConnections(gone)
        found = await connections.resolve_ref("eval-user", f"drive:{EVAL_REF_TAG}:item-1")
        assert found == DriveRefRefusal(disconnected_ref_line(f"drive:{EVAL_REF_TAG}:item-1"))
        assert await connections.list_for_user("eval-user") == []


def test_ref_item_id_reads_tagged_and_untagged_refs():
    assert ref_item_id("[Drive: a.pdf (1KB) ref=drive:ev:item-1]") == "item-1"
    assert ref_item_id("drive:ITEM-OLD") == "ITEM-OLD"
    assert ref_item_id("report.pdf") is None

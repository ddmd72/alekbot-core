"""The scan parts of the email card scripts (email index v2, slice 5): the stored-scan selection, the metered
attachment service settling the run's dollar cap with measured cost, and the dry run's counting budget."""
from pathlib import Path

import pytest

from scripts.email.card_script_support import MeteredAttachmentText, SpendCap, page_gate
from scripts.email.card_store_status import scan_selection, under_scripts_memory
from scripts.email.recard_subscription import dry_run_projection
from src.adapters.job_reader_page_budget import JobReaderPageBudget
from src.domain.drive_index_params import READER_PAGE_USD_ESTIMATE
from src.domain.drive_extraction import DriveModelUnavailable
from tests.unit.services.test_email_attachment_text_service import CREDS, MSG, ScriptedParser, pdf
from tests.unit.services.test_email_attachment_text_service_scans import (
    LAB, LEASE, ScanRig, ScriptedReader, read_out, scan,
)


def doc(state: str, *reads: str) -> dict:
    return {"card_state": state, "card": {"attachments": [{"name": f"f{i}.pdf", "kind": "pdf", "size": 1, "read": r}
                                                           for i, r in enumerate(reads)]}}


def test_scan_selection_takes_carded_and_empty_mail_with_a_scan_not_read():
    ids, states = scan_selection([
        ("m1", doc("carded", "text", "scan_not_read")),
        ("m2", doc("empty", "scan_not_read")),
        ("m3", doc("carded", "text", "read_by_reader")),
        ("m4", doc("pending", "scan_not_read")),
        ("m5", {"card_state": "carded", "card": {}}),
        ("m6", doc("carded")),
    ])
    assert ids == ["m1", "m2"] and dict(states) == {"carded": 1, "empty": 1}


def test_ids_are_only_written_under_scripts_memory():
    assert under_scripts_memory(Path("scripts/memory/email_cards/scans.txt"))
    assert not under_scripts_memory(Path("docs/scans.txt"))


async def test_the_cap_is_settled_with_the_measured_reader_cost_not_the_estimate():
    cap = SpendCap(1.0)
    rig = ScanRig({LAB: pdf("lab")}, parser=ScriptedParser({LAB: scan(3)}), reader=ScriptedReader({LAB: read_out(0.0021)}))
    metered = MeteredAttachmentText(rig.mailbox, rig.document_service, cap)
    budget = JobReaderPageBudget(10, gate=page_gate(cap))
    (a,) = await metered.read_attachments(CREDS, MSG, rig.parts, account_id="acc1", deadline_at=rig.now + 3600,
                                          cache={}, reader_budget=budget)
    assert a.outcome == "read_by_reader"
    assert cap.reserved == pytest.approx(0.0) and cap.spent == pytest.approx(0.0021)
    assert (metered.reader_pages, metered.scans_read, metered.scans_not_read) == (3, 1, 0)
    assert metered.reader_usd == pytest.approx(0.0021) and metered.by_mail[MSG] == (3, pytest.approx(0.0021))


async def test_a_reader_outage_still_settles_what_it_reported():
    cap = SpendCap(1.0)
    outage = DriveModelUnavailable("down", model_calls=1, model_cost=0.0004)
    rig = ScanRig({LAB: pdf("lab")}, parser=ScriptedParser({LAB: scan(3)}), reader=ScriptedReader({LAB: outage}))
    metered = MeteredAttachmentText(rig.mailbox, rig.document_service, cap)
    (a,) = await metered.read_attachments(CREDS, MSG, rig.parts, account_id="acc1", deadline_at=rig.now + 3600,
                                          cache={}, reader_budget=JobReaderPageBudget(10, gate=page_gate(cap)))
    assert a.outcome == "scan_not_read" and cap.reserved == pytest.approx(0.0)
    assert cap.spent == pytest.approx(0.0004) and metered.scans_not_read == 1


async def test_the_dollar_cap_refuses_a_scan_the_page_budget_would_allow():
    cap = SpendCap(READER_PAGE_USD_ESTIMATE * 2)          # room for two pages: a three-page scan is refused
    rig = ScanRig({LAB: pdf("lab")}, parser=ScriptedParser({LAB: scan(3)}))
    metered = MeteredAttachmentText(rig.mailbox, rig.document_service, cap)
    (a,) = await metered.read_attachments(CREDS, MSG, rig.parts, account_id="acc1", deadline_at=rig.now + 3600,
                                          cache={}, reader_budget=JobReaderPageBudget(10, gate=page_gate(cap)))
    assert a.outcome == "scan_not_read" and rig.reader.requests == [] and cap.spent == 0.0


async def test_the_dry_run_budget_counts_the_admissible_scans_and_their_pages_and_reads_nothing():
    rig = ScanRig({LAB: pdf("lab"), LEASE: pdf("lease")}, parser=ScriptedParser({LAB: scan(3), LEASE: scan(11)}))
    metered = MeteredAttachmentText(rig.mailbox, rig.document_service, SpendCap(1.0))
    dry = JobReaderPageBudget(0)
    await metered.read_attachments(CREDS, MSG, rig.parts, account_id="acc1", deadline_at=rig.now + 3600, cache={},
                                   reader_budget=dry)
    assert (dry.asked, dry.asked_pages, dry.used) == (1, 3, 0) and rig.reader.requests == []
    assert metered.scans_not_read == 2
    line = dry_run_projection(metered.scans_not_read, dry.asked, dry.asked_pages)
    assert line.startswith("scans_seen=2 admissible=1 admissible_pages=3 ")
    assert "projected_reader_usd=$0.0018" in line and "projected_scan_mail_usd=$0.0018" in line

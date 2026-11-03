"""The connector's access rule (DRIVE_MCP_CONNECTOR_RFC §7.1, §7.3, E11): `ConnectorAccess`, `in_connector` and the
connector branch of `requires_ancestry` / `decide`. Chains are built by hand: `R&D/Tom's plan.docx` inside connector
access, `12 Oak Street/FA-0137.pdf` outside it, and `Acme Hamburg/Tom's plan.docx` on a Google shared drive (no
reachable parent)."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_access_decision import ALLOWED
from src.domain.drive_access_op import AccessOp
from src.domain.drive_access_policy import (
    CONNECTOR_REFUSED_LINE, ITEM_FORBIDS, READ_OPS, WRITE_OPS, decide, in_connector, requires_ancestry,
)
from src.domain.drive_ancestry import Ancestry
from src.domain.drive_audience import DriveAudience
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_access import ConnectionAccess
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_item_access import ItemAccess

ROOT, RD, OAK, PLAN, INVOICE, HAMBURG, HPLAN = "root", "f-rd", "f-oak", "i-plan", "i-fa0137", "f-hamburg", "i-hplan"
AREA = DriveCapabilities(max_path_chars=400)
ACCOUNT = DriveCapabilities(max_path_chars=400, feed_scope=FeedScope.ACCOUNT)
C = DriveAudience.CONNECTOR

PLAN_CHAIN = Ancestry((PLAN, RD, ROOT), reached_root=True)
INVOICE_CHAIN = Ancestry((INVOICE, OAK, ROOT), reached_root=True)
CUT_CHAIN = Ancestry((PLAN,), reached_root=False)           # the parent could not be read


def access(*, folders=(), whole=False, connector=None):
    return ConnectionAccess(drive_name="Drive", root_item_id=ROOT, scope_folder_ids=frozenset(folders),
                            scope_whole_drive=whole, write_folder_id=None, connector_access=connector)


RD_ONLY = ConnectorAccess.folders((RD,))


def test_connector_access_is_whole_or_folders_never_both_never_empty():
    assert ConnectorAccess.whole_drive() == ConnectorAccess(whole=True)
    assert ConnectorAccess.folders((RD, OAK, RD)).folder_ids == (RD, OAK)
    for bad in (lambda: ConnectorAccess(), lambda: ConnectorAccess(whole=True, folder_ids=(RD,)),
                lambda: ConnectorAccess.folders(()), lambda: ConnectorAccess.folders(("",)),
                lambda: ConnectorAccess.folders(RD), lambda: ConnectorAccess(folder_ids=[RD])):
        with pytest.raises(ValueError):
            bad()


def test_no_connector_access_admits_nothing():
    assert not in_connector(access(whole=True), PLAN_CHAIN)
    assert not in_connector(access(whole=True), None)


def test_whole_admits_without_a_chain():
    assert in_connector(access(connector=ConnectorAccess.whole_drive()), None)


def test_folders_need_a_chain():
    with pytest.raises(ValueError):
        in_connector(access(connector=RD_ONLY), None)


@pytest.mark.parametrize("reached_root", [True, False])
def test_a_chain_holding_a_connector_folder_is_admitted_reached_root_or_not(reached_root):
    chain = Ancestry((HPLAN, HAMBURG), reached_root=reached_root)
    assert in_connector(access(connector=ConnectorAccess.folders((HAMBURG,))), chain)


def test_a_cut_chain_or_one_without_a_connector_folder_is_refused():
    assert not in_connector(access(connector=RD_ONLY), CUT_CHAIN)
    assert not in_connector(access(connector=RD_ONLY), INVOICE_CHAIN)


@pytest.mark.parametrize("scope_folders, item_access, chain, expected", [
    ((), None, PLAN_CHAIN, ALLOWED),                                         # in both (AREA default scope)
    ((OAK,), None, INVOICE_CHAIN, CONNECTOR_REFUSED_LINE),                   # scope only
    ((OAK,), None, PLAN_CHAIN, CONNECTOR_REFUSED_LINE),                      # connector only: R&D moved out of scope
    ((), ItemAccess(can_download=False), PLAN_CHAIN, ITEM_FORBIDS[AccessOp.OPEN]),
])
def test_decide_under_the_audience_needs_scope_and_connector_on_one_chain(scope_folders, item_access, chain,
                                                                         expected):
    decision = decide(AccessOp.OPEN, access(folders=scope_folders, connector=RD_ONLY), AREA, item_access, chain,
                      audience=C)
    assert (decision if expected is ALLOWED else decision.line) == expected


@pytest.mark.parametrize("op, folders, whole, chain, caps", [
    (AccessOp.READ, (RD,), False, PLAN_CHAIN, AREA),
    (AccessOp.LIST, (RD,), False, INVOICE_CHAIN, AREA),
    (AccessOp.SEARCH, (), False, None, AREA),
    (AccessOp.SEARCH, (), False, None, ACCOUNT),
    (AccessOp.OPEN, (), True, CUT_CHAIN, ACCOUNT),
    (AccessOp.CREATE, (), False, None, AREA),
])
@pytest.mark.parametrize("connector", [None, ConnectorAccess.whole_drive(), RD_ONLY])
def test_decide_outside_the_audience_is_unchanged_whatever_the_connector_access(op, folders, whole, chain, caps,
                                                                               connector):
    base = access(folders=folders, whole=whole)
    with_field = replace(base, connector_access=connector)
    assert decide(op, with_field, caps, None, chain) == decide(op, base, caps, None, chain)
    assert requires_ancestry(op, with_field, caps) == requires_ancestry(op, base, caps)


@pytest.mark.parametrize("op", sorted(WRITE_OPS))
def test_every_write_under_the_audience_is_refused(op):
    whole = access(whole=True, connector=ConnectorAccess.whole_drive())
    writable = replace(whole, write_folder_id="")
    assert decide(op, writable, AREA, None, PLAN_CHAIN, audience=C).line == CONNECTOR_REFUSED_LINE
    assert decide(op, writable, AREA, None, None, audience=C).line == CONNECTOR_REFUSED_LINE


@pytest.mark.parametrize("op", sorted(READ_OPS))
def test_requires_ancestry_under_the_audience_with_connector_folders_is_true_for_every_read(op):
    assert requires_ancestry(op, access(whole=True, connector=RD_ONLY), AREA, audience=C)
    assert not requires_ancestry(op, access(whole=True, connector=RD_ONLY), AREA)
    assert not requires_ancestry(op, access(whole=True, connector=ConnectorAccess.whole_drive()), AREA, audience=C)
    with pytest.raises(ValueError):
        decide(op, access(whole=True, connector=RD_ONLY), AREA, None, None, audience=C)


def _connection(**kw):
    return DriveConnection(connection_id="conn-a", user_id="u-alex", account_id="acc-alex", credential_id="cred-a",
                           kind="fake", drive_identity="fake:a", ref_tag="a1", created_at=datetime(2026, 11, 3,
                                                                                                 tzinfo=timezone.utc),
                           **kw)


def test_connection_access_of_carries_the_connector_access():
    assert ConnectionAccess.of(_connection(), ROOT).connector_access is None
    assert ConnectionAccess.of(_connection(connector_access=RD_ONLY), ROOT).connector_access == RD_ONLY


def test_a_drive_connection_refuses_a_connector_access_that_is_not_one():
    assert _connection().connector_access is None
    for bad in ("whole", {"mode": "whole"}, (RD,)):
        with pytest.raises(ValueError):
            _connection(connector_access=bad)

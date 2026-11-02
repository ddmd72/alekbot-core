"""Δ14: every drive row's reconnect link names the row's own kind, and a user with no drive is offered every offered
drive kind. On the page source and on the routes (a registry with two drive kinds). Invented cast only."""
import json
import re
import shutil
from unittest.mock import MagicMock

import pytest


from src.composition.connector_registry import ConnectorRegistry
from src.domain.connector_spec import ConnectorSpec
from src.domain.drive_connection import DriveConnection
from src.domain.integration import Integration
from tests.unit.web.test_cabinet_drive_connections_2b import T0, USER, World, call
from tests.unit.web.test_cabinet_drive_index_strings import _dicts
from tests.unit.web.test_cabinet_drive_redesign import CARD, COUNTERS, INDEX, PICKER, _run


def _spec(kind: str) -> ConnectorSpec:
    return ConnectorSpec(kind=kind, integration=Integration.DRIVE, realm=f"realm_{kind}", required_scopes=("files",),
                         multiple=True, id_space=kind, label=kind.upper())


class _Edge:
    pass


def test_the_row_reconnect_href_is_built_from_the_row_kind():
    html, _ = _dicts()
    assert 'connectHref("drive", row.reconnect' not in html
    assert "/auth/connect/${encodeURIComponent(row.kind)}?credential=${encodeURIComponent(row.reconnect.credential_id)}" in html


def test_a_user_with_no_drive_gets_the_add_drive_over_every_offered_kind():
    html, _ = _dicts()
    assert 'connectButton(data.connect_href, t("platform.connect_drive"))' not in html
    status = re.search(r"\n      function renderDriveStatus\(data\) \{.*?\n      }\n", html, re.S).group(0)
    assert "${renderAddDrive(data)}" in status
    assert not re.search(r'data\.connect_href\s*=\s*await connectHref\("drive"\)', html)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_add_drive_offers_a_button_per_offered_kind_with_no_drive_linked():
    html, _ = _dicts()
    add = re.search(r"\n      function renderAddDrive\(data\) \{.*?\n      }\n", html, re.S).group(0)
    data = {"connections": [], "add_kinds": [{"kind": "fake_a", "label": "FAKE_A"}, {"kind": "fake_b", "label": "FAKE_B"}],
            "dr_copy": None, "default_name": "Drive"}
    out = _run(f"driveData = {json.dumps(data)}; renderDriveStatus(driveData);"
               " process.stdout.write(JSON.stringify({ html: els['drive-container'].innerHTML }));",
               scope=CARD + COUNTERS + INDEX + PICKER + add)   # the real renderAddDrive, not the harness stub
    assert "addDrive('fake_a')" in out["html"] and "addDrive('fake_b')" in out["html"]
    assert "T:platform.not_connected" in out["html"]


async def test_two_offered_drive_kinds_are_listed_and_each_row_sends_its_own_kind():
    w = World()
    registry = ConnectorRegistry(edges={"realm_fake_a": MagicMock(), "realm_fake_b": MagicMock()},
                                 specs=(_spec("fake_a"), _spec("fake_b")))
    w.registry = registry
    for n, kind in enumerate(("fake_a", "fake_b"), start=1):
        w.repo.connections[(USER, f"conn-{n}")] = DriveConnection(
            connection_id=f"conn-{n}", user_id=USER, account_id="acc-1", credential_id=f"cred-{n}", kind=kind,
            drive_identity=f"{kind}:d{n}", ref_tag=f"b{n}", mount_name=f"Acme {n}", created_at=T0)
    app = w.app(connectors=registry)
    _, listed = await call(app, "get", "/api/connectors")
    assert [c["kind"] for c in listed["connectors"] if c["integration"] == "drive"] == ["fake_a", "fake_b"]
    _, rows = await call(app, "get", "/api/drive/connections")
    assert {r["kind"] for r in rows["connections"]} == {"fake_a", "fake_b"}
    assert all(r["reconnect"] for r in rows["connections"])   # no grant stored: a reconnect, each of its own kind

"""Helpers for the HTML render client tests: the real sandbox app serving `render.pdf` and `render.png`."""
import httpx
from quart import Quart

from src.domain.sandbox_contract import SandboxOp
from src.web import sandbox_app
from tests.unit.web.test_sandbox_app import CEILING, FakeEngine, FakeGate, FakeRecycler

HOUSEHOLD_BUDGET_HTML = (
    "<html><body><h1>Household budget 2026</h1><table><tr><td>rent</td><td>11,400</td></tr>"
    "<tr><td>groceries</td><td>4,200</td></tr><tr><td>transport</td><td>960</td></tr>"
    "<tr><td>holidays</td><td>2,500</td></tr></table></body></html>"
)
ROME_TRIP_HTML = (
    "<html><body><h2>Rome trip</h2><p>Albergo Immaginario, booking RM-55120</p>"
    "<p>10-14 Aug 2025, 560 EUR</p></body></html>"
)


def render_app_transport(tmp_path, pdf_engine=None, png_engine=None, ceiling=CEILING) -> httpx.ASGITransport:
    """`real_app_transport` variant registering {RENDER_PDF, RENDER_PNG} to the given engines."""
    pdf_engine = pdf_engine or FakeEngine(tmp_path, payload=b"%PDF-1.7")
    png_engine = png_engine or FakeEngine(tmp_path, payload=b"\x89PNG")
    work_root = tmp_path / "work"
    work_root.mkdir(exist_ok=True)
    app = Quart(__name__)
    app.register_blueprint(sandbox_app.create_sandbox_blueprint(
        {SandboxOp.RENDER_PDF: pdf_engine, SandboxOp.RENDER_PNG: png_engine}, FakeGate(), FakeRecycler(), ceiling,
        {"work_root": str(work_root), "engine_user": None, "recycle": "never",
         "output_root": str(tmp_path / "engine-out")}))
    transport = httpx.ASGITransport(app=app)
    transport.pdf_engine = pdf_engine  # type: ignore[attr-defined]
    transport.png_engine = png_engine  # type: ignore[attr-defined]
    return transport

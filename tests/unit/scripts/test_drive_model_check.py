"""The owner's live model check (scripts/validation/drive_model_check.py; USER_DRIVE_INDEXING_RFC §24.12) run offline:
fake providers, prompts assembled from upload files by the real PromptAssemblyService, output only under the memory
root. Proves the script drives the production runners side by side and records cost without billing anyone."""
import json
from pathlib import Path

import pytest

from scripts.validation import drive_model_check as check
from src.services.provider_registry import ProviderRegistry
from tests.unit.agents.drive_model_fakes import RoutedLLM, reply

JPEG = b"\xff\xd8\xff\xe0jpeg"


def _uploads(tmp_path: Path) -> Path:
    up = tmp_path / "uploads"
    up.mkdir()
    for agent in ("drive_reader", "drive_triage"):
        tag = agent.split("_")[1].upper()
        for cls in ("cognitive_process", "output_format"):
            tid = f"{cls.upper()}_DRIVE_{tag}"
            (up / f"{tid}.json").write_text(json.dumps({"token_id": tid, "category": cls, "class": cls,
                                                        "content": f'rules: "{tid}"', "metadata": {}}))
        (up / f"{agent}_agent_v1.json").write_text(json.dumps({
            "blueprint_id": f"{agent}_agent_v1", "outer_class": "A extends Agent",
            "class_order": ["cognitive_process", "output_format"]}))
        (up / f"{agent}.json").write_text(json.dumps({"agent_id": agent, "blueprint_id": f"{agent}_agent_v1", "tokens": {
            f"COGNITIVE_PROCESS_DRIVE_{tag}": {"order": 10}, f"OUTPUT_FORMAT_DRIVE_{tag}": {"order": 10}}}))
    return up


class _Splitter:
    async def pdf_page_count(self, path):
        return 3

    async def cut_pdf_window(self, path, first_page, page_count, out_path):
        Path(out_path).write_bytes(b"%PDF window")


def _registry(handler):
    registry = ProviderRegistry()
    registry.register("claude", RoutedLLM(handler))
    registry.register("openai", RoutedLLM(handler))
    return registry


async def test_read_writes_text_brief_and_cost_per_provider(tmp_path):
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "scan.pdf").write_bytes(b"%PDF")
    (folder / "receipt.jpg").write_bytes(JPEG)
    (folder / "notes.docx").write_bytes(b"PK")
    brief = {"doc_type": "receipt", "description": "x", "doc_date": "2026-05-01", "parties": [], "amount": None}
    registry = _registry(lambda r: reply({"text": "TOTAL 9.99", "brief": brief}))
    out = await check.run(["read", str(folder)], registry=registry,
                          assembly=check.local_assembly_service(_uploads(tmp_path)), splitter=_Splitter(),
                          memory=tmp_path / "memory")
    assert out.parent == tmp_path / "memory"
    summary = json.loads((out / "read" / "summary.json").read_text())
    assert set(summary) == {"claude", "openai"}
    rows = {r["file"]: r for r in summary["claude"]["files"]}
    assert set(rows) == {"scan.pdf", "receipt.jpg"}
    assert rows["scan.pdf"]["status"] == "text" and rows["scan.pdf"]["pages"] == 3
    assert summary["claude"]["calls"] == 2 and summary["claude"]["cost"] > 0
    assert (out / "read" / "openai" / "receipt.txt").read_text() == "TOTAL 9.99"
    assert json.loads((out / "read" / "claude" / "scan.brief.json").read_text())["doc_date"] == "2026-05-01"


async def test_triage_compares_sizes_per_provider(tmp_path):
    folder = tmp_path / "s0g"
    folder.mkdir()
    for item in ("01", "02"):
        for size in ("medium", "large", "c2048x2048"):
            (folder / f"{item}_{size}.jpg").write_bytes(JPEG + item.encode())

    def handler(request):
        n = sum(1 for p in request.messages[0].parts if p.file_data)
        return reply({"images": [{"index": i, "verdict": "photo", "caption": "a dog"} for i in range(1, n + 1)]})

    out = await check.run(["triage", str(folder), "--providers", "claude"], registry=_registry(handler),
                          assembly=check.local_assembly_service(_uploads(tmp_path)), memory=tmp_path / "memory")
    data = json.loads((out / "triage" / "verdicts.json").read_text())
    assert set(data["totals"]) == {"claude/medium", "claude/large"}
    assert data["totals"]["claude/medium"]["images"] == 2 and data["totals"]["claude/medium"]["calls"] == 1
    assert data["items"]["01"] == {"claude/medium": {"verdict": "photo", "caption": "a dog"},
                                   "claude/large": {"verdict": "photo", "caption": "a dog"}}


def test_output_root_is_the_gitignored_memory_folder():
    assert check.MEMORY == Path(check.__file__).resolve().parents[2] / "scripts" / "memory"


@pytest.mark.parametrize("name,expected", [("01_medium.jpg", ("01", "medium")), ("ab_c2048x2048.png", ("ab", "c2048x2048")),
                                           ("report.json", None)])
def test_rendition_names(name, expected):
    m = check._RENDITION.match(name)
    assert (m.group("item"), m.group("size")) == expected if expected else m is None

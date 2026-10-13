"""The live model check's Search v2 modes (scripts/validation/drive_model_check.py; USER_DRIVE_INDEXING_RFC §25.4) run
offline: fake providers, prompts assembled from upload files by the real PromptAssemblyService, output only under the
memory root. `profile` cuts the input from stored chunks, profiles and verifies; `passages` sends chunk pairs and
grounds the items; triage records the v2 photo fields."""
import json
import re
from pathlib import Path

from scripts.validation import drive_model_check as check
from src.services.provider_registry import ProviderRegistry
from tests.unit.agents.drive_model_fakes import RoutedLLM, reply
from tests.unit.agents.test_drive_profiler_agent import ANSWER

TEXT = ("Factura nº FA-0137. Fecha: 15/03/2024. Cliente: Juan García, Valencia. Total: 1.250,00 €. "
        "Electricidad del mes de marzo. ") * 6


def _uploads(tmp_path: Path) -> Path:
    up = tmp_path / "uploads"
    up.mkdir()
    for agent, tag in (("drive_profiler", "PROFILER"), ("drive_passages", "PASSAGES"), ("drive_triage", "TRIAGE")):
        for cls in ("cognitive_process", "output_format"):
            tid = f"{cls.upper()}_DRIVE_{tag}"
            (up / f"{tid}.json").write_text(json.dumps({"token_id": tid, "category": cls, "class": cls,
                                                        "content": f'rules: "{tid}"', "metadata": {}}))
        (up / f"{agent}_agent_v1.json").write_text(json.dumps({
            "blueprint_id": f"{agent}_agent_v1", "outer_class": "A extends Agent",
            "class_order": ["cognitive_process", "output_format"]}))
        (up / f"{agent}.json").write_text(json.dumps({"agent_id": agent, "blueprint_id": f"{agent}_agent_v1",
                                                      "tokens": {f"COGNITIVE_PROCESS_DRIVE_{tag}": {"order": 10},
                                                                 f"OUTPUT_FORMAT_DRIVE_{tag}": {"order": 10}}}))
    return up


def _registry(handler):
    registry = ProviderRegistry()
    registry.register("openai", RoutedLLM(handler))
    return registry


def _folder(tmp_path):
    folder = tmp_path / "texts"
    folder.mkdir()
    (folder / "factura.txt").write_text(TEXT, encoding="utf-8")
    (folder / "short.md").write_text("Buy milk", encoding="utf-8")
    (folder / "photo.jpg").write_bytes(b"\xff\xd8")
    return folder


async def test_profile_writes_the_verified_profile_and_cost(tmp_path):
    out = await check.run(["profile", str(_folder(tmp_path)), "--providers", "openai"],
                          registry=_registry(lambda r: reply(ANSWER)),
                          assembly=check.local_assembly_service(_uploads(tmp_path)), memory=tmp_path / "memory")
    summary = json.loads((out / "profile" / "summary.json").read_text())
    rows = {r["file"]: r for r in summary["openai"]["files"]}
    assert set(rows) == {"factura.txt", "short.md"}
    assert rows["short.md"]["status"] == "no_model_input"
    assert rows["factura.txt"]["status"] == "ok" and rows["factura.txt"]["cost"] > 0
    assert rows["factura.txt"]["drops"]["dates"] == 0
    saved = json.loads((out / "profile" / "openai" / "factura.profile.json").read_text())
    assert saved["verified"]["entities"][0]["printed"] == "Valencia"
    assert summary["openai"]["calls"] == 1 and summary["openai"]["timing"]["calls"] == 1


async def test_passages_ground_the_items(tmp_path):
    def handler(request):
        labels = [int(n) for n in re.findall(r'<passage n="(\d+)">', request.messages[0].parts[0].text)]
        return reply({"passages": [{"index": i, "items": [
            {"type": "keyword", "text": "electricity", "evidence": "Electricidad del mes de marzo"},
            {"type": "keyword", "text": "Paris", "evidence": "not in the text"}]} for i in labels]})

    out = await check.run(["passages", str(_folder(tmp_path)), "--providers", "openai", "--max-chunks", "2"],
                          registry=_registry(handler), assembly=check.local_assembly_service(_uploads(tmp_path)),
                          memory=tmp_path / "memory")
    summary = json.loads((out / "passages" / "summary.json").read_text())
    rows = {r["file"]: r for r in summary["openai"]["files"]}
    assert rows["factura.txt"]["outcomes"] == {"0": "ok"} and rows["factura.txt"]["kept"] == 1
    assert rows["factura.txt"]["drops"]["evidence"] == 1
    hints = json.loads((out / "passages" / "openai" / "factura.hints.json").read_text())
    assert hints["0"][0]["evidence"] == "Electricidad del mes de marzo"


async def test_triage_records_the_photo_fields(tmp_path):
    folder = tmp_path / "s0g"
    folder.mkdir()
    (folder / "01_large.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg01")

    def handler(request):
        return reply({"images": [{"index": 1, "verdict": "photo", "description": "a dog on a beach",
                                  "objects": [{"name": "dog", "forms": ["perro"]}], "scene": "beach",
                                  "visible_text": ""}]})

    out = await check.run(["triage", str(folder), "--providers", "openai", "--sizes", "large"],
                          registry=_registry(handler), assembly=check.local_assembly_service(_uploads(tmp_path)),
                          memory=tmp_path / "memory")
    data = json.loads((out / "triage" / "verdicts.json").read_text())
    assert data["items"]["01"]["openai/large"] == {"verdict": "photo", "description": "a dog on a beach",
                                                   "scene": "beach", "visible_text": "",
                                                   "objects": [{"name": "dog", "forms": ["perro"]}]}

"""The G3 snapshot overlay serves the drive specialist's notes protocol too (scripts/validation/drive_eval/g3_overrides.py).

Firestore does not hold `PROTOCOL_DRIVE_NOTES` until the owner uploads it: an overlay that lacked the id would make the
assembly service skip the token, and the "after" run would silently drop the notes protocol."""

from scripts.validation.drive_eval import g3_overrides as g3
from tests.unit.scripts.test_drive_eval_g3_overrides import SNAP, AllowAll, Blueprints, Profiles, Tokens
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService

NOTES = "PROTOCOL_DRIVE_NOTES"


def test_the_notes_protocol_is_an_overlay_id():
    assert NOTES in g3.OVERLAY_TOKEN_IDS


async def test_the_overlay_renders_the_notes_protocol_from_the_snapshot_when_the_real_repository_lacks_it():
    live_ids = [i for i in g3.OVERLAY_TOKEN_IDS if i != NOTES]
    svc = PromptAssemblyService(Tokens(*live_ids), Blueprints(), Profiles(), AllowAll(), None, None)  # type: ignore[arg-type]
    g3.install_snapshot_prompts(svc, SNAP)

    prompt = await svc._assemble_static_template("drive", "acc", "usr")

    text = (SNAP / "tokens" / "system" / f"{NOTES}.groovy").read_text(encoding="utf-8").split("\n---\n", 1)[1]
    longest = max((line.strip() for line in text.splitlines()), key=len)
    assert longest in prompt

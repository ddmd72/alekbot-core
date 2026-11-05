"""Wire — a native's `DriveItem.exported` (GOOGLE_DRIVE_RFC §3.5; EXTERNAL_FILES U4b): a Doc, Sheet, Slides or Drawing
is served as its export, so the item says so and its `mime_type` is the export's; a binary file and a native with no
export (a Form) are not exported. Served by the stateful Drive fake."""
import pytest

from tests.support.google_drive_fake import DOC, DRAWING, FORM, SHEET, SLIDES, google_setup


@pytest.fixture
async def setup(monkeypatch):
    return await google_setup(monkeypatch)


@pytest.mark.parametrize("native", [DOC, SHEET, SLIDES, DRAWING])
async def test_an_exportable_native_is_exported(setup, native):
    node = setup.a.add_native(setup.a.root.id, "Q3 plan", native)
    assert (await setup.adapter.get_item(setup.handle, node.id)).exported is True


async def test_a_binary_file_and_a_form_are_not_exported(setup):
    binary = setup.a.add_file(setup.a.root.id, "Q3 plan.md", b"# plan", mime="text/markdown")
    form = setup.a.add_native(setup.a.root.id, "Garden sign-up", FORM)
    assert (await setup.adapter.get_item(setup.handle, binary.id)).exported is False
    assert (await setup.adapter.get_item(setup.handle, form.id)).exported is False

"""The reader and triage runners carry the capture their model said (USER_DRIVE_INDEXING_RFC §26.3, D34): a PDF's is the
one most windows agreed on (the first seen wins a tie), an image's is its one answer, and a bumped READER_VERSION says
the stored reads are stale (§26.7). Triage's reaches the result of a text image only."""
from tests.unit.agents.drive_model_fakes import reply
from tests.unit.composition.test_drive_reader_runner import (
    JPEG, FakeSplitter, _key, _request, _runner as reader_runner,
)
from tests.unit.composition.test_drive_triage_runner import (
    TEXT, PHOTO, _entry, _image, _indexes, _runner as triage_runner,
)
from src.composition.drive_reader_runner import READER_VERSION
from src.domain.drive_index_params import MODEL_PAGES_PER_CALL


def _reader_reply(captures):
    """Window n (1-based, from the source key `|w<n>`) answers captures[n - 1]; a half keeps its window's answer."""
    def handler(request):
        window = int(_key(request).split("|w")[1][0])
        return reply({"text": f"text of {_key(request)}", "capture": captures[window - 1], "brief": None})
    return handler


class TestReader:
    def test_the_reader_version_is_2(self):
        assert READER_VERSION == 2

    async def test_most_windows_decide_a_pdf(self, tmp_path):
        pages = MODEL_PAGES_PER_CALL * 3
        runner, _, _ = reader_runner(_reader_reply(["scan", "screenshot", "scan"]), splitter=FakeSplitter(pages))
        out = await runner.extract(_request(tmp_path, pages=pages))
        assert (out.capture, out.capture_source) == ("scan", "automatic")

    async def test_the_first_seen_wins_a_tie(self, tmp_path):
        pages = MODEL_PAGES_PER_CALL * 2
        runner, _, _ = reader_runner(_reader_reply(["screenshot", "scan"]), splitter=FakeSplitter(pages))
        out = await runner.extract(_request(tmp_path, pages=pages))
        assert out.capture == "screenshot"

    async def test_a_window_with_no_capture_does_not_vote(self, tmp_path):
        pages = MODEL_PAGES_PER_CALL * 3
        runner, _, _ = reader_runner(_reader_reply(["", "digital", ""]), splitter=FakeSplitter(pages))
        assert (await runner.extract(_request(tmp_path, pages=pages))).capture == "digital"

    async def test_no_window_saying_one_is_none(self, tmp_path):
        runner, _, _ = reader_runner(_reader_reply(["fax"]), splitter=FakeSplitter(MODEL_PAGES_PER_CALL))
        out = await runner.extract(_request(tmp_path, pages=MODEL_PAGES_PER_CALL))
        assert (out.capture, out.capture_source) == ("", "") and out.text

    async def test_an_image_carries_its_one_answer(self, tmp_path):
        runner, _, _ = reader_runner(lambda r: reply({"text": "Receipt 12.50", "capture": "screenshot",
                                                      "brief": None}))
        out = await runner.extract(_request(tmp_path, kind="image", data=JPEG))
        assert (out.capture, out.capture_source) == ("screenshot", "automatic")

    async def test_the_version_stamp_in_the_outcome(self, tmp_path):
        runner, _, _ = reader_runner(lambda r: reply({"text": "x y", "capture": "scan", "brief": None}))
        assert (await runner.extract(_request(tmp_path, kind="image", data=JPEG))).extracted_by == "reader@2"


class TestDominant:
    def test_a_halved_windows_pieces_are_one_vote(self):
        from src.composition.drive_reader_runner import _dominant
        halved = ["scan", "scan", "scan", "scan"]          # one window read as four pieces
        others = [_dominant(["screenshot"]), _dominant(["screenshot"])]
        assert _dominant([_dominant(halved), *others]) == "screenshot"

    def test_no_votes_is_none(self):
        from src.composition.drive_reader_runner import _dominant
        assert _dominant([]) == "" and _dominant(["", ""]) == ""


class TestTriage:
    async def test_a_text_image_carries_its_capture_and_a_photo_none(self):
        def handler(request):
            return reply({"images": [{**_entry(1, "text"), "capture": "document_photo"},
                                     {**_entry(2, description="a dog"), "capture": "scan"}]})

        runner, _, _ = triage_runner(handler)
        out = await runner.triage("user-1", "acc-1", [_image(1), _image(2)])
        by_id = {r.item_id: r for r in out.results}
        assert (by_id["item-1"].verdict, by_id["item-1"].capture) == (TEXT, "document_photo")
        assert (by_id["item-2"].verdict, by_id["item-2"].capture) == (PHOTO, "")

    async def test_an_answer_without_a_capture_is_still_a_result(self):
        runner, _, _ = triage_runner(lambda r: reply({"images": [_entry(i, "text") for i in _indexes(r)]}))
        out = await runner.triage("user-1", "acc-1", [_image(1)])
        assert [(r.verdict, r.capture) for r in out.results] == [(TEXT, "")]

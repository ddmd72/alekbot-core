"""The Cabinet page's picker section for folders outside the drive's root: the page reads `out.data.outer`, renders the
`drive.picker_outer` heading, the key exists in the four dictionaries, and no provider word is in the page."""
from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts


def test_the_page_reads_the_outer_list_and_renders_its_heading():
    html, _ = _dicts()
    assert "out.data.outer" in html
    assert 't("drive.picker_outer")' in html


def test_the_heading_exists_in_every_language_and_differs():
    _, dicts = _dicts()
    texts = [dicts[lang].get("drive.picker_outer") for lang in LANGS]
    assert all(texts)
    assert len(set(texts)) == len(LANGS)


def test_no_provider_word_in_the_page():
    html, _ = _dicts()
    for word in ("onedrive", "google drive", "google_drive", "gdrive"):
        assert word not in html.lower(), word

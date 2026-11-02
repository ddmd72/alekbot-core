"""The data-URI stripper's edges (drive RFC G1b): text that only looks like an image is left intact (a `[` without its
`]`, an `<img` without its `>`, an `<img>` whose src is not a data URI, a link or an https image), while a real data
image is replaced by its alt text however its target is spelled (nested brackets, spaces before the URI, a title in
either quote, a `<…>` reference definition). The risk is content mangled or swallowed. One input table; invented cast."""
import pytest

from src.domain.text_data_uris import drop_data_uri_images

_U = "data:image/png;base64,AAAA"

_TABLE = [
    # (id, text, expected)
    ("nested-brackets-in-alt", f"Plan ![Garden [plan] 2026/27]({_U}) end", "Plan Garden [plan] 2026/27 end"),
    ("spaces-before-the-uri", f"Plan ![Hamburg talk](  {_U}) end", "Plan Hamburg talk end"),
    ("tab-before-the-uri", f"Plan ![Hamburg talk](\t{_U}) end", "Plan Hamburg talk end"),
    ("double-quoted-title", f'Plan ![Hamburg talk]({_U} "Acme Hamburg") end', "Plan Hamburg talk end"),
    ("single-quoted-title", f"Plan ![Hamburg talk]({_U} 'Acme Hamburg') end", "Plan Hamburg talk end"),
    ("whitespace-before-the-title", f'Plan ![a]({_U}   \n "Acme Hamburg") end', "Plan a end"),
    ("title-then-more-text", f'![a]({_U} "t") one ![b]({_U}) two', "a one b two"),
    ("html-attributes-in-any-order", f'Plan <img alt="Hamburg talk" src="{_U}"> end', "Plan Hamburg talk end"),
    ("html-single-quotes-self-closing", f"Plan <IMG src='{_U}' alt='Lukas Weber'/> end", "Plan Lukas Weber end"),
    ("angle-bracket-reference-definition", f"![Garden plan][p1] end\n[p1]: <{_U}>\n", "Garden plan end\n"),
    # intact: nothing here is a data image
    ("unclosed-alt-bracket", "Plan ![Garden plan 2026/27 and data: more", "Plan ![Garden plan 2026/27 and data: more"),
    ("unclosed-alt-before-a-target", f"Plan ![Garden plan ({_U}) end", f"Plan ![Garden plan ({_U}) end"),
    ("bracket-without-close", f"[Garden plan data: ![x]({_U})", "[Garden plan data: x"),
    ("img-without-a-closing-gt", f'Plan <img src="{_U}" alt="x" end data:', f'Plan <img src="{_U}" alt="x" end data:'),
    ("img-with-an-https-src", f'<img src="https://acme.example.invalid/a.png" alt="x"> {_U}',
     f'<img src="https://acme.example.invalid/a.png" alt="x"> {_U}'),
    ("img-without-a-src", f'<img alt="x"> {_U}', f'<img alt="x"> {_U}'),
    ("unknown-reference-stays", f"![Hamburg talk][p2] end\n[p1]: {_U}\n", "![Hamburg talk][p2] end\n"),
    ("reference-without-its-close", f"![Hamburg talk][p1 end\n[p1]: {_U}\n", "![Hamburg talk][p1 end\n"),
    ("a-line-that-is-not-a-definition", f"kept [x]: y data:\n[p1]: {_U}\n", "kept [x]: y data:\n"),
    # cut mid-target: the rest of the file is the payload (RFC G1b), the alt survives
    ("title-never-closed", f'Plan ![Hamburg talk]({_U} "Acme Hamburg) AAAA more', "Plan Hamburg talk"),
    ("target-never-closed", f"Plan ![Hamburg talk]({_U} AAAA", "Plan Hamburg talk"),
]


@pytest.mark.parametrize("text,expected", [(t, e) for _, t, e in _TABLE], ids=[i for i, _, _ in _TABLE])
def test_the_stripper_on_its_edges(text, expected):
    assert drop_data_uri_images(text) == expected


@pytest.mark.parametrize("_id,text,_expected", _TABLE, ids=[i for i, _, _ in _TABLE])
def test_the_stripper_never_grows_the_text(_id, text, _expected):
    assert len(drop_data_uri_images(text)) <= len(text)

"""WorkspaceService — view and grep mark the text of inbox-folder files as outside content."""
from src.domain.workspace import VIEW_FULL_MAX_CHARS, WorkspaceCall
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"
P = "projects/a.md"
INBOX = "scratch/inbox/job/2026-10-20t06-00-00z.md"
NONCE = "abcdef0123456789"
OPEN = f'<external_content nonce="{NONCE}" ref="workspace:{INBOX}" name="{INBOX}">'
CLOSE = f'</external_content nonce="{NONCE}">'


def _svc():
    store = InMemoryWorkspaceStore()
    return WorkspaceService(store, nonce=lambda: NONCE), store


def _call():
    return WorkspaceCall(user_id=USER, origin="chat", fire_key=None, known={})


def _inside(text: str) -> str:
    assert text.count(OPEN) == 1 and text.count(CLOSE) == 1
    return text.split(OPEN, 1)[1].split(CLOSE, 1)[0]


async def test_view_of_an_inbox_file_wraps_its_text():
    svc, store = _svc()
    store.seed(USER, INBOX, "line one\nline two")
    r = await svc.run(_call(), "view", {"path": INBOX})
    assert not r.failed
    inside = _inside(r.text)
    assert "line one" in inside and "line two" in inside
    before = r.text.split(OPEN, 1)[0]
    assert "line one" not in before and "outside content" in before
    assert f"workspace:{INBOX}" in before


async def test_view_of_another_file_is_unchanged():
    svc, store = _svc()
    store.seed(USER, P, "line one\nline two")
    r = await svc.run(_call(), "view", {"path": P})
    assert "external_content" not in r.text and "outside content" not in r.text
    assert "line one" in r.text


async def test_view_window_of_an_inbox_file_is_wrapped():
    svc, store = _svc()
    store.seed(USER, INBOX, "\n".join(f"row {n}" for n in range(1, 11)))
    r = await svc.run(_call(), "view", {"path": INBOX, "offset": 2, "limit": 3})
    assert "row 2" in _inside(r.text) and "row 5" not in r.text


async def test_view_outline_of_a_long_inbox_file_is_wrapped():
    svc, store = _svc()
    store.seed(USER, INBOX, "# Heading one\n" + "x" * (VIEW_FULL_MAX_CHARS + 10))
    r = await svc.run(_call(), "view", {"path": INBOX})
    assert "Heading one" in _inside(r.text)
    assert "long file" in r.text


async def test_view_of_an_old_version_of_an_inbox_file_is_wrapped():
    svc, store = _svc()
    first = store.seed(USER, INBOX, "old text")
    store.seed(USER, INBOX, "new text")
    r = await svc.run(_call(), "view", {"path": INBOX, "generation": first.generation})
    assert "old text" in _inside(r.text)


async def test_grep_wraps_only_matches_from_inbox_files():
    svc, store = _svc()
    store.seed(USER, P, "needle in projects")
    store.seed(USER, INBOX, "needle in inbox\nnothing here")
    r = await svc.run(_call(), "grep", {"pattern": "needle", "path": ""})
    assert not r.failed
    assert "needle in inbox" in _inside(r.text)
    assert "needle in projects" not in _inside(r.text)
    outside_part = r.text.replace(_inside(r.text), "")
    assert "needle in projects" in outside_part
    assert INBOX in outside_part and "2 matches in 2 files" in outside_part


async def test_grep_without_inbox_files_is_unchanged():
    svc, store = _svc()
    store.seed(USER, P, "needle in projects")
    r = await svc.run(_call(), "grep", {"pattern": "needle", "path": ""})
    assert "external_content" not in r.text and "needle in projects" in r.text

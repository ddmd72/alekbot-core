"""WorkspaceService — insert and str_replace results echo the changed block (AGENT_WORKSPACE_RFC §7.3, D18)."""
from src.domain.workspace import WorkspaceCall
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"
P = "projects/garden-shed.md"
BASE = "survey booked\nplans drawn\nform submitted\n"


def _svc():
    store = InMemoryWorkspaceStore()
    return WorkspaceService(store), store


def _call(known=None):
    return WorkspaceCall(user_id=USER, origin="chat", known=dict(known or {}))


async def test_insert_result_echoes_the_block_with_context():
    svc, store = _svc()
    e = store.seed(USER, P, BASE)
    r = await svc.run(_call({P: e.generation}), "insert", {"path": P, "insert_line": 2, "insert_text": "letter sent"})
    now = store.versions_of(USER, P)[-1]
    assert r.text == (
        f"inserted 1 line at line 3 of workspace:{P} · generation {now.generation} · file now 4 lines · "
        f"{len(store.live_text(USER, P).encode())} B\n"
        "     2\tplans drawn\n     3\tletter sent\n     4\tform submitted"
    )


async def test_str_replace_result_echoes_the_block_with_context():
    svc, store = _svc()
    store.seed(USER, P, BASE)
    r = await svc.run(_call(), "str_replace", {"path": P, "old_str": "plans drawn", "new_str": "plans revised"})
    assert r.text.splitlines()[0].startswith(f"replaced in workspace:{P} · generation ")
    assert r.text.splitlines()[1:] == ["     1\tsurvey booked", "     2\tplans revised", "     3\tform submitted"]


async def test_str_replace_deletion_shows_the_lines_around_the_cut():
    svc, store = _svc()
    store.seed(USER, P, BASE)
    r = await svc.run(_call(), "str_replace", {"path": P, "old_str": "plans drawn\n", "new_str": ""})
    assert r.text.splitlines()[1:] == ["     1\tsurvey booked", "     2\tform submitted"]

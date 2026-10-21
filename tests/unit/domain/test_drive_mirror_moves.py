import random

import pytest

from src.domain.drive_mirror_names import Move, fold, order_moves, temp_name


def _mv(key, fp, fn, tp, tn):
    return Move(key, fp, fn, tp, tn)


def _simulate(start, ordered):
    """start: {key: (parent, name)}; asserts no move targets a held name and returns the end state."""
    state = dict(start)
    for m in ordered:
        held = {(p, fold(n)) for k, (p, n) in state.items() if k != m.key}
        assert (m.to_parent, fold(m.to_name)) not in held, f"{m} targets a held name"
        if not m.is_create:
            assert state[m.key] == (m.from_parent, m.from_name)
        state[m.key] = (m.to_parent, m.to_name)
    return state


def test_swap_broken_by_a_temporary_name():
    out = order_moves([_mv("a", "P", "x", "P", "y"), _mv("b", "P", "y", "P", "x")])
    assert len(out) == 3
    assert any(m.to_name.startswith("x~tmp") or m.to_name.startswith("y~tmp") for m in out)
    end = _simulate({"a": ("P", "x"), "b": ("P", "y")}, out)
    assert end == {"a": ("P", "y"), "b": ("P", "x")}


def test_three_cycle():
    moves = [_mv("a", "P", "1", "P", "2"), _mv("b", "P", "2", "P", "3"), _mv("c", "P", "3", "P", "1")]
    out = order_moves(moves)
    assert len(out) == 4
    end = _simulate({"a": ("P", "1"), "b": ("P", "2"), "c": ("P", "3")}, out)
    assert end == {"a": ("P", "2"), "b": ("P", "3"), "c": ("P", "1")}


def test_case_only_rename_is_one_move():
    m = _mv("a", "P", "readme.md", "P", "README.md")
    assert order_moves([m]) == [m]


def test_noop_is_dropped():
    assert order_moves([_mv("a", "P", "x", "P", "x")]) == []


def test_vacate_first_order():
    out = order_moves([_mv("b", "P", "y", "P", "z"), _mv("a", "P", "x", "P", "y")][::-1])
    assert [m.key for m in out] == ["b", "a"]
    out2 = order_moves([_mv("a", "P", "x", "P", "y"), _mv("b", "P", "y", "P", "z")])
    assert [m.key for m in out2] == ["b", "a"]


def test_yield_before_create():
    create = _mv("folder", None, None, "P", "X.md")
    yield_ = _mv("file", "P", "X.md", "P", "X~1a2b3c4d.md")
    out = order_moves([create, yield_])
    assert [m.key for m in out] == ["file", "folder"]
    assert out[1].is_create


def test_temp_name_shape():
    t = temp_name("n.md", "k")
    assert t.startswith("n.md~tmp") and len(t) == len("n.md~tmp") + 8


def _case(seed):
    """Documents that stay put hold slots; movers and creations draw injective targets from every other slot, so
    swaps, cycles, vacate-first chains into free names and creations waiting for a yield all occur."""
    rng = random.Random(seed)
    slots = [(p, n) for p in ("P", "Q") for n in ("a", "B", "c", "D", "e", "f")]
    rng.shuffle(slots)
    n_stay, n_move, n_create = rng.randint(0, 3), rng.randint(2, 6), rng.randint(0, 3)
    held = slots[:n_stay + n_move]                       # stayers first, then the movers' start slots
    stayers, starts = held[:n_stay], held[n_stay:]
    pool = [s for s in slots if s not in stayers]       # a target is never a slot a stayer holds
    targets = rng.sample(pool, min(len(pool), n_move + n_create))
    start = {f"k{i}": s for i, s in enumerate(starts)}
    moves = []
    for i, d in enumerate(targets[:n_move]):
        if rng.random() < 0.15:
            d = (d[0], d[1].swapcase())                  # a case-only variant of the target
        moves.append(Move(f"k{i}", starts[i][0], starts[i][1], d[0], d[1]))
    for j, d in enumerate(targets[n_move:]):
        moves.append(Move(f"c{j}", None, None, d[0], d[1]))
    rng.shuffle(moves)
    for i, s in enumerate(stayers):
        start[f"s{i}"] = s
    return start, moves


@pytest.mark.parametrize("seed", range(300))
def test_no_move_targets_a_name_held_at_its_step(seed):
    start, moves = _case(seed)
    ordered = order_moves(moves)
    end = _simulate(start, ordered)
    for m in moves:
        assert end[m.key] == (m.to_parent, m.to_name)
    for k, s in start.items():
        if k.startswith("s"):
            assert end[k] == s


def test_move_origin_is_both_or_neither():
    with pytest.raises(ValueError):
        Move("k", "P", None, "P", "x")
    with pytest.raises(ValueError):
        Move("k", None, "n", "P", "x")

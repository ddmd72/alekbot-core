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
    rng = random.Random(seed)
    parents = ["P", "Q"]
    names = ["a", "B", "c", "D", "e"]
    slots = [(p, n) for p in parents for n in names]
    rng.shuffle(slots)
    n = rng.randint(2, 9)
    src, dst = slots[:n], list(slots[:n])
    rng.shuffle(dst)                                   # permutation of occupied slots: swaps, cycles, chains
    extra = [s for s in slots[n:] if rng.random() < 0.5]
    start, moves = {}, []
    for i, (s, d) in enumerate(zip(src, dst)):
        if rng.random() < 0.15:
            d = (d[0], d[1].swapcase())                # a case-only variant of the target
        start[f"k{i}"] = s
        moves.append(Move(f"k{i}", s[0], s[1], d[0], d[1]))
    for j, s in enumerate(extra[:2]):                  # creations into free or vacated names
        moves.append(Move(f"c{j}", None, None, s[0], s[1]))
    rng.shuffle(moves)
    return start, moves


@pytest.mark.parametrize("seed", range(200))
def test_no_move_targets_a_name_held_at_its_step(seed):
    start, moves = _case(seed)
    targets = [(m.to_parent, fold(m.to_name)) for m in moves]
    if len(set(targets)) != len(targets):
        pytest.skip("generated targets collide; assign_names guarantees distinct targets")
    ordered = order_moves(moves)
    end = _simulate(start, ordered)
    for m in moves:
        assert end[m.key] == (m.to_parent, m.to_name)

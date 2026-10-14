"""Planner + tick properties over random drives (USER_DRIVE_INDEXING_RFC §17.1; RFC Δ8: seeded random).

Each seed builds a random tree, interleaves random mutations with ticks (random page sizes, a resync,
re-emissions, `.git` arriving late or leaving), settles, and checks the index against an oracle that
derives the expected records from the final tree without the planner.
"""
import random

import pytest

from src.domain.drive_card import card_eligible, card_hash
from src.domain.drive_index import RecordState, Scope

from .drive_index_fakes import ROOT
from .test_drive_indexing_service import World

SEEDS = range(200)
MARKERS = (".git", "pyvenv.cfg")


def oracle(drive):
    """item_id -> (scope, carded) for every item that must have a record; files inside opaque roots have none."""
    kids = {}
    for c in drive.items.values():
        kids.setdefault(c.parent_id, []).append(c)
    expected = {}

    def walk(parent_id, inherited):
        for c in kids.get(parent_id, []):
            if inherited is not Scope.NORMAL:
                if c.is_folder:
                    expected[c.item_id] = (Scope.INSIDE_OPAQUE, False)
                    walk(c.item_id, Scope.INSIDE_OPAQUE)
                elif c.name in MARKERS:
                    expected[c.item_id] = (Scope.INSIDE_OPAQUE, False)   # kept to notice it leaving
                continue
            if c.is_folder:
                opaque = c.name.startswith(".") or any(k.name in MARKERS for k in kids.get(c.item_id, []))
                scope = Scope.OPAQUE_ROOT if opaque else Scope.NORMAL
                expected[c.item_id] = (scope, True)
                walk(c.item_id, Scope.INSIDE_OPAQUE if opaque else Scope.NORMAL)
            else:
                expected[c.item_id] = (Scope.NORMAL, True)

    walk(ROOT, Scope.NORMAL)
    return expected


def random_ops(rng, w, n_ops):
    folders = [ROOT]
    names = ["Договор", "notes", "IMG_4912", "Scan", "budget", "plan", "src", "lib", "data", "Фото"]
    for _ in range(n_ops):
        op = rng.random()
        live = [i for i in w.drive.items]
        if op < 0.35 or not live:
            parent = rng.choice(folders)
            if rng.random() < 0.3:
                folders.append(w.drive.add(f"{rng.choice(names)}{rng.randint(0, 99)}", parent=parent, folder=True))
            else:
                w.drive.add(f"{rng.choice(names)}{rng.randint(0, 99)}.{rng.choice(['pdf', 'py', 'jpg', 'md'])}",
                            parent=parent)
        elif op < 0.45:
            parent = rng.choice(folders[1:] or folders)
            w.drive.add(rng.choice(MARKERS), parent=parent, folder=rng.random() < 0.5)
        elif op < 0.55:
            item = rng.choice(live)
            w.drive.rename(item, rng.choice([".hidden", "renamed", "Новая папка", "x.pdf"]))
        elif op < 0.65:
            item = rng.choice(live)
            target = rng.choice(folders)
            if target != item and not _is_descendant(w.drive, target, item):
                w.drive.move(item, target)
        elif op < 0.75:
            item = rng.choice(live)
            w.drive.delete(item)
            folders = [f for f in folders if f == ROOT or f in w.drive.items]
        elif op < 0.85:
            w.drive.reemit(rng.choice(live))
        elif op < 0.9:
            w.drive.resync_next = True
        else:
            yield "tick"


def _is_descendant(drive, item_id, ancestor):
    seen = set()
    while item_id in drive.items and item_id not in seen:
        if item_id == ancestor:
            return True
        seen.add(item_id)
        item_id = drive.items[item_id].parent_id
    return item_id == ancestor


@pytest.mark.parametrize("seed", SEEDS)
async def test_random_drive_converges_to_the_oracle(seed):
    rng = random.Random(seed)
    w = World(page_size=rng.choice([1, 3, 7, 50]))
    for step in random_ops(rng, w, n_ops=40):
        if step == "tick":
            await w.tick()
            w.clock.advance(1)
    await w.settle_due(max_ticks=200)   # an image without a rendition converges after its retries (review B minor)

    expected = oracle(w.drive)
    stored = {i: r for (u, i), r in w.repo.records.items()}
    assert set(stored) == set(expected), f"seed {seed}"
    for item_id, (scope, carded) in expected.items():
        rec = stored[item_id]
        assert rec.scope is scope, (seed, rec.name, rec.scope, scope)
        assert rec.state is RecordState.METADATA_ONLY, (seed, rec.name, rec.state)   # (a) terminal
        assert rec.due_at is None, (seed, rec.name)                                   # (b) convergence
        assert (item_id in {k[1] for k in w.repo.vectors}) is carded, (seed, rec.name)
        assert rec.name == w.drive.items[item_id].name and rec.parent_id == w.drive.items[item_id].parent_id
        if carded:
            assert card_eligible(rec) and rec.card_hash == card_hash(rec)


@pytest.mark.parametrize("seed", range(40))
async def test_replaying_the_feed_changes_nothing(seed):
    rng = random.Random(seed)
    w = World(page_size=rng.choice([2, 50]))
    for _ in random_ops(rng, w, n_ops=25):
        pass
    await w.settle(max_ticks=200)
    before = dict(w.repo.records)
    for item_id in list(w.drive.items):
        w.drive.reemit(item_id)
    await w.settle(max_ticks=200)
    assert {k: (r.name, r.scope, r.state, r.card_hash) for k, r in w.repo.records.items()} == \
           {k: (r.name, r.scope, r.state, r.card_hash) for k, r in before.items()}


@pytest.mark.parametrize("seed", range(20))
async def test_disabled_user_never_gets_a_record(seed):
    rng = random.Random(seed)
    w = World()
    w.users.enabled["u"] = False
    for step in random_ops(rng, w, n_ops=30):
        if step == "tick":
            await w.tick()
    await w.tick()
    assert w.repo.records == {} and w.repo.states == {}

"""Text-quality gate (USER_DRIVE_INDEXING_RFC §8.1 step 6, §5.5 class 3, §17.2). Pure; seeded generators (RFC Δ8)."""
import json
import random

import pytest

from src.domain.drive_text_gate import MIN_RESIDUE_LINES, is_machine_text, measure

SEEDS = range(50)
_SYLLABLES = ("ka", "to", "ri", "men", "sa", "lo", "pre", "di", "vo", "na", "tur", "el", "os", "ve", "ra", "po")
_CYRILLIC = ("ка", "то", "ри", "мен", "са", "ло", "пре", "ді", "во", "на", "тур", "ель", "ос", "ве", "ра", "по")


class Lexicon:
    """A Zipf-weighted pseudo-vocabulary: prose-like token variety without a corpus."""

    def __init__(self, rng, syllables=_SYLLABLES, size=3000):
        self.rng = rng
        self.words = ["".join(rng.choice(syllables) for _ in range(rng.randint(1, 4))) for _ in range(size)]
        self.weights = [1 / (i + 1) for i in range(size)]

    def sentence(self, n=None):
        words = self.rng.choices(self.words, self.weights, k=n or self.rng.randint(6, 18))
        return " ".join(words).capitalize() + self.rng.choice([".", "!", "?"])


def stamp(rng):
    return f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d} {rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:" \
           f"{rng.randint(0, 59):02d},{rng.randint(0, 999):03d}"


# -- what must stay indexed (§17.2) ------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", SEEDS)
def test_prose_passes(seed):
    rng = random.Random(seed)
    lex = Lexicon(rng, _CYRILLIC if seed % 2 else _SYLLABLES)
    text = "\n\n".join(" ".join(lex.sentence() for _ in range(rng.randint(1, 6))) for _ in range(rng.randint(20, 80)))
    assert not is_machine_text(text)


@pytest.mark.parametrize("seed", SEEDS)
def test_meeting_transcript_passes(seed):
    rng = random.Random(seed)
    lex = Lexicon(rng)
    speakers = ["Clara", "Jamie", "Dr. Quillen", "Ганна"]
    lines = []
    for i in range(rng.randint(60, 400)):
        said = lex.sentence() if rng.random() > 0.25 else rng.choice(["ok", "yes", "right", "mm-hm", "agreed"])
        form = rng.random()
        if form < 0.4:
            lines.append(f"[00:{i // 60:02d}:{i % 60:02d}] {rng.choice(speakers)}: {said}")
        elif form < 0.7:
            lines.append(f"<v {rng.choice(speakers)}>{said}")
        else:
            lines.append(f"{rng.choice(speakers)}: {said}")
    assert not is_machine_text("\n".join(lines))


def test_raw_vtt_cues_pass():
    rng = random.Random(1)
    lex = Lexicon(rng)
    cues = [f"{i}\n00:{i // 60:02d}:{i % 60:02d}.000 --> 00:{i // 60:02d}:{i % 60:02d}.900\n{lex.sentence()}\n"
            for i in range(1, 200)]
    assert not is_machine_text("WEBVTT\n\n" + "\n".join(cues))


@pytest.mark.parametrize("seed", SEEDS)
def test_chat_export_passes(seed):
    rng = random.Random(seed)
    lex = Lexicon(rng)
    lines = [f"[{rng.randint(1, 28):02d}.10.26, {rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:11] "
             f"{rng.choice(['Jamie', 'Мама', 'Ana'])}: {lex.sentence()}" for _ in range(rng.randint(50, 300))]
    assert not is_machine_text("\n".join(lines))


@pytest.mark.parametrize("seed", SEEDS)
def test_dated_journal_passes(seed):
    rng = random.Random(seed)
    lex = Lexicon(rng)
    lines = [f"{rng.randint(1, 28)}.{rng.randint(1, 12)}.2026 — {lex.sentence()} {lex.sentence()}"
             for _ in range(rng.randint(30, 200))]
    assert not is_machine_text("\n".join(lines))


def test_tables_are_exempt():
    rows = "\n".join(f"| 2026-10-{i % 28 + 1:02d} | GET /api/items | 200 |" for i in range(2000))
    assert not is_machine_text("| date | request | status |\n|---|---|---|\n" + rows)


def test_too_little_residue_is_never_judged():
    text = "\n".join(["2026-10-01 GET /x 200"] * (MIN_RESIDUE_LINES - 1))
    assert not is_machine_text(text)


def test_timestamp_only_lines_are_ignored():
    rng = random.Random(2)
    lex = Lexicon(rng)
    stamps = "\n".join(stamp(rng) for _ in range(2000))
    numbers = "\n".join(str(rng.randint(0, 10 ** 6)) for _ in range(2000))
    prose = "\n".join(lex.sentence() for _ in range(40))
    m = measure(stamps + "\n" + numbers + "\n" + prose)
    assert m.residue_lines == 40 and not is_machine_text(stamps + "\n" + numbers + "\n" + prose)


# -- what is machine text (§5.5 class 3) -------------------------------------------------------------------------

@pytest.mark.parametrize("seed", SEEDS)
def test_server_log_is_machine_text(seed):
    rng = random.Random(seed)
    lines = [f"{stamp(rng)} {rng.choice(['INFO', 'WARN', 'DEBUG'])} [worker-{rng.randint(1, 8)}] "
             f"{rng.choice(['GET', 'POST'])} /api/items/{rng.randint(1, 99999)} {rng.choice([200, 404, 500])} "
             f"{rng.randint(1, 900)}ms" for _ in range(rng.randint(40, 600))]
    assert is_machine_text("\n".join(lines))


@pytest.mark.parametrize("seed", SEEDS)
def test_access_log_is_machine_text(seed):
    rng = random.Random(seed)
    lines = [f'10.0.{rng.randint(0, 255)}.{rng.randint(0, 255)} - - [01/Oct/2026:12:{rng.randint(0, 59):02d}:00 +0000] '
             f'"GET /static/{rng.choice("abc")}.js HTTP/1.1" 200 {rng.randint(100, 9000)}' for _ in range(300)]
    assert is_machine_text("\n".join(lines))


def test_request_ids_do_not_hide_repetition():
    rng = random.Random(4)
    lines = [f"{rng.random():.6f} req={rng.getrandbits(64):016x} user=u{rng.randint(1, 30)} action=view status=ok"
             for _ in range(300)]
    assert is_machine_text("\n".join(lines))


def test_sql_dump_is_machine_text():
    text = "\n".join(f"INSERT INTO users (id, name, email) VALUES ({i}, 'user{i}', 'user{i}@example.com');"
                     for i in range(400))
    assert is_machine_text(text)


def test_bracket_dump_is_machine_text():
    text = json.dumps([{"a": i, "b": [i * 2, {"c": i * 3}]} for i in range(300)], indent=1)
    assert measure(text).near_empty > 0.6 and is_machine_text(text)


@pytest.mark.slow
def test_measure_is_deterministic():
    rng = random.Random(9)
    text = "\n".join(Lexicon(rng).sentence() for _ in range(100))
    assert measure(text) == measure(text)

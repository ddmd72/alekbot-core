"""Agent workspace review logic (AGENT_WORKSPACE_RFC §16) on synthetic data: no network."""
import json

from scripts.validation.workspace_review_logic import (
    build_traces,
    classify_trigger,
    parse_line,
    render_report,
    review,
)

F = "reminder:1729401234567@2026-10-20T06:00:00+00:00"
P = "projects/morning-briefing.md"
ALERT = ('[System: [SELF-REMINDER] "Morning briefing"\nnote_id: 1729401234567\nSchedule: x\n'
         f"Workspace file: workspace:{P}\n\nMorning briefing: follow the workspace file.]")
SAME_FIRE = ("(this version was written by an earlier attempt of this same fire; its result may not have reached the "
             "user.\nThe version before this fire is generation 101.)")


def _line(op, path=P, origin="background:execute_reminder", fire=F, had_history="-", result="ok", nbytes=10,
          ts="2026-10-21T06:01:00Z", user="u1"):
    logged_fire = fire.removeprefix("reminder:") if fire else "-"  # the §16 log form
    return parse_line(ts, f"[Workspace] op={op} user={user} path={path} origin={origin} fire={logged_fire} "
                          f"had_history={had_history} bytes={nbytes} gen=101 result={result} ms=12")


def _row(trace, turn, text, calls=None, ts="2026-10-21T06:00:00Z"):
    return {"trace_id": trace, "timestamp": ts, "turn": turn, "request_text": text,
            "tool_calls": json.dumps(calls) if calls else None}


def _ws(command, **args):
    return {"name": "workspace", "args": {"command": command, "path": P, **args}}


def test_parse_line():
    line = _line("view")
    assert (line.op, line.user, line.path, line.fire, line.had_history, line.result, line.nbytes) == (
        "view", "u1", P, F, None, "ok", 10)
    created = _line("create", fire=None, had_history="1", origin="chat")
    assert created.fire is None and created.had_history is True
    assert parse_line("t", "[Worker] something else") is None


def test_classify_triggers():
    [reminder] = build_traces([_row("t1", 0, ALERT)])
    [skill] = build_traces([
        _row("t2", 0, "practise Spanish", [{"name": "use_skill", "args": {"name": "spanish-tutor"}}]),
        _row("t2", 1, '[Skill "spanish-tutor" v1]\nAt the start, view `workspace:projects/spanish-tutor.md`.',
             [_ws("view")]),
    ])
    [user] = build_traces([_row("t3", 0, "keep a file for the flat sale", [_ws("create")])])
    assert [classify_trigger(t) for t in (reminder, skill, user)] == ["reminder", "skill", "user or instruction"]


def test_view_rate_and_calls_per_trigger():
    traces = build_traces([_row("t1", 0, ALERT, [_ws("view")]), _row("t2", 0, ALERT)])
    r = review([], traces)
    assert (r.reminder_runs_with_file, r.reminder_runs_viewed, r.view_rate) == (2, 1, 0.5)
    assert r.calls_by_trigger == {"reminder": 1}


def test_recreated_without_restore():
    lines = [
        _line("create", had_history="1"),
        _line("create", path="projects/b.md", had_history="1", origin="chat"),
        _line("create", path="projects/c.md", had_history="1"),
        _line("restore", path="projects/c.md"),
    ]
    assert [line.path for line in review(lines, []).recreated_without_restore] == [P]


def test_same_fire_reruns_that_ignored_the_earlier_version():
    ignored = [_row("b", 0, ALERT, [_ws("view")]), _row("b", 1, SAME_FIRE, [_ws("str_replace")])]
    worked_from_it = [_row("c", 0, ALERT, [_ws("view")]),
                      _row("c", 1, SAME_FIRE, [_ws("view", generation="101"), _ws("create")])]
    assert review([], build_traces(ignored + worked_from_it)).same_fire_ignored == ["b"]


def test_file_growth():
    lines = [_line("create", nbytes=100, ts="2026-10-21T06:01:00Z"),
             _line("str_replace", nbytes=150, ts="2026-10-22T06:01:00Z"),
             _line("str_replace", nbytes=220, ts="2026-10-23T06:01:00Z"),
             _line("create", path="projects/flat.md", nbytes=300, origin="chat", fire=None),
             _line("str_replace", path="projects/flat.md", nbytes=250, origin="chat", fire=None),
             _line("view", nbytes=220)]
    growth = {g.path: g for g in review(lines, []).growth}
    assert (growth[P].writes, growth[P].first, growth[P].last, growth[P].only_grows) == (3, 100, 220, True)
    assert growth["projects/flat.md"].only_grows is False


def test_conflicts_and_origins():
    lines = [_line("str_replace", result="conflict"), _line("view", origin="background:daily_email_review", fire=None)]
    r = review(lines, [])
    assert len(r.conflicts) == 1
    assert r.lines_by_origin == {"background:execute_reminder": 1, "background:daily_email_review": 1}


def test_report():
    text = render_report(review([], []), "2026-09-23")
    assert text.startswith("# Agent workspace review (since 2026-09-23)")
    assert "n/a" in text and "manual" in text and "≥ 90 %" in text and "## File growth" in text

"""scripts/pii_gate/semantic_check.py against a fake Messages API on loopback.

The profile, key and diff lines are invented; the fake server scripts the model's verdicts.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.pii_gate import semantic_check

API_KEY = "sk-test-not-a-real-key-0001"
PROFILE = "Invented owner: sister Quilla Moss, blood marker XQ-7, lives on Larkspur Lane."
LEAKY_LINE = "# the owner's sister Quilla has marker XQ-7"


def diff_of(files: dict[str, list[str]]) -> str:
    out = []
    for path, lines in files.items():
        out.append(f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
                   f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{x}\n" for x in lines))
    return "".join(out)


def reply(findings) -> dict:
    return {"type": "message", "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": json.dumps({"findings": findings})}]}


class FakeMessagesApi:
    """Records each request; answers from a script of (status, body) and repeats the last entry."""

    def __init__(self):
        self.requests: list[dict] = []
        self.script: list[tuple[int, object]] = [(200, reply([]))]
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["content-length"]))
                api.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": json.loads(raw)})
                status, body = api.script[min(len(api.requests) - 1, len(api.script) - 1)]
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/messages"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "PII_GATE_MODEL", "PII_GATE_API_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    monkeypatch.setenv("PII_PROFILE", PROFILE)
    monkeypatch.setattr(semantic_check, "RETRY_DELAY_S", 0)
    with FakeMessagesApi() as fake:
        yield fake


def run(api, capsys, tmp_path, files: dict[str, list[str]]):
    path = tmp_path / "change.diff"
    path.write_text(diff_of(files))
    code = semantic_check.main(["--diff-file", str(path), "--api-url", api.url])
    out = capsys.readouterr()
    return code, out.out, out.err


class TestRequest:
    def test_the_request_carries_model_key_profile_and_a_nonce_wrapped_diff(self, api, capsys, tmp_path):
        code, _, _ = run(api, capsys, tmp_path, {"docs/a.md": ["hello", LEAKY_LINE]})
        assert code == 0
        (req,) = api.requests
        assert req["path"] == "/v1/messages"
        assert req["headers"]["x-api-key"] == API_KEY
        assert req["headers"]["anthropic-version"] == semantic_check.ANTHROPIC_VERSION
        body = req["body"]
        assert body["model"] == "claude-haiku-5-5"
        assert PROFILE in body["system"]
        user = body["messages"][0]["content"]
        nonce = re.search(r"<diff-([0-9a-f]{32})>", user).group(1)
        assert f"</diff-{nonce}>" in user
        assert f"<diff-{nonce}>" in body["system"] and "untrusted" in body["system"]
        inner = user.split(f"<diff-{nonce}>")[1].split(f"</diff-{nonce}>")[0]
        assert '<file path="docs/a.md">' in inner and f"2: {LEAKY_LINE}" in inner
        assert body["output_config"]["format"]["type"] == "json_schema"

    def test_the_model_comes_from_the_environment(self, api, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("PII_GATE_MODEL", "claude-test-model")
        run(api, capsys, tmp_path, {"a.md": ["hello"]})
        assert api.requests[0]["body"]["model"] == "claude-test-model"

    def test_each_call_gets_a_fresh_nonce(self, api, capsys, tmp_path):
        run(api, capsys, tmp_path, {"a.md": ["hello"]})
        run(api, capsys, tmp_path, {"a.md": ["hello"]})
        nonces = {re.search(r"<diff-([0-9a-f]+)>", r["body"]["messages"][0]["content"]).group(1) for r in api.requests}
        assert len(nonces) == 2

    def test_the_output_never_holds_the_key_the_profile_or_a_diff_line(self, api, capsys, tmp_path):
        api.script = [(200, reply([{"file": "docs/a.md", "line": 2, "category": "family",
                                    "reason": "names a relative of the owner"}]))]
        code, out, err = run(api, capsys, tmp_path, {"docs/a.md": ["hello", LEAKY_LINE]})
        assert code == 1
        everything = out + err
        for secret in (API_KEY, PROFILE, "Quilla", "XQ-7", LEAKY_LINE, "Larkspur"):
            assert secret not in everything


class TestVerdict:
    def test_no_findings_pass(self, api, capsys, tmp_path):
        code, out, _ = run(api, capsys, tmp_path, {"a.md": ["an ordinary line"]})
        assert code == 0 and "pass" in out

    def test_a_finding_blocks_and_prints_file_line_category_and_reason(self, api, capsys, tmp_path):
        api.script = [(200, reply([{"file": "docs/a.md", "line": 2, "category": "health",
                                    "reason": "states a medical marker of a real person"}]))]
        code, out, err = run(api, capsys, tmp_path, {"docs/a.md": ["hello", LEAKY_LINE]})
        assert code == 1
        assert "docs/a.md:2 health - states a medical marker of a real person" in out.splitlines()
        assert "BLOCKED" in err

    def test_a_bare_json_list_in_a_code_fence_is_accepted(self, api, capsys, tmp_path):
        text = '```json\n[{"file": "a.md", "line": 1, "category": "contact", "reason": "a real phone number"}]\n```'
        api.script = [(200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]})]
        code, out, _ = run(api, capsys, tmp_path, {"a.md": ["call me"]})
        assert code == 1 and "a.md:1 contact - a real phone number" in out

    def test_a_reason_that_quotes_the_change_is_withheld(self, api, capsys, tmp_path):
        api.script = [(200, reply([{"file": "docs/a.md", "line": 2, "category": "family",
                                    "reason": "says sister Quilla has marker XQ-7"}]))]
        code, out, _ = run(api, capsys, tmp_path, {"docs/a.md": ["hello", LEAKY_LINE]})
        assert code == 1
        assert "docs/a.md:2 family - (reason withheld: it quoted the change)" in out.splitlines()

    def test_an_unknown_category_or_file_is_not_echoed(self, api, capsys, tmp_path):
        api.script = [(200, reply([{"file": "Quilla Moss notes", "line": 1, "category": "Quilla",
                                    "reason": "names a relative"}]))]
        code, out, _ = run(api, capsys, tmp_path, {"a.md": ["hello"]})
        assert code == 1
        assert "Quilla" not in out
        assert "(a file not in this chunk):1 other - names a relative" in out.splitlines()


class TestFailClosed:
    def test_an_api_error_is_retried_once_then_fails_closed(self, api, capsys, tmp_path):
        api.script = [(500, {"type": "error", "error": {"type": "api_error"}})]
        code, _, err = run(api, capsys, tmp_path, {"a.md": ["hello"]})
        assert code == 2
        assert len(api.requests) == 2
        assert "HTTP 500 (api_error)" in err and "fail closed" in err

    def test_one_failure_then_a_verdict_passes(self, api, capsys, tmp_path):
        api.script = [(529, {"type": "error", "error": {"type": "overloaded_error"}}), (200, reply([]))]
        code, _, _ = run(api, capsys, tmp_path, {"a.md": ["hello"]})
        assert code == 0 and len(api.requests) == 2

    @pytest.mark.parametrize("body", [
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "I found nothing, all good!"}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"findings": [{"file": "a.md"}]}'}]},
        {"stop_reason": "refusal", "content": []},
        "not json at all",
    ])
    def test_a_reply_that_is_not_the_verdict_is_retried_once_then_fails_closed(self, api, capsys, tmp_path, body):
        api.script = [(200, body)]
        code, _, _ = run(api, capsys, tmp_path, {"a.md": ["hello"]})
        assert code == 2 and len(api.requests) == 2

    def test_an_unreachable_api_fails_closed(self, api, capsys, tmp_path):
        path = tmp_path / "change.diff"
        path.write_text(diff_of({"a.md": ["hello"]}))
        code = semantic_check.main(["--diff-file", str(path), "--api-url", "http://127.0.0.1:9/v1/messages"])
        assert code == 2 and "unreachable" in capsys.readouterr().err

    @pytest.mark.parametrize("missing", ["PII_PROFILE", "ANTHROPIC_API_KEY"])
    def test_a_missing_input_fails_closed_without_a_call(self, api, capsys, tmp_path, monkeypatch, missing):
        monkeypatch.delenv(missing)
        code, _, err = run(api, capsys, tmp_path, {"a.md": ["hello"]})
        assert code == 2 and missing in err and api.requests == []


class TestChunksAndSkips:
    def test_a_large_change_is_split_into_bounded_chunks_that_cover_every_line_once(self, api, capsys, tmp_path,
                                                                                  monkeypatch):
        monkeypatch.setattr(semantic_check, "CHUNK_BYTES", 600)
        files = {f"f{n}.md": [f"line {n}-{i} " + "x" * 40 for i in range(6)] for n in range(5)}
        code, out, _ = run(api, capsys, tmp_path, files)
        assert code == 0
        assert len(api.requests) > 1
        seen = []
        for req in api.requests:
            content = req["body"]["messages"][0]["content"]
            assert len(render_of(content).encode()) <= 600
            seen += re.findall(r"line \d+-\d+", content)
        assert sorted(seen) == sorted(f"line {n}-{i}" for n in range(5) for i in range(6))
        assert f"chunk {len(api.requests)}/{len(api.requests)}" in out

    def test_a_line_longer_than_a_chunk_is_cut_not_dropped(self, api, capsys, tmp_path, monkeypatch):
        monkeypatch.setattr(semantic_check, "CHUNK_BYTES", 400)
        code, _, _ = run(api, capsys, tmp_path, {"min.js": ["y" * 1000]})
        assert code == 0
        sent = "".join("".join(re.findall(r"1: (y+)", r["body"]["messages"][0]["content"])) for r in api.requests)
        assert sent == "y" * 1000

    def test_lockfiles_the_gazetteer_and_binary_files_are_not_sent(self, api, capsys, tmp_path):
        path = tmp_path / "change.diff"
        path.write_text(diff_of({"docx_generator/package-lock.json": ['"lodash": "4"'],
                                 "src/assets/geo/places.tsv": ["Larkspur Lane"],
                                 "src/real.py": ["x = 1"]})
                        + "diff --git a/i.png b/i.png\nnew file mode 100644\nBinary files /dev/null and b/i.png differ\n")
        code = semantic_check.main(["--diff-file", str(path), "--api-url", api.url])
        assert code == 0
        (req,) = api.requests
        content = req["body"]["messages"][0]["content"]
        assert "src/real.py" in content
        for skipped in ("package-lock", "lodash", "src/assets", "Larkspur", "i.png"):
            assert skipped not in content

    def test_an_empty_or_skipped_only_change_passes_without_a_call(self, api, capsys, tmp_path):
        code, out, _ = run(api, capsys, tmp_path, {"yarn.lock": ["a@1"], "b.md": ["   "]})
        assert code == 0 and api.requests == [] and "no added lines" in out


def render_of(content: str) -> str:
    return re.search(r"<diff-[0-9a-f]+>\n(.*)\n</diff-[0-9a-f]+>", content, re.S).group(1)

"""main.py's `/worker` route awaits its OIDC gate before anything else and returns the gate's refusal unchanged (P7).

`/worker` lives inside `main()` and cannot be built in a test (inventory F3), so this is a static check of its shape;
the gate's own behaviour (no token, wrong service account, unverified email, empty identity set on Cloud Run) is
tested in `tests/unit/web/test_worker_oidc_verifier.py` and `tests/unit/web/test_oidc_identity_set.py`. Weaker than a
route test by design; it goes when `/worker` moves into a blueprint factory the anonymous-route probe can build.
"""
import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "main.py"


def _main_tree() -> ast.Module:
    return ast.parse(MAIN.read_text())


def _worker_handlers(tree: ast.Module) -> list:
    """Every function decorated `@<app>.route("/worker", ...)`."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "route"
                        and dec.args and isinstance(dec.args[0], ast.Constant) and dec.args[0].value == "/worker"):
                    found.append(node)
    return found


def test_there_is_exactly_one_worker_route_and_it_is_async():
    handlers = _worker_handlers(_main_tree())
    assert len(handlers) == 1
    assert isinstance(handlers[0], ast.AsyncFunctionDef)


def test_the_gate_is_the_first_thing_the_worker_route_does():
    [handler] = _worker_handlers(_main_tree())
    body = [s for s in handler.body if not isinstance(s, (ast.Import, ast.ImportFrom))]
    first = body[0]
    # if not await _worker_oidc_gate(request.headers.get("Authorization")):
    assert isinstance(first, ast.If) and not first.orelse
    test = first.test
    assert isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    assert isinstance(test.operand, ast.Await)
    call = test.operand.value
    assert isinstance(call, ast.Call) and ast.unparse(call.func) == "_worker_oidc_gate"
    assert [ast.unparse(a) for a in call.args] == ["request.headers.get('Authorization')"]


def test_the_refusal_is_returned_as_is_and_nothing_runs_before_it():
    [handler] = _worker_handlers(_main_tree())
    first = [s for s in handler.body if not isinstance(s, (ast.Import, ast.ImportFrom))][0]
    *before, last = first.body
    assert isinstance(last, ast.Return)
    assert ast.unparse(last.value) == "(jsonify({'error': 'unauthorized'}), 401)"
    # Only a log line may precede the return: no await, no other call.
    for stmt in before:
        assert isinstance(stmt, ast.Expr) and ast.unparse(stmt.value.func).startswith("logger.")
        assert not any(isinstance(n, ast.Await) for n in ast.walk(stmt))


def test_the_gate_is_built_fail_closed_on_cloud_run():
    tree = _main_tree()
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "_worker_oidc_gate" for t in n.targets)]
    assert len(assigns) == 1
    call = assigns[0].value
    assert isinstance(call, ast.Call) and ast.unparse(call.func) == "build_oidc_gate"
    keywords = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert keywords["on_cloud_run"] == "bool(os.getenv('K_SERVICE'))"
    assert "SERVICE_ACCOUNT_EMAIL" in ast.unparse(call.args[0])

"""The main service's `/worker` route awaits its OIDC gate before anything else and returns the gate's refusal unchanged (P7).

The route is served by `create_main_service_blueprint` (`src/web/main_service_routes.py`), the gate injected by
`main()`: `_worker_oidc_gate`, built fail-closed on Cloud Run. The route's behaviour is tested on a bare app in
`tests/unit/web/test_main_service_routes.py` and by the anonymous-route probe; this static check keeps what a request
cannot show: the route is async, the gate is its first statement, only a log line precedes the refusal, and the gate
`main()` injects is the fail-closed one. The gate's own behaviour (no token, wrong service account, unverified email,
empty identity set on Cloud Run) is tested in `tests/unit/web/test_worker_oidc_verifier.py` and
`tests/unit/web/test_oidc_identity_set.py`.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "main.py"
# reviewer edit, tidy main.py: /worker moved out of main() into this blueprint factory
ROUTES = ROOT / "src" / "web" / "main_service_routes.py"


def _main_tree() -> ast.Module:
    return ast.parse(MAIN.read_text())


def _routes_tree() -> ast.Module:
    return ast.parse(ROUTES.read_text())


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
    # Counted over main.py and the routes module together: a second /worker back in main.py is still caught.
    assert _worker_handlers(_main_tree()) == []
    handlers = _worker_handlers(_routes_tree())
    assert len(handlers) == 1
    assert isinstance(handlers[0], ast.AsyncFunctionDef)


def _gate_parameter() -> str:
    """The factory's gate parameter, by position: the second argument of `create_main_service_blueprint`."""
    [factory] = [n for n in _routes_tree().body
                 if isinstance(n, ast.FunctionDef) and n.name == "create_main_service_blueprint"]
    return factory.args.args[1].arg


def test_the_gate_is_the_first_thing_the_worker_route_does():
    [handler] = _worker_handlers(_routes_tree())
    body = [s for s in handler.body if not isinstance(s, (ast.Import, ast.ImportFrom))]
    first = body[0]
    # if not await oidc_gate(request.headers.get("Authorization")):
    assert isinstance(first, ast.If) and not first.orelse
    test = first.test
    assert isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    assert isinstance(test.operand, ast.Await)
    call = test.operand.value
    assert isinstance(call, ast.Call) and ast.unparse(call.func) == _gate_parameter() == "oidc_gate"
    assert [ast.unparse(a) for a in call.args] == ["request.headers.get('Authorization')"]
    # ...and the gate main() injects there is `_worker_oidc_gate` (built fail-closed, the last test below).
    calls = [n for n in ast.walk(_main_tree()) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "create_main_service_blueprint"]
    assert len(calls) == 1
    [registered] = calls
    injected = [ast.unparse(a) for a in registered.args[1:2]]
    injected += [ast.unparse(k.value) for k in registered.keywords if k.arg == _gate_parameter()]
    assert injected == ["_worker_oidc_gate"]


def test_the_refusal_is_returned_as_is_and_nothing_runs_before_it():
    [handler] = _worker_handlers(_routes_tree())
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

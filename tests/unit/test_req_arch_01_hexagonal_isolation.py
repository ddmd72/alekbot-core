import pytest
import ast
import os
import re

from tests.unit.arch_tech_debt import (
    CROSS_PORT_WHITELIST,
    PORT_DATA_MODELS_WHITELIST,
    PORT_IN_DOMAIN_WHITELIST,
    CROSS_ADAPTER_WHITELIST,
    MODEL_NAME_WHITELIST_FILES,
    HTTP_CLIENT_WHITELIST_FILES,
    PLATFORM_FORMAT_WHITELIST_FILES,
    HANDLER_IMPLEMENTS_PORT_WHITELIST,
)


# ===========================================================================
# ██████████████████████████████████████████████████████████████████████████
#
#  ATTENTION — THIS MESSAGE IS FOR AI ASSISTANTS (CLAUDE AND ANY OTHER).
#
#  If this test is failing — DO NOT TOUCH THIS FILE.
#  Do not add exceptions. Do not weaken rules. Do not rewrite checks.
#  Do not add entries to arch_tech_debt.py without an explicit user instruction.
#
#  A failing test means an architectural violation exists in production code (src/).
#  Correct action: fix the violation in src/, not adapt the test to pass.
#
#  Changes to this file and to arch_tech_debt.py require explicit sign-off
#  from the codebase owner. "The test is in the way" is not a reason to change it.
#
# ██████████████████████████████████████████████████████████████████████████
# ===========================================================================


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iter_py_files(path: str):
    for root, _, files in os.walk(path):
        for file in files:
            if file.endswith(".py"):
                yield os.path.join(root, file)


def _parse(file_path: str) -> ast.Module:
    with open(file_path, "r") as f:
        return ast.parse(f.read(), filename=file_path)


def _resolve_module(file_path: str, module: str, level: int) -> str:
    """Resolve a relative import to an absolute dotted module path.

    Handles both absolute (level=0) and relative (level>0) imports so that
    all import-rule checks work correctly regardless of import style.
    """
    if level == 0:
        return module
    parts = file_path.replace(os.sep, "/").split("/")
    pkg = parts[:-1]  # drop filename, keep directory components
    for _ in range(level - 1):
        if pkg:
            pkg.pop()
    base = ".".join(pkg)
    return f"{base}.{module}" if module else base


def _is_type_checking_guard(node: ast.If) -> bool:
    """Return True if the node is an ``if TYPE_CHECKING:`` block."""
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    ):
        return True
    return False


def _collect_imports(layer_path: str) -> list[tuple[str, str, int]]:
    """Return (file_path, resolved_module, lineno) for every runtime ImportFrom in a layer.

    Relative imports are resolved to their absolute dotted path so that all
    downstream checks work uniformly regardless of import style.

    Imports inside ``if TYPE_CHECKING:`` blocks are **excluded** — they are
    type-only and do not create runtime dependencies.
    """
    results = []
    for fp in _iter_py_files(layer_path):
        tree = _parse(fp)
        # Collect line numbers that belong to TYPE_CHECKING guard bodies
        tc_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _is_type_checking_guard(node):
                for child in ast.walk(node):
                    if hasattr(child, "lineno"):
                        tc_lines.add(child.lineno)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.lineno in tc_lines:
                    continue
                resolved = _resolve_module(fp, node.module, node.level or 0)
                results.append((fp, resolved, node.lineno))
            elif isinstance(node, ast.Import):
                if node.lineno in tc_lines:
                    continue
                for alias in node.names:
                    results.append((fp, alias.name, node.lineno))
    return results


def _assert_no_forbidden(layer_path: str, forbidden: list[str]) -> None:
    violations = []
    for fp, module, lineno in _collect_imports(layer_path):
        for f in forbidden:
            if module.startswith(f):
                violations.append(f"  {fp}:{lineno} imports {module}")
    assert not violations, "Hexagonal violation(s):\n" + "\n".join(violations)


def _get_adapter_subpackage(file_path: str) -> str | None:
    """Return the immediate sub-package name for an adapter file, or None if top-level.

    Examples:
      src/adapters/slack/http_adapter.py  → 'slack'
      src/adapters/claude_adapter.py      → None  (top-level)
    """
    parts = file_path.replace(os.sep, "/").split("/")
    try:
        idx = parts.index("adapters")
    except ValueError:
        return None
    if idx + 2 < len(parts):
        return parts[idx + 1]
    return None


# ---------------------------------------------------------------------------
# REQ-ARCH-01 — Import rules
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-01")
def test_domain_layer_isolation():
    """domain/ → stdlib + pydantic only. No src.* imports permitted."""
    _assert_no_forbidden("src/domain", [
        "src.adapters", "src.services", "src.handlers", "src.agents",
        "src.infrastructure", "src.composition", "src.config", "src.utils",
        "src.ports", "src.web",
    ])


@pytest.mark.requirement("REQ-ARCH-01")
def test_ports_layer_isolation():
    """ports/ → domain/ + stdlib + ABC only. Cross-port imports are separately guarded."""
    _assert_no_forbidden("src/ports", [
        "src.adapters", "src.services", "src.handlers", "src.agents",
        "src.infrastructure", "src.composition", "src.config", "src.utils",
        "src.web",
    ])


@pytest.mark.requirement("REQ-ARCH-01")
def test_services_layer_isolation():
    """services/ → domain/ + ports/ + utils/ only.

    Forbidden: adapters (concrete implementations), handlers (orchestrators),
    composition (wiring layer), agents (must be decoupled), web (delivery layer),
    infrastructure (coordinator/registry — business logic must not know about routing),
    config (env-level settings must be injected, not imported directly).
    utils/ (logger, telemetry) is allowed as cross-cutting infrastructure.
    """
    _assert_no_forbidden("src/services", [
        "src.adapters", "src.handlers", "src.composition",
        "src.agents", "src.web", "src.infrastructure", "src.config",
    ])


@pytest.mark.requirement("REQ-ARCH-01")
def test_agents_layer_isolation():
    """agents/ → domain/ + ports/ + infrastructure/ + utils/ only.

    Forbidden: adapters (concrete implementations), composition (wiring layer),
    services (must be injected via constructor, not imported directly),
    config (use domain value objects or constructor injection).
    TYPE_CHECKING imports are excluded — they are type-only, not runtime deps.
    """
    _assert_no_forbidden("src/agents", [
        "src.adapters", "src.composition", "src.services", "src.config",
        "src.handlers", "src.web",
    ])


# ---------------------------------------------------------------------------
# REQ-ARCH-14 — infrastructure/ must not import agents/, services/, adapters/,
#               composition/, or web/
#
# infrastructure/ is a foundational layer (coordinator, registry, config).
# It must not depend on higher layers. Agents are registered into the
# coordinator by the composition layer, not imported by infrastructure.
# TYPE_CHECKING imports are excluded — they are type-only, not runtime deps.
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-14")
def test_infrastructure_layer_isolation():
    """infrastructure/ must not import agents, services, adapters, composition, or web."""
    _assert_no_forbidden("src/infrastructure", [
        "src.agents", "src.services", "src.adapters", "src.composition", "src.web",
        "src.config", "src.handlers",
    ])


# ---------------------------------------------------------------------------
# REQ-ARCH-02 — No print() in src/ (except bootstrap modules)
# ---------------------------------------------------------------------------

# config/ and telemetry.py use print() before logger is initialized — intentional.
_PRINT_WHITELIST = {
    os.path.normpath("src/config/settings.py"),
    os.path.normpath("src/utils/telemetry.py"),
}


@pytest.mark.requirement("REQ-ARCH-02")
def test_no_print_in_src():
    """No print() calls in src/ — use logger instead."""
    violations = []
    for fp in _iter_py_files("src"):
        if os.path.normpath(fp) in _PRINT_WHITELIST:
            continue
        for node in ast.walk(_parse(fp)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                violations.append(f"  {fp}:{node.lineno}")
    assert not violations, "print() found (use logger):\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# REQ-ARCH-03 — Every *Agent class must inherit BaseAgent
# ---------------------------------------------------------------------------

_AGENT_CLASS_WHITELIST = {
    "BaseAgent",
    "CircuitBreaker",
    "ToolResponse",
    "AgentLoopResult",
    "_QuickLoopResult",
    "_TrackingFactManagement",
}


@pytest.mark.requirement("REQ-ARCH-03")
def test_agent_classes_inherit_base_agent():
    """Every class named *Agent in agents/ must inherit from BaseAgent."""
    violations = []
    for fp in _iter_py_files("src/agents"):
        tree = _parse(fp)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not node.name.endswith("Agent"):
                continue
            if node.name in _AGENT_CLASS_WHITELIST:
                continue
            base_names = [
                b.id if isinstance(b, ast.Name) else
                b.attr if isinstance(b, ast.Attribute) else None
                for b in node.bases
            ]
            if "BaseAgent" not in base_names:
                violations.append(
                    f"  {fp}:{node.lineno} class {node.name} does not inherit BaseAgent "
                    f"(bases: {[b for b in base_names if b]})"
                )
    assert not violations, "Agent inheritance violation(s):\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# REQ-ARCH-04 — Every *Port / *Repository / *Queue / *Store in ports/ must be abstract
# ---------------------------------------------------------------------------

_ABSTRACT_SUFFIXES = ("Port", "Repository", "Queue", "Store", "Service")
_ABSTRACT_BASES = {"ABC", "Protocol"}

# Non-abstract classes legitimately living in ports/ (exception subclasses, enums).
# Data models (Pydantic BaseModel subclasses) are tracked separately in arch_tech_debt.py.
_PORT_CONCRETE_WHITELIST = {
    "ResourceType", "Action", "Role",    # Enums in iam_port
    "IAMDecision",                       # dataclass in platform_auth_port
    "AssemblyError", "HtmlRenderError",  # Exception subclasses (legit in ports)
} | PORT_DATA_MODELS_WHITELIST | {"TokenClaims", "OAuthTokens", "OAuthUserInfo"}


@pytest.mark.requirement("REQ-ARCH-04")
def test_ports_are_abstract():
    """Every *Port / *Repository / *Queue / *Store class in ports/ must use ABC or Protocol."""
    violations = []
    for fp in _iter_py_files("src/ports"):
        tree = _parse(fp)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not node.name.endswith(_ABSTRACT_SUFFIXES):
                continue
            if node.name in _PORT_CONCRETE_WHITELIST:
                continue
            base_names = {
                b.id if isinstance(b, ast.Name) else
                b.attr if isinstance(b, ast.Attribute) else None
                for b in node.bases
            }
            if not base_names & _ABSTRACT_BASES:
                violations.append(
                    f"  {fp}:{node.lineno} class {node.name} is not abstract "
                    f"(bases: {[b for b in base_names if b]})"
                )
    assert not violations, "Port abstraction violation(s):\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# REQ-ARCH-05 — No direct infrastructure imports in services/ and agents/
# ---------------------------------------------------------------------------

_INFRA_PACKAGES = [
    "google.cloud",
    "google.generativeai",
    "anthropic",
    "openai",
    "slack_sdk",
    "telegram",
    "firebase_admin",
    "grpc",
]


@pytest.mark.requirement("REQ-ARCH-05")
def test_no_direct_infra_imports_in_services():
    """services/ must not import infrastructure packages directly — go through ports."""
    _assert_no_forbidden("src/services", _INFRA_PACKAGES)


@pytest.mark.requirement("REQ-ARCH-05")
def test_no_direct_infra_imports_in_agents():
    """agents/ must not import infrastructure packages directly — go through ports."""
    _assert_no_forbidden("src/agents", _INFRA_PACKAGES)


# ---------------------------------------------------------------------------
# REQ-ARCH-06 — ports/ must not import other ports/ (cross-port coupling)
#
# A port file may only import from src.domain, stdlib, and ABC.
# Importing from another port creates hidden coupling between port contracts.
# Exception: intra-subpackage __init__.py aggregation is acceptable.
# Known violations: see arch_tech_debt.py → CROSS_PORT_WHITELIST
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-06")
def test_ports_do_not_import_other_ports():
    """ports/ files must not import from other ports/ — only domain/, stdlib, ABC."""
    violations = []
    for fp, module, lineno in _collect_imports("src/ports"):
        if not module.startswith("src.ports."):
            continue
        fp_norm = fp.replace(os.sep, "/")
        if os.path.basename(fp_norm) == "__init__.py":
            continue  # intra-subpackage __init__.py aggregation is acceptable
        if (fp_norm, module) in CROSS_PORT_WHITELIST:
            continue
        violations.append(f"  {fp}:{lineno} imports {module}")
    assert not violations, (
        "Cross-port import — ports must only import from domain/:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-07 — *Port / *Repository interfaces must not live in domain/
#
# domain/ is for pure data models and value objects. ABCs belong in ports/.
# Known violations: see arch_tech_debt.py → PORT_IN_DOMAIN_WHITELIST
# ---------------------------------------------------------------------------

_INTERFACE_SUFFIXES_IN_DOMAIN = ("Port", "Repository", "Queue", "Store")


@pytest.mark.requirement("REQ-ARCH-07")
def test_no_port_interfaces_in_domain():
    """*Port / *Repository / *Queue / *Store classes must not be defined in domain/."""
    violations = []
    for fp in _iter_py_files("src/domain"):
        for node in ast.walk(_parse(fp)):
            if not isinstance(node, ast.ClassDef):
                continue
            if not node.name.endswith(_INTERFACE_SUFFIXES_IN_DOMAIN):
                continue
            if node.name in PORT_IN_DOMAIN_WHITELIST:
                continue
            violations.append(
                f"  {fp}:{node.lineno} class {node.name} should live in ports/, not domain/"
            )
    assert not violations, (
        "Port/Repository interface in domain/ — move to ports/:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-08 — adapters/ must not import other adapters cross-subpackage
#
# An adapter file may only import from domain/, ports/, config/, and its own
# immediate sub-package. Cross-subpackage dependencies couple delivery layers.
# Known violations: see arch_tech_debt.py → CROSS_ADAPTER_WHITELIST
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-08")
def test_adapters_do_not_import_cross_subpackage():
    """adapters/ files must not import adapters from a different sub-package."""
    violations = []
    for fp, module, lineno in _collect_imports("src/adapters"):
        if not module.startswith("src.adapters."):
            continue
        fp_norm = fp.replace(os.sep, "/")
        subpkg = _get_adapter_subpackage(fp_norm)
        if subpkg and module.startswith(f"src.adapters.{subpkg}."):
            continue  # intra-subpackage: allowed
        if (fp_norm, module) in CROSS_ADAPTER_WHITELIST:
            continue
        violations.append(f"  {fp}:{lineno} imports {module}")
    assert not violations, (
        "Cross-subpackage adapter import — adapters must not depend on other adapters:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-09 — Pydantic data models (BaseModel) must not live in ports/
#
# ports/ contains only ABCs and Protocols. BaseModel subclasses are domain
# objects and belong in domain/.
# Known violations: see arch_tech_debt.py → PORT_DATA_MODELS_WHITELIST
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-09")
def test_no_data_models_in_ports():
    """Pydantic BaseModel subclasses must not be defined in ports/ — they belong in domain/."""
    violations = []
    for fp in _iter_py_files("src/ports"):
        for node in ast.walk(_parse(fp)):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name in PORT_DATA_MODELS_WHITELIST:
                continue
            base_names = {
                b.id if isinstance(b, ast.Name) else
                b.attr if isinstance(b, ast.Attribute) else ""
                for b in node.bases
            }
            if "BaseModel" in base_names:
                violations.append(
                    f"  {fp}:{node.lineno} class {node.name}(BaseModel) should be in domain/"
                )
    assert not violations, (
        "Pydantic model in ports/ — move to domain/:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-10 — handlers/ must not import adapters, web, or config
#
# handlers/ is an orchestration layer: it may use domain/, ports/, services/,
# infrastructure/, composition/, utils/. Direct adapter or web imports
# bypass the port abstraction. Config values should be domain value objects
# injected via constructor.
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-10")
def test_handlers_layer_isolation():
    """handlers/ must not import adapters, web, config, agents, or composition layer directly.

    Handlers are orchestrators — they receive all dependencies via constructor injection.
    composition/ is the wiring root that creates handlers; handlers must not import back
    from it (circular architectural dependency). Use TYPE_CHECKING for type hints.
    """
    _assert_no_forbidden("src/handlers", [
        "src.adapters", "src.web", "src.config", "src.agents", "src.composition",
    ])


# ---------------------------------------------------------------------------
# REQ-ARCH-15 — web/ must not import adapters/, agents/, infrastructure/,
#               or handlers/
#
# web/ is the HTTP delivery layer. It receives wired services from
# composition/ and calls them via port interfaces. Direct adapter, agent,
# infrastructure, or handler imports bypass the port abstraction.
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-15")
def test_web_layer_isolation():
    """web/ must not import adapters, agents, infrastructure, or handlers."""
    _assert_no_forbidden("src/web", [
        "src.adapters", "src.agents", "src.infrastructure", "src.handlers",
    ])


# ---------------------------------------------------------------------------
# REQ-ARCH-16 — adapters/ must not import services/, agents/, handlers/,
#               infrastructure/, composition/, or web/
#
# adapters/ implement port interfaces using concrete external libraries.
# They may only depend on domain/ (models), ports/ (contracts), config/
# (environment settings), and utils/ (logger, telemetry). Importing
# higher layers couples the delivery layer to business logic.
# Cross-subpackage adapter imports are separately guarded by REQ-ARCH-08.
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-16")
def test_adapters_layer_isolation():
    """adapters/ must not import services, agents, handlers, infrastructure,
    composition, or web."""
    _assert_no_forbidden("src/adapters", [
        "src.services", "src.agents", "src.handlers",
        "src.infrastructure", "src.composition", "src.web",
    ])


# ---------------------------------------------------------------------------
# REQ-ARCH-11 — composition/ must not import web/
#
# composition/ is the wiring root (ServiceContainer, factories). It may import
# adapters, services, agents, etc. The only forbidden direction: web/ → because
# web depends on composition, not vice versa.
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-11")
def test_composition_does_not_import_web():
    """composition/ must not import web/ — web depends on composition, not vice versa."""
    _assert_no_forbidden("src/composition", [
        "src.web",
    ])


# ---------------------------------------------------------------------------
# REQ-ARCH-12 — No provider-specific model name strings in agents/, services/,
#               domain/, or ports/
#
# Model name strings (claude-*, gemini-*, gpt-*, grok-*, o1-*, o3-*) must only
# appear in adapters/ (MODEL_TIERS) and config/. Placing them in higher layers
# couples business logic to a concrete LLM provider.
# Whitelisted files: see arch_tech_debt.py → MODEL_NAME_WHITELIST_FILES
# ---------------------------------------------------------------------------

_MODEL_NAME_RE = re.compile(r"^(claude|gemini|gpt|grok|o1|o3)[-./]", re.IGNORECASE)
_MODEL_NAME_LAYERS = ["src/agents", "src/services", "src/domain", "src/ports"]


@pytest.mark.requirement("REQ-ARCH-12")
def test_no_model_name_strings_in_wrong_layers():
    """Model name strings (claude-*, gemini-*, gpt-*, grok-*, o1-*, o3-*) must not appear
    in agents/, services/, domain/, or ports/ — they belong in adapters/ and config/."""
    violations = []
    for layer in _MODEL_NAME_LAYERS:
        for fp in _iter_py_files(layer):
            if os.path.normpath(fp) in MODEL_NAME_WHITELIST_FILES:
                continue
            for node in ast.walk(_parse(fp)):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and _MODEL_NAME_RE.match(node.value)
                ):
                    violations.append(f"  {fp}:{node.lineno} literal {node.value!r}")
    assert not violations, (
        "Provider-specific model name strings in wrong layer "
        "(move to adapters/ MODEL_TIERS or config/):\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-13 — No provider name comparisons in agents/ or services/
#
# Branching on provider name strings (== "gemini", == "claude", etc.) in agents
# or services couples business logic to a concrete LLM provider. Provider-specific
# logic belongs in adapters/.
# Note: dict-key usage ({"gemini": ...}) is not a comparison and is not caught here.
# ---------------------------------------------------------------------------

_PROVIDER_NAMES = {"gemini", "claude", "openai", "grok", "anthropic"}
_PROVIDER_BRANCH_LAYERS = ["src/agents", "src/services"]


@pytest.mark.requirement("REQ-ARCH-13")
def test_no_provider_name_comparisons_in_agents_services():
    """Provider name string comparisons (== 'gemini', == 'claude', etc.) must not appear
    in agents/ or services/ — provider branching belongs in adapters/."""
    violations = []
    for layer in _PROVIDER_BRANCH_LAYERS:
        for fp in _iter_py_files(layer):
            for node in ast.walk(_parse(fp)):
                if not isinstance(node, ast.Compare):
                    continue
                all_nodes = [node.left] + node.comparators
                for n in all_nodes:
                    if (
                        isinstance(n, ast.Constant)
                        and isinstance(n.value, str)
                        and n.value.lower() in _PROVIDER_NAMES
                    ):
                        violations.append(
                            f"  {fp}:{node.lineno} comparison with provider name {n.value!r}"
                        )
    assert not violations, (
        "Provider name comparison in agents/ or services/ "
        "(provider branching belongs in adapters/):\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-17 — No direct os.environ / os.getenv in core layers
#
# Environment variables must be read in config/ (EnvironmentConfig) and
# passed via constructor injection. Direct access in domain/, ports/,
# services/, agents/, or handlers/ bypasses the config layer and makes
# code untestable without env manipulation.
# Allowed layers: config/, utils/ (bootstrap), adapters/ (boundary),
# infrastructure/ (feature flags), composition/, web/.
# ---------------------------------------------------------------------------

_ENV_VAR_CORE_LAYERS = ["src/domain", "src/ports", "src/services", "src/agents", "src/handlers"]


def _is_os_environ_or_getenv(node: ast.AST) -> bool:
    """Return True if node is os.environ[...] or os.getenv(...)."""
    # os.getenv(...)
    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "getenv"
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        ):
            return True
    # os.environ[...] or os.environ.get(...)
    if isinstance(node, (ast.Subscript, ast.Call)):
        target = node.value if isinstance(node, ast.Subscript) else getattr(node.func, "value", None)
        if (
            target is not None
            and isinstance(target, ast.Attribute)
            and target.attr == "environ"
            and isinstance(target.value, ast.Name)
            and target.value.id == "os"
        ):
            return True
    return False


@pytest.mark.requirement("REQ-ARCH-17")
def test_no_env_var_access_in_core_layers():
    """os.environ / os.getenv must not appear in domain/, ports/, services/,
    agents/, or handlers/ — read env vars in config/ and inject via DI."""
    violations = []
    for layer in _ENV_VAR_CORE_LAYERS:
        for fp in _iter_py_files(layer):
            for node in ast.walk(_parse(fp)):
                if _is_os_environ_or_getenv(node):
                    violations.append(f"  {fp}:{node.lineno} direct env var access")
    assert not violations, (
        "Direct os.environ/os.getenv in core layer "
        "(read in config/, inject via DI):\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-18 — No HTTP client libraries in core layers
#
# aiohttp, httpx, requests must not be imported in domain/, ports/,
# agents/, handlers/, or infrastructure/. HTTP calls belong in adapters/.
# services/ exceptions: see arch_tech_debt.py → HTTP_CLIENT_WHITELIST_FILES.
# ---------------------------------------------------------------------------

_HTTP_CLIENT_PACKAGES = ["aiohttp", "httpx", "requests"]
_HTTP_CLIENT_LAYERS = [
    "src/domain", "src/ports", "src/agents", "src/handlers",
    "src/infrastructure", "src/services",
]


@pytest.mark.requirement("REQ-ARCH-18")
def test_no_http_client_libs_in_core_layers():
    """HTTP client libraries (aiohttp, httpx, requests) must not be imported
    in domain/, ports/, agents/, handlers/, infrastructure/, or services/
    — HTTP calls belong in adapters/."""
    violations = []
    for layer in _HTTP_CLIENT_LAYERS:
        for fp, module, lineno in _collect_imports(layer):
            if os.path.normpath(fp) in HTTP_CLIENT_WHITELIST_FILES:
                continue
            for pkg in _HTTP_CLIENT_PACKAGES:
                if module == pkg or module.startswith(f"{pkg}."):
                    violations.append(f"  {fp}:{lineno} imports {module}")
    assert not violations, (
        "HTTP client library in core layer "
        "(move HTTP calls to adapters/):\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-19 — No direct adapter instantiation outside composition/
#
# Classes named *Adapter or Firestore* must only be instantiated in
# composition/ (ServiceContainer, factories) and main.py. Instantiating
# them in services/, agents/, handlers/, web/, or infrastructure/ bypasses
# the DI wiring and couples business logic to concrete implementations.
# ---------------------------------------------------------------------------

_ADAPTER_CLASS_PATTERNS = re.compile(
    r"(Adapter|^Firestore|^Gcp[A-Z]|^Gcs[A-Z])"
)
_ADAPTER_INSTANTIATION_LAYERS = [
    "src/services", "src/agents", "src/handlers",
    "src/web", "src/infrastructure",
]


def _get_call_name(node: ast.Call) -> str | None:
    """Extract the class/function name from a Call node."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


@pytest.mark.requirement("REQ-ARCH-19")
def test_no_adapter_instantiation_outside_composition():
    """Adapter classes (*Adapter, Firestore*, Gcp*, Gcs*) must only be
    instantiated in composition/ and main.py — not in services/, agents/,
    handlers/, web/, or infrastructure/."""
    violations = []
    for layer in _ADAPTER_INSTANTIATION_LAYERS:
        for fp in _iter_py_files(layer):
            for node in ast.walk(_parse(fp)):
                if not isinstance(node, ast.Call):
                    continue
                name = _get_call_name(node)
                if name and _ADAPTER_CLASS_PATTERNS.search(name):
                    violations.append(
                        f"  {fp}:{node.lineno} instantiates {name}()"
                    )
    assert not violations, (
        "Adapter instantiation outside composition/ "
        "(wire dependencies in ServiceContainer/main.py):\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-20 — No platform-specific formatting in agents/ or services/
#
# String literals referencing Slack mrkdwn, Telegram MarkdownV2, or
# platform-specific link syntax (<url|text>) in agents/ or services/
# couple business logic to a delivery platform. Formatting rules should
# be injected via the prompt system or handled in adapters/.
# Known exceptions: see arch_tech_debt.py → PLATFORM_FORMAT_WHITELIST_FILES.
# ---------------------------------------------------------------------------

_PLATFORM_FORMAT_RE = re.compile(
    r"mrkdwn|MarkdownV2|parse_mode|Slack_Formatting|<https?://[^>]+\|[^>]+>"
)
_PLATFORM_FORMAT_LAYERS = ["src/agents", "src/services"]


@pytest.mark.requirement("REQ-ARCH-20")
def test_no_platform_formatting_in_agents_services():
    """Platform-specific formatting references (mrkdwn, MarkdownV2, Slack link
    syntax) must not appear as string literals in agents/ or services/ — handle
    formatting in adapters/ or inject via prompt system."""
    violations = []
    for layer in _PLATFORM_FORMAT_LAYERS:
        for fp in _iter_py_files(layer):
            if os.path.normpath(fp) in PLATFORM_FORMAT_WHITELIST_FILES:
                continue
            for node in ast.walk(_parse(fp)):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and _PLATFORM_FORMAT_RE.search(node.value)
                ):
                    violations.append(
                        f"  {fp}:{node.lineno} platform-specific format: "
                        f"{node.value[:60]!r}..."
                    )
    assert not violations, (
        "Platform-specific formatting in agents/ or services/ "
        "(handle in adapters/ or prompt system):\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-21 — No Firestore collection name literals in core layers
#
# Collection names (domain_facts_v2, sessions, etc.) must be defined in
# config/environment.py and passed to adapters via constructor. Hardcoding
# them in services/, agents/, handlers/, domain/, or ports/ couples business
# logic to the storage schema.
# ---------------------------------------------------------------------------

_COLLECTION_NAME_RE = re.compile(
    r"^("
    r"domain_\w+_v\d+"          # domain_facts_v2, domain_users_v2, etc.
    r"|domain_prompt_\w+"       # domain_prompt_tokens_v3, domain_prompt_blueprints_v3
    r"|email_indexing_(?:jobs|state)\w*"  # email_indexing_jobs_v1, email_indexing_state
    r"|email_exclusions"
    r"|oauth_credentials"
    r"|user_notification_state"
    r"|consolidation_queue"
    r"|event_dedup"
    r"|user_context"
    r"|sessions"                # Firestore sessions collection
    r")$"
)
_COLLECTION_NAME_LAYERS = [
    "src/domain", "src/ports", "src/services", "src/agents", "src/handlers",
]


@pytest.mark.requirement("REQ-ARCH-21")
def test_no_collection_names_in_core_layers():
    """Firestore collection name literals (domain_facts_v2, sessions, etc.)
    must not appear in domain/, ports/, services/, agents/, or handlers/ —
    define in config/environment.py and pass via DI."""
    violations = []
    for layer in _COLLECTION_NAME_LAYERS:
        for fp in _iter_py_files(layer):
            for node in ast.walk(_parse(fp)):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and len(node.value) > 5
                    and _COLLECTION_NAME_RE.match(node.value)
                ):
                    violations.append(
                        f"  {fp}:{node.lineno} collection name literal {node.value!r}"
                    )
    assert not violations, (
        "Firestore collection name in core layer "
        "(define in config/environment.py, pass via DI):\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-24 — agents/ must not import other agents/ (cross-agent coupling)
#
# Each agent is an independent specialist registered in the coordinator.
# Cross-agent runtime imports create hidden coupling and bypass the
# coordinator's routing, circuit-breaker, and registration model.
# The correct mechanism for agent→agent calls is coordinator.handle_delegation().
#
# Only src.agents.base_agent is allowed — it provides the common BaseAgent ABC.
# TYPE_CHECKING imports are excluded (handled by _collect_imports).
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-24")
def test_agents_do_not_import_sibling_agents():
    """Agent files must not import from other agent files at runtime.

    The only allowed intra-agents import is src.agents.base_agent (BaseAgent ABC).
    All agent→agent calls must go through coordinator.handle_delegation().
    __init__.py re-exports are excluded.
    TYPE_CHECKING imports are excluded (handled by _collect_imports).
    """
    violations = []
    for fp, module, lineno in _collect_imports("src/agents"):
        if not module.startswith("src.agents."):
            continue
        # base_agent is the common base class — always allowed
        if module == "src.agents.base_agent" or module.startswith("src.agents.base_agent."):
            continue
        fp_norm = fp.replace(os.sep, "/")
        # __init__.py re-exports are allowed
        if os.path.basename(fp_norm) == "__init__.py":
            continue
        violations.append(f"  {fp}:{lineno} imports {module}")
    assert not violations, (
        "Agent-to-agent import — use coordinator.handle_delegation() "
        "instead of importing other agents directly:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-22 — services/ must not import other services/ (cross-service coupling)
#
# Each service receives its dependencies via constructor injection from the
# composition root. Direct service→service imports create hidden coupling
# and make the dependency graph harder to reason about.
#
# Allowed: imports within the same file (obviously), and re-exports from
# __init__.py. Inheritance within the same module is also acceptable.
# TYPE_CHECKING imports are excluded (handled by _collect_imports).
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-22")
def test_services_do_not_import_other_services():
    """services/ files must not import from other services/ files.

    Dependencies between services should be wired via constructor injection
    in the composition root (ServiceContainer / main.py).
    """
    violations = []
    for fp, module, lineno in _collect_imports("src/services"):
        if not module.startswith("src.services."):
            continue
        fp_norm = fp.replace(os.sep, "/")
        # Allow __init__.py re-exports
        if os.path.basename(fp_norm) == "__init__.py":
            continue
        # Allow intra-subpackage imports (e.g., prompt_v3/a.py → prompt_v3/b.py)
        fp_parts = fp_norm.split("/")
        try:
            svc_idx = fp_parts.index("services")
        except ValueError:
            continue
        # Determine the sub-package of the importing file
        # e.g. src/services/prompt_v3/foo.py → subpkg = "prompt_v3"
        remaining = fp_parts[svc_idx + 1:]
        file_subpkg = remaining[0] if len(remaining) > 1 else None
        # Determine the sub-package of the imported module
        # e.g. src.services.prompt_v3.bar → subpkg = "prompt_v3"
        mod_parts = module.split(".")
        # mod_parts = ["src", "services", "prompt_v3", "bar"]
        mod_after_svc = mod_parts[2:]  # after "src.services."
        mod_subpkg = mod_after_svc[0] if len(mod_after_svc) > 1 else None
        if file_subpkg and mod_subpkg and file_subpkg == mod_subpkg:
            continue  # intra-subpackage: allowed
        violations.append(f"  {fp}:{lineno} imports {module}")
    assert not violations, (
        "Cross-service import — services must receive dependencies via DI, "
        "not import other services:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-23 — adapters/ must not import other adapters (intra-subpackage OK)
#
# Strengthens REQ-ARCH-08: catches ALL cross-adapter imports, including
# within the same sub-package but from a different concrete adapter file.
# Only __init__.py aggregation and true intra-subpackage utility imports
# are allowed.
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-ARCH-23")
def test_adapters_do_not_import_sibling_adapter_implementations():
    """Adapter *_adapter.py files must not import other *_adapter.py files.

    This catches intra-subpackage adapter-to-adapter coupling (e.g.,
    LLMSecurityAdapter importing RegexSecurityAdapter as fallback).
    Shared base classes and utilities (base.py, response_channel.py,
    mcp_client.py) within a sub-package are allowed.
    Cross-subpackage is already caught by REQ-ARCH-08.
    """
    violations = []
    for fp, module, lineno in _collect_imports("src/adapters"):
        if not module.startswith("src.adapters."):
            continue
        fp_norm = fp.replace(os.sep, "/")
        if os.path.basename(fp_norm) == "__init__.py":
            continue
        # Only flag when the TARGET module is also an adapter implementation
        mod_parts = module.split(".")
        target_module = mod_parts[-1] if mod_parts else ""
        if not target_module.endswith("_adapter"):
            continue  # importing base.py, response_channel.py, etc. is OK
        # Intra-subpackage adapter-to-adapter
        subpkg = _get_adapter_subpackage(fp_norm)
        if subpkg and module.startswith(f"src.adapters.{subpkg}."):
            violations.append(f"  {fp}:{lineno} imports {module}")
        elif not subpkg:
            # Top-level adapter importing another top-level adapter
            violations.append(f"  {fp}:{lineno} imports {module}")
    assert not violations, (
        "Adapter-to-adapter import — wire fallbacks via constructor "
        "injection in composition root:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# REQ-ARCH-25 — handlers/ must not import ports/ directly
#
# Ports are contracts between the domain and the infrastructure. Handlers are
# orchestrators — they coordinate services and domain objects. A handler that
# imports a port directly couples dispatch logic to infrastructure contracts,
# bypassing the service layer that is the proper owner of port interactions.
#
# Rule: ports must be accessed through services, never through dispatchers.
# If a handler needs to call a port method, extract that logic into a service.
# ---------------------------------------------------------------------------


@pytest.mark.requirement("REQ-ARCH-25")
def test_handlers_do_not_import_ports_directly():
    """handlers/ must not import src.ports directly.

    Ports are infrastructure contracts owned by services. Handlers receive
    wired service objects via constructor injection and call service methods —
    they never call port methods directly.

    Exception: a handler may import its own primary (driving) port to declare
    that it implements that interface — see arch_tech_debt.py →
    HANDLER_IMPLEMENTS_PORT_WHITELIST.
    """
    violations = []
    for fp, module, lineno in _collect_imports("src/handlers"):
        if not module.startswith("src.ports"):
            continue
        fp_norm = fp.replace(os.sep, "/")
        if (fp_norm, module) in HANDLER_IMPLEMENTS_PORT_WHITELIST:
            continue
        violations.append(f"  {fp}:{lineno} imports {module}")
    assert not violations, (
        "Handler imports port directly — extract port call into a service:\n"
        + "\n".join(violations)
    )

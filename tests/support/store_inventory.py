"""The inventory of stateful store adapters and the kit that covers each (TEST_COVERAGE_SYSTEMIC_RFC §3 H2, P11 card M).

One definition, read by `tests/unit/adapters/test_every_stateful_store_has_a_kit.py`:

* `stateful_store_classes()`: every public class of the store modules of `src/adapters/` that implements a port (AST
  only, the adapters are never imported).
* `KITS`: `module.Class` -> the test file(s) that run the real class over a fake with semantics (`EvalDb`, the GCS fake,
  the Cloud Tasks client fake).
* `CLIENT_FAKES` and `BUILDERS`: how the meta-test recognises such a construction in a kit's source.

A class in neither `KITS` nor `tests/unit/adapters/stateful_store_allowlist.txt` fails the meta-test; so does a class in both.
"""
import ast
import fnmatch
from pathlib import Path
from typing import Dict, List, Set, Tuple

REPO = Path(__file__).resolve().parents[2]
ADAPTERS = REPO / "src" / "adapters"

# A module under src/adapters/ is a store module when its file name matches one of these.
STORE_MODULE_GLOBS: Tuple[str, ...] = (
    "firestore_*.py", "gcs_*.py", "gcp_task_queue.py", "fact_management_adapter.py", "*_queue.py",
)
# A public class of a store module without a port base is still a store when its `__init__` takes one of these.
CLIENT_PARAMS: frozenset = frozenset({"db", "client", "firestore_client", "storage_client"})

# Names that stand for a fake with semantics. A kit constructs a store "over" one when an argument of the construction
# derives from a call of one of these (or of a local subclass), directly or through a name or attribute assigned from it,
# a fixture that returns it, or a parameter annotated with it. `EvalDb` is `tests/support/firestore_eval.py` (and the
# drive suites' own copy in `tests/unit/adapters/firestore_drive_eval_fakes.py`); `NameAwareDb` is that copy with
# document-name awareness; `NoteDb` is that copy with the drive notes' versions subcollection
# (`tests/unit/adapters/firestore_drive_note_fakes.py`); `FactStore` wraps an `EvalDb`; `FakeGcs` is `tests/support/gcs_fake.py`;
# `FailingTasksClient` is the Cloud Tasks client of `tests/support/redelivery_world.py`.
CLIENT_FAKES: frozenset = frozenset({"EvalDb", "NameAwareDb", "NoteDb", "FactStore", "FakeGcs", "FailingTasksClient"})

# Names that stand for a mock. A construction whose arguments derive from one of these and from no fake is refused, and so is a
# helper that assigns one to an attribute of the store (`adapter._client = MagicMock()`); `patch(target)` and `patch.object(o, n)`
# with no replacement count too (the replacement is a `MagicMock`). A mock for a configuration argument next to a fake client is fine.
MOCKS: frozenset = frozenset({"MagicMock", "Mock", "AsyncMock", "NonCallableMagicMock", "NonCallableMock", "create_autospec"})

# Second discovery rule: a module of `src/adapters/` that imports one of these `google.cloud` client libraries is a store module
# (its file name matches `STORE_MODULE_GLOBS`) or is listed here with the reason it is not one. A stale line fails.
CLOUD_CLIENT_LIBRARIES: Tuple[str, ...] = ("firestore", "storage", "tasks_v2", "bigquery")
NOT_STORE_MODULES: Dict[str, str] = {
    "src.adapters.bigquery_prompt_content_adapter":
        "the BigQuery prompt-content write sink, outside the RFC's H2 list (an append-only log, no store port to read back)",
}

_STORE = "src.adapters."

# A named builder: a callable that constructs the listed classes over a fake, so a kit that calls it need not import the class.
# `name -> (file that defines it, the classes it builds)`; the meta-test checks the definition constructs each class and that
# some kit calls the builder.
BUILDERS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "FactStore": ("tests/support/fact_store.py", (
        _STORE + "firestore_repo.FirestoreFactRepository", _STORE + "fact_management_adapter.FactManagementAdapter")),
    "ReviewWorld": ("tests/support/fact_review_world.py", (
        _STORE + "firestore_fact_review_store.FirestoreFactReviewStore",
        _STORE + "firestore_fact_lifecycle_counters.FirestoreFactLifecycleCounters",
        _STORE + "firestore_memory_write_lease.FirestoreMemoryWriteLease")),
    "make_eval_repo": ("tests/unit/adapters/firestore_drive_eval_fakes.py", (
        _STORE + "firestore_drive_index_repo.FirestoreDriveIndexRepository",)),
}

_A = "tests/unit/adapters/"
KITS: Dict[str, Tuple[str, ...]] = {
    # P4 fact kits
    _STORE + "firestore_repo.FirestoreFactRepository": (
        _A + "test_fact_repository_kit.py", _A + "test_fact_repository_versions_kit.py", _A + "test_fact_write_kit.py"),
    _STORE + "fact_management_adapter.FactManagementAdapter": (_A + "test_fact_management_kit.py",),
    # P6 access kits
    _STORE + "firestore_whitelist_repo.FirestoreWhitelistRepository": (_A + "test_whitelist_store_kit.py",),
    _STORE + "firestore_mcp_client_repository.FirestoreMCPClientRepository": (
        _A + "test_mcp_client_store_kit.py", _A + "test_mcp_client_store_cipher_kit.py"),
    _STORE + "firestore_oauth_credentials_adapter.FirestoreOAuthCredentialsAdapter": (
        _A + "test_oauth_credentials_store_kit.py",),
    _STORE + "firestore_invite_code_repo.FirestoreInviteCodeRepository": (_A + "test_invite_code_store_kit.py",),
    # P11 Firestore kits
    _STORE + "firestore_account_repo.FirestoreAccountRepository": (_A + "test_account_store_kit.py",),
    _STORE + "firestore_quota_service.FirestoreQuotaService": (_A + "test_quota_service_kit.py",),
    _STORE + "firestore_user_repo.FirestoreUserRepository": (_A + "test_user_store_kit.py",),
    _STORE + "firestore_agent_note_adapter.FirestoreAgentNoteAdapter": (_A + "test_agent_note_store_kit.py",),
    _STORE + "firestore_channel_binding_adapter.FirestoreChannelBindingAdapter": (
        _A + "test_channel_binding_store_kit.py",),
    _STORE + "firestore_companion_cache_repository.FirestoreCompanionCacheRepository": (
        _A + "test_companion_cache_store_kit.py",),
    _STORE + "firestore_companion_memory_repository.FirestoreCompanionMemoryRepository": (
        _A + "test_companion_memory_store_kit.py",),
    _STORE + "firestore_companion_extraction_queue.FirestoreCompanionExtractionQueue": (
        _A + "test_companion_extraction_queue_store_kit.py",),
    _STORE + "firestore_consolidation_queue.FirestoreConsolidationQueue": (_A + "test_consolidation_queue_store_kit.py",),
    _STORE + "firestore_dedup_store.FirestoreDedupStore": (_A + "test_dedup_store_kit.py",),
    _STORE + "firestore_task_claim_store.FirestoreTaskClaimStore": (_A + "test_task_claim_store_kit.py",),
    _STORE + "firestore_email_exclusions_adapter.FirestoreEmailExclusionsAdapter": (
        _A + "test_email_exclusions_store_kit.py",),
    _STORE + "firestore_email_job_repo.FirestoreEmailJobRepository": (_A + "test_email_job_store_kit.py",),
    _STORE + "firestore_indexed_email_repo.FirestoreIndexedEmailRepository": (_A + "test_indexed_email_store_kit.py",),
    _STORE + "firestore_ephemeral_store.FirestoreEphemeralStore": (_A + "test_ephemeral_store_kit.py",),
    _STORE + "firestore_long_turn_registry.FirestoreLongTurnRegistry": (_A + "test_long_turn_store_kit.py",),
    _STORE + "firestore_memory_fact_draft_repository.FirestoreMemoryFactDraftRepository": (
        _A + "test_memory_fact_draft_store_kit.py",),
    _STORE + "firestore_notification_state_adapter.FirestoreNotificationStateAdapter": (
        _A + "test_notification_state_store_kit.py",),
    _STORE + "firestore_session_store.FirestoreSessionStore": (_A + "test_session_store_kit.py",),
    _STORE + "firestore_short_link_repository.FirestoreShortLinkRepository": (_A + "test_short_link_store_kit.py",),
    _STORE + "firestore_skill_repository.FirestoreSkillRepository": (_A + "test_skill_store_kit.py",),
    _STORE + "firestore_task_config_repository.FirestoreTaskConfigRepository": (_A + "test_task_config_store_kit.py",),
    _STORE + "firestore_task_search_index.FirestoreTaskSearchIndex": (_A + "test_task_search_index_store_kit.py",),
    # P11 GCS kits (tests/support/gcs_fake.py) and the Cloud Tasks kit (FailingTasksClient)
    _STORE + "gcs_file_storage_adapter.GcsFileStorageAdapter": (_A + "test_file_storage_store_kit.py",),
    _STORE + "gcs_media_adapter.GcsMediaAdapter": (_A + "test_media_store_kit.py",),
    _STORE + "gcs_workspace_adapter.GcsWorkspaceAdapter": (_A + "test_workspace_store_kit.py",),
    _STORE + "gcp_task_queue.GcpTaskQueue": (_A + "test_task_queue_store_kit.py",),
    # Facts S2-S4 stores: their own suites run them over `EvalDb`; the review store only through the review world
    _STORE + "firestore_fact_lifecycle_counters.FirestoreFactLifecycleCounters": (
        _A + "test_fact_lifecycle_counters.py", _A + "test_fact_lifecycle_counters_typed.py"),
    _STORE + "firestore_memory_write_lease.FirestoreMemoryWriteLease": (_A + "test_firestore_memory_write_lease.py",),
    _STORE + "firestore_fact_review_store.FirestoreFactReviewStore": (_A + "test_firestore_fact_review_store.py",),
    # Drive repositories: the evaluating-fake suites (the mirror's contract suite runs one case list against the in-memory
    # reference and the adapter; the index repository's parity suite does the same for the v2 methods)
    _STORE + "firestore_drive_mirror_repo.FirestoreDriveMirrorRepository": (_A + "test_drive_mirror_repo_contract.py",),
    _STORE + "firestore_drive_index_repo.FirestoreDriveIndexRepository": (_A + "test_drive_index_v2_parity.py",),
    _STORE + "firestore_drive_note_repo.FirestoreDriveNoteRepository": (_A + "test_drive_note_repo_contract.py",),
}

ALLOWLIST_FILE = REPO / "tests" / "unit" / "adapters" / "stateful_store_allowlist.txt"


def _dotted(path: Path) -> str:
    return ".".join(path.relative_to(REPO).with_suffix("").parts)


def _port_names(tree: ast.Module) -> Set[str]:
    """Names this module imports from `src.ports` (absolute or relative)."""
    names: Set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        absolute = node.level == 0 and (node.module == "src.ports" or node.module.startswith("src.ports."))
        relative = node.level > 0 and (node.module == "ports" or node.module.startswith("ports."))
        if absolute or relative:
            names |= {alias.asname or alias.name for alias in node.names}
    return names


def _base_root(base: ast.expr) -> str:
    while isinstance(base, ast.Attribute):
        base = base.value
    return base.id if isinstance(base, ast.Name) else ""


def _takes_client(cls: ast.ClassDef) -> bool:
    for item in cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
            return any(a.arg in CLIENT_PARAMS for a in item.args.args + item.args.kwonlyargs)
    return False


def _imports_cloud_client(tree: ast.Module) -> bool:
    def library(dotted: str) -> bool:
        return any(dotted == f"google.cloud.{lib}" or dotted.startswith((f"google.cloud.{lib}.", f"google.cloud.{lib}_"))
                   for lib in CLOUD_CLIENT_LIBRARIES)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if library(node.module) or (node.module == "google.cloud" and any(library(f"google.cloud.{a.name}")
                                                                              for a in node.names)):
                return True
        elif isinstance(node, ast.Import) and any(library(a.name) for a in node.names):
            return True
    return False


def matches_store_globs(path: Path) -> bool:
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in STORE_MODULE_GLOBS)


def cloud_client_modules() -> List[str]:
    """Dotted module of every file under `src/adapters/` that imports one of `CLOUD_CLIENT_LIBRARIES` (a lazy import inside a
    function counts), whatever its name."""
    return [_dotted(path) for path in sorted(ADAPTERS.rglob("*.py"))
            if _imports_cloud_client(ast.parse(path.read_text(encoding="utf-8")))]


def store_classes_in_source(source: str) -> List[str]:
    """Class names of one store module's source that are stores: public, with a direct base imported from `src.ports`, or with
    no base at all and an `__init__` that takes a client (`CLIENT_PARAMS`). Private classes (the retry forwarder, the JSON encoder)
    and classes with another base are not stores."""
    tree = ast.parse(source)
    ports = _port_names(tree)
    return [node.name for node in tree.body
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_") and (
                any(_base_root(b) in ports for b in node.bases)
                or (not any(_base_root(b) for b in node.bases) and _takes_client(node)))]


def stateful_store_classes() -> List[str]:
    """`module.Class` of every store class (`store_classes_in_source`) defined in a store module of `src/adapters/`
    (`STORE_MODULE_GLOBS`). A store adapter with no port is still a store when its `__init__` takes a client. AST only: the
    adapters are never imported."""
    found: List[str] = []
    for path in sorted(ADAPTERS.rglob("*.py")):
        if matches_store_globs(path):
            found += [f"{_dotted(path)}.{name}" for name in store_classes_in_source(path.read_text(encoding="utf-8"))]
    return found


def read_allowlist() -> List[Tuple[str, str]]:
    """`(module.Class, reason)` per line of `stateful_store_allowlist.txt` (`module.Class  # reason`; `#` lines and blanks skipped)."""
    entries: List[Tuple[str, str]] = []
    for raw in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, reason = line.partition("#")
        entries.append((name.strip(), reason.strip()))
    return entries

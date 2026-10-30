"""T-5 (MULTI_DRIVE_RFC §8, §10, §13): an AST scan over `src/domain/drive_*`. Every key function (`*_key`, `*_doc_id`)
takes a `ConnectionScope`, or is listed here, user-scoped by design or no stored key at all, each with its reason. A new
key function that is neither fails until someone decides which it is."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# User-scoped by design (RFC §8 second bullet, §10): the reason is the test's documentation.
USER_SCOPED = {
    "usage_doc_id": "monthly model usage, {user}:{YYYY-MM}: budgets are shared per user (D8)",
    "initial_build_doc_id": "the initial build by provider account: a reconnect of the account finds it (D8, §10)",
    "transcript_doc_id": "the transcript store by content key: a copy on any connection pays nothing (§10)",
    "mirror_root_doc_id": "mirror roots by drive identity: the folder outlives a connection (§10)",
    "ledger_key": "the mirror ledger by the mirror's drive identity and the source identity (§10)",
    "archive_folder_key": "an archive folder of the ledger, by drive identity (§10)",
    "note_doc_id": "owner notes are the user's (§8: notes by identity and tag)",
    "item_binding_key": "a note's binding by item identity, unique across providers (§7.2)",
    "tagged_ref_key": "a note's ref key by the connection's tag (§9)",
    "binding_key": "the single-drive note binding key (part D removes it)",
    "ref_key": "the untagged note ref key (part D removes it)",
    "record_binding_key": "the single-drive record binding key (part D removes it)",
}

# Not a stored document key: a value, a query key or an ordering key that no document is addressed by.
NOT_A_DOCUMENT_KEY = {
    "month_key": "the YYYY-MM string a usage document id is built from",
    "safe_doc_id": "the escaping of any id, not a key of its own",
    "skeleton_key": "a search key form of a token",
    "source_class_of_about_key": "classifies a search key",
    "source_class_of_word_key": "classifies a search key",
    "source_class_of_name_key": "classifies a search key",
    "parse_binding_key": "reads a note key back",
    "audit_key": "the note audit's ordering key, in process",
    "search_key": "a pass-through search's dedup key, in process",
    "tier_order_key": "a result ordering key",
    "request_key": "a transcript window's request key inside one batch",
    "split_request_key": "reads a request key back",
    "hold_key": "a record's hold digest, stored on the record itself",
    "profile_hold_key": "a record's hold digest, stored on the record itself",
    "passages_hold_key": "a record's hold digest, stored on the record itself",
    "transcript_content_key": "the content key of a stored transcript, user-scoped through transcript_doc_id",
    "record_content_key": "the content key of a record's stored transcript, user-scoped through transcript_doc_id",
}


def _key_functions():
    for path in sorted((ROOT / "src" / "domain").glob("drive_*.py")):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_") \
                    and (node.name.endswith("_key") or node.name.endswith("_doc_id")):
                yield path.name, node


def _takes_scope(node: ast.FunctionDef) -> bool:
    return any(a.annotation is not None and ast.unparse(a.annotation) == "ConnectionScope" for a in node.args.args)


def test_every_domain_key_function_is_connection_scoped_or_listed_with_a_reason():
    unlisted = [f"{module}:{node.name}" for module, node in _key_functions()
                if not _takes_scope(node) and node.name not in USER_SCOPED and node.name not in NOT_A_DOCUMENT_KEY]
    assert unlisted == []


def test_the_connection_scoped_keys_are_the_index_ones():
    scoped = {node.name for _, node in _key_functions() if _takes_scope(node)}
    assert {"record_doc_id", "chunk_doc_id", "sync_state_doc_id", "transcript_job_doc_id", "transcript_batch_doc_id",
            "tick_task_key", "continuation_task_key", "nudge_task_key", "toggle_task_key"} <= scoped


def test_no_listed_name_takes_a_scope_as_well():
    both = [node.name for _, node in _key_functions()
            if _takes_scope(node) and (node.name in USER_SCOPED or node.name in NOT_A_DOCUMENT_KEY)]
    assert both == []


def test_the_single_drive_index_keys_are_gone():
    names = {node.name for _, node in _key_functions()}
    assert not names & {"record_key", "chunk_key", "job_doc_id", "batch_doc_id", "initial_doc_id", "doc_key",
                        "tick_key", "continuation_key", "nudge_key", "toggle_key"}

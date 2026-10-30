"""Keys are the runtime's (MULTI_DRIVE_RFC §10 I6, T-14): for the same item, drive and account, every key the cut-over
script writes equals the key the new runtime computes from what the OneDrive adapter reports (the transcript store by
content identity and by the item fallback, the ledger, an archive folder, the mirror root, the initial build, the
note's `i:` and `r:` keys); and the script imports those functions instead of typing a formula (AST)."""
import ast

from src.adapters.microsoft.onedrive_adapter import (
    content_identity_from_graph, onedrive_drive_identity, onedrive_item_identity,
)
from src.domain.drive_mirror import archive_folder_key, ledger_key, mirror_root_doc_id
from src.domain.drive_note import item_binding_key, tagged_ref_key
from src.domain.drive_transcript import transcript_doc_id
from src.domain.drive_transcript_windows import transcript_content_key
from src.domain.drive_usage import initial_build_doc_id
from tests.unit.scripts.multi_drive_cutover_world import (
    ARCHIVED_KEY, DAY, DRIVE, HASH_KEY, ITEM_KEY, REALM, SCRIPT, SUBJECT, USER, world,
)

# What Graph reports for the two recordings and the drive (the runtime's input).
GRAPH_HASHED = {"id": "item-1", "name": "call.m4a", "size": 100, "cTag": "c:{V1},4",
                "file": {"mimeType": "audio/mp4", "hashes": {"quickXorHash": "QXH1aGFzaA=="}},
                "parentReference": {"driveId": DRIVE, "id": "fold-1"}}
GRAPH_UNHASHED = {"id": "item-2", "name": "memo.m4a", "size": 7, "cTag": "c:{V2},1",
                  "file": {"mimeType": "audio/mp4"}, "parentReference": {"driveId": DRIVE, "id": "fold-1"}}


def _runtime_store_id(raw):
    identity = onedrive_item_identity(raw["parentReference"]["driveId"], raw["id"])
    key, _ = transcript_content_key(content_identity_from_graph(raw), identity, raw["cTag"])
    return transcript_doc_id(USER, key)


async def _applied():
    w = world()
    code, lines = await w.go("apply")
    assert code == 0, lines
    return w


async def test_the_stored_transcripts_land_where_the_runtime_reads_them():
    w = await _applied()
    heads = w.col(w.names.transcripts)
    assert _runtime_store_id(GRAPH_HASHED) in heads          # by content identity
    assert _runtime_store_id(GRAPH_UNHASHED) in heads        # by the item fallback (identity + content version)
    # the old keys were made for exactly these items: the seed's keys are today's formula for the same input
    assert HASH_KEY == "QXH1aGFzaA==:100" and ITEM_KEY == f"item:item-2:{GRAPH_UNHASHED['cTag']}"


async def test_the_mirror_root_ledger_and_archive_folders_land_where_the_runtime_reads_them():
    w = await _applied()
    drive_identity = onedrive_drive_identity(GRAPH_HASHED["parentReference"]["driveId"])
    assert mirror_root_doc_id(USER, drive_identity) in w.col(w.names.roots)
    ledger = w.col(w.names.ledger)
    assert ledger_key(USER, "file", drive_identity, onedrive_item_identity(DRIVE, GRAPH_HASHED["id"])) in ledger
    assert ledger_key(USER, "folder", drive_identity, onedrive_item_identity(DRIVE, "fold-1")) in ledger
    for day, path in (("", "Archive"), (DAY, ""), (DAY, "Calls")):
        assert archive_folder_key(USER, drive_identity, day, path) in ledger


async def test_the_initial_build_and_the_note_keys_are_the_runtimes():
    w = await _applied()
    (conn,) = await w.store.list_for_user(USER)
    assert initial_build_doc_id(USER, REALM, SUBJECT) in w.col(w.names.usage)
    keys = w.doc(w.names.notes, f"{USER}:n1")["match_keys"]
    assert item_binding_key(onedrive_item_identity(DRIVE, GRAPH_HASHED["id"])) in keys
    assert tagged_ref_key(conn.ref_tag, GRAPH_HASHED["id"]) in keys


async def test_the_ledgers_content_keys_are_the_stores_new_keys():
    w = await _applied()
    archived = w.doc(w.names.ledger, ledger_key(USER, "file", onedrive_drive_identity(DRIVE),
                                                onedrive_item_identity(DRIVE, "item-9")))
    expected, _ = transcript_content_key(None, onedrive_item_identity(DRIVE, "item-9"), ARCHIVED_KEY.split(":", 2)[2])
    assert archived["content_key"] == expected


async def test_old_keys_round_trip_through_the_new_form():
    script = world().script
    for old in (HASH_KEY, ITEM_KEY, ARCHIVED_KEY):
        new = script.new_content_key(old, DRIVE, "microsoft_onedrive")
        assert new != old and script.legacy_content_key(new, "microsoft_onedrive", script.content_family()) == old
    assert script.new_content_key(script.new_content_key(HASH_KEY, DRIVE, "microsoft_onedrive"), DRIVE,
                                  "microsoft_onedrive") is None         # a new key is never converted twice


# -- AST: the script copies no key formula -------------------------------------------------------------------------

_PRODUCT_KEYS = {
    "src.adapters.microsoft.onedrive_adapter": {"content_identity_from_graph", "onedrive_drive_identity",
                                                "onedrive_item_identity"},
    "src.domain.drive_mirror": {"archive_folder_key", "ledger_key", "mirror_root_doc_id"},
    "src.domain.drive_note": {"item_binding_key", "tagged_ref_key"},
    "src.domain.drive_transcript": {"transcript_doc_id"},
    "src.domain.drive_transcript_windows": {"transcript_content_key"},
    "src.domain.drive_usage": {"initial_build_doc_id"},
    "src.domain.drive_index": {"sync_state_doc_id"},
    "src.domain.drive_connection": {"connection_doc_id"},
}


def _tree():
    return ast.parse(SCRIPT.read_text(encoding="utf-8"))


def test_the_script_imports_every_key_function_from_the_product():
    imported = {}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.setdefault(node.module, set()).update(alias.name for alias in node.names)
    for module, names in _PRODUCT_KEYS.items():
        assert names <= imported.get(module, set()), module


def test_the_script_defines_no_key_function_of_its_own_beyond_the_legacy_shapes():
    product = set().union(*_PRODUCT_KEYS.values())
    own = [node.name for node in ast.walk(_tree()) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert not product & set(own)
    keyish = {name for name in own if name.endswith(("_key", "_doc_id", "_identity")) and not name.startswith("legacy_")}
    # new_content_key only composes the product's functions (below); the other two read a key, they build none:
    # parse_legacy_content_key splits an old one, _is_new_key tests a prefix
    assert keyish == {"new_content_key", "parse_legacy_content_key", "_is_new_key"}


def test_the_new_content_key_only_composes_the_products_functions():
    fn = next(node for node in ast.walk(_tree()) if isinstance(node, ast.FunctionDef) and node.name == "new_content_key")
    called = {node.func.id for node in ast.walk(fn) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert {"transcript_content_key", "content_identity_from_graph", "onedrive_item_identity"} <= called
    assert not [node for node in ast.walk(fn) if isinstance(node, ast.JoinedStr)]


def test_no_hash_formula_is_typed_in_the_script():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "hashlib" not in source and "sha256" not in source
    f_strings_outside_legacy = []
    for fn in ast.walk(_tree()):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and not fn.name.startswith("legacy_"):
            prefix_tests = {id(arg) for node in ast.walk(fn) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute) and node.func.attr == "startswith"
                            for arg in node.args}      # a prefix compared against, never a key written
            for node in ast.walk(fn):
                if isinstance(node, ast.JoinedStr) and id(node) not in prefix_tests:
                    literal = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
                    if literal in (":", "i:", "r:") or literal.endswith(":initial") or ":archive:" in literal:
                        f_strings_outside_legacy.append((fn.name, literal))
    assert f_strings_outside_legacy == []

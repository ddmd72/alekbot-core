"""DriveOperations' public surface and layering (DRIVE_SPECIALIST_RFC §5.5, §11.1 "Layering")."""
import ast
import inspect
import pathlib

from src.domain.drive_op import DriveOp
from src.infrastructure.drive_operations import DriveOperations

ROOT = pathlib.Path(__file__).resolve().parents[3] / "src" / "infrastructure"


def _public_coroutines():
    return {name for name, fn in inspect.getmembers(DriveOperations, inspect.iscoroutinefunction)
            if not name.startswith("_")}


def test_every_public_coroutine_is_classified_as_a_read_or_a_mutation_and_never_both():
    reads, mutations = DriveOperations.READ_METHODS, DriveOperations.MUTATION_METHODS
    assert not reads & mutations
    assert reads | mutations == _public_coroutines()


def test_op_of_names_a_drive_op_for_every_method_that_returns_an_outcome():
    outcome_methods = {name for name in _public_coroutines()
                       if "DriveOutcome" in str(inspect.signature(getattr(DriveOperations, name)).return_annotation)}
    assert set(DriveOperations.OP_OF) == outcome_methods
    assert all(isinstance(op, DriveOp) for op in DriveOperations.OP_OF.values())
    assert {DriveOperations.OP_OF[m] for m in DriveOperations.MUTATION_METHODS} == {
        DriveOp.COPY, DriveOp.SAVE_TEXT, DriveOp.CREATE_FOLDER, DriveOp.MOVE, DriveOp.APPEND, DriveOp.REPLACE,
        DriveOp.DELETE}


def test_drive_operations_and_drive_tools_import_no_port_and_no_adapter():
    """REQ-ARCH-14 covers adapters only; the RFC also keeps UserDrivePort out (the services own it)."""
    for name in ("drive_operations.py", "drive_tools.py"):
        modules = []
        for node in ast.walk(ast.parse((ROOT / name).read_text())):
            if isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
        bad = [m for m in modules if "adapters" in m or "user_drive_port" in m or m == "UserDrivePort"]
        assert bad == [], (name, bad)

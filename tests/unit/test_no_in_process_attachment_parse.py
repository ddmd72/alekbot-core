"""U4a-R: the in-process attachment parse is gone. Email attachments open through the document service only."""
import ast
import inspect
from pathlib import Path

from src.ports.email_provider_port import EmailProviderPort

SRC = Path(__file__).resolve().parents[2] / "src"
OWNER = SRC / "utils" / "file_conversion.py"


def _imports_convert_file_to_text(tree: ast.AST) -> bool:
    return any(isinstance(n, ast.ImportFrom) and any(a.name == "convert_file_to_text" for a in n.names)
               for n in ast.walk(tree))


def test_no_src_module_imports_convert_file_to_text():
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if p != OWNER and _imports_convert_file_to_text(ast.parse(p.read_text(encoding="utf-8")))]
    assert offenders == []


def test_email_provider_port_has_no_deep_download():
    assert "deep" not in inspect.signature(EmailProviderPort.batch_get_full_content).parameters

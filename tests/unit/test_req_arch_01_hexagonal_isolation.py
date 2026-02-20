import pytest
import sys
import ast
import os

@pytest.mark.requirement("REQ-ARCH-01")
def test_domain_layer_isolation():
    """
    Verify that Domain layer does not import from Adapters or Infrastructure.
    Covers: REQ-ARCH-01 (Hexagonal Isolation)
    """
    domain_path = "src/domain"
    forbidden_imports = ["src.adapters", "src.services", "src.handlers"]
    
    for root, _, files in os.walk(domain_path):
        for file in files:
            if file.endswith(".py"):
                file_path = os.path.join(root, file)
                with open(file_path, "r") as f:
                    tree = ast.parse(f.read())
                
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        module = node.module if isinstance(node, ast.ImportFrom) else None
                        if module:
                            for forbidden in forbidden_imports:
                                assert not module.startswith(forbidden), \
                                    f"Domain file {file} imports from forbidden layer: {module}"

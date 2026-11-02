"""T-1 over the modules the slice-2b core adds (MULTI_DRIVE_RFC §13): no string constant, docstrings included, names a
provider or a provider shape (the scan of `test_multi_drive_no_provider_strings`)."""
import pytest

from tests.unit.domain.test_multi_drive_no_provider_strings import ROOT, _hits

C2B_MODULES = (
    "src/domain/drive_access_decision.py",
    "src/domain/drive_access_op.py",
    "src/domain/drive_access_policy.py",
    "src/domain/drive_ancestry.py",
    "src/domain/drive_connection_access.py",
    "src/domain/drive_feed_scope.py",
    "src/domain/drive_item_access.py",
    "src/domain/drive_setting_refused.py",
    "src/domain/drive_mounts.py",
    "src/domain/drive_mount_line.py",
)


@pytest.mark.parametrize("module", C2B_MODULES)
def test_no_provider_string_constant(module):
    assert _hits((ROOT / module).read_text()) == []

"""tests/plugins/datafile_audit.py: finds the tests that testmon cannot select (PYTEST_DATAFILE_AUDIT=1)."""

import pytest

from tests.support.plugin_runs import isolate_switches, make_importable_for_children, plugin_conftest

pytest_plugins = ["pytester"]

HELPER = '''
def read_me():
    return open(__file__).read()

def boom():
    raise ValueError("x")
'''


@pytest.fixture(autouse=True)
def _audit_on(monkeypatch):
    isolate_switches(monkeypatch)
    monkeypatch.setenv("PYTEST_DATAFILE_AUDIT", "1")


def _run(pytester, source, *args, helper=False):
    pytester.makeconftest(plugin_conftest("datafile_audit"))
    if helper:
        pytester.makepyfile(helper=HELPER)
    pytester.makepyfile(test_inner=source)
    return pytester.runpytest("-p", "no:cacheprovider", *args)


def test_a_data_read_without_the_marker_fails_the_run(pytester):
    result = _run(pytester, "def test_reads():\n    open(__file__).read()\n")
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    out = result.stdout.str()
    assert "test_inner.py::test_reads" in out
    assert "reads:  test_inner.py" in out


def test_a_pathlib_read_and_a_relative_open_are_seen(pytester):
    source = (
        "from pathlib import Path\n\n"
        "def test_pathlib():\n    Path(__file__).read_text()\n\n"
        "def test_relative():\n    open('test_inner.py').read()\n"
    )
    out = _run(pytester, source).stdout.str()
    assert "test_inner.py::test_pathlib" in out
    assert "test_inner.py::test_relative" in out


def test_a_marked_test_passes(pytester):
    source = "import pytest\n\n@pytest.mark.datafile\ndef test_reads():\n    open(__file__).read()\n"
    result = _run(pytester, source)
    assert result.ret == pytest.ExitCode.OK
    assert "without @pytest.mark.datafile" not in result.stdout.str()


def test_a_module_level_pytestmark_covers_every_test(pytester):
    source = (
        "import pytest\n\npytestmark = pytest.mark.datafile\n\n"
        "def test_a():\n    open(__file__).read()\n\ndef test_b():\n    open(__file__).read()\n"
    )
    assert _run(pytester, source).ret == pytest.ExitCode.OK


def test_an_import_is_not_a_data_read(pytester):
    source = "import helper\n\ndef test_module_level_import():\n    import json\n    import helper as again\n    assert again\n"
    result = _run(pytester, source, helper=True)
    assert result.ret == pytest.ExitCode.OK


def test_a_lazy_import_inside_a_test_is_not_a_data_read(pytester):
    pytester.makepyfile(lazy="DATA = open(__file__).read()\n")
    source = "def test_first_importer():\n    import lazy\n    assert lazy.DATA\n"
    assert _run(pytester, source).ret == pytest.ExitCode.OK


def test_a_subprocess_without_the_marker_fails_the_run(pytester):
    source = "import subprocess, sys\n\ndef test_child():\n    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
    result = _run(pytester, source)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    out = result.stdout.str()
    assert "test_inner.py::test_child" in out
    assert "spawns:" in out


def test_a_marked_subprocess_test_passes(pytester):
    source = (
        "import subprocess, sys, pytest\n\n@pytest.mark.datafile\n"
        "def test_child():\n    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
    )
    assert _run(pytester, source).ret == pytest.ExitCode.OK


def test_a_read_at_collection_time_belongs_to_every_test_of_the_module(pytester):
    source = "DATA = open(__file__).read()\n\ndef test_one():\n    pass\n\ndef test_two():\n    pass\n"
    result = _run(pytester, source)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    out = result.stdout.str()
    assert "test_inner.py::test_one" in out
    assert "test_inner.py::test_two" in out
    assert "(2)" in out


def test_a_read_in_a_fixture_is_attributed_to_the_test_using_it(pytester):
    source = (
        "import pytest\n\n@pytest.fixture\ndef text():\n    return open(__file__).read()\n\n"
        "def test_uses(text):\n    pass\n\ndef test_does_not():\n    pass\n"
    )
    out = _run(pytester, source).stdout.str()
    assert "test_inner.py::test_uses" in out
    assert "test_inner.py::test_does_not" not in out


def test_source_read_for_a_traceback_is_not_a_data_read(pytester):
    source = (
        "import helper, traceback, warnings\n\ndef test_formats_a_traceback():\n"
        "    try:\n        helper.boom()\n    except ValueError:\n        assert 'boom' in traceback.format_exc()\n"
    )
    assert _run(pytester, source, helper=True).ret == pytest.ExitCode.OK


def test_inspect_getsource_is_a_data_read(pytester):
    source = "import helper, inspect\n\ndef test_reads_source():\n    assert 'boom' in inspect.getsource(helper)\n"
    result = _run(pytester, source, helper=True)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert "reads:  helper.py" in result.stdout.str()


def test_a_read_outside_the_rootdir_is_not_flagged(pytester):
    source = "import os\n\ndef test_reads_the_stdlib():\n    open(os.__file__).read()\n"
    assert _run(pytester, source).ret == pytest.ExitCode.OK


def test_files_under_ignored_directories_are_not_flagged(pytester):
    pytester.mkdir("node_modules")
    pytester.path.joinpath("node_modules", "x.js").write_text("x")
    pytester.path.joinpath(".testmondata").write_text("x")
    source = "def test_reads():\n    open('node_modules/x.js').read()\n    open('.testmondata').read()\n"
    assert _run(pytester, source).ret == pytest.ExitCode.OK


def test_a_marked_test_that_touches_nothing_is_listed_as_stale_without_failing(pytester):
    source = "import pytest\n\n@pytest.mark.datafile\ndef test_pure():\n    pass\n"
    result = _run(pytester, source)
    assert result.ret == pytest.ExitCode.OK
    out = result.stdout.str()
    assert "touched no repo file" in out
    assert "test_inner.py::test_pure" in out


def test_a_skipped_marked_test_is_not_stale(pytester):
    source = "import pytest\n\n@pytest.mark.datafile\n@pytest.mark.skip\ndef test_skipped():\n    pass\n"
    result = _run(pytester, source)
    assert "touched no repo file" not in result.stdout.str()


def test_without_the_env_var_the_plugin_does_nothing(pytester, monkeypatch):
    monkeypatch.delenv("PYTEST_DATAFILE_AUDIT")
    result = _run(pytester, "def test_reads():\n    open(__file__).read()\n")
    assert result.ret == pytest.ExitCode.OK
    assert "datafile" not in result.stdout.str()


def test_the_recorder_of_an_outer_session_comes_back_after_an_inner_one(pytester):
    from tests.plugins import datafile_audit

    before = datafile_audit._active
    _run(pytester, "def test_ok():\n    pass\n")
    assert datafile_audit._active is before


@pytest.mark.slow
@pytest.mark.datafile
def test_findings_travel_from_xdist_workers_to_the_controller(pytester, monkeypatch):
    make_importable_for_children(monkeypatch)
    source = (
        "DATA = open(__file__).read()\n\ndef test_one():\n    pass\n\n"
        "def test_two():\n    open(__file__).read()\n"
    )
    result = _run(pytester, source, "-n", "2")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    out = result.stdout.str()
    assert "test_inner.py::test_one" in out
    assert "test_inner.py::test_two" in out

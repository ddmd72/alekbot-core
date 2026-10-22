"""scripts/check_coverage_layers.py: per-layer branch-aware floors that only rise."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_coverage_layers as layers

REPO_ROOT = Path(__file__).resolve().parents[3]


def summary(statements, covered, branches=0, covered_branches=0):
    return {"summary": {"num_statements": statements, "covered_lines": covered, "num_branches": branches,
                        "covered_branches": covered_branches}}


class TestLayerOf:
    @pytest.mark.parametrize(
        "path, layer",
        [
            ("src/domain/fact.py", "src/domain"),
            ("src/domain/prompt_v3/token.py", "src/domain"),
            ("src/ports/llm_port.py", "src/ports"),
            ("src/adapters/slack/http_adapter.py", "src/adapters"),
            ("src/web/oauth_app.py", "src/web"),
            ("src/utils/logger.py", "src (other)"),
            ("src/locales/uk.py", "src (other)"),
            ("main.py", "root entry points"),
            ("relay_main.py", "root entry points"),
            ("scripts/x.py", None),
            ("src_other/x.py", None),
            ("domain/x.py", None),
        ],
    )
    def test_every_path_is_in_exactly_one_layer(self, path, layer):
        assert layers.layer_of(path) == layer


class TestAggregate:
    def test_sums_the_counts_and_never_averages_percentages(self):
        files = {
            "src/domain/a.py": summary(10, 10),                  # 100 %
            "src/domain/b.py": summary(90, 45, 20, 10),          # 52.3 % branch-aware
            "src/web/c.py": summary(4, 2, 2, 0),
            "scripts/ignored.py": summary(100, 0),
        }
        out = layers.aggregate(files)
        domain = out["src/domain"]
        assert (domain.statements, domain.branches, domain.covered_lines, domain.covered_branches) == (100, 20, 55, 10)
        assert domain.percent == pytest.approx(100 * 65 / 120)       # (55 + 10) / (100 + 20): coverage.py's formula
        assert domain.line_percent == pytest.approx(55.0)
        assert out["src/web"].percent == pytest.approx(100 * 2 / 6)
        assert out["src/ports"].percent == 100.0                     # an empty layer cannot fail

    def test_absolute_paths_are_made_repo_relative(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        out = layers.aggregate({str(tmp_path / "src/domain/a.py"): summary(10, 5)})
        assert out["src/domain"].statements == 10


class TestFloors:
    def totals(self, **measured):
        out = {name: layers.LayerTotals(statements=1000, covered_lines=1000) for name, _ in layers.LAYERS}
        for name, percent in measured.items():
            out[name.replace("_", "/", 1)] = layers.LayerTotals(statements=1000, covered_lines=int(percent * 10))
        return out

    def test_floor_to_tenth_rounds_down(self):
        assert layers.floor_to_tenth(79.299) == 79.2 and layers.floor_to_tenth(88.0) == 88.0 and layers.floor_to_tenth(91.9999) == 91.9

    def test_a_layer_below_its_floor_is_a_violation(self):
        floors = {name: 50.0 for name, _ in layers.LAYERS}
        totals = self.totals(src_web=49.9)
        violations, rises, unfloored = layers.check(totals, floors)
        assert [v.split(":")[0] for v in violations] == ["src/web"] and unfloored == []

    def test_at_the_floor_passes_and_half_a_point_above_prints_the_rise(self):
        floors = {name: 100.0 for name, _ in layers.LAYERS}
        floors["src/web"] = 63.0
        floors["src/adapters"] = 60.0
        totals = self.totals(src_web=63.4, src_adapters=60.5)
        violations, rises, _ = layers.check(totals, floors)
        assert violations == []
        assert len(rises) == 1 and "src/adapters" in rises[0] and "floor can rise to 60.5" in rises[0]

    def test_a_layer_without_a_floor_fails_the_gate(self):
        _, _, unfloored = layers.check(self.totals(), {})
        assert len(unfloored) == len(layers.LAYERS)

    def test_the_committed_floors_name_every_layer(self):
        assert set(layers.FLOORS) == {name for name, _ in layers.LAYERS}
        assert all(0.0 < v <= 100.0 for v in layers.FLOORS.values())


class TestEntryPointsNoTestImports:
    def test_a_source_file_counts_as_all_missed(self, tmp_path, monkeypatch):
        rc = tmp_path / "rc"
        rc.write_text("[report]\nexclude_lines =\n    if __name__ == .__main__.:\n")
        monkeypatch.setattr(layers, "COVERAGERC", str(rc))
        source = tmp_path / "entry.py"
        source.write_text("import os\n\ndef f(x):\n    if x:\n        return 1\n    return 2\n\nif __name__ == '__main__':\n    f(1)\n")
        zero = layers.static_zero_totals(str(source))
        assert zero.statements == 5 and zero.covered_lines == 0 and zero.branches == 2 and zero.percent == 0.0

    def test_only_unmeasured_existing_entry_points_are_found(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        for name in ("main.py", "relay_main.py"):
            (tmp_path / name).write_text("x = 1\n")
        files = {"relay_main.py": summary(1, 1)}
        assert layers.missing_entry_points(files) == ["main.py"]


@pytest.mark.datafile  # runs the script as a process on a coverage.json file
class TestScript:
    def run(self, tmp_path, files, **env):
        report = tmp_path / "coverage.json"
        report.write_text(json.dumps({"files": files}))
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts/check_coverage_layers.py")], cwd=tmp_path, capture_output=True, text=True,
            env={"COVERAGE_JSON_PATH": str(report), "PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
        )

    def test_exits_zero_when_every_floor_is_met_and_prints_the_table(self, tmp_path):
        files = {f"src/{layer}/x.py": summary(100, 100) for layer in ("domain", "ports", "infrastructure", "agents", "services",
                                                                       "handlers", "adapters", "web", "composition", "utils")}
        files["relay_main.py"] = summary(100, 100)
        done = self.run(tmp_path, files)
        assert done.returncode == 0, done.stderr
        assert "branch-aware %" in done.stdout and "all 11 floors met" in done.stdout

    def test_exits_one_naming_the_layer_below_its_floor(self, tmp_path):
        files = {"src/domain/x.py": summary(100, 10)}
        done = self.run(tmp_path, files)
        assert done.returncode == 1 and "src/domain: 10.00% < floor" in done.stderr

    def test_exits_one_without_a_coverage_file(self, tmp_path):
        done = subprocess.run([sys.executable, str(REPO_ROOT / "scripts/check_coverage_layers.py")], cwd=tmp_path,
                              capture_output=True, text=True, env={"COVERAGE_JSON_PATH": str(tmp_path / "none.json")})
        assert done.returncode == 1 and "not found" in done.stderr

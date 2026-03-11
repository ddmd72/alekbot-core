#!/usr/bin/env python3
import argparse
import shutil
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
REPORTS_DIR = PROJECT_ROOT / "reports"
ARCHIVE_REPORTS_DIR = PROJECT_ROOT / "docs" / "archive" / "reports"

SCRIPT_MOVES: Dict[str, str] = {
    # Memory ops
    "migrate_memory.py": "memory/migrate.py",
    "rebuild_memory.py": "memory/rebuild.py",
    "sync_memory.py": "memory/sync.py",
    "deduplicate_memory.py": "memory/deduplicate.py",
    "copy_memory_to_dev.py": "memory/copy_to_dev.py",
    "memory_diff.py": "memory/ops/diff.py",
    "rollback_memory.py": "memory/ops/rollback.py",
    "update_memory_component.py": "memory/ops/update_component.py",
    "upload_kernel.py": "memory/ops/upload_kernel.py",
    "upload_system_components.py": "memory/ops/upload_components.py",
    "upload_to_prod.py": "memory/ops/upload_prod.py",

    # Prompt
    "debug_system_prompt.py": "prompt/debug_system_prompt.py",
    "debug_prompt_comparison.py": "prompt/debug_prompt_comparison.py",
    "debug_light_prompt.py": "prompt/debug_light_prompt.py",
    "debug_prompt_output.groovy": "prompt/debug_prompt_output.groovy",

    # Vectors
    "analyze_dev_vectors.py": "vectors/analyze_dev_vectors.py",
    "analyze_prod_vectors.py": "vectors/analyze_prod_vectors.py",
    "check_vector.py": "vectors/check_vector.py",
    "force_regenerate_dev_vectors.py": "vectors/force_regenerate_dev_vectors.py",
    "force_regenerate_prod_vectors.py": "vectors/force_regenerate_prod_vectors.py",
    "regenerate_vectors.py": "vectors/regenerate_vectors.py",
    "debug_prod_vectors.py": "vectors/debug_prod_vectors.py",
    "test_vector_search.py": "vectors/test_vector_search.py",
    "test_prod_vector_search.py": "vectors/test_prod_vector_search.py",
    "check_dev_glove.py": "vectors/check_dev_glove.py",
    "check_glove_data.py": "vectors/check_glove_data.py",
    "test_glove_search.py": "vectors/test_glove_search.py",

    # Validation
    "check_models.py": "validation/check_models.py",
    "check_prod_facts.py": "validation/check_prod_facts.py",
    "test_gemini_2.py": "validation/test_gemini_2.py",
    "test_web_search_agent.py": "validation/test_web_search_agent.py",

    # Deprecated
    "check_car_data.py": "deprecated/check_car_data.py",
    "direct_check.py": "deprecated/direct_check.py",
    "count_facts.py": "deprecated/count_facts.py",
}

ROOT_MOVES: Dict[str, Path] = {
    "debug_prompt_output.groovy": REPORTS_DIR / "prompt" / "debug_prompt_output.groovy",
    "debug_light_prompt_output.groovy": REPORTS_DIR / "prompt" / "debug_light_prompt_output.groovy",
    "debug_prompt_dev.groovy": REPORTS_DIR / "prompt" / "debug_prompt_dev.groovy",
    "debug_prompt_prod.groovy": REPORTS_DIR / "prompt" / "debug_prompt_prod.groovy",
    "comparison_report.txt": REPORTS_DIR / "prompt" / "comparison_report.txt",
    "debug_light_prompt_output.groovy": REPORTS_DIR / "prompt" / "debug_light_prompt_output.groovy",
    "FINAL_INTEGRATION_REPORT.md": ARCHIVE_REPORTS_DIR / "FINAL_INTEGRATION_REPORT.md",
    "HEXAGONAL_REFACTORING_SUMMARY.md": ARCHIVE_REPORTS_DIR / "HEXAGONAL_REFACTORING_SUMMARY.md",
    "INTEGRATION_INSTRUCTIONS.md": ARCHIVE_REPORTS_DIR / "INTEGRATION_INSTRUCTIONS.md",
    "REFACTORING_SUMMARY.md": ARCHIVE_REPORTS_DIR / "REFACTORING_SUMMARY.md",
}


def ensure_parent(path: Path, dry_run: bool):
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)


def move_file(src: Path, dest: Path, dry_run: bool, report: List[str]):
    if not src.exists():
        return
    ensure_parent(dest, dry_run)
    report.append(f"{src.relative_to(PROJECT_ROOT)} -> {dest.relative_to(PROJECT_ROOT)}")
    if dry_run:
        return
    shutil.move(str(src), str(dest))


def main():
    parser = argparse.ArgumentParser(description="Reorganize scripts and root clutter")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    args = parser.parse_args()
    dry_run = not args.apply

    report: List[str] = []

    # Move scripts
    for src_name, dest_rel in SCRIPT_MOVES.items():
        src = SCRIPTS_DIR / src_name
        dest = SCRIPTS_DIR / dest_rel
        move_file(src, dest, dry_run, report)

    # Move root clutter
    for filename, dest in ROOT_MOVES.items():
        src = PROJECT_ROOT / filename
        move_file(src, dest, dry_run, report)

    # Write report
    report_path = REPORTS_DIR / "debug" / "reorganize_report.txt"
    ensure_parent(report_path, dry_run)
    report_content = "\n".join(report) if report else "No files moved."
    if dry_run:
        print("[DRY-RUN] Planned moves:\n" + report_content)
    else:
        report_path.write_text(report_content, encoding="utf-8")
        print(f"✅ Reorganization complete. Report: {report_path}")


if __name__ == "__main__":
    main()

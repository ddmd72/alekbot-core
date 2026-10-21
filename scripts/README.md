# Scripts Directory

This folder contains all utility scripts grouped by domain. Use `--help` where available.

## Structure

```
scripts/
├── memory/            # Memory ops (migration, rebuild, sync)
│   ├── migrate.py
│   ├── rebuild.py
│   ├── sync.py
│   ├── deduplicate.py
│   ├── copy_to_dev.py
│   └── ops/          # Component operations
│       ├── diff.py
│       ├── rollback.py
│       ├── update_component.py
│       ├── upload_components.py
│       ├── upload_kernel.py
│       └── upload_prod.py
├── prompt/           # Prompt debugging/comparison
├── vectors/          # Vector / embedding diagnostics
│   └── ...
├── validation/       # Validation & test scripts
│   ├── check_models.py
│   ├── test_gemini_2.py
│   └── test_web_search_agent.py
├── deprecated/       # Legacy scripts (do not use)
│   ├── check_car_data.py
│   ├── direct_check.py
│   └── count_facts.py
└── reorganize_project.py # One-time reorganization helper
```

## Notes
- Prompt debug outputs are written to `reports/prompt/` (not root).
- Deprecated scripts should not be used without explicit approval.

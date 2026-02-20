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
│   ├── debug_system_prompt.py
│   ├── debug_light_prompt.py
│   └── debug_prompt_comparison.py
├── vectors/          # Vector / embedding diagnostics
│   ├── analyze_dev_vectors.py
│   ├── analyze_prod_vectors.py
│   ├── check_vector.py
│   ├── regenerate_vectors.py
│   └── ...
├── validation/       # Validation & test scripts
│   ├── check_models.py
│   ├── check_prod_facts.py
├── debug_firestore_latency.py # Firestore latency diagnostics
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

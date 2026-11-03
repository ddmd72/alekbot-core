# Utils — traps

- **Temp-file names are bounded in bytes, not characters** (`safe_temp_suffix()` in `file_conversion.py`): NAME_MAX is
  255 bytes and a Cyrillic character costs 2 (`docs/05_building_blocks/file_storage/README.md` §2.1.1).
- **With `LOGFIRE_CAPTURE_CONTENT=true`, Logfire's own exporter is off** (`send_to_logfire=False`): `_init_logfire` sends
  spans through `RedactingExporter` (`logfire_redacting_exporter.py`), which replaces file parts of `gen_ai.*` attributes
  with `file_omitted`; Cloud Trace is wrapped the same way. A span processor cannot do this (attributes are read-only at
  `on_end`). The document reader's provider calls additionally run under `sdk_spans_suppressed()`.

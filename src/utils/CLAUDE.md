# Utils — traps

- **Temp-file names are bounded in bytes, not characters** (`safe_temp_suffix()` in `file_conversion.py`): NAME_MAX is
  255 bytes and a Cyrillic character costs 2 (`docs/05_building_blocks/file_storage/README.md` §2.1.1).
- **Logfire's scrubbing never reaches `gen_ai.*.messages`**, so file bytes in an LLM call would be exported. With
  `LOGFIRE_CAPTURE_CONTENT=true` Logfire's own exporter is off (`send_to_logfire=False`) and `_init_logfire` sends spans
  through `RedactingExporter` (`logfire_redacting_exporter.py`), which replaces file parts with `file_omitted`; it wraps
  every exporter (Cloud Trace included). A span processor cannot do this (attributes are read-only at `on_end`). The
  document reader's provider calls also run under `sdk_spans_suppressed()`.

# Utils — traps

- **Temp-file names are bounded in bytes, not characters** (`safe_temp_suffix()` in `file_conversion.py`): NAME_MAX is
  255 bytes and a Cyrillic character costs 2 (`docs/05_building_blocks/file_storage/README.md` §2.1.1).

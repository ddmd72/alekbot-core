# Services — traps

- **Text the bot did not write is sent literally** (a stored fact, a drive path, a translated acknowledgement):
  `notify_raw(literal=True)` / `send_literal`, never through the formatter agent, which would rewrite or link it.
- **`_Transcripts` belongs to the bot:** the indexer overwrites every `.md` in it, so `UserDriveService` refuses chat writes
  into the folder; keep that guard (`docs/05_building_blocks/user_drive/README.md` §4.23).
- **Drive services take a `DriveHandle`, never a user id,** and get one from `DriveConnectionService` (the one owner of
  connections and the primary). Index, chunk, hint, transcript-job and sync-state keys are `{connection_id}:…`; the usage,
  initial build, transcript store, mirror roots/ledger and notes are user- or drive-scoped on purpose
  (`user_drive/README.md` §3, "Keys by scope"). A `drive:` ref without a tag is never resolved ("search again").
- **The parser child runs one at a time per adapter object** (`SubprocessContentExtractor`, `MAIN_PARSER_MAX_CHILDREN = 1`
  in `composition/document_service_setup.py`), and in the main service the parser, the PDF splitter and the document
  reader share that one slot across all users. Every wait for it is bounded by the open's `deadline_at`; past it
  `ParserBusy` raises, no child starts, and the open answers a "converter busy" note (`PARSER_UNAVAILABLE` / `busy`).
  Never add an unbounded `await` on the slot to an open path (`docs/05_building_blocks/file_conversion/README.md`).

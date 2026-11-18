# Services — traps

- **Text the bot did not write is sent literally** (a stored fact, a drive path, a translated acknowledgement):
  `notify_raw(literal=True)` / `send_literal`, never through the formatter agent, which would rewrite or link it.
- **`_Transcripts` belongs to the bot:** the indexer overwrites every `.md` in it, so `UserDriveService` refuses chat writes
  into the folder; keep that guard (`docs/05_building_blocks/user_drive/README.md` §4.23).
- **Drive services take a `DriveHandle`, never a user id,** and get one from `DriveConnectionService` (the one owner of
  connections and the primary). Index, chunk, hint, transcript-job and sync-state keys are `{connection_id}:…`; the usage,
  initial build, transcript store, mirror roots/ledger and notes are user- or drive-scoped on purpose
  (`user_drive/README.md` §3, "Keys by scope"). A `drive:` ref without a tag is never resolved ("search again").
- **Parsing goes through the sandbox light service** (`SandboxContentExtractor`, `docs/05_building_blocks/file_conversion/README.md`
  § Parser service); an outage is `ParserUnavailable` (`PARSER_UNAVAILABLE`), never the file's outcome. Never add an
  in-process parser to a deployed composition.
- **A cache filled after an awaited read and cleared by a sync invalidation guards its write-back with a version counter**
  read before the await and compared right before the write (no await between them), not an asyncio.Lock: a lock cannot stop
  a read that began before the invalidation from writing its old value back (`ChannelBindingService._versions`,
  `PromptAssemblyService._cache_epoch`; P15-S2).

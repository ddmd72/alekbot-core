# Drive re-read by comparing text (U5)

**Decision (owner, 2026-11-05).** A parser version bump re-reads every record stamped with an older `parser@N`, and the
re-read decides by comparing the new text with the stored chunks, not by selecting which file types to re-read:

- Equal chunk sequence (count, text, offsets) and equal text-derived record fields: the record is stamped with the new
  version and its `reader_family`. No new generation, no chunk write, no embedding, no profile or hint work.
- Anything different: the normal index path. Chunks are reused by text hash, so only chunks no stored chunk holds are
  embedded; profile and hints run only where their own fingerprint moved.
- An upgrade re-read (stale parser stamp, content not edited) never calls the paid reader. `reader@N` records are not
  touched. The pass is held off at deploy until the owner OKs the read-only probe's number.

**Why.** Records store the kind by name, not the reader that produced the text, so "re-read only PDFs, subtitles, HTML
and `.eml`" cannot be answered from the index. The comparison needs no migration and no new stored hash: the old text is
the stored chunks. It cannot skip a changed file (it only decides how a finished re-read commits) and cannot re-embed an
unchanged one.

**Rejected.**
- *A global re-read with full commits:* every unchanged file would write a full generation with vectors, copy its hints
  and re-grade its profile, for no change in the index.
- *A per-type version* (one version per reader): needs the reader recorded on every record first; the stamp would also
  have to be right per reader, and the child reported only one constant.
- *A stored record-level text hash:* old records lack it, and establishing it needs the same full re-read.

**Reliability.** The child reports the `parser_version` it ran and the adapters stamp from that report, so a record can
never carry a new version's stamp over an old version's text.

**Revisit when** per-type re-reads become useful: `reader_family` is now stored on every record the parser reads, so a
later bump that changes one reader can re-read only that family (an empty family means "any family").

Specs: `docs/10_rfcs/EXTERNAL_FILES_RFC.md` §7 U5; `docs/05_building_blocks/user_drive/README.md` §4.12.

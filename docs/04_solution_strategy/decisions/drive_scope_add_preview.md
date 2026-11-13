# Drive scope: a size confirmation before new folders enter the index

## Decision

When the Cabinet folder picker saves a scope that adds folders (or the whole drive) to a drive connection's indexed
scope, the page first calls `POST /api/drive/connections/<id>/scope/preview`. If something is new, the picker's foot
shows how many files by kind (documents, images, recordings, other) lie in how many folders, with Add and Cancel.
Cancel saves nothing and keeps the picker open with its selection. A failed count still lets the owner add.

What is counted: only what the saved scope does not already cover, with the indexer's own rules
(`domain/drive_scope_preview.py` reuses `item_rule`, `admission`, `size_cap` and the folder rules of `classify`), so the
preview matches what the indexer would take. The walk (`UserDriveService.scope_preview`) is read-only, shares no state
with the indexer and stops at a file cap, a folder cap or a deadline, then reports a lower bound (`capped`).
A repository folder (`.git`, `pyvenv.cfg` among its children) counts as opaque; the density probe is not mirrored, so a
very dense folder may be over-counted.

## Rejected alternatives

- **Size in bytes.** A Google-native document has no byte size, and the cost of indexing follows the text volume and
  the number of files, not the storage a file occupies.
- **Always-on counts in the picker.** Google gives no folder child counts, so every row would need a walk of its own.
- **A dollar estimate.** The cost per kind is not measured well enough to promise a figure.

## Triggers to revisit

Enough measured first-indexing runs to estimate dollars per kind: then the confirmation can show a range instead of
(or beside) the counts.

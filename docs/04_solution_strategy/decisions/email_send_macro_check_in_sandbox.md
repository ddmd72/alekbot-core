# The macro check of an email attachment runs in the sandbox

**Date:** 2026-11-05
**Status:** Live (U4b)
**Scope:** `send_file_to_user` and the drive save (`copy_file_to_drive`) from an `email:` ref (EXTERNAL_FILES_RFC D13); the sandbox light op `parse.container_facts`.

## Decision

D13 refuses active kinds from email attachments before they are sent, including Office or ODF files with macros. Seeing
a macro means listing a ZIP's parts. That read is done by the sandbox light service (`parse.container_facts`: the ZIP
central directory and `[Content_Types].xml` up to 1 MiB, a member cap), behind `ContainerFactsPort`. The main service
compares head bytes only and never opens a ZIP. When the op cannot answer (outage, timeout, `error`, `too_large`) the file
is refused as "could not be checked": fail closed. Owner decision O2, 2026-11-05.

## Extension (owner, 2026-11-05)

- **Legacy OLE2 Office files.** `.doc`, `.xls`, `.ppt` (and the same bytes under any name: the OLE2 magic) are an active
  kind when they carry a VBA project. The same op answers them: `status: ole` with `vba_storage`, true when the compound
  file's directory holds a `Macros`, `_VBA_PROJECT_CUR`, `VBA` or `_VBA_PROJECT` entry. Only the directory is read, capped
  at `MAX_ZIP_MEMBERS` entries (`too_large`, unchecked); `olefile` is already in the light image. A plain OLE2 file is not
  active; an encrypted OOXML package (an OLE2 file with `EncryptedPackage`) has no VBA storage and stays a checked, macro-free
  answer as before. Failing closed is unchanged: no answer, `error` or `too_large` is `unchecked_container`.
- **Save is checked too.** `DriveOperations.copy` with an `email:` source runs the same `active_kind_of` before the upload
  (on the attachment's name and on the name it would get on the drive) and refuses with the same note, now worded "not
  forwarded or saved". Drive-to-drive copies and chat files stay unchecked: D13 is about email only.

## Rejected

- **A central-directory scan in the main service.** Small, but it puts hostile container parsing back into the process
  the sandbox exists to keep clean, and the repo has just removed the last in-process parsers.
- **Name or mime only.** A renamed `.docm` or a wrong mime passes; the bytes must decide (D13).

## Why

The sandbox is the one place that handles hostile bytes (UNTRUSTED_CONTENT_SANDBOX_RFC); one more op on the light
service costs a client method, not a new trust boundary. Failing closed keeps a sandbox outage from becoming an unchecked
send.

## Consequence

The sandbox light service must run the new revision before the main service uses the op; the cloudbuild order deploys
main first, so a short window refuses every ZIP-family email send (`docs/07_deployment/README.md`).

## Revisit when

Send of email attachments is wanted while the sandbox is down, or the same facts are needed for drive or chat files.

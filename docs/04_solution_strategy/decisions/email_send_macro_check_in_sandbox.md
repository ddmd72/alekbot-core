# The macro check of an email attachment runs in the sandbox

**Date:** 2026-11-05
**Status:** Live (U4b)
**Scope:** `send_file_to_user` from an `email:` ref (EXTERNAL_FILES_RFC D13); the sandbox light op `parse.container_facts`.

## Decision

D13 refuses active kinds from email attachments before they are sent, including Office or ODF files with macros. Seeing
a macro means listing a ZIP's parts. That read is done by the sandbox light service (`parse.container_facts`: the ZIP
central directory and `[Content_Types].xml` up to 1 MiB, a member cap), behind `ContainerFactsPort`. The main service
compares head bytes only and never opens a ZIP. When the op cannot answer (outage, timeout, `error`, `too_large`) the file
is refused as "could not be checked": fail closed. Owner decision O2, 2026-11-05.

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

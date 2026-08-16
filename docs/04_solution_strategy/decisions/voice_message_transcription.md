# Voice messages are transcribed into the user's turn, not into file content

**Date:** 2026-08-16
**Status:** Live (Slack + Telegram, verified end to end)

## Decision

A voice message (Slack voice memo, Telegram voice note) is transcribed in
`ConversationHandler` **before** the file path runs, and the transcript becomes
`context.text` — the user's own message. The attachment is dropped from
`context.attachments` and never reaches GCS.

An *attached audio file* (an uploaded mp3) is unaffected: it still goes through
`convert_file_to_text` and its transcript is file content wrapped in `[File: …]`.

## Why not simply wire the port into the existing file path

Because the transcript would never reach session history, and history is the whole point.

`FileConversionService.process_attachment` returns a **reference-only** MessagePart — content
lives in GCS, not in the turn. So a spoken message would have left history holding the
synthetic "no text + attachment" fallback instead. Observed live before the fix: a Slack voice
memo recorded `Подивись на цей файл` as the user turn, with the transcript reachable only
through a file-ref resolution that is not persisted. Consolidation would have had nothing to
read, and speaking would not have made the exocortex smarter — which was the requirement.

Second benefit: file resolution currently downloads twice (`_resolve_file_refs` then
`open_file`), which on audio means paying for transcription twice. Short-circuiting avoids it.

## Provider and model

OpenAI `gpt-transcribe` (`OPENAI_TRANSCRIPTION_MODEL` overrides). It is the only family that
accepts the `languages` parameter — `gpt-4o-transcribe` does not, which alone settles the
choice. Without a key the port stays `None` and the pre-existing "transcription unavailable"
alert still fires.

## Spoken languages are a per-user setting

`UserBotConfig.voice_languages` — ISO-639-1 codes, ordered (first is primary), a **list**.
Cabinet: `PUT /api/user/voice-languages`; shape validated by `normalize_voice_languages()` in
`domain/language.py`.

Deliberately not `LanguageCode`: that enum is the closed set of *translated UI languages*
(uk/en/fr/es) and has no `ru` — it cannot express what a household speaks. A multilingual
speaker is the normal case here, not an edge one: one 22-second test message carried Russian,
English (`welcome home`, `Get`, `MCP`) and Ukrainian (`я тебе кохаю`), and all three survived.

This forced `languages` onto `AudioTranscriptionPort.transcribe` — the setting is per user and
per call, so it cannot live on a singleton adapter.

## Rejected

- **A vocabulary `prompt` with domain terms** — shipped first, then removed. OpenAI requires
  `prompt` to match the audio language, and the language is a user setting; a Russian carrier
  sentence hardcoded in an adapter is wrong on both counts.
- **`keywords`** (also `gpt-transcribe`-only) — no evidence yet that term biasing is needed.
  Revisit after real transcripts, not before.
- **Reusing `preferred_language`** — cannot express `ru`; also a different axis (UI vs speech).
- **xAI STT** (`/v1/stt`, $0.10/h vs OpenAI's ~$0.18–0.36/h) — cheaper, but no documented
  per-request language list. At this volume the price difference is cents per month, so the
  language lever wins. Revisit if that changes.

## Traps found live (all three cost a deploy)

1. **`.oga`.** Telegram serves voice at `…/voice/file_0.oga`, and the download layer names the
   temp file after that URL. The API infers the container from the **filename** and rejects
   `oga` (`400 Unsupported file format oga`) although it is valid Ogg. The adapter therefore
   derives the extension from the mime type; the temp path's extension is an artifact of the
   platform, not a fact about the audio.
2. **A failed transcription must still leave text in the turn.** The attachment has already
   left the file path, so the synthetic fallback cannot fire either — the Router received an
   empty query and returned `CANNOT_HANDLE`, dropping the request into the Quick fallback. The
   `[System: …]` note now becomes the turn itself.
3. **Slack's voice marker is the file name, not the mimetype.** Voice memos arrive as
   `audio_message.*` (and carry `subtype: slack_audio`); the mimetype has been observed as both
   `audio/mp4` and `video/mp4`, so a mimetype check silently loses one form. Telegram needs no
   heuristic — `message.voice` is its own type, distinct from `message.audio` (an uploaded
   track, which stays a file).

## Revisit if

Per-user vocabulary biasing becomes necessary, a cheaper provider gains a language list, or
voice volume grows enough that transcription cost stops being noise (it does not pass through
`TokenLedger` — a known, accepted blind spot).

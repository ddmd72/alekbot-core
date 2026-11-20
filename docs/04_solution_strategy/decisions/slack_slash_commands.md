# Slack slash commands run as `$` commands, queued, answered in the bot's DM

**Date:** 2026-11-20
**Status:** Live
**Scope:** `src/adapters/slack/command_route.py`, `HTTPModeAdapter._process_slash_command`

## Decision

`POST /slack/commands` verifies the Slack signature on the raw capped body, parses the form, enqueues a `slash_command`
event on the Slack intake queue and answers an empty 200. The worker runs `/<name> <text>` as the `$<name> <text>` command
through the same dispatch as a typed one, always in the bot's DM with the user (`conversations_open`). A new slash command
is Slack app config only (Request URL, scope `commands`, reinstall); the code knows no command names.

## Rejected alternatives

- **Run the command inline in the route.** Slack allows 3 s and a `$` command can take longer; Cloud Run throttles CPU
  after the response, so work started in the route stalls. The queue answers both.
- **Answer in the invoking channel.** A slash command can be typed in a DM between two humans: its id also starts with
  `D`, the bot is not in it, and `$geo`'s DM-only check would pass for it.
- **One route per command.** A code change per command, and a registry of names that the `$` handler already owns.

## Revisit when

A command must answer in a channel (then the channel becomes part of the event and the DM-only checks need a real
membership test, not an id prefix).

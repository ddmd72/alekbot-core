# `search_emails` adds fresh headers and exact keys

**Date:** 2026-11-13
**Status:** Built, pending deploy (the two `EMAILSEARCH_*` tokens ship by upload)
**Scope:** `EmailSearchAgent._handle_search_emails`, `EmailProviderPort.list_headers`, `GmailProviderAdapter`.

## Decision

Inside the same `search_emails` intent, code (not Smart) appends two live, header-only lists to the index results:
mail newer than the index (newest first, cap 100) and literal matches of up to 3 identifiers the extraction copied from
the request (any date, cap 20 per key). Promotions, spam and trash are never listed. No new LLM call.

## Why

The index holds a filtered subset (the classifier keeps factual mail) and lags by up to a day, so "mail of today",
"what X just forwarded" and a bare order or case number were not found. A bare identifier has no meaning to embed.

## The window

The fresh list opens at the older of the index watermark and 00:00 of yesterday in the user's timezone; no watermark means
no lower bound. A requested period narrows the start or sets a past end; a request that ends before the window opens skips the
list. Bounds are epoch seconds: Gmail reads a `YYYY/MM/DD` date as midnight Pacific time.

## Rejected

- A separate live-mailbox intent for Smart: Smart over-searches and its prompt is already long; the code decides instead.
- Live search over long periods by topic: keyword search misses synonyms and languages, the reason of RFC section 14.3, which stands for topics.

## Revisit when

Header lists are routinely truncated at 100, or key matches flood the result.

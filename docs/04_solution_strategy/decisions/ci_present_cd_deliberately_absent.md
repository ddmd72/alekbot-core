# Decision: CI yes, CD (auto-deploy) deliberately no

**Date:** 2026-06-03
**Status:** Accepted
**Context:** A ruff lint gate + GitHub Actions CI were added (commit `33fb214`): `make check`
(ruff + unit/architecture tests) now runs on every push to `main` and every PR. The natural
follow-on question — should a green CI also trigger an automatic deployment (CD)? — is answered
here so "no auto-deploy" reads as a choice, not an omission.

**Decision:** Run CI (lint + tests) automatically on push/PR. Do **not** auto-deploy. Deployment
stays manual via `make deploy-dev` / `make deploy` (`gcloud builds submit`). There are zero Cloud
Build push-triggers; nothing ships without an explicit local command.

**Why:**
- **Solo dev, single live environment, no staging.** Production is the developer's own exocortex,
  used daily. There is no buffer between a merge and the thing being relied on.
- **Green CI ≠ behaviorally safe for an LLM app.** Unit tests mock all I/O; they cannot catch
  prompt regressions, real provider behavior, or model drift. Auto-shipping every green `main` to
  the only prod would be risk without payoff.
- **Manual deploy = controlled moment.** The developer decides when prod changes. No release-velocity
  pressure (no team, ~$100/mo budget) makes that control worth more than the automation.

**Rejected alternatives:**
- *Auto-deploy on green `main`:* no staging to absorb a bad-but-green change; single prod is too
  exposed.
- *Deploy workflow gated by manual GitHub-Actions approval:* adds a GCP-auth + secrets surface to
  CI for marginal gain over the existing local `make deploy`.
- *Leave it undocumented:* reads as "didn't get to CD" rather than a deliberate posture.

**Trigger to revisit:** a genuinely separate staging/prod environment, a second contributor, or
e2e coverage that actually validates runtime LLM behavior — any of which would make automated
delivery a net win.

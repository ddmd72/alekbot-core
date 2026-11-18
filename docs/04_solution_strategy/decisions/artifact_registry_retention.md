# Artifact Registry retention: delete versions older than 14 days, keep the 5 newest

Status: decided 2026-11-17 (owner). Script: `scripts/infrastructure/artifact_registry_retention.py`.

## Context

`make deploy` pushes every image under one moving tag (`:latest`); each push leaves the previous version untagged and
the image registry grows without bound. All Cloud Run services and jobs of the bot deploy from that registry. Rollback is
needed only while a deploy is fresh and for a few days after it.

## Decision

- One policy on every repository that holds the images of a Cloud Run service or job: `DELETE` versions older than
  14 days plus `KEEP` the 5 most recent versions of each package. KEEP wins over DELETE, so 5 versions always survive,
  however old.
- The policy lives in git: the script sets it (`gcloud artifacts repositories set-cleanup-policies … --no-dry-run`), so
  it is reviewed and repeatable instead of being a console setting nobody sees.
- The existing backlog is cleaned once, explicitly, by the same script (`--apply`), not left to the policy to drain.
- Before deleting anything the script refuses (exit non-zero, nothing changed) when an image that serves traffic, belongs
  to one of the 3 newest revisions of a service or is a job's image is in the delete set. Older revisions only get a
  warning: they lose rollback, by design.

## Rejected

- **KEEP-only policy**: deletes nothing; a delete rule is required for any version to go.
- **Console-only policy**: invisible from code, drifts, cannot be reviewed.
- **A long rollback history** (many versions or months): rollback is needed only right after a deploy; the cost is storage
  that grows with every deploy.

## Revisit when

- A rollback target older than 14 days is ever needed (raise the window), or deploys become rare enough that 5 versions
  span weeks of history.
- A service or job is added that deploys from another registry (the script discovers packages from Cloud Run; pass
  `--repository LOCATION/REPO` for anything it cannot see).
- Registry storage cost is again a visible line, or Google changes how cleanup policies treat `gcr.io`-domain repositories.

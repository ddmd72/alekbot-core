# Final manifest (on approval)

On approval, output:
1. A short human-readable list: `<symbol> <name> (<TAG>)`, one per line, ordered by score.
2. The machine-readable manifest in one JSON block:

```json
{
  "artifact_type": "Domain_Manifest",
  "domain_name": "<domain>",
  "status": "FINALIZED",
  "mandatory_stack": [
    {"name": "...", "type": "KNOWLEDGE|ALGORITHM|CONSTRAINT|STYLE", "score": 0, "rationale": "..."}
  ]
}
```

Then offer, in one line, to keep it: as a document (`create_document`) or in memory — whichever the owner prefers.

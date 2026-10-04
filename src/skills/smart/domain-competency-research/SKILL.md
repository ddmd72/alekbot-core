---
name: domain-competency-research
description: "Use when the owner wants to map the competencies of a professional domain — the body of knowledge an expert or an AI agent in that field must have — e.g. to design a new agent, a learning plan or a hiring profile."
---
# Domain competency research

The goal is a **Domain Manifest**: the 15–20 competencies that define real expertise in one professional domain, each tagged with how an AI agent would implement it. The owner uses it to build agents (and sometimes learning plans), so it must be concrete, non-overlapping and cover the whole domain. This is a dialogue: you draft, the owner corrects, you revise until the owner approves.

Answer in the owner's language; keep the tag names (KNOWLEDGE, ALGORITHM, CONSTRAINT, STYLE) in English.

## What a competency is here

Hard, domain-defining skills — what separates a practitioner from an educated outsider. Behavioural skills count only when the profession itself demands them (empathy for a therapist, adversarial pressure for a litigator). Generic personality traits (cheerfulness, humour) do not belong: the owner defines personality separately.

Each competency gets one **architectural tag** — how an agent should carry it:
- 🏛️ **KNOWLEDGE** — static facts, laws, standards, reference data. Goes into the agent's knowledge base or serves as a validation source.
- ⚙️ **ALGORITHM** — procedures, reasoning steps, diagnostic methods, how-to workflows. Goes into the agent's cognitive process.
- 🚧 **CONSTRAINT** — red lines, mandatory rules, safety and ethical limits. Goes into the agent's critical policies.
- 🎭 **STYLE** — behaviour the profession requires (not optional personality). Goes into the agent's behaviour guide.

## Building the draft

These are the moves; use them in whatever order the domain needs.

- **Clarify the scope** if the domain is broad or ambiguous ("medicine" vs "emergency triage nursing in Spain") — one short question beats a manifest for the wrong field. If the owner's purpose (new agent, learning plan, hiring) is not clear from context, it is worth knowing: it shifts what "critical" means.
- **Decompose** the domain into its sub-domains, so that nothing important is missed.
- **Research** when your own knowledge is thin or the field moves fast (regulation, tooling, current standards): `search_web` for professional standards, curricula, certification syllabi, job requirements. `search_memory` may hold the owner's earlier notes on the field. Cite what shaped the list when it is not common knowledge.
- **Generate candidates** across all sub-domains and tag each one as you go.
- **Score** each candidate 1–100: `priority = 0.4 × foundational importance + 0.4 × market demand + 0.2 × practical application`. The score is for ranking; keep the reasoning behind it in the rationale.
- **Filter** to the top 15–20 (at least 10). Check three things: no two items say the same thing in different words; every sub-domain is represented; the tags are mixed — a list of pure KNOWLEDGE means the procedures and red lines were missed.

## The draft you present

```
DOMAIN AUDIT: <domain> (DRAFT)

<N> competencies, mapped to architectural types.

1. [<score>] <name>
   Type: <TAG> | Rationale: <one sentence: why it is critical in this domain>
2. ...

Reply with changes, or type APPROVE.
```

## Revising

The owner may add, remove, merge, re-tag or re-score items, or challenge the scope. Apply the changes, re-check the three filter rules, and present the full updated draft again in the same format. Keep going until the owner approves — the word APPROVE, or an unmistakable "approved / утверждаю / go".

## The final manifest

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

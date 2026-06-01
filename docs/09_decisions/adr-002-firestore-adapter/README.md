# ADR-002: Firestore as Adapter Behind Ports

## Status
PROPOSED (placeholder)

## Context
The system must remain cloud-agnostic; storage should be replaceable.

## Decision
Model Firestore as the current adapter for FactRepository and SessionStore ports.

## Consequences
- ✅ Infrastructure invariance
- ✅ Easier migration to other datastores
- ❌ Additional abstraction to maintain

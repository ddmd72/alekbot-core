# mypy: strict on the pure layers, informational on adapters (2026-10-07)

**Decision.** `make check` (and CI) runs `mypy --strict` on `src/domain` + `src/ports` (`mypy.ini`).
`src/adapters` is checked by `make check-types` (`mypy-adapters.ini`, lenient) which prints an error
count and never fails. `services/`, `agents/`, `handlers/` are not type-checked yet.

**Why.** The codebase is AI-pair-programmed; tests catch behaviour, not contract drift (a wrong
annotation, an unresolvable forward reference, `None` assigned to a `list` field). The two pure layers
have no third-party SDKs, so strict mode reports our code, not the libraries'. The first run found 81
errors in 32 files — mostly bare generics, plus a real unresolvable `SecurityPort` reference, an
unhandled `FinishReason | None`, and `SearchConfig` carrying a `None` state that `__post_init__` papered
over (now `default_factory`).

**Rejected.**
- Strict everywhere at once — adapters wrap SDKs with partial stubs (117 lenient errors already; strict would be mostly noise).
- Report-only for every layer — a gate that never fails is read once and ignored.
- `# type: ignore` on the `SearchConfig` None defaults — hid a real design smell (7 lines).

**Update 2026-10-07 (same day).** The 117 adapter findings were fixed, then `check_untyped_defs` was turned on
(6 more): `make check-types` is at **0 errors**, with one module excluded — the frozen, unreferenced,
untested `google_tasks_adapter` (drifted from the Task domain; cannot be repaired without the API).
Real findings among them: `RealtimeSessionPort.receive_events` was declared `async def` although both
adapters are async generators (and the only caller does `async for`); `gather(return_exceptions=True)`
results were filtered with `Exception`, letting a `CancelledError` reach `enumerate()`; the legacy
`PromptComponentRepository` port was still the 3-level v1 contract; an xAI/OpenAI audio event with no
payload built an `AudioFrame(None)`. Not gated yet only because ~13 dependencies are unpinned — a new
SDK release could break CI with errors that are not ours.

**Revisit when.** The unpinned dependencies are pinned (then add `check-types` to `make check`); or a
contract-drift bug lands in `services/`/`agents/` (extend the gate there).

import dataclasses
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..domain.entities import FactEntity, normalize_fact_taxonomy
from ..domain.exceptions import FactBornDueError, FactExpiryError, FactResolutionError, FactWriteConflict
from ..domain.fact_expiry import EXPIRY_IGNORED_ON_CLOSE
from ..domain.fact_lifecycle import CONSOLIDATION_ACCEPTS, superseded_closure
from ..domain.fact_review_answers import history_fact, history_moved_refusal, history_refusal, history_trail
from ..domain.fact_tool_result import (
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_MERGED,
    expiry_refusal_tags,
    update_result,
    with_notes,
)
from ..domain.fact_update import ParsedFactUpdate, parse_fact_update, typed_update_refusal
from ..domain.fact_update_outcome import FactUpdateOutcome, UpdateRefusal
from ..domain.fact_update_lifecycle import finish_fact_update
from ..domain.fact_update_plan import (
    FactUpdatePlan,
    concurrent_update_refusal,
    merge_source_moved_refusal,
    plan_fact_update,
    successor_refusal,
)
from ..domain.fact_write_context import FactWriteContext, reported_date_for_merge, reported_date_for_new
from ..domain.new_fact_attributes import NewFactAttributes, decided_attributes, new_fact_decision, parse_new_fact
from ..domain.search import SearchLimits, consolidation_hit
from ..domain.stored_fact import StoredFact
from ..ports.fact_management_port import FactManagementPort
from ..ports.fact_update_port import FactUpdatePort
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_write_port import FactWritePort
from ..ports.search_enrichment_port import SearchEnrichmentPort
from ..utils.logger import logger


class FactManagementAdapter(FactManagementPort, FactUpdatePort):
    """
    Implementation of deliberate fact management tools (FactManagementPort, the model's) and of code's typed update
    (FactUpdatePort); both updates run one apply.

    Coordinates FactRepository, EmbeddingService, FactWriteService and
    SearchEnrichmentService to provide search/create/update/merge/discard
    operations for ConsolidationAgent v3.

    Session 2026-02-16: Deliberate Fact Management RFC
    Session 2026-02-20: Renamed from FirestoreFactManagementAdapter (does not
    access Firestore directly — orchestrates ports only).
    """

    def __init__(
        self,
        repository: FactRepository,
        embedding_service: EmbeddingService,
        fact_write_service: FactWritePort,
        search_enrichment_service: SearchEnrichmentPort,
        max_retries: int = 3
    ) -> None:
        self._repo = repository
        self._embedding = embedding_service
        self._fact_write_service = fact_write_service
        self._search_enrichment = search_enrichment_service
        self._max_retries = max_retries

        logger.info(
            "🔧 [FactManagement] Initialized with multi-vector search support "
            "(SearchEnrichmentService)"
        )

    async def search_existing_facts(
        self,
        keywords: List[str],
        primary_query: str,
        alternative_query: str = "",
        limit: int = 20,
        *,
        timezone: str,
    ) -> List[Dict[str, Any]]:
        """
        Search existing facts using multi-vector RRF strategy.
        
        Session 2026-02-16: Updated to use SearchEnrichmentService
        - Multi-vector search (text + metadata + tags)
        - RRF ranking for quality results
        - skip_semantic_dedup=True (keep ALL facts with different IDs)
        - NO domain filter (consolidation needs all relevant facts)
        
        Args:
            keywords: Domain keywords for tag-based search
            primary_query: Main semantic search phrase
            alternative_query: Alternative phrasing (optional)
            limit: Max results to return (default: 20)
            timezone: The account owner's IANA zone: `reported_date` and the dates of `expiry` / `due` are shown
                in his calendar

        Returns:
            List of fact dictionaries with fact_id, content, similarity, source, the taxonomy fields,
            reported_date, expiry (the stored decision in the shape the model sends, or None) and due
        """
        try:
            logger.info(
                f"🔍 [FactManagement] Multi-vector search: "
                f"keywords={len(keywords)}, primary_len={len(primary_query)}, limit={limit}"
            )
            
            # Call SearchEnrichmentService with consolidation mode
            enriched = await self._search_enrichment.enrich_context(
                keywords=keywords,
                search_phrase_1=primary_query,
                search_phrase_2=alternative_query or primary_query,
                limits=SearchLimits(
                    keyword_limit=7,
                    phrase_one_limit=10,
                    phrase_two_limit=10,
                    total_limit=limit,
                ),
                skip_semantic_dedup=True,  # Keep ALL facts with different IDs for MERGE!
            )

            # Convert EnrichedFact → Dict — taxonomy fields already carried by EnrichedFact
            results: List[Dict[str, Any]] = [consolidation_hit(ef, timezone) for ef in enriched.facts]

            # Log top 3 results (with safe similarity formatting)
            for i, result in enumerate(results[:3]):
                sim = result.get('similarity')
                sim_str = f"{sim:.3f}" if sim is not None else "N/A"
                logger.info(
                    f"   [{i+1}] fact_id={result['fact_id'][:8]}... "
                    f"similarity={sim_str} "
                    f"source={result['source']} "
                    f"domain={result['domain']} "
                    f"content_len={len(result['content'])}"
                )

            logger.info(
                f"✅ [FactManagement] Found {len(results)} facts "
                f"(multi-vector RRF, semantic_dedup=SKIPPED, id_dedup_only=True)"
            )

            return results
            
        except Exception as e:
            logger.error(f"❌ [FactManagement] Search failed: {e}", exc_info=True)
            return []  # Graceful degradation

    async def create_fact(
        self, content: str, metadata: Dict[str, Any], *, context: FactWriteContext
    ) -> Dict[str, Any]:
        """
        Create new fact with validation and error handling.

        Args:
            content: Fact text
            metadata: Fact metadata (must include account_id, user_id, domain, etc.)
            context: The pass this write belongs to (actor, owner's zone, span, who decides dates)

        Returns:
            Result dict with fact_id, status, message (and note, refused when there is something to say)
        """
        try:
            # Fact text and metadata values are personal data: log shapes only.
            logger.info(
                f"🔧 [FactManagement] create_fact() TOOL CALL: "
                f"content_len={len(content)} metadata keys={list(metadata.keys())}"
            )

            # Validate required fields
            required = ["account_id", "user_id", "domain", "temporal_class", "context_priority"]
            missing = [f for f in required if f not in metadata]
            if missing:
                logger.error(f"❌ [FactManagement] Missing required fields: {missing}")
                return {
                    "fact_id": None,
                    "status": STATUS_FAILED,
                    "message": f"Missing required fields: {missing}"
                }

            # Domain-level normalization (was duplicated here — now in domain/entities.py)
            metadata = normalize_fact_taxonomy(metadata)

            attrs = parse_new_fact(self._new_fact_data(content, metadata), context.timezone)
            decision = new_fact_decision(attrs, "create_fact")
            now = datetime.now(timezone.utc)
            derived = await self._derived_from_dates(attrs, metadata["account_id"], context)
            reported, date_notes = reported_date_for_new(
                context, attrs.reported_date, derived, now, derived_named=len(attrs.derived_from)
            )
            notes = [*attrs.notes, *date_notes]
            attrs, due_note = decided_attributes(attrs, decision, reported, now, context)
            notes.extend([due_note] if due_note else [])

            entity = (await self._fact_write_service.build_facts(
                metadata["account_id"], metadata["user_id"], [attrs]
            ))[0]
            await self._repo.add_fact(entity)
            logger.info(f"✅ [FactManagement] Created fact (content_len={len(content)})")

            result: Dict[str, Any] = {
                "fact_id": entity.id,
                "status": STATUS_CREATED,
                "message": "Fact created successfully",
            }
            return _with_notes(result, notes, "create_fact")

        except FactExpiryError as e:
            logger.warning(f"⚠️  [FactManagement] create_fact refused (expiry): {e}")
            return {
                "fact_id": None,
                "status": STATUS_FAILED,
                **expiry_refusal_tags(isinstance(e, FactBornDueError)),
                "message": str(e),
            }
        except Exception as e:
            logger.error(f"❌ [FactManagement] Create fact error: {e}", exc_info=True)
            return {
                "fact_id": None,
                "status": STATUS_FAILED,
                "message": f"Error creating fact: {str(e)}"
            }

    async def _derived_from_dates(
        self, attrs: NewFactAttributes, account_id: str, context: FactWriteContext
    ) -> List[datetime]:
        """`reported_date` of each readable fact named by `derived_from` (same account). The domain decides whether a pass
        whose model dates do not count uses them, and says so when it does not."""
        dates: List[datetime] = []
        for fact_id in attrs.derived_from:
            source = await self._repo.get_fact_by_id(fact_id)
            if source is not None and source.account_id == account_id:
                dates.append(source.reported_date)
        return dates

    @staticmethod
    def _new_fact_data(content: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "content": content,
            "tags": metadata.get("tags", []),
            "type": metadata.get("type", "event"),
            "metadata": metadata.get("metadata", {}),
            "domain": metadata.get("domain"),
            "temporal_class": metadata.get("temporal_class"),
            "state": metadata.get("state"),
            "context_priority": metadata.get("context_priority"),
            "context": metadata.get("context"),
            "reported_date": metadata.get("reported_date"),
        }
        # Present only when sent: an absent expiry and a null one are different answers to the parser.
        data.update({key: metadata[key] for key in ("expiry", "derived_from") if key in metadata})
        return data

    async def update_fact(
        self,
        fact_id: str,
        updates: Dict[str, Any],
        account_id: str,
        *,
        context: FactWriteContext,
    ) -> Dict[str, Any]:
        """
        The model's update: `updates` parsed (`parse_fact_update`), then the one apply `apply_fact_update` uses.

        The parse runs inside the apply, after the fact is resolved, so a missing or unresolvable fact is reported
        before an invalid argument, as it always was.

        Args:
            fact_id: UUID of fact to update; a superseded id is followed to its current version
            updates: Fields to update (content, tags, metadata, state, etc.), as the model sends them
            account_id: Caller account; a fact of another account is reported as not found
            context: The pass this write belongs to (actor, owner's zone, span, who decides dates)

        Returns:
            Result dict with fact_id (the document written), status, version, message: the outcome of the one apply,
            rendered (update_result)
        """
        return update_result(
            await self._update(fact_id, account_id, context, lambda: parse_fact_update(updates, context.timezone))
        )

    async def apply_fact_update(
        self,
        fact_id: str,
        update: ParsedFactUpdate,
        account_id: str,
        *,
        context: FactWriteContext,
        evidence_at: Optional[datetime] = None,
        reason: Optional[str] = None,
        history: Optional[str] = None,
        expected_id: Optional[str] = None,
    ) -> FactUpdateOutcome:
        """
        Code's update: `update` already decided, with the inputs only code has. It is first held to the rules the
        model's arguments are parsed under (`typed_update_refusal`); a refused one is failed, nothing read or written.

        Args:
            evidence_at: The review's or the owner's evidence moment (finish_fact_update); the history fact's date
            reason: The code-built trail reason of a review or owner write (finish_fact_update)
            history: The text of a fact to create beside an archive (history_refusal, history_fact)
            expected_id: The exact document the caller decided on; a first resolution to another document is MOVED

        Returns:
            The typed outcome (update_fact renders the same outcome); history_fact_id with history
        """
        invalid = typed_update_refusal(update)
        if invalid is not None:
            logger.warning(f"⚠️  [FactManagement] update {fact_id[:8]} refused (invalid update): {invalid}")
            return FactUpdateOutcome.refused(fact_id, UpdateRefusal.INVALID, invalid)
        if history is not None:
            refusal = history_refusal(history, update, context, evidence_at)
            if refusal is not None:
                logger.warning(f"⚠️  [FactManagement] update {fact_id[:8]} refused (history): {refusal}")
                return FactUpdateOutcome.refused(fact_id, UpdateRefusal.HISTORY, refusal)
        return await self._update(
            fact_id, account_id, context, lambda: update, evidence_at=evidence_at, reason=reason, history=history,
            expected_id=expected_id,
        )

    async def _update(
        self,
        fact_id: str,
        account_id: str,
        context: FactWriteContext,
        parse: Callable[[], ParsedFactUpdate],
        *,
        evidence_at: Optional[datetime] = None,
        reason: Optional[str] = None,
        history: Optional[str] = None,
        expected_id: Optional[str] = None,
    ) -> FactUpdateOutcome:
        """The one apply of both entries: every refusal becomes a refused outcome. `parse` gives the update once, after
        the first resolution."""
        try:
            return await self._update_with_one_retry(
                fact_id, account_id, context, parse, evidence_at=evidence_at, reason=reason, history=history,
                expected_id=expected_id,
            )
        except FactExpiryError as e:
            logger.warning(f"⚠️  [FactManagement] update_fact {fact_id[:8]} refused (expiry): {e}")
            kind = UpdateRefusal.BORN_DUE if isinstance(e, FactBornDueError) else UpdateRefusal.EXPIRY
            return FactUpdateOutcome.refused(fact_id, kind, str(e))
        except FactResolutionError as e:
            logger.warning(f"⚠️  [FactManagement] Cannot resolve fact {fact_id[:8]}: {e}")
            return FactUpdateOutcome.refused(fact_id, UpdateRefusal.UNRESOLVED, str(e))
        except FactWriteConflict:
            logger.warning(f"⚠️  [FactManagement] Fact {fact_id[:8]} changed concurrently twice; update refused")
            return FactUpdateOutcome.refused(
                fact_id, UpdateRefusal.CONFLICT, f"fact {fact_id} changed concurrently; search again"
            )
        except Exception as e:
            logger.error(f"❌ [FactManagement] Update fact error: {e}", exc_info=True)
            return FactUpdateOutcome.refused(fact_id, UpdateRefusal.ERROR, f"Error updating fact: {str(e)}")

    async def _update_with_one_retry(
        self,
        fact_id: str,
        account_id: str,
        context: FactWriteContext,
        parse: Callable[[], ParsedFactUpdate],
        *,
        evidence_at: Optional[datetime],
        reason: Optional[str],
        history: Optional[str],
        expected_id: Optional[str] = None,
    ) -> FactUpdateOutcome:
        logger.debug(f"🔄 [FactManagement] Updating fact {fact_id[:8]}...")
        parsed: Optional[ParsedFactUpdate] = None
        embedded: Optional[Tuple[List[str], List[List[float]]]] = None
        built_history: Optional[FactEntity] = None   # the history fact, built and embedded once, without its trail
        shown_text: Optional[str] = None             # the text of `fact_id`, which the history describes
        first_resolved_id: Optional[str] = None
        attempt = 0
        while True:
            attempt += 1
            # The pass's row decides which stored states its own target may be in (the review and the owner reach stale).
            stored = await self._repo.resolve_current(fact_id, account_id, context.accepts)
            if stored is None:
                logger.warning(f"⚠️  [FactManagement] Fact not found: {fact_id[:8]}")
                return FactUpdateOutcome.refused(fact_id, UpdateRefusal.NOT_FOUND, "Fact not found")
            if parsed is None:
                parsed = parse()
            if first_resolved_id is None:
                first_resolved_id = stored.fact.id
                if expected_id is not None and stored.fact.id != expected_id:
                    logger.warning(f"⚠️  [FactManagement] Fact {fact_id[:8]} is no longer the document decided on; update refused")
                    return FactUpdateOutcome.refused(
                        fact_id, UpdateRefusal.MOVED, f"fact {fact_id} changed concurrently; search again"
                    )
            else:
                moved = concurrent_update_refusal(fact_id, first_resolved_id, stored.fact.id, parsed)
                if moved is not None:
                    logger.warning(f"⚠️  [FactManagement] Fact {fact_id[:8]} moved forward between attempts; update refused")
                    return FactUpdateOutcome.refused(fact_id, UpdateRefusal.MOVED, moved)
            if history is not None:
                if shown_text is None:
                    shown_text = await self._requested_text(fact_id, stored)
                refusal = history_moved_refusal(shown_text, stored.fact, fact_id)
                if refusal is not None:
                    logger.warning(f"⚠️  [FactManagement] update_fact {fact_id[:8]} refused (history): {refusal}")
                    return FactUpdateOutcome.refused(fact_id, UpdateRefusal.HISTORY, refusal)
            update = await self._with_resolved_successor(parsed, stored, account_id)
            now = datetime.now(timezone.utc)
            finish = finish_fact_update(
                plan_fact_update(stored.fact, update, now), stored.fact, update, context, now,
                evidence_at=evidence_at, reason=reason,
            )
            plan = finish.plan
            history_entity: Optional[FactEntity] = None
            if history is not None:
                if evidence_at is None or reason is None:
                    raise RuntimeError("history_refusal and finish_fact_update let no history through without both")
                # Built once: a retry lands on a document with the text the history describes (history_moved_refusal).
                if built_history is None:
                    attrs = history_fact(stored.fact, history, evidence_at, context.timezone)
                    built_history = (await self._fact_write_service.build_facts(
                        account_id, stored.fact.created_by_user_id, [attrs]
                    ))[0]
                history_entity = history_trail(built_history, stored.fact.id, now, context.actor)
            try:
                if history_entity is not None:
                    # finish_fact_update built the archive (`archive_fields`, history_refusal let only an archive
                    # through); the repository writes it beside the new fact, in one transaction.
                    await self._repo.archive_and_add(stored, plan.fields, history_entity)
                    written = stored.fact
                elif plan.kind == "version":
                    new_fact, embedded = await self._complete_version(plan, embedded)
                    closure = superseded_closure(stored.fact, new_fact, now, context.actor)
                    await self._repo.write_superseding(new_fact, [(stored, closure)])
                    written = new_fact
                else:
                    await self._repo.update_fact_fields(stored, plan.fields)
                    written = stored.fact
            except FactWriteConflict:
                logger.warning(
                    f"⚠️  [FactManagement] Fact {fact_id[:8]} conflicted on write (attempt {attempt})"
                )
                if attempt == 2:
                    raise
                continue
            logger.info(f"✅ [FactManagement] Updated fact {written.id[:8]} to version {written.version}")
            _log_ignored_expiry(finish.notes, "update_fact")
            return FactUpdateOutcome(
                fact_id=fact_id,
                written_id=written.id,
                version=written.version,
                current_id=stored.fact.id,
                history_fact_id=history_entity.id if history_entity is not None else None,
                notes=tuple(finish.notes),
            )

    async def _requested_text(self, fact_id: str, stored: StoredFact) -> str:
        """The text of `fact_id` itself, which `stored` was resolved from. A requested document that cannot be read
        gives "", which no stored text equals, so a moved history archive is refused."""
        if stored.fact.id == fact_id:
            return stored.fact.text
        requested = await self._repo.get_fact_by_id(fact_id)
        return requested.text if requested is not None else ""

    async def _with_resolved_successor(
        self, update: ParsedFactUpdate, stored: StoredFact, account_id: str
    ) -> ParsedFactUpdate:
        if update.superseded_by is None:
            return update
        try:
            successor = await self._repo.resolve_current(update.superseded_by, account_id, CONSOLIDATION_ACCEPTS)
        except FactResolutionError as e:
            reason = str(e).removesuffix("; search again")
            raise FactResolutionError(f"successor {reason}; search again", current_id=e.current_id) from e
        if successor is None:
            raise FactResolutionError("successor fact not found")
        same = successor_refusal(successor.fact.id, stored.fact.id)
        if same is not None:
            raise ValueError(same)
        return dataclasses.replace(update, superseded_by=successor.fact.id)

    async def _complete_version(
        self, plan: FactUpdatePlan, embedded: Optional[Tuple[List[str], List[List[float]]]]
    ) -> Tuple[FactEntity, Optional[Tuple[List[str], List[List[float]]]]]:
        """Fill the version's vectors; an embedding already made for the same texts is reused."""
        if plan.version_fact is None:
            raise RuntimeError(f"a {plan.kind} plan carries no version document")
        if not plan.vectors_stale:
            return plan.version_fact, embedded
        texts = plan.embedding_texts or []
        if embedded is None or embedded[0] != texts:
            vectors = list(await self._embedding.get_embeddings_batch(texts, "RETRIEVAL_DOCUMENT"))
            if len(vectors) != 3 or any(vector is None for vector in vectors):
                raise RuntimeError("The embedding service returned no vector for a fact")
            embedded = (texts, vectors)
        vector, tags_vector, metadata_vector = embedded[1]
        filled = plan.version_fact.model_copy(
            update={"vector": vector, "tags_vector": tags_vector, "metadata_vector": metadata_vector}
        )
        return filled, embedded

    async def merge_facts(
        self,
        fact_ids: List[str],
        merged_content: str,
        metadata: Dict[str, Any],
        *,
        context: FactWriteContext,
    ) -> Dict[str, Any]:
        """
        Merge multiple facts into one in a single transaction.

        Args:
            fact_ids: List of fact UUIDs to merge; each is followed to its current version
            merged_content: Combined fact text
            metadata: Metadata for new merged fact
            context: The pass this write belongs to (actor, owner's zone, span, who decides dates)

        Returns:
            Result dict with new_fact_id, old_fact_ids, status, message
        """
        try:
            return await self._merge_with_one_retry(fact_ids, merged_content, metadata, context)
        except FactExpiryError as e:
            logger.warning(f"⚠️  [FactManagement] merge_facts refused (expiry): {e}")
            return {**self._merge_failed(str(e)), **expiry_refusal_tags(isinstance(e, FactBornDueError))}
        except FactResolutionError as e:
            logger.warning(f"⚠️  [FactManagement] Merge refused: {e}")
            return self._merge_failed(str(e))
        except FactWriteConflict:
            logger.warning("⚠️  [FactManagement] Merge sources changed concurrently twice; merge refused")
            return self._merge_failed("a source changed concurrently; search again")
        except Exception as e:
            logger.error(f"❌ [FactManagement] Merge facts error: {e}", exc_info=True)
            return self._merge_failed(f"Error merging facts: {str(e)}")

    @staticmethod
    def _merge_failed(message: str) -> Dict[str, Any]:
        return {
            "new_fact_id": None,
            "old_fact_ids": [],
            "old_facts_state": "FAILED",
            "status": STATUS_FAILED,
            "message": message,
        }

    async def _merge_with_one_retry(
        self, fact_ids: List[str], merged_content: str, metadata: Dict[str, Any], context: FactWriteContext
    ) -> Dict[str, Any]:
        logger.info(f"🔀 [FactManagement] Merging {len(fact_ids)} facts...")
        account_id = metadata.get("account_id")
        metadata = normalize_fact_taxonomy(metadata)
        attrs = parse_new_fact(self._new_fact_data(merged_content, metadata), context.timezone)
        decision = new_fact_decision(attrs, "merge_facts")

        notes: List[str] = []
        merged: Optional[FactEntity] = None
        first_ids: Optional[List[str]] = None
        attempt = 0
        while True:
            attempt += 1
            sources = await self._resolve_sources(fact_ids, account_id)
            ids = [source.fact.id for source in sources]
            if first_ids is None:
                first_ids = ids
            else:
                moved = merge_source_moved_refusal(first_ids, ids)
                if moved is not None:
                    logger.warning("⚠️  [FactManagement] A merge source moved forward between attempts")
                    return self._merge_failed(moved)
            if not sources:
                logger.error("❌ [FactManagement] No facts successfully superseded")
                return self._merge_failed("Failed to supersede any facts")
            if merged is None:
                now = datetime.now(timezone.utc)
                reported, date_notes = reported_date_for_merge(
                    context, attrs.reported_date, [source.fact.reported_date for source in sources], now
                )
                notes = [*attrs.notes, *date_notes]
                decided, due_note = decided_attributes(attrs, decision, reported, now, context)
                notes.extend([due_note] if due_note else [])
                merged = (await self._fact_write_service.build_facts(
                    metadata["account_id"], metadata["user_id"], [decided]
                ))[0]
            now = datetime.now(timezone.utc)
            closures = [(source, superseded_closure(source.fact, merged, now, context.actor)) for source in sources]
            try:
                await self._repo.write_superseding(merged, closures)
            except FactWriteConflict:
                logger.warning(f"⚠️  [FactManagement] Merge conflicted on write (attempt {attempt})")
                if attempt == 2:
                    raise
                continue
            logger.info(f"✅ [FactManagement] Merged {len(ids)} facts into new fact")
            result: Dict[str, Any] = {
                "new_fact_id": merged.id,
                "old_fact_ids": ids,
                "old_facts_state": "SUPERSEDED",
                "status": STATUS_MERGED,
                "message": f"Merged {len(ids)} facts",
            }
            return _with_notes(result, notes, "merge_facts")

    async def _resolve_sources(self, fact_ids: List[str], account_id: Optional[str]) -> List[StoredFact]:
        """The current document of each source, input order, duplicates after forwarding dropped. A missing or
        foreign id is skipped; a chain that ends elsewhere raises FactResolutionError."""
        sources: Dict[str, StoredFact] = {}
        for fact_id in fact_ids:
            stored = await self._repo.resolve_current(fact_id, account_id, CONSOLIDATION_ACCEPTS)
            if stored is None:
                logger.warning(f"⚠️  [FactManagement] Fact not found for merge: {fact_id[:8]}")
                continue
            sources.setdefault(stored.fact.id, stored)
        return list(sources.values())


def _with_notes(result: Dict[str, Any], notes: Sequence[str], tool: str) -> Dict[str, Any]:
    """Put the notes into the tool result as `note`, and log an ignored expiry (the domain does not log)."""
    _log_ignored_expiry(notes, tool)
    return with_notes(result, notes)


def _log_ignored_expiry(notes: Sequence[str], tool: str) -> None:
    if EXPIRY_IGNORED_ON_CLOSE in notes:
        logger.info(f"ℹ️  [FactManagement] {tool}: {EXPIRY_IGNORED_ON_CLOSE}")

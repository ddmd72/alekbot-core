import dataclasses
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Sequence, Tuple

from ..domain.entities import FactEntity, normalize_fact_taxonomy
from ..domain.exceptions import FactBornDueError, FactExpiryError, FactResolutionError, FactWriteConflict
from ..domain.fact_expiry import EXPIRY_IGNORED_ON_CLOSE, EXPIRY_SHAPES, NOTHING_WRITTEN, Expiry
from ..domain.fact_lifecycle import (
    CLOSING_STATES,
    CONSOLIDATION_ACCEPTS,
    born_due,
    check_pin,
    compute_expiry,
    expiry_pinned,
)
from ..domain.fact_update import ParsedFactUpdate, parse_fact_update
from ..domain.fact_update_lifecycle import finish_fact_update
from ..domain.fact_update_plan import FactUpdatePlan, plan_fact_update
from ..domain.local_time import local_moment
from ..domain.fact_write_context import (
    FactWriteContext,
    as_utc,
    kept_reported_date_note,
    reported_date_for_merge,
    reported_date_for_new,
)
from ..domain.new_fact_attributes import NewFactAttributes, parse_new_fact
from ..domain.search import SearchLimits
from ..domain.stored_fact import StoredFact
from ..ports.fact_management_port import FactManagementPort
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_write_port import FactWritePort
from ..ports.search_enrichment_port import SearchEnrichmentPort
from ..utils.logger import logger


def _in_owner_zone(stamp: Optional[str], zone: str) -> Optional[str]:
    """An ISO timestamp as an ISO timestamp with the owner's offset; text that is not ISO is returned as stored."""
    if not stamp:
        return stamp
    try:
        return local_moment(as_utc(stamp), zone).isoformat(timespec="seconds")
    except ValueError:
        return stamp


class FactManagementAdapter(FactManagementPort):
    """
    Implementation of deliberate fact management tools.

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
            results: List[Dict[str, Any]] = []
            for ef in enriched.facts:
                expiry, due = ef.expiry_decision(timezone)
                results.append({
                    "fact_id": ef.fact_id,
                    "content": ef.content,
                    "similarity": ef.relevance_score,
                    "source": ef.source,
                    "type": ef.fact_type,
                    "domain": ef.domain,
                    "temporal_class": ef.temporal_class,
                    "state": ef.state,
                    "context_priority": ef.context_priority,
                    "tags": ef.tags or [],
                    "metadata": ef.metadata or {},
                    "reported_date": _in_owner_zone(ef.reported_date, timezone),
                    "expiry": expiry,
                    "due": due,
                    "version": ef.version,
                })

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
                    "status": "failed",
                    "message": f"Missing required fields: {missing}"
                }

            # Domain-level normalization (was duplicated here — now in domain/entities.py)
            metadata = normalize_fact_taxonomy(metadata)

            attrs = parse_new_fact(self._new_fact_data(content, metadata), context.timezone)
            decision = _required_decision(attrs, "create_fact")
            now = datetime.now(timezone.utc)
            derived = await self._derived_from_dates(attrs, metadata["account_id"], context)
            reported, date_note = reported_date_for_new(
                context, attrs.reported_date, derived, now, derived_named=len(attrs.derived_from)
            )
            notes = [*attrs.notes, *([date_note] if date_note else [])]
            attrs, due_note = _decided(attrs, decision, reported, now, context)
            notes.extend([due_note] if due_note else [])

            entity = (await self._fact_write_service.build_facts(
                metadata["account_id"], metadata["user_id"], [attrs]
            ))[0]
            await self._repo.add_fact(entity)
            logger.info(f"✅ [FactManagement] Created fact (content_len={len(content)})")

            result: Dict[str, Any] = {
                "fact_id": entity.id,
                "status": "created",
                "message": "Fact created successfully",
            }
            return _with_notes(result, notes, "create_fact")

        except FactExpiryError as e:
            logger.warning(f"⚠️  [FactManagement] create_fact refused (expiry): {e}")
            return {"fact_id": None, "status": "failed", **_expiry_refusal(e), "message": str(e)}
        except Exception as e:
            logger.error(f"❌ [FactManagement] Create fact error: {e}", exc_info=True)
            return {
                "fact_id": None,
                "status": "failed",
                "message": f"Error creating fact: {str(e)}"
            }

    async def _derived_from_dates(
        self, attrs: NewFactAttributes, account_id: str, context: FactWriteContext
    ) -> List[datetime]:
        """`reported_date` of each readable fact named by `derived_from` (same account); only a cleanup pass uses them."""
        if context.model_dates:
            return []
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
        self, fact_id: str, updates: Dict[str, Any], account_id: str, *, context: FactWriteContext
    ) -> Dict[str, Any]:
        """
        Update existing fact with error handling.

        Args:
            fact_id: UUID of fact to update; a superseded id is followed to its current version
            updates: Fields to update (content, tags, metadata, state, etc.)
            account_id: Caller account; a fact of another account is reported as not found
            context: The pass this write belongs to (actor, owner's zone, span, who decides dates)

        Returns:
            Result dict with fact_id (the document written), status, version, message
        """
        try:
            return await self._update_with_one_retry(fact_id, updates, account_id, context)
        except FactExpiryError as e:
            logger.warning(f"⚠️  [FactManagement] update_fact {fact_id[:8]} refused (expiry): {e}")
            return {"fact_id": fact_id, "status": "failed", **_expiry_refusal(e), "message": str(e)}
        except FactResolutionError as e:
            logger.warning(f"⚠️  [FactManagement] Cannot resolve fact {fact_id[:8]}: {e}")
            return {"fact_id": fact_id, "status": "failed", "message": str(e)}
        except FactWriteConflict:
            logger.warning(f"⚠️  [FactManagement] Fact {fact_id[:8]} changed concurrently twice; update refused")
            return {
                "fact_id": fact_id,
                "status": "failed",
                "message": f"fact {fact_id} changed concurrently; search again",
            }
        except Exception as e:
            logger.error(f"❌ [FactManagement] Update fact error: {e}", exc_info=True)
            return {
                "fact_id": fact_id,
                "status": "failed",
                "message": f"Error updating fact: {str(e)}"
            }

    async def _update_with_one_retry(
        self, fact_id: str, updates: Dict[str, Any], account_id: str, context: FactWriteContext
    ) -> Dict[str, Any]:
        logger.debug(f"🔄 [FactManagement] Updating fact {fact_id[:8]}...")
        parsed: Optional[ParsedFactUpdate] = None
        embedded: Optional[Tuple[List[str], List[List[float]]]] = None
        first_resolved_id: Optional[str] = None
        attempt = 0
        while True:
            attempt += 1
            stored = await self._repo.resolve_current(fact_id, account_id, CONSOLIDATION_ACCEPTS)
            if stored is None:
                logger.warning(f"⚠️  [FactManagement] Fact not found: {fact_id[:8]}")
                return {"fact_id": fact_id, "status": "not_found", "message": "Fact not found"}
            if parsed is None:
                parsed = parse_fact_update(updates, context.timezone)
            if first_resolved_id is None:
                first_resolved_id = stored.fact.id
            elif stored.fact.id != first_resolved_id and (parsed.content is not None or parsed.tags is not None):
                # content and tags replace whole: written over a version the model never saw, they would drop its change
                logger.warning(f"⚠️  [FactManagement] Fact {fact_id[:8]} moved forward between attempts; update refused")
                return {
                    "fact_id": fact_id,
                    "status": "failed",
                    "message": f"fact {fact_id} changed concurrently; search again",
                }
            update = await self._with_resolved_successor(parsed, stored, account_id)
            now = datetime.now(timezone.utc)
            finish = finish_fact_update(plan_fact_update(stored.fact, update, now), stored.fact, update, context, now)
            plan = finish.plan
            try:
                if plan.kind == "version":
                    new_fact, embedded = await self._complete_version(plan, embedded)
                    await self._repo.write_superseding(new_fact, [stored], now, actor=context.actor)
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
            result = {
                "fact_id": written.id,
                "status": "updated",
                "version": written.version,
                "message": "Fact updated successfully",
            }
            _with_notes(result, finish.notes, "update_fact")
            if stored.fact.id != fact_id:
                result["resolved_from"] = fact_id
                result["message"] = (
                    f"Fact updated successfully; fact {fact_id} was replaced earlier, "
                    f"so the update was applied to its current version {stored.fact.id}"
                )
            return result

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
        if successor.fact.id == stored.fact.id:
            raise ValueError("superseded_by must name a different fact than the one being closed")
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
            return {**self._merge_failed(str(e)), **_expiry_refusal(e)}
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
            "status": "failed",
            "message": message,
        }

    async def _merge_with_one_retry(
        self, fact_ids: List[str], merged_content: str, metadata: Dict[str, Any], context: FactWriteContext
    ) -> Dict[str, Any]:
        logger.info(f"🔀 [FactManagement] Merging {len(fact_ids)} facts...")
        account_id = metadata.get("account_id")
        metadata = normalize_fact_taxonomy(metadata)
        attrs = parse_new_fact(self._new_fact_data(merged_content, metadata), context.timezone)
        decision = _required_decision(attrs, "merge_facts")

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
            elif ids != first_ids:
                logger.warning("⚠️  [FactManagement] A merge source moved forward between attempts")
                return self._merge_failed("source changed; search again")
            if not sources:
                logger.error("❌ [FactManagement] No facts successfully superseded")
                return self._merge_failed("Failed to supersede any facts")
            if merged is None:
                now = datetime.now(timezone.utc)
                reported = reported_date_for_merge(
                    context, attrs.reported_date, [source.fact.reported_date for source in sources], now
                )
                notes = list(attrs.notes)
                if not context.model_dates and attrs.reported_date is not None:
                    notes.append(kept_reported_date_note(reported, context.timezone))
                decided, due_note = _decided(attrs, decision, reported, now, context)
                notes.extend([due_note] if due_note else [])
                merged = (await self._fact_write_service.build_facts(
                    metadata["account_id"], metadata["user_id"], [decided]
                ))[0]
            try:
                await self._repo.write_superseding(merged, sources, datetime.now(timezone.utc), actor=context.actor)
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
                "status": "merged",
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


def _expiry_refusal(error: FactExpiryError) -> Dict[str, Any]:
    """The tags of an expiry refusal: `refused`, and `born_due` when the expiry was well formed but already due."""
    return {"refused": "expiry", "born_due": True} if isinstance(error, FactBornDueError) else {"refused": "expiry"}


def _required_decision(attrs: NewFactAttributes, tool: str) -> Optional[Expiry]:
    """The expiry decision a new fact is written with: None for a fact born closed (it carries none). A current fact
    needs one inside `fact_attributes` unless it is pinned (a pinned one gets `none`)."""
    if attrs.state in CLOSING_STATES:
        return None
    pinned = expiry_pinned(attrs.domain, attrs.type, attrs.tags)
    if attrs.expiry is None and not pinned:
        raise FactExpiryError(
            f"{tool} needs `expiry` inside `fact_attributes`, decided from the content: use one of {EXPIRY_SHAPES}. "
            f"{NOTHING_WRITTEN}"
        )
    return check_pin(attrs.expiry, pinned)


def _decided(
    attrs: NewFactAttributes, decision: Optional[Expiry], reported: datetime, now: datetime, context: FactWriteContext
) -> Tuple[NewFactAttributes, Optional[str]]:
    """`attrs` with the resolved `reported_date` and, for a current fact, the computed expiry fields (never born due)."""
    if decision is None:
        return dataclasses.replace(attrs, reported_date=reported), None
    fields, note = born_due(
        compute_expiry(decision, reported, context.timezone), reported, now, context.timezone, not context.model_dates
    )
    return dataclasses.replace(attrs, reported_date=reported, expiry_fields=fields), note


def _with_notes(result: Dict[str, Any], notes: Sequence[str], tool: str) -> Dict[str, Any]:
    """Put the notes into the tool result as `note`, and log an ignored expiry (the domain does not log)."""
    if EXPIRY_IGNORED_ON_CLOSE in notes:
        logger.info(f"ℹ️  [FactManagement] {tool}: {EXPIRY_IGNORED_ON_CLOSE}")
    if notes:
        result["note"] = "; ".join(notes)
    return result

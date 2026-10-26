"""What one review pass is given (Facts S4 plan §3.3, §3.4): the batch of stale facts, each with its earlier versions and
its nearest current facts, the open questions about them, and whether the owner's language is mirrored, with his recent
messages when it is.

`FactReviewTask` is the domain shape; `encode_review_task` and `decode_review_task` are its one form as plain data, the
form the agent task's payload carries."""
from typing import Any, Dict, Final, Iterable, List, Mapping, Optional, Sequence, Self, Tuple

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .entities import FactEntity
from .fact_review_batch import ReviewBatch
from .fact_review_question import FactReviewQuestion
from .llm import Message

MAX_OWNER_MESSAGES: Final[int] = 10
MAX_OWNER_MESSAGES_CHARS: Final[int] = 1500
SYSTEM_FRAME_PREFIX: Final[str] = "[System:"  # a frame the system wrote into the history, not the owner's words
PAYLOAD_KEY: Final[str] = "review"
MAX_NEIGHBOURS: Final[int] = 5  # current facts shown beside a batch fact
# How many nearest current facts to read for MAX_NEIGHBOURS outside the fact's own lineage: a lineage has at most one
# current document, so one more is enough.
NEIGHBOUR_QUERY: Final[int] = MAX_NEIGHBOURS + 1
_NO_VECTORS: Final[Mapping[str, None]] = {"vector": None, "tags_vector": None, "metadata_vector": None}


def owner_message_texts(history: Iterable[Message]) -> List[str]:
    """The texts of the owner's own parts in a session history (role `user`), oldest first, each part on its own;
    `owner_messages_block` then keeps the ones the review reads."""
    return [part.text for message in history if message.role == "user" for part in message.parts if part.text]


def owner_messages_block(texts: Iterable[str]) -> Tuple[str, ...]:
    """The owner's messages the review reads his language from: `texts` (oldest first) without empty ones and without
    the system's frames (`[System: …`), the latest MAX_OWNER_MESSAGES of them, and of those the latest that fit in
    MAX_OWNER_MESSAGES_CHARS altogether (the oldest one kept is cut from its start to fit). Oldest first, verbatim."""
    kept = [t for t in texts if t.strip() and not t.lstrip().startswith(SYSTEM_FRAME_PREFIX)][-MAX_OWNER_MESSAGES:]
    block: List[str] = []
    room = MAX_OWNER_MESSAGES_CHARS
    for text in reversed(kept):
        if room <= 0:
            break
        block.append(text if len(text) <= room else text[len(text) - room:])
        room -= len(block[-1])
    return tuple(reversed(block))


class ReviewItem(BaseModel):
    """One batch fact. `versions`: the earlier versions of its lineage, oldest first. `neighbours`: the current facts
    nearest to it by its own vector, its own lineage left out."""

    model_config = ConfigDict(frozen=True)

    fact: FactEntity
    versions: Tuple[FactEntity, ...] = ()
    neighbours: Tuple[FactEntity, ...] = ()


def review_item(fact: FactEntity, lineage: Sequence[FactEntity], nearest: Sequence[FactEntity]) -> ReviewItem:
    """The batch fact with what the pass reads beside it. `lineage`: every document of its lineage (oldest first); the
    ones before it (a lower version) are its versions. `nearest`: current facts by its vector, nearest first; the first
    MAX_NEIGHBOURS outside its lineage are its neighbours. No vector is kept: the pass reads text."""
    versions = sorted((f for f in lineage if f.version < fact.version and f.id != fact.id), key=lambda f: f.version)
    return ReviewItem(
        fact=_text_only(fact),
        versions=tuple(_text_only(f) for f in versions),
        neighbours=tuple(_text_only(f) for f in neighbours_of(fact, nearest)),
    )


def neighbours_of(fact: FactEntity, nearest: Sequence[FactEntity]) -> List[FactEntity]:
    """The fact's neighbours: of the current facts nearest to it by its vector (`nearest`, nearest first), the first
    MAX_NEIGHBOURS outside its own lineage."""
    return [f for f in nearest if f.lineage_id != fact.lineage_id][:MAX_NEIGHBOURS]


def questions_about(facts: Iterable[FactEntity], questions: Iterable[FactReviewQuestion]) -> Tuple[FactReviewQuestion, ...]:
    """The open questions about these facts: the questions a pass over them is given."""
    ids = {f.id for f in facts}
    return tuple(q for q in questions if q.is_open and q.fact_id in ids)


def _text_only(fact: FactEntity) -> FactEntity:
    return fact.model_copy(update=dict(_NO_VECTORS))


class FactReviewTask(BaseModel):
    """`items`: the batch, as read when the pass began. `questions`: the open questions about its facts. `mirror`: the
    owner's response language mirrors his messages, so the pass reports their language (and, for a language without UI
    strings, the labels); `owner_messages` (oldest first) belong to that mode only and are held to
    `owner_messages_block`. `now` is the run's one instant, which the pass uses for every decision it makes (none: the
    pass reads its own clock)."""

    model_config = ConfigDict(frozen=True)

    items: Tuple[ReviewItem, ...] = Field(min_length=1)
    questions: Tuple[FactReviewQuestion, ...] = ()
    mirror: bool
    owner_messages: Tuple[str, ...] = ()
    now: Optional[AwareDatetime] = None

    @field_validator("owner_messages", mode="after")
    @classmethod
    def _as_the_block(cls, texts: Tuple[str, ...]) -> Tuple[str, ...]:
        return owner_messages_block(texts)

    @model_validator(mode="after")
    def _owner_messages_belong_to_mirror_mode(self) -> Self:
        if self.owner_messages and not self.mirror:
            raise ValueError("owner messages are given only when the language mirrors the owner")
        return self

    @property
    def batch(self) -> ReviewBatch:
        return ReviewBatch(tuple(item.fact for item in self.items), self.questions)

    def question_for(self, fact_id: str) -> Optional[FactReviewQuestion]:
        """The open question about the fact, if there is one."""
        return next((q for q in self.questions if q.fact_id == fact_id and q.is_open), None)


def encode_review_task(task: FactReviewTask) -> Dict[str, Any]:
    """The task as the agent task's payload fields: plain data (dicts, lists, times and enum members) under
    `PAYLOAD_KEY`, read back to an equal task by `decode_review_task`.

    In process only: the times inside a fact's `lifecycle_events` stay datetimes here, but they are untyped, so through
    JSON they would come back as text the review's rules cannot read. Do not send this through a JSON queue until the
    event list has a type."""
    return {PAYLOAD_KEY: task.model_dump()}


def decode_review_task(payload: Mapping[str, Any]) -> FactReviewTask:
    """The task in an agent task's payload. Raises ValueError when it holds none or one that is not a valid task."""
    raw = payload.get(PAYLOAD_KEY)
    if not isinstance(raw, Mapping):
        raise ValueError(f"the payload carries no review task under {PAYLOAD_KEY!r}")
    try:
        return FactReviewTask.model_validate(raw)
    except ValidationError as error:
        raise ValueError(f"the review task is not valid: {error}") from error

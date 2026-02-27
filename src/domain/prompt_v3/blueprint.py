"""
Blueprint v4 — defines prompt assembly structure by class order.

Part of Prompt Design System v4 (RFC: docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md).
"""

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Blueprint:
    """Defines prompt assembly structure.

    Specifies the outer Groovy class declaration and the ordered list of
    section names to render. The assembly service generates each section
    wrapper and fills it with tokens from the agent profile.

    Assembly output:
        class {outer_class} {
            {section1} {
                // all tokens with class=section1, sorted by their order
            }
            {section2} { ... }
        }

    Examples:
        >>> blueprint = Blueprint(
        ...     id="universal_agent_v1",
        ...     outer_class="Alek extends Agent",
        ...     class_order=["properties", "cognitive_process", "policies"]
        ... )
        >>> blueprint.validate()  # passes
        >>>
        >>> Blueprint(id="x", outer_class="", class_order=["properties"]).validate()
        ValueError: outer_class cannot be empty
    """

    id: str
    outer_class: str     # e.g., "Alek extends Agent"
    class_order: List[str]  # e.g., ["properties", "cognitive_process", ...]

    def validate(self) -> None:
        """Check structural integrity of the blueprint.

        Raises:
            ValueError: If outer_class is empty, class_order is empty,
                        or class_order contains duplicate names.
        """
        if not self.outer_class.strip():
            raise ValueError("outer_class cannot be empty")
        if not self.class_order:
            raise ValueError("class_order cannot be empty")
        if len(self.class_order) != len(set(self.class_order)):
            dupes = [c for c in self.class_order if self.class_order.count(c) > 1]
            raise ValueError(f"class_order has duplicates: {list(set(dupes))}")

    def __hash__(self):
        return hash((self.id, self.outer_class, tuple(self.class_order)))

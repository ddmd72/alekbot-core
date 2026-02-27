"""
ProfileToken — token assignment entry in an agent profile.

Part of Prompt Design System v4 (RFC: docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProfileToken:
    """Token assignment in an agent profile or account/user override.

    Stored in Firestore as a map entry under the profile's `tokens` field:
        tokens: {
            "HUMOR_PRESET_RANEVSKAYA": {"order": 40},
            "COGNITIVE_PROCESS_QUICK": {"order": 10, "non_overridable": true}
        }

    Fields:
        token_id: which token to include (maps to token document)
        order: rendering position within the token's class (lower = earlier, step 10)
        non_overridable: if True, account/user overrides cannot replace this token

    Examples:
        >>> t = ProfileToken.from_dict("HUMOR_PRESET_RANEVSKAYA", {"order": 40})
        >>> t.token_id
        'HUMOR_PRESET_RANEVSKAYA'
        >>> t.order
        40
        >>> t.non_overridable
        False

        >>> locked = ProfileToken.from_dict(
        ...     "COGNITIVE_PROCESS_QUICK",
        ...     {"order": 10, "non_overridable": True}
        ... )
        >>> locked.non_overridable
        True
    """

    token_id: str
    order: int
    non_overridable: bool = False

    @staticmethod
    def from_dict(token_id: str, data: dict) -> "ProfileToken":
        """Deserialize from Firestore map entry.

        Args:
            token_id: the map key (= token document ID)
            data: the map value dict with 'order' and optional 'non_overridable'
        """
        return ProfileToken(
            token_id=token_id,
            order=int(data["order"]),
            non_overridable=bool(data.get("non_overridable", False)),
        )

    def to_dict(self) -> dict:
        """Serialize to Firestore map entry value (without the key).

        Only includes non_overridable when True to keep documents compact.
        """
        d: dict = {"order": self.order}
        if self.non_overridable:
            d["non_overridable"] = True
        return d

"""
CompositeAdapter - Aggregates multiple security adapters with configurable strategy.

Full implementation for MVP.
"""

import logging
from typing import List

from src.ports.security_port import (
    SecurityPort,
    ValidationResult,
    RiskLevel,
    TrustZone,
)

logger = logging.getLogger(__name__)


class CompositeAdapter(SecurityPort):
    """Aggregates multiple security adapters with configurable strategy.

    Fully implemented for MVP. Supports three aggregation strategies:
    - worst_case: Conservative (highest risk wins)
    - majority_vote: Democratic (majority consensus)
    - all_pass: Strict (all adapters must say SAFE)

    Examples:
        >>> from src.adapters.security.regex_adapter import RegexSecurityAdapter
        >>>
        >>> # MVP Configuration (single adapter)
        >>> regex_adapter = RegexSecurityAdapter()
        >>> composite = CompositeAdapter(
        ...     adapters=[regex_adapter],
        ...     strategy="worst_case"
        ... )
        >>>
        >>> # Future Configuration (multiple adapters)
        >>> llm_adapter = LLMSecurityAdapter()
        >>> composite = CompositeAdapter(
        ...     adapters=[regex_adapter, llm_adapter],
        ...     strategy="majority_vote"
        ... )
    """

    def __init__(self, adapters: List[SecurityPort], strategy: str = "worst_case"):
        """Initialize CompositeAdapter.

        Args:
            adapters: List of SecurityPort implementations
            strategy: Aggregation strategy ("worst_case", "majority_vote", "all_pass")

        Raises:
            ValueError: If no adapters provided or invalid strategy

        Examples:
            >>> adapter1 = RegexSecurityAdapter()
            >>> composite = CompositeAdapter(
            ...     adapters=[adapter1],
            ...     strategy="worst_case"
            ... )
        """
        if not adapters:
            raise ValueError("CompositeAdapter requires at least one adapter")

        valid_strategies = {"worst_case", "majority_vote", "all_pass"}
        if strategy not in valid_strategies:
            raise ValueError(
                f"Invalid strategy: {strategy}. "
                f"Must be one of {valid_strategies}"
            )

        self.adapters = adapters
        self.strategy = strategy

    async def validate(
        self,
        text: str,
        context: str,
        zone: TrustZone = TrustZone.UNTRUSTED
    ) -> ValidationResult:
        """Run all adapters and aggregate results.

        Args:
            text: Text to validate
            context: Context for logging
            zone: Trust zone classification

        Returns:
            Aggregated ValidationResult

        Raises:
            ValueError: If any adapter blocks content (depending on strategy)

        Examples:
            >>> composite = CompositeAdapter([RegexSecurityAdapter()], "worst_case")
            >>> result = await composite.validate("Hello", "test", TrustZone.UNTRUSTED)
            >>> assert result.risk_level == RiskLevel.SAFE
        """

        results = []
        for adapter in self.adapters:
            try:
                result = await adapter.validate(text, context, zone)
                results.append(result)
            except ValueError as e:
                # Adapter blocked content
                logger.warning(
                    f"Adapter {adapter.__class__.__name__} blocked content: {e}"
                )
                # Create CRITICAL result for blocked content
                results.append(ValidationResult(
                    sanitized_text="",
                    risk_level=RiskLevel.CRITICAL,
                    risk_score=1.0,
                    patterns_detected=[str(e)],
                    action_taken="blocked",
                    metadata={
                        "adapter": adapter.__class__.__name__,
                        "error": str(e),
                        "blocked": True
                    }
                ))

        # Aggregate results based on strategy
        if self.strategy == "worst_case":
            return self._worst_case(results)
        elif self.strategy == "majority_vote":
            return self._majority_vote(results)
        elif self.strategy == "all_pass":
            return self._all_pass(results)
        else:
            # Should never reach here due to constructor validation
            raise ValueError(f"Unknown strategy: {self.strategy}")

    def _worst_case(self, results: List[ValidationResult]) -> ValidationResult:
        """Conservative: highest risk wins.

        Args:
            results: List of ValidationResult from all adapters

        Returns:
            ValidationResult with highest risk score

        Raises:
            ValueError: If any result has action_taken="blocked"

        Examples:
            >>> # If one adapter says SAFE and another says HIGH,
            >>> # return HIGH result
        """
        # Check if any adapter blocked content
        blocked = [r for r in results if r.action_taken == "blocked"]
        if blocked:
            worst = blocked[0]  # Already CRITICAL
            all_patterns = []
            for r in results:
                all_patterns.extend(r.patterns_detected)
            raise ValueError(
                f"Security validation failed: {list(set(all_patterns))} "
                f"(risk_level={worst.risk_level.value})"
            )

        # Find result with highest risk
        worst = max(results, key=lambda r: r.risk_score)

        # Aggregate all detected patterns
        all_patterns = []
        for r in results:
            all_patterns.extend(r.patterns_detected)

        return ValidationResult(
            sanitized_text=worst.sanitized_text,
            risk_level=worst.risk_level,
            risk_score=worst.risk_score,
            patterns_detected=list(set(all_patterns)),  # Deduplicate
            action_taken=worst.action_taken,
            metadata={
                "strategy": "worst_case",
                "adapter_count": len(results),
                "adapters": [r.metadata.get("adapter", "unknown") for r in results]
            }
        )

    def _majority_vote(self, results: List[ValidationResult]) -> ValidationResult:
        """Majority consensus on risk level.

        Args:
            results: List of ValidationResult from all adapters

        Returns:
            ValidationResult with majority risk level

        Raises:
            ValueError: If majority says blocked

        Examples:
            >>> # If 2 adapters say SAFE and 1 says MEDIUM,
            >>> # return SAFE result (majority wins)
        """
        # Count risk levels
        risk_counts = {}
        for r in results:
            risk_counts[r.risk_level] = risk_counts.get(r.risk_level, 0) + 1

        majority_risk = max(risk_counts, key=risk_counts.get)

        # If majority is CRITICAL and any adapter blocked, raise error
        if majority_risk == RiskLevel.CRITICAL:
            blocked = [r for r in results if r.action_taken == "blocked"]
            if blocked:
                all_patterns = []
                for r in results:
                    all_patterns.extend(r.patterns_detected)
                raise ValueError(
                    f"Security validation failed (majority vote): {list(set(all_patterns))}"
                )

        # Get first result with majority risk level
        majority_result = next(r for r in results if r.risk_level == majority_risk)

        # Aggregate patterns from all results
        all_patterns = []
        for r in results:
            all_patterns.extend(r.patterns_detected)

        return ValidationResult(
            sanitized_text=majority_result.sanitized_text,
            risk_level=majority_risk,
            risk_score=majority_result.risk_score,
            patterns_detected=list(set(all_patterns)),
            action_taken=majority_result.action_taken,
            metadata={
                "strategy": "majority_vote",
                "adapter_count": len(results),
                "vote_counts": {k.value: v for k, v in risk_counts.items()}
            }
        )

    def _all_pass(self, results: List[ValidationResult]) -> ValidationResult:
        """Only pass if ALL adapters say SAFE.

        Args:
            results: List of ValidationResult from all adapters

        Returns:
            ValidationResult (SAFE only if all are SAFE)

        Raises:
            ValueError: If any adapter blocked content

        Examples:
            >>> # If all adapters say SAFE, return SAFE
            >>> # If even one says MEDIUM, return worst case
        """
        # Check if any adapter blocked
        blocked = [r for r in results if r.action_taken == "blocked"]
        if blocked:
            all_patterns = []
            for r in results:
                all_patterns.extend(r.patterns_detected)
            raise ValueError(
                f"Security validation failed (all_pass): {list(set(all_patterns))}"
            )

        # If all adapters say SAFE, return SAFE
        if all(r.risk_level == RiskLevel.SAFE for r in results):
            return ValidationResult(
                sanitized_text=results[0].sanitized_text,
                risk_level=RiskLevel.SAFE,
                risk_score=0.0,
                patterns_detected=[],
                action_taken="passed",
                metadata={
                    "strategy": "all_pass",
                    "adapter_count": len(results),
                    "all_safe": True
                }
            )

        # At least one adapter flagged risk → use worst case
        return self._worst_case(results)

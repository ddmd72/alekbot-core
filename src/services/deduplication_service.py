"""
Smart Deduplication Service — re-export for backward compatibility.

Canonical location: src/domain/deduplication_service.py
Session 2026-02-20: Moved to domain (zero external dependencies, pure logic).
"""
from ..domain.deduplication_service import SmartDeduplicationService

__all__ = ["SmartDeduplicationService"]

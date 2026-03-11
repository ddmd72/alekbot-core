#!/usr/bin/env python3
"""
Test 3-level priority resolution for prompt components.

SESSION_25: Validation script for USER > AGENT > SYSTEM resolution.

Usage:
    python scripts/prompt/test_3level_resolution.py

Test cases:
1. SYSTEM-only (no overrides)
2. AGENT-level override (smart vs quick)
3. USER-level override (full override)
4. USER exclusion (is_enabled=False)
5. USER fallthrough (text="")
6. Component not found
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.domain.prompt import PromptComponent, ComponentScope, OwnerType
from src.adapters.firestore_prompt_repository import FirestorePromptComponentRepository
from src.utils.logger import logger


class MockFirestoreClient:
    """Mock Firestore client for testing without actual DB."""
    
    def __init__(self):
        # Mock data: component_id -> list of components at different levels
        self.mock_data = {
            "cognitive_process": [
                # SYSTEM level
                {
                    "component_id": "cognitive_process",
                    "owner_type": "SYSTEM",
                    "owner_value": None,
                    "scope": "class.Alek",
                    "order": 10,
                    "text": "SYSTEM cognitive_process content",
                    "is_enabled": True,
                    "version": "1.0"
                },
                # AGENT/smart level
                {
                    "component_id": "cognitive_process",
                    "owner_type": "AGENT",
                    "owner_value": "smart",
                    "scope": "class.Alek",
                    "order": 10,
                    "text": "SMART agent cognitive_process with tools",
                    "is_enabled": True,
                    "version": "1.0"
                },
                # AGENT/quick level
                {
                    "component_id": "cognitive_process",
                    "owner_type": "AGENT",
                    "owner_value": "quick",
                    "scope": "class.Alek",
                    "order": 10,
                    "text": "QUICK agent cognitive_process (lightweight)",
                    "is_enabled": True,
                    "version": "1.0"
                }
            ],
            "humor_engine": [
                # SYSTEM level
                {
                    "component_id": "humor_engine",
                    "owner_type": "SYSTEM",
                    "owner_value": None,
                    "scope": "class.Alek.properties",
                    "order": 20,
                    "text": "SYSTEM humor_engine enabled",
                    "is_enabled": True,
                    "version": "1.0"
                },
                # USER/test-user EXCLUDED
                {
                    "component_id": "humor_engine",
                    "owner_type": "USER",
                    "owner_value": "test-user-excluded",
                    "scope": "class.Alek.properties",
                    "order": 20,
                    "text": "",
                    "is_enabled": False,  # EXCLUSION pattern
                    "version": "1.0"
                }
            ],
            "behavior_guide": [
                # SYSTEM level
                {
                    "component_id": "behavior_guide",
                    "owner_type": "SYSTEM",
                    "owner_value": None,
                    "scope": "class.Alek.properties",
                    "order": 30,
                    "text": "SYSTEM behavior_guide default",
                    "is_enabled": True,
                    "version": "1.0"
                },
                # AGENT/smart level
                {
                    "component_id": "behavior_guide",
                    "owner_type": "AGENT",
                    "owner_value": "smart",
                    "scope": "class.Alek.properties",
                    "order": 30,
                    "text": "SMART agent behavior_guide advanced",
                    "is_enabled": True,
                    "version": "1.0"
                },
                # USER/test-user FALLTHROUGH (empty text)
                {
                    "component_id": "behavior_guide",
                    "owner_type": "USER",
                    "owner_value": "test-user-fallthrough",
                    "scope": "class.Alek.properties",
                    "order": 30,
                    "text": "",  # FALLTHROUGH pattern - use AGENT level
                    "is_enabled": True,
                    "version": "1.0"
                }
            ],
            "custom_component": [
                # USER-only component (no SYSTEM or AGENT defaults)
                {
                    "component_id": "custom_component",
                    "owner_type": "USER",
                    "owner_value": "test-user-custom",
                    "scope": "class.Alek.properties",
                    "order": 40,
                    "text": "USER custom component content",
                    "is_enabled": True,
                    "version": "1.0"
                }
            ]
        }
    
    def collection(self, name: str):
        """Return mock collection."""
        return MockCollection(self.mock_data)


class MockCollection:
    """Mock Firestore collection."""
    
    def __init__(self, mock_data):
        self.mock_data = mock_data
        self.filters = {}
    
    def where(self, **kwargs):
        """Mock where filter."""
        # Store filters from FieldFilter object
        if 'filter' in kwargs:
            field_filter = kwargs['filter']
            # FieldFilter has attributes: field, op, value
            # Access them directly instead of parsing string
            try:
                field_name = field_filter.field
                value = field_filter.value
                self.filters[field_name] = value
            except AttributeError:
                # Fallback: try to parse as string
                filter_str = str(field_filter)
                if "==" in filter_str:
                    parts = filter_str.split("==")
                    field_name = parts[0].strip()
                    value = parts[1].strip().strip("'\"") if len(parts) > 1 else None
                    self.filters[field_name] = value
        return self
    
    def limit(self, count):
        """Mock limit."""
        self.limit_count = count
        return self
    
    async def stream(self):
        """Mock stream - yield matching documents."""
        component_id = self.filters.get("component_id")
        owner_type = self.filters.get("owner_type")
        owner_value = self.filters.get("owner_value")
        
        # Clean string values
        if isinstance(component_id, str):
            component_id = component_id.strip("'\"")
        if isinstance(owner_type, str):
            owner_type = owner_type.strip("'\"")
        if isinstance(owner_value, str):
            owner_value = owner_value.strip("'\"")
        
        if component_id and component_id in self.mock_data:
            for doc_data in self.mock_data[component_id]:
                # Check if matches filters
                if owner_type is not None and doc_data["owner_type"] != owner_type:
                    continue
                
                # Special handling for owner_value (can be None)
                if "owner_value" in self.filters:
                    doc_owner_value = doc_data.get("owner_value")
                    # Both None or both match
                    if owner_value != doc_owner_value:
                        continue
                
                # Yield mock document
                yield MockDocument(doc_data)


class MockDocument:
    """Mock Firestore document."""
    
    def __init__(self, data):
        self.data = data
        self.id = f"{data['component_id']}_{data['owner_type']}"
    
    def to_dict(self):
        return self.data


async def test_system_only():
    """Test Case 1: SYSTEM-only resolution (no overrides)."""
    print("\n" + "="*70)
    print("TEST 1: SYSTEM-only resolution")
    print("="*70)
    
    mock_db = MockFirestoreClient()
    repo = FirestorePromptComponentRepository(mock_db, "test_components")
    
    # Resolve cognitive_process for quick agent, no user
    result = await repo.resolve_component(
        component_id="cognitive_process",
        agent_type="router",  # No AGENT/router override exists
        user_id=None
    )
    
    assert result is not None, "Should find SYSTEM component"
    assert result.owner_type == OwnerType.SYSTEM, "Should be SYSTEM level"
    assert "SYSTEM cognitive_process" in result.content
    
    print(f"✅ PASSED: Found SYSTEM component")
    print(f"   Content: {result.content[:50]}...")
    print(f"   Owner: {result.owner_type.value}")


async def test_agent_override():
    """Test Case 2: AGENT-level override (smart vs quick)."""
    print("\n" + "="*70)
    print("TEST 2: AGENT-level override (smart vs quick)")
    print("="*70)
    
    mock_db = MockFirestoreClient()
    repo = FirestorePromptComponentRepository(mock_db, "test_components")
    
    # Test smart agent
    result_smart = await repo.resolve_component(
        component_id="cognitive_process",
        agent_type="smart",
        user_id=None
    )
    
    assert result_smart is not None
    assert result_smart.owner_type == OwnerType.AGENT
    assert result_smart.owner_value == "smart"
    assert "SMART agent" in result_smart.content
    
    print(f"✅ PASSED: Smart agent gets AGENT/smart component")
    print(f"   Content: {result_smart.content[:50]}...")
    
    # Test quick agent
    result_quick = await repo.resolve_component(
        component_id="cognitive_process",
        agent_type="quick",
        user_id=None
    )
    
    assert result_quick is not None
    assert result_quick.owner_type == OwnerType.AGENT
    assert result_quick.owner_value == "quick"
    assert "QUICK agent" in result_quick.content
    
    print(f"✅ PASSED: Quick agent gets AGENT/quick component")
    print(f"   Content: {result_quick.content[:50]}...")


async def test_user_override():
    """Test Case 3: USER-level full override."""
    print("\n" + "="*70)
    print("TEST 3: USER-level full override")
    print("="*70)
    
    mock_db = MockFirestoreClient()
    repo = FirestorePromptComponentRepository(mock_db, "test_components")
    
    # User has custom component (no SYSTEM default exists)
    result = await repo.resolve_component(
        component_id="custom_component",
        agent_type="smart",
        user_id="test-user-custom"
    )
    
    assert result is not None
    assert result.owner_type == OwnerType.USER
    assert result.owner_value == "test-user-custom"
    assert "USER custom" in result.content
    
    print(f"✅ PASSED: User gets USER-level component")
    print(f"   Content: {result.content[:50]}...")
    print(f"   Owner: {result.owner_type.value}/{result.owner_value}")


async def test_user_exclusion():
    """Test Case 4: USER exclusion pattern (is_enabled=False)."""
    print("\n" + "="*70)
    print("TEST 4: USER exclusion (is_enabled=False)")
    print("="*70)
    
    mock_db = MockFirestoreClient()
    repo = FirestorePromptComponentRepository(mock_db, "test_components")
    
    # User has excluded humor_engine
    result = await repo.resolve_component(
        component_id="humor_engine",
        agent_type="smart",
        user_id="test-user-excluded"
    )
    
    assert result is None, "Should be excluded (None)"
    
    print(f"✅ PASSED: Component EXCLUDED by user")
    print(f"   Result: None (component removed from prompt)")


async def test_user_fallthrough():
    """Test Case 5: USER fallthrough pattern (text='')."""
    print("\n" + "="*70)
    print("TEST 5: USER fallthrough (empty text)")
    print("="*70)
    
    mock_db = MockFirestoreClient()
    repo = FirestorePromptComponentRepository(mock_db, "test_components")
    
    # User has fallthrough for behavior_guide → should get AGENT/smart
    result = await repo.resolve_component(
        component_id="behavior_guide",
        agent_type="smart",
        user_id="test-user-fallthrough"
    )
    
    assert result is not None
    assert result.owner_type == OwnerType.AGENT, "Should fallthrough to AGENT"
    assert result.owner_value == "smart"
    assert "SMART agent" in result.content
    
    print(f"✅ PASSED: USER fallthrough to AGENT level")
    print(f"   Content: {result.content[:50]}...")
    print(f"   Owner: {result.owner_type.value}/{result.owner_value}")


async def test_not_found():
    """Test Case 6: Component not found at any level."""
    print("\n" + "="*70)
    print("TEST 6: Component not found")
    print("="*70)
    
    mock_db = MockFirestoreClient()
    repo = FirestorePromptComponentRepository(mock_db, "test_components")
    
    # Request non-existent component
    result = await repo.resolve_component(
        component_id="nonexistent_component",
        agent_type="smart",
        user_id="test-user"
    )
    
    assert result is None, "Should return None for non-existent component"
    
    print(f"✅ PASSED: Non-existent component returns None")


async def main():
    """Run all test cases."""
    print("\n" + "="*70)
    print("🧪 SESSION_25: 3-Level Priority Resolution Tests")
    print("="*70)
    print("\nTesting: USER > AGENT > SYSTEM priority")
    print("Patterns: Override, Fallthrough, Exclusion")
    
    try:
        await test_system_only()
        await test_agent_override()
        await test_user_override()
        await test_user_exclusion()
        await test_user_fallthrough()
        await test_not_found()
        
        print("\n" + "="*70)
        print("✅ ALL TESTS PASSED!")
        print("="*70)
        print("\n3-level resolution working correctly:")
        print("  1. ✅ SYSTEM fallback")
        print("  2. ✅ AGENT overrides (smart/quick)")
        print("  3. ✅ USER overrides (highest priority)")
        print("  4. ✅ Exclusion pattern (is_enabled=False)")
        print("  5. ✅ Fallthrough pattern (text='')")
        print("  6. ✅ Not found handling")
        
        return 0
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

#!/usr/bin/env python3
"""
Test Prompt Assembly - Prompt Design System v3
=================================================
Tests end-to-end prompt assembly after migration.

Usage:
    python test_prompt_assembly.py --env dev
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from google.cloud import firestore

from src.adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
from src.adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from src.services.prompt_v3.context_formatter import ContextFormatter


# Mock SecurityPort for testing
class MockSecurityPort:
    """Mock security port for testing."""

    async def validate(self, text: str, context: str, zone=None):
        """Mock validation - returns text as-is."""
        from src.domain.prompt_v3.security import ValidationResult, RiskLevel

        return ValidationResult(
            sanitized_text=text,
            risk_level=RiskLevel.SAFE,
            risk_score=0.0,
            patterns_detected=[],
            action_taken="passed",
            metadata={}
        )


async def test_smart_agent_assembly(env: str, project_id: str = None):
    """Test smart agent prompt assembly."""
    print("=" * 70)
    print("TESTING SMART AGENT PROMPT ASSEMBLY")
    print("=" * 70)

    # Initialize Firestore
    if project_id:
        db = firestore.Client(project=project_id)
    else:
        db = firestore.Client()

    # Initialize repositories
    security_port = MockSecurityPort()
    token_repo = FirestoreTokenRepository(
        db,
        f"{env}_prompt_tokens",
        security_port
    )
    blueprint_repo = FirestoreBlueprintRepository(
        db,
        f"{env}_prompt_blueprints"
    )
    profile_repo = FirestoreAgentProfileRepository(
        db,
        f"{env}_agent_profiles"
    )

    # Initialize service
    formatter = ContextFormatter()
    service = PromptAssemblyService(
        token_repo,
        blueprint_repo,
        profile_repo,
        security_port,
        formatter
    )

    # Test data
    biographical_facts = [
        "Name: Dmytro",
        "Location: Kyiv, Ukraine",
        "Occupation: Software Engineer",
        "Languages: Ukrainian, English, Russian"
    ]

    conversation_history = [
        {"role": "user", "content": "Привіт!"},
        {"role": "assistant", "content": "Привіт! Як справи?"}
    ]

    # Assemble prompt
    print("\n1. Assembling prompt for smart agent...")
    print(f"   User ID: None (using SYSTEM defaults)")
    print(f"   Account ID: None")
    print(f"   Biographical facts: {len(biographical_facts)} items")
    print(f"   Conversation history: {len(conversation_history)} messages")

    try:
        # Enable debug logging
        import logging
        logging.basicConfig(level=logging.DEBUG)

        prompt = await service.assemble(
            agent_type="smart",
            user_id=None,
            account_id=None,
            biographical_facts=biographical_facts,
            conversation_history=conversation_history
        )

        print(f"\n2. ✅ Prompt assembled successfully!")
        print(f"   Total length: {len(prompt)} characters")
        print(f"   Total lines: {len(prompt.splitlines())} lines")

        # Show preview
        print("\n3. Preview (first 50 lines):")
        print("-" * 70)
        lines = prompt.splitlines()
        for i, line in enumerate(lines[:50], 1):
            print(f"{i:3d} | {line}")

        if len(lines) > 50:
            print(f"... ({len(lines) - 50} more lines)")

        # Verify key sections exist
        print("\n4. Verifying key sections...")
        sections = {
            "knowledge_base": "knowledge_base {" in prompt,
            "properties": "properties {" in prompt,
            "cognitive_process": "cognitive_process {" in prompt,
            "policies": "policies {" in prompt,
            "protocols": "protocols {" in prompt,
            "output_format": "output_format {" in prompt,
            "final_directives": "final_directives {" in prompt,
            "biographical_context": any(fact in prompt for fact in biographical_facts),
            "conversation_history": "Привіт!" in prompt,
        }

        for section, exists in sections.items():
            status = "✓" if exists else "✗"
            print(f"   {status} {section}: {'present' if exists else 'MISSING'}")

        # Check for token replacement
        print("\n5. Checking token replacement...")
        remaining_tokens = [
            line for line in prompt.splitlines()
            if "{{" in line and "}}" in line and not line.strip().startswith("//")
        ]

        if remaining_tokens:
            print(f"   ⚠️  Found {len(remaining_tokens)} unreplaced tokens:")
            for token_line in remaining_tokens[:5]:
                print(f"      {token_line.strip()}")
        else:
            print("   ✓ All tokens replaced successfully")

        # Check for runtime placeholders
        print("\n6. Checking runtime placeholders...")
        if "[[BIOGRAPHICAL_CONTEXT]]" in prompt:
            print("   ✗ BIOGRAPHICAL_CONTEXT placeholder not replaced")
        else:
            print("   ✓ BIOGRAPHICAL_CONTEXT replaced")

        if "[[CONVERSATION_HISTORY]]" in prompt:
            print("   ✗ CONVERSATION_HISTORY placeholder not replaced")
        else:
            print("   ✓ CONVERSATION_HISTORY replaced")

        # Save to file
        output_file = Path(__file__).parent / f"assembled_prompt_{env}_smart.groovy"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(prompt)

        print(f"\n7. ✅ Prompt saved to: {output_file}")

        print("\n" + "=" * 70)
        print("TEST COMPLETED SUCCESSFULLY!")
        print("=" * 70)

        return True

    except Exception as e:
        print(f"\n❌ Error during prompt assembly: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_quick_agent_assembly(env: str, project_id: str = None):
    """Test quick agent prompt assembly."""
    print("\n" + "=" * 70)
    print("TESTING QUICK AGENT PROMPT ASSEMBLY")
    print("=" * 70)

    # Initialize Firestore
    if project_id:
        db = firestore.Client(project=project_id)
    else:
        db = firestore.Client()

    # Initialize repositories
    security_port = MockSecurityPort()
    token_repo = FirestoreTokenRepository(
        db,
        f"{env}_prompt_tokens",
        security_port
    )
    blueprint_repo = FirestoreBlueprintRepository(
        db,
        f"{env}_prompt_blueprints"
    )
    profile_repo = FirestoreAgentProfileRepository(
        db,
        f"{env}_agent_profiles"
    )

    # Initialize service
    formatter = ContextFormatter()
    service = PromptAssemblyService(
        token_repo,
        blueprint_repo,
        profile_repo,
        security_port,
        formatter
    )

    print("\n1. Assembling prompt for quick agent...")

    try:
        prompt = await service.assemble(
            agent_type="quick",
            user_id=None,
            account_id=None,
            biographical_facts=["Quick test fact"],
            conversation_history=[]
        )

        print(f"   ✅ Quick agent prompt assembled: {len(prompt)} characters")

        # Save to file
        output_file = Path(__file__).parent / f"assembled_prompt_{env}_quick.groovy"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(prompt)

        print(f"   ✅ Saved to: {output_file}")

        return True

    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Test Prompt Assembly for Prompt Design System v3"
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["dev", "staging", "prod"],
        help="Environment (dev/staging/prod)",
    )
    parser.add_argument(
        "--project-id",
        help="GCP project ID (optional)",
    )
    parser.add_argument(
        "--agent",
        choices=["smart", "quick", "all"],
        default="all",
        help="Which agent to test (default: all)",
    )

    args = parser.parse_args()

    print("Testing prompt assembly with migrated data...")
    print(f"Environment: {args.env}")

    # Run tests
    results = []

    if args.agent in ["smart", "all"]:
        result = asyncio.run(test_smart_agent_assembly(args.env, args.project_id))
        results.append(("smart", result))

    if args.agent in ["quick", "all"]:
        result = asyncio.run(test_quick_agent_assembly(args.env, args.project_id))
        results.append(("quick", result))

    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    for agent, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {agent} agent: {status}")

    # Exit code
    if all(result for _, result in results):
        print("\n✅ All tests passed!")
        sys.exit(0)
    else:
        print("\n❌ Some tests failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()

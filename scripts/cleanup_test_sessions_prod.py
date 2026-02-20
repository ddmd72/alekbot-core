#!/usr/bin/env python3
"""
Cleanup test sessions from PRODUCTION sessions collection.

⚠️  WARNING: This operates on PRODUCTION data!
"""
import asyncio
from google.cloud import firestore


async def cleanup():
    """Delete test sessions based on document ID patterns."""
    print("\n" + "="*70)
    print("🔴 WARNING: PRODUCTION DATA CLEANUP 🔴")
    print("="*70)
    print("\nThis will delete test sessions from the PRODUCTION 'sessions' collection.")
    print("Real user sessions (UUID format) will be preserved.\n")
    print("Test session patterns to be deleted:")
    print("  - session_*")
    print("  - Pure numeric timestamps (e.g., 1769025250.341749)")
    print("  - session_r2_*")
    print("  - *validation_test_user*")
    print("\n" + "="*70 + "\n")

    # Confirmation prompt
    confirm = input("Type 'DELETE TEST SESSIONS' to confirm: ")
    if confirm != "DELETE TEST SESSIONS":
        print("❌ Aborted. No sessions were deleted.")
        return

    print("\n🧹 Starting cleanup of PRODUCTION test sessions...\n")

    db = firestore.AsyncClient()
    col = db.collection('sessions')  # PRODUCTION collection

    count = 0
    async for doc in col.stream():
        doc_id = doc.id

        # Check if this is a test session
        is_test = (
            doc_id.startswith('session_') or
            doc_id.replace('.', '').isdigit() or
            doc_id.startswith('session_r2_') or
            'validation_test_user' in doc_id
        )

        if is_test:
            await doc.reference.delete()
            count += 1

            if count % 10 == 0:
                print(f'  Deleted {count}...')

    print(f'\n✅ Deleted {count} test sessions from PRODUCTION')


if __name__ == '__main__':
    asyncio.run(cleanup())

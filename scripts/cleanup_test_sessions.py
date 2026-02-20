#!/usr/bin/env python3
"""
Cleanup test sessions from development_sessions collection.
"""
import asyncio
from google.cloud import firestore


async def cleanup():
    """Delete test sessions based on document ID patterns."""
    db = firestore.AsyncClient()
    col = db.collection('development_sessions')

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

    print(f'✅ Deleted {count} test sessions')


if __name__ == '__main__':
    asyncio.run(cleanup())

from google.cloud import firestore
import os

os.environ['GOOGLE_CLOUD_PROJECT'] = 'gen-lang-client-0554950952'
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'config/secrets/firebase-admin-key.json'

db = firestore.Client(project='gen-lang-client-0554950952')

print("📂 Checking Firestore collections...")
print()

# Check users collection
users_ref = db.collection('development_users_oauth')
users = list(users_ref.limit(5).stream())
print(f"👤 Users collection: {len(users)} documents")
for user in users:
    data = user.to_dict()
    print(f"  - {user.id}: {data.get('display_name')} ({data.get('email')})")

print()

# Check accounts collection
accounts_ref = db.collection('development_accounts_oauth')
accounts = list(accounts_ref.limit(5).stream())
print(f"💳 Accounts collection: {len(accounts)} documents")
for account in accounts:
    data = account.to_dict()
    print(f"  - {account.id}: tier={data.get('tier')}, active={data.get('is_active')}")


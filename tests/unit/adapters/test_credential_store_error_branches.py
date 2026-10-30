"""FirestoreCredentialStore error branches of slice 1a: a delete without an id writes nothing, and the cross-user
account listing skips a stored document that lacks its key. `save` refuses a credential without an integration through
the `missing` check above its type-checker guard, so that guard is unreachable and has no test."""
from tests.unit.adapters.test_credential_store_by_credential_kit import ALEX, CLARA, Store, cred


async def test_delete_with_an_empty_credential_id_deletes_nothing():
    store = Store()
    await store.repo.save(cred(subject="sub-1"))
    await store.repo.save(cred(CLARA, subject="sub-1"))
    before = {doc_id: dict(doc) for doc_id, doc in store.docs.items()}
    await store.repo.delete(ALEX, "")
    assert store.docs == before


async def test_list_by_account_skips_a_document_without_its_user_or_credential_id():
    store = Store()
    await store.repo.save(cred(subject="sub-1"))
    (kept,) = await store.repo.list_for_user(ALEX)
    account = {"realm": "microsoft", "subject": "sub-1"}
    store.docs["orphan-no-user"] = {"account": account, "credential_id": "cred-1", "kind": "microsoft_onedrive"}
    store.docs["orphan-no-credential"] = {"account": account, "user_id": CLARA, "kind": "microsoft_onedrive"}
    keys = await store.repo.list_by_account("microsoft", "sub-1")
    assert [(k.user_id, k.credential_id) for k in keys] == [(ALEX, kept.credential_id)]

# Prompt override tests on the Firestore test database

`test_prompt_4level_cloud.py` seeds tokens, a blueprint, an agent profile and account/user overrides into fixed
`test_integ_prompt_*` collections of the Firestore **test** database, assembles prompts with the real
`PromptAssemblyService` and Firestore repositories, and checks that USER > ACCOUNT > AGENT priority holds.

It uses the `client` fixture of `tests/cloud/conftest.py` (the guards: no emulator, database name ends in `-test`, the
test service account), so it runs only through `make test-cloud` (`RUN_CLOUD_TESTS=1`, `TEST_FIRESTORE_DATABASE`,
`TEST_SA_EMAIL`, `GOOGLE_CLOUD_PROJECT`); a bare `pytest` skips it. Each test deletes the documents it wrote.

# Tests

pytest + pytest-asyncio. asyncio_mode=auto.

## Running

```bash
make test              # All
make test-unit         # tests/unit/
make test-integration  # tests/integration/
make test-e2e-all      # E2E all agents (against real APIs)
```

## Structure

```
tests/
  conftest.py      — Shared fixtures: mock_env_config, mock_llm_service, mock_repository
  unit/            — No external dependencies. AsyncMock for ports.
  integration/     — With mocked external services.
  performance/     — Benchmarks (@pytest.mark.performance).
```

## Test Pattern

```python
class TestMyService:
    @pytest.fixture
    def service(self, mock_repository):
        return MyService(repository=mock_repository)

    @pytest.mark.asyncio
    async def test_success(self, service, mock_repository):
        mock_repository.add_fact = AsyncMock(return_value="id123")
        result = await service.process(entity)
        assert result == "id123"
        mock_repository.add_fact.assert_called_once()
```

## Rules

- Port mocks: `AsyncMock(spec=PortClass)`.
- Marker `@pytest.mark.requirement("REQ-XXX")` for business requirements.
- One test class per service/agent.
- Arrange-Act-Assert.

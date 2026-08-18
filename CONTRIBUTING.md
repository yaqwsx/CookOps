# Contributing to CookOps

Issues and pull requests are welcome. Before making a substantial change,
please check the relevant [specification](specification/README.md) and explain
the intended behavior in the issue or pull request.

Keep changes focused, use English for source code and project documentation,
and include tests for changed behavior. Run the applicable local checks:

```sh
cd backend
uv sync --dev
uv run ruff check .
uv run mypy
cd ../frontend
npm ci
npm run test
npm run lint
npm run typecheck
cd ../oauth-server
npm ci
npm test
npm run check
```

Backend database tests require `TEST_DATABASE_URL`. The checked-in OAuth test
Compose file starts its `postgres` service on
internal port `5432`, but publishes no host port. Compose sets
`TEST_DATABASE_URL=postgresql://cookops_oauth_test:cookops_oauth_test@postgres:5432/cookops_oauth_test`
inside `oauth-test`; run those OAuth integration tests with:

```sh
docker compose -f spikes/mcp-oauth/compose.yaml run --build --rm oauth-test
```

For backend tests from the host, export `TEST_DATABASE_URL` with a PostgreSQL
URL reachable from the host, then run:

```sh
export TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE'
cd backend
uv run pytest
```

Pull requests should describe what changed, how it was tested, and any
remaining limitations. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md)
and report security issues privately as described in [SECURITY.md](SECURITY.md).

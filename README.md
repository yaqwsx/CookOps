# CookOps

CookOps is an open-source application for managing recipes, ingredients,
shopping, costs, and cooking plans. The product specification lives in
[`specification/`](specification/README.md).

## Local development

Requirements: Python 3.12+, `uv`, and Node.js 20.19+ (Node.js 22.x for the
OAuth server).

```sh
cd backend
uv sync --dev
uv run ruff check .
uv run mypy
cd ../frontend
npm ci
npm run test
npm run build
cd ../oauth-server
npm ci
npm test
```

Backend database tests require `TEST_DATABASE_URL`. The checked-in OAuth test
Compose file starts its `postgres` service on internal
port `5432` and does not publish that port to the host. Use it for the OAuth
integration tests; Compose sets
`TEST_DATABASE_URL=postgresql://cookops_oauth_test:cookops_oauth_test@postgres:5432/cookops_oauth_test`
inside the test container:

```sh
docker compose -f spikes/mcp-oauth/compose.yaml run --build --rm oauth-test
```

To run the backend database tests from the host, provide a PostgreSQL URL for
a database reachable from the host, for example:

```sh
export TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE'
cd backend
uv run pytest
```

For the deployment foundation, see [`deploy/README.md`](deploy/README.md).

## Project documents

- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)

CookOps is distributed under the [MIT License](LICENSE).

# Deployment foundation

Copy `.env.example` to `.env`, replace every placeholder, then run:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build
```

All published service ports bind to loopback; the host Apache virtual host remains
the only public entry point. Apache forwards the OAuth protocol path to its
loopback provider for browser interaction completion. It deliberately does **not**
mount MCP: FastAPI still needs an RFC 7662 verifier before that path may be added.

The bootstrap PostgreSQL user is used only while the empty volume is initialized.
The API uses `cookops_api` and the OAuth provider uses `cookops_oauth` in its own
`oauth` schema; set both URL-safe application passwords independently.

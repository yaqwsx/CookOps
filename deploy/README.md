# Deployment foundation

Copy `.env.example` to `.env`, replace every placeholder, then run:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build
```

All published service ports bind to loopback; the host Apache virtual host remains
the only public entry point. This foundation deliberately does **not** add Apache
OAuth or MCP routes: FastAPI still needs the authenticated consent transport and
RFC 7662 MCP verifier before those paths may be mounted.

The bootstrap PostgreSQL user is used only while the empty volume is initialized.
The API uses `cookops_api` and the OAuth provider uses `cookops_oauth` in its own
`oauth` schema; set both URL-safe application passwords independently.

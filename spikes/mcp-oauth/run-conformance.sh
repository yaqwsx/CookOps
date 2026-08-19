#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  printf 'usage: %s <running-mcp-url>\n' "$0" >&2
  exit 64
fi

url=$1
if ! node -e '
  try {
    const url = new URL(process.argv[1]);
    if (url.protocol !== "https:" || !url.hostname || url.username || url.password ||
        url.pathname !== "/mcp" || url.search || url.hash || /[\u0000-\u0020\u007f]/.test(process.argv[1])) throw Error();
  } catch { process.exit(1); }
' "$url"; then
  printf 'expected one safe HTTPS URL with exact path /mcp\n' >&2
  exit 64
fi

# Keep the official runner outside project dependencies: this spike is disposable.
exec docker compose -f "$(dirname "$0")/compose.yaml" run --rm conformance \
  --url "$url" --suite active

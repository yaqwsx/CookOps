#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  printf 'usage: %s <running-mcp-url>\n' "$0" >&2
  exit 64
fi

node_major=$(node -p 'Number(process.versions.node.split(".")[0])')
if (( node_major < 22 )); then
  printf 'MCP conformance 0.1.16 requires Node.js 22+ (found %s)\n' "$(node --version)" >&2
  exit 69
fi

# Keep the official runner outside project dependencies: this spike is disposable.
exec npx --yes @modelcontextprotocol/conformance@0.1.16 server \
  --url "$1" --suite active

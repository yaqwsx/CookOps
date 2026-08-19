#!/usr/bin/env bash
set -euo pipefail
script=$(cd "$(dirname "$0")" && pwd)/run-conformance.sh
for url in '' 'http://example.test/mcp' 'https://example.test/not-mcp' 'https://user:pass@example.test/mcp' 'https://example.test/mcp?x=1'; do
  if "$script" "$url" >/dev/null 2>&1; then exit 1; fi
done
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cat >"$tmp/docker" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >"$MOCK_ARGS"
exit 17
MOCK
chmod +x "$tmp/docker"
MOCK_ARGS="$tmp/args" PATH="$tmp:$PATH" "$script" https://example.test/mcp >/dev/null 2>&1 || status=$?
[[ ${status:-0} -eq 17 ]]
grep -F -- 'run --rm conformance --url https://example.test/mcp --suite active' "$tmp/args"

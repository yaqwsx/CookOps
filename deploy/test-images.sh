#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
docker build --quiet --tag cookops-api-image-test "$root/backend" >/dev/null
docker build --quiet --tag cookops-web-image-test "$root/frontend" >/dev/null
docker run --rm --entrypoint=nginx cookops-web-image-test -t

prohibited_proxy_route() {
    awk '
        $1 ~ /^ProxyPass(Match|Reverse)?$/ {
            route = $2
            if (route ~ /(^|[^[:alnum:]_])\/?(oauth|mcp)(\/|[^[:alnum:]_]|$)/ ||
                route ~ /^\/\.well-known\/(openid-configuration|oauth-authorization-server|oauth-protected-resource)(\/|$)/) {
                prohibited = 1
                exit
            }
        }
        END { exit prohibited ? 0 : 1 }
    ' "$@"
}
for proxy_rule in \
    'ProxyPass /mcp http://127.0.0.1:8000/' \
    'ProxyPass /oauth http://127.0.0.1:3000/' \
    'ProxyPass /.well-known/openid-configuration/oauth http://127.0.0.1:3000/' \
    'ProxyPassMatch ^/mcp http://127.0.0.1:8000/'; do
    if ! printf '%s\n' "$proxy_rule" | prohibited_proxy_route; then
        echo "OAuth/MCP proxy guard failed to recognize $proxy_rule" >&2
        exit 1
    fi
done
if printf '%s\n' 'ProxyPass /api/ http://oauth-server:3000/health/' | prohibited_proxy_route; then
    echo 'OAuth/MCP proxy guard incorrectly inspected an upstream target' >&2
    exit 1
fi
if prohibited_proxy_route "$root/deploy/apache/cookops.conf.example"; then
    echo 'OAuth or MCP routes must stay unmounted until their production boundaries exist' >&2
    exit 1
fi

if docker run --rm --env 'COOKOPS_TRUSTED_PROXY_IPS=*' cookops-api-image-test; then
    echo 'wildcard proxy trust unexpectedly accepted' >&2
    exit 1
fi

container_id=$(docker run --detach --publish 127.0.0.1::8080 cookops-web-image-test)
cleanup() {
    docker rm --force "$container_id" >/dev/null
}
trap cleanup EXIT
port=$(docker port "$container_id" 8080/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p')
test -n "$port"
for attempt in 1 2 3 4 5; do
    if curl --fail --silent --show-error "http://127.0.0.1:$port/health/live" | grep -qx 'ok'; then
        break
    fi
    sleep 1
done
curl --fail --silent --show-error "http://127.0.0.1:$port/health/live" | grep -qx 'ok'
curl --fail --silent --show-error "http://127.0.0.1:$port/not-a-file" | grep -q '<div id="root"></div>'

#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
docker build --quiet --tag cookops-api-image-test "$root/backend" >/dev/null
docker build --quiet --tag cookops-web-image-test "$root/frontend" >/dev/null
docker run --rm --entrypoint=nginx cookops-web-image-test -t

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

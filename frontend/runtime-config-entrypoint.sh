#!/bin/sh
set -eu

config=/tmp/cookops-runtime-config.js
provider=${COOKOPS_HUMAN_AUTH_PROVIDER:-dummy}

case "$provider" in
    dummy)
        printf '%s\n' 'window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };' >"$config"
        ;;
    google)
        client_id=${COOKOPS_GOOGLE_CLIENT_ID:?COOKOPS_GOOGLE_CLIENT_ID must be set for Google authentication}
        case "$client_id" in
            *[!A-Za-z0-9._:-]*)
                echo 'COOKOPS_GOOGLE_CLIENT_ID contains unsupported characters' >&2
                exit 1
                ;;
        esac
        printf 'window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "google", googleClientId: "%s" } };\n' "$client_id" >"$config"
        ;;
    *)
        echo 'COOKOPS_HUMAN_AUTH_PROVIDER must be dummy or google' >&2
        exit 1
        ;;
esac

exec /docker-entrypoint.sh "$@"

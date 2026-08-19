#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
project="cookops_backup_restore_$$"
work=$(mktemp -d "${TMPDIR:-/tmp}/cookops-backup-restore.XXXXXX")
archive_dir="$work/archives"
restore_dir="$work/restore"
mkdir -m 700 "$archive_dir" "$restore_dir"

cleanup() {
    docker compose --project-name "$project" --env-file "$root/deploy/.env.example" \
        -f "$root/deploy/compose.yaml" down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

export COOKOPS_BACKUP_DIR="$archive_dir"
export COOKOPS_RESTORE_DIR="$restore_dir"
export COOKOPS_BACKUP_ARCHIVE=cookops-roundtrip.zip
export COOKOPS_RESTORE_MEDIA_SUBDIR=restored-media
# Use syntactically valid disposable credentials; no service is exposed publicly by this test.
export COOKOPS_GOOGLE_CLIENT_ID=backup-restore-test-client
export COOKOPS_BROWSER_SESSION_HMAC_KEY=BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc
export OAUTH_COOKIE_KEYS=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
export OAUTH_RESOURCE_SERVER_SECRET=backup-restore-resource-secret-32-characters
export OAUTH_JWKS='{"keys":[{}]}'
export OAUTH_ADAPTER_SECRET_BASE64URL=BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc
export OAUTH_INTERACTION_APPROVAL_SECRET_BASE64URL=CAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg
export OAUTH_APPROVAL_API_CREDENTIAL_BASE64URL=CQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQk
export OAUTH_INTERACTION_DETAILS_API_CREDENTIAL_BASE64URL=CgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgo
export OAUTH_GRANTS_API_CREDENTIAL_BASE64URL=DAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA

compose() {
    docker compose --project-name "$project" --env-file "$root/deploy/.env.example" \
        -f "$root/deploy/compose.yaml" "$@"
}

run_quiet() {
    log="$work/compose-step.log"
    if "$@" >"$log" 2>&1; then
        return 0
    fi
    echo "compose step failed: $*" >&2
    cat "$log" >&2
    return 1
}

compose up --detach postgres >/dev/null
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if compose exec -T postgres pg_isready -U cookops -d cookops >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
compose exec -T postgres pg_isready -U cookops -d cookops >/dev/null

# Build the production migration images through Compose; no mock pg_dump/pg_restore is used.
run_quiet compose run --build --rm api-migrate
run_quiet compose run --build --rm oauth-migrate

compose exec -T -e PGPASSWORD=replace-with-url-safe-api-db-password postgres \
    psql -v ON_ERROR_STOP=1 -U cookops_api -d cookops <<'SQL' >/dev/null
CREATE TABLE public.backup_roundtrip_probe (id integer PRIMARY KEY, value text NOT NULL);
INSERT INTO public.backup_roundtrip_probe VALUES (1, 'before-backup');
INSERT INTO public.users
    (id, display_name, verified_email, normalized_email, preferred_locale, created_at)
VALUES
    ('00000000-0000-0000-0000-000000000001', 'Backup Test User', 'backup@example.test', 'backup@example.test', 'en', '2026-01-01T00:00:00Z');
INSERT INTO public.organizations
    (id, name, description, default_currency, created_at, created_by_user_id)
VALUES
    ('00000000-0000-0000-0000-000000000002', 'Backup Test Organization', NULL, 'CZK', '2026-01-01T00:00:00Z', '00000000-0000-0000-0000-000000000001');
INSERT INTO public.events
    (id, organization_id, name, start_date, end_date, location, general_note,
     base_expected_attendance, budget_amount, currency, created_at, created_by_user_id, lifecycle)
VALUES
    ('00000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000002',
     'Backup Test Event', '2026-01-01', '2026-01-01', NULL, NULL, 1, 0, 'CZK',
     '2026-01-01T00:00:00Z', '00000000-0000-0000-0000-000000000001', 'active');
INSERT INTO public.receipts
    (id, organization_id, event_id, title, total_amount, currency, receipt_date, note,
     created_at, created_by_user_id, last_modified_at, last_modified_by_user_id)
VALUES
    ('00000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000002',
     '00000000-0000-0000-0000-000000000003', 'Backup Test Receipt', 20, 'CZK',
     '2026-01-01', NULL, '2026-01-01T00:00:00Z', '00000000-0000-0000-0000-000000000001',
     '2026-01-01T00:00:00Z', '00000000-0000-0000-0000-000000000001');
INSERT INTO public.receipt_attachments
    (id, organization_id, receipt_id, storage_state, media_type, position_key,
     storage_object_key, thumbnail_object_key, byte_size, pixel_width, pixel_height,
     content_hash, created_at, created_by_user_id, finalized_at, finalized_by_user_id)
VALUES
    ('00000000-0000-0000-0000-000000000005', '00000000-0000-0000-0000-000000000002',
     '00000000-0000-0000-0000-000000000004', 'ready', 'image/jpeg', 'A',
     'objects/roundtrip-object.bin', 'thumbnails/roundtrip-thumbnail.webp', 20, 1, 1,
     decode('bab4e8b2c12f76d051af1a6db19d752f3848931e1dae7100893455071d5bd8d4', 'hex'),
     '2026-01-01T00:00:00Z', '00000000-0000-0000-0000-000000000001',
     '2026-01-01T00:00:00Z', '00000000-0000-0000-0000-000000000001');
SQL

run_quiet compose run --rm --no-deps --entrypoint sh api -c \
    'set -eu; mkdir -p /var/lib/cookops/receipts/objects /var/lib/cookops/receipts/thumbnails; printf "%s" object-before-backup > /var/lib/cookops/receipts/objects/roundtrip-object.bin; printf "%s" thumbnail-before-backup > /var/lib/cookops/receipts/thumbnails/roundtrip-thumbnail.webp' \
    >/dev/null

run_quiet compose run --rm backup
archive="$archive_dir/$COOKOPS_BACKUP_ARCHIVE"
test -f "$archive"
test "$(stat -c '%a' "$archive")" = 600

compose exec -T -e PGPASSWORD=replace-with-url-safe-api-db-password postgres \
    psql -v ON_ERROR_STOP=1 -U cookops_api -d cookops \
    -c "UPDATE public.backup_roundtrip_probe SET value = 'after-backup' WHERE id = 1" >/dev/null
run_quiet compose run --rm --no-deps --entrypoint sh api -c \
    'set -eu; rm -f /var/lib/cookops/receipts/objects/roundtrip-object.bin /var/lib/cookops/receipts/thumbnails/roundtrip-thumbnail.webp' \
    >/dev/null

run_quiet compose run --rm restore
grep -q 'verified 1 READY receipt attachment media references' "$work/compose-step.log"
run_quiet compose run --rm oauth-migrate

probe=$(compose exec -T -e PGPASSWORD=replace-with-url-safe-api-db-password postgres \
    psql -v ON_ERROR_STOP=1 -At -U cookops_api -d cookops \
    -c 'SELECT value FROM public.backup_roundtrip_probe WHERE id = 1')
test "$probe" = before-backup
metadata=$(compose exec -T -e PGPASSWORD=replace-with-url-safe-api-db-password postgres \
    psql -v ON_ERROR_STOP=1 -At -U cookops_api -d cookops \
    -c "SELECT storage_state || '|' || storage_object_key || '|' || thumbnail_object_key || '|' || byte_size::text || '|' || encode(content_hash, 'hex') FROM public.receipt_attachments WHERE id = '00000000-0000-0000-0000-000000000005'")
test "$metadata" = 'ready|objects/roundtrip-object.bin|thumbnails/roundtrip-thumbnail.webp|20|bab4e8b2c12f76d051af1a6db19d752f3848931e1dae7100893455071d5bd8d4'
run_quiet compose run --rm --no-deps --entrypoint sh restore -c \
    'set -eu; test "$(cat /var/lib/cookops/restore/$COOKOPS_RESTORE_MEDIA_SUBDIR/objects/roundtrip-object.bin)" = object-before-backup; test "$(cat /var/lib/cookops/restore/$COOKOPS_RESTORE_MEDIA_SUBDIR/thumbnails/roundtrip-thumbnail.webp)" = thumbnail-before-backup'

echo "backup/restore round trip passed (project=$project)"

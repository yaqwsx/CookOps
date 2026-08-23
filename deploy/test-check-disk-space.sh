#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/cookops-disk-space.XXXXXX")
trap 'rm -rf "$work"' EXIT

cat >"$work/df" <<'EOF'
#!/bin/sh
case "$1:$2" in
  -P:/postgres|-Pi:/postgres) echo "Filesystem 1024-blocks Used Available Capacity Mounted on"; echo "x 100 50 50 50% /postgres";;
  -P:/media|-Pi:/media) echo "Filesystem 1024-blocks Used Available Capacity Mounted on"; echo "x 100 90 10 90% /media";;
  -P:/backup|-Pi:/backup) echo "Filesystem 1024-blocks Used Available Capacity Mounted on"; echo "x 100 95 5 95% /backup";;
  *) exit 1;;
esac
EOF
chmod 700 "$work/df"

PATH="$work:$PATH" COOKOPS_POSTGRES_DATA_TARGET=/postgres COOKOPS_RECEIPT_MEDIA_TARGET=/media COOKOPS_BACKUP_DIR_TARGET=/backup "$root/check-disk-space.sh" >/dev/null 2>&1 && exit 1 || status=$?
[ "$status" -eq 2 ]

cat >"$work/df" <<'EOF'
#!/bin/sh
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "x 100 85 15 85% $2"
EOF
chmod 700 "$work/df"
set +e
COOKOPS_POSTGRES_DATA_TARGET=/postgres COOKOPS_RECEIPT_MEDIA_TARGET=/media COOKOPS_BACKUP_DIR_TARGET=/backup PATH="$work:$PATH" "$root/check-disk-space.sh" >/dev/null 2>&1
status=$?
set -e
[ "$status" -eq 1 ]

cat >"$work/df" <<'EOF'
#!/bin/sh
case "$2" in /missing) exit 1;; esac
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "x 100 50 50 50% $2"
EOF
chmod 700 "$work/df"
PATH="$work:$PATH" COOKOPS_POSTGRES_DATA_TARGET=/postgres COOKOPS_RECEIPT_MEDIA_TARGET=/media COOKOPS_BACKUP_DIR_TARGET=/backup "$root/check-disk-space.sh" >/dev/null

set +e
COOKOPS_POSTGRES_DATA_TARGET=/missing COOKOPS_RECEIPT_MEDIA_TARGET=/media COOKOPS_BACKUP_DIR_TARGET=/backup PATH="$work:$PATH" "$root/check-disk-space.sh" >/dev/null 2>&1
status=$?
set -e
[ "$status" -eq 2 ]

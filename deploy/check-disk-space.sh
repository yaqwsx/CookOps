#!/bin/sh
set -eu

warning=${COOKOPS_DISK_WARNING_PERCENT:-80}
critical=${COOKOPS_DISK_CRITICAL_PERCENT:-90}
timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

case "$warning" in ''|*[!0-9]*) echo "disk_space_error=invalid_warning_threshold timestamp=$timestamp" >&2; exit 2;; esac
case "$critical" in ''|*[!0-9]*) echo "disk_space_error=invalid_critical_threshold timestamp=$timestamp" >&2; exit 2;; esac
[ "$warning" -lt "$critical" ] && [ "$critical" -le 100 ] || { echo "disk_space_error=invalid_threshold_order timestamp=$timestamp" >&2; exit 2; }

overall=0
check_target() {
    name=$1
    target=$2
    if [ -z "$target" ]; then
        echo "disk_space timestamp=$timestamp target=$name status=critical reason=missing_target"
        overall=2
        return
    fi
    if ! df -P "$target" >/dev/null 2>&1 || ! df -Pi "$target" >/dev/null 2>&1; then
        echo "disk_space timestamp=$timestamp target=$name status=critical reason=missing_or_unreadable_target path=$target"
        overall=2
        return
    fi
    blocks=$(df -P "$target" 2>/dev/null | awk 'NR == 2 { gsub(/%/, "", $5); print $5 }')
    inodes=$(df -Pi "$target" 2>/dev/null | awk 'NR == 2 { gsub(/%/, "", $5); print $5 }')
    case "$blocks:$inodes" in
      *[!0-9:]*|:*)
        echo "disk_space timestamp=$timestamp target=$name status=critical reason=missing_or_unreadable_target path=$target"
        overall=2
        return
        ;;
    esac
    status=healthy
    [ "$blocks" -ge "$warning" ] || [ "$inodes" -ge "$warning" ] && status=warning
    [ "$blocks" -ge "$critical" ] || [ "$inodes" -ge "$critical" ] && status=critical
    echo "disk_space timestamp=$timestamp target=$name status=$status blocks_used_percent=$blocks inodes_used_percent=$inodes path=$target"
    [ "$status" = critical ] && overall=2 || { [ "$status" = warning ] && [ "$overall" -lt 1 ] && overall=1 || :; }
}

check_target postgres_data "${COOKOPS_POSTGRES_DATA_TARGET:-}"
check_target receipt_media "${COOKOPS_RECEIPT_MEDIA_TARGET:-}"
check_target backup_bind "${COOKOPS_BACKUP_DIR_TARGET:-${COOKOPS_BACKUP_DIR:-}}"
exit "$overall"

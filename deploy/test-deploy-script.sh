#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/cookops-deploy-test.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
git -C "$work" init -q
git -C "$work" config user.email test@example.invalid
git -C "$work" config user.name test
git -C "$work" config commit.gpgsign false
mkdir -p "$work/deploy"
cp "$root/deploy/deploy.sh" "$work/deploy/deploy.sh"
: >"$work/deploy/compose.yaml"
: >"$work/deploy/.env"
git -C "$work" add .
git -C "$work" commit -qm fixture
revision=$(git -C "$work" rev-parse HEAD)
git -C "$work" rm -q deploy/compose.yaml
git -C "$work" commit -qm 'remove operator compose'
linked="$work-linked"
git -C "$work" worktree add -q "$linked" HEAD

output=$(COOKOPS_APPLICATION_DIR="$linked" \
    COOKOPS_PUBLIC_HEALTH_URL=https://cookops.example/health/ready \
    "$root/deploy/deploy.sh" --dry-run "$revision")
printf '%s\n' "$output" | grep -Fqx "DRY RUN: would deploy $revision"
printf '%s\n' "$output" | grep -F 'DRY RUN: would build immutable images tagged ' >/dev/null

grep -Fq 'refs/cookops-deploy/' "$root/deploy/deploy.sh"
grep -Fq 'update-ref -d "$fetch_ref"' "$root/deploy/deploy.sh"
if grep -Fq 'FETCH_HEAD' "$root/deploy/deploy.sh"; then
    echo 'deployment still resolves shared FETCH_HEAD' >&2
    exit 1
fi

if COOKOPS_APPLICATION_DIR="$linked" COOKOPS_PUBLIC_HEALTH_URL=https://cookops.example/health/ready \
    "$root/deploy/deploy.sh" --dry-run not-a-revision >/dev/null 2>&1; then
    echo "invalid revision was accepted" >&2
    exit 1
fi
if COOKOPS_APPLICATION_DIR="$linked" COOKOPS_PUBLIC_HEALTH_URL=https://cookops.example/health/ready \
    COOKOPS_GIT_REMOTE=-bad "$root/deploy/deploy.sh" --dry-run "$revision" >/dev/null 2>&1; then
    echo 'remote option injection was accepted' >&2
    exit 1
fi
if COOKOPS_APPLICATION_DIR="$linked" COOKOPS_PUBLIC_HEALTH_URL=http://cookops.example/health/ready \
    "$root/deploy/deploy.sh" --dry-run "$revision" >/dev/null 2>&1; then
    echo 'non-HTTPS public health URL was accepted' >&2
    exit 1
fi
printf '%s\n' 'deploy dry-run contract passed'

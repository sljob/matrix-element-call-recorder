#!/bin/bash
# ==========================================================================
#  Provide the recorder core (p63-observer.js) in ./vendor/.
#
#  Two modes, and the first one needs nothing from you:
#
#  1. LOCAL (default). If p63-observer.js already sits in this repository at
#     src/recorder/ (or next to it, in the parent repository), it is copied
#     into vendor/ and hashed. A fresh clone is installable with no network
#     access and no commit SHA. This is the normal path.
#
#  2. PINNED. Set RECORDER_UPSTREAM_COMMIT to a full 40-character SHA to fetch
#     the observer from sljob/matrix-element-call-recorder at that commit
#     instead. Use this when you want a byte-identical copy of a reviewed
#     release rather than whatever is in your working tree.
#
#         RECORDER_UPSTREAM_COMMIT=<40-char sha> bash tools/fetch-upstream.sh
#
#  0.1.0 and 0.1.1 only had mode 2, and refused to install without a SHA even
#  though the file was already in the repository. That made a plain clone dead
#  on arrival; hence the default change.
# ==========================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${RECORDER_UPSTREAM_REPO:-sljob/matrix-element-call-recorder}"
COMMIT="${RECORDER_UPSTREAM_COMMIT:-${SOURCE_COMMIT:-}}"
NAME="p63-observer.js"
OUT="$ROOT/vendor/$NAME"

mkdir -p "$ROOT/vendor"
chmod 755 "$ROOT/vendor"

# ---------------------------------------------------------------- mode 1
if [[ -z "$COMMIT" ]]; then
    echo "No commit SHA given: looking for an existing copy in this tree."

    found=""
    for cand in \
        "$ROOT/src/recorder/$NAME" \
        "$ROOT/../src/recorder/$NAME" \
        "$ROOT/../../src/recorder/$NAME"
    do
        if [[ -s "$cand" ]]; then
            found="$cand"
            break
        fi
    done

    if [[ -z "$found" ]]; then
        cat >&2 <<EOF
REFUSING: no local copy of $NAME and no commit to fetch it from.

Looked in:
  $ROOT/src/recorder/$NAME
  $ROOT/../src/recorder/$NAME
  $ROOT/../../src/recorder/$NAME

Either put the file at src/recorder/$NAME, or fetch a pinned release:

    RECORDER_UPSTREAM_COMMIT=<40-char sha> bash tools/fetch-upstream.sh
EOF
        exit 1
    fi

    cp -f "$found" "$OUT"
    chmod 644 "$OUT"
    SHA="$(sha256sum "$OUT" | awk '{print $1}')"
    {
        echo "repo    = local:$found"
        echo "commit  = $(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
        echo "fetched = $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "note    = copied from this repository, not fetched from the network"
        echo
        echo "$SHA  vendor/$NAME"
    } > "$ROOT/vendor/SOURCE"
    chmod 644 "$ROOT/vendor/SOURCE"

    echo "ok   $NAME ($(wc -c <"$OUT" | tr -d ' ') bytes)"
    echo "     from $found"
    echo "     sha256 $SHA"
    echo
    echo "Wrote $ROOT/vendor/SOURCE"
    exit 0
fi

# ---------------------------------------------------------------- mode 2
if [[ ! "$COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "REFUSING: '$COMMIT' is not a full 40-char SHA." >&2
    echo "Branches and tags move; a pin that can move is not a pin." >&2
    echo "Leave RECORDER_UPSTREAM_COMMIT unset to use the copy in this tree." >&2
    exit 1
fi

echo "Fetching $NAME from $REPO at ${COMMIT:0:12}"

url="https://raw.githubusercontent.com/$REPO/$COMMIT/src/recorder/$NAME"
printf 'fetch %-24s ' "$NAME"
if ! curl -fsSL --max-time 180 "$url" -o "$OUT.tmp"; then
    printf 'FAILED\n'
    echo "  url: $url" >&2
    echo "  Check that this path exists at that commit." >&2
    rm -f "$OUT.tmp"
    echo
    echo "Nothing was installed. Unset RECORDER_UPSTREAM_COMMIT to fall back" >&2
    echo "to the copy that already exists in this repository." >&2
    exit 1
fi

if [[ ! -s "$OUT.tmp" ]]; then
    printf 'EMPTY\n'
    rm -f "$OUT.tmp"
    exit 1
fi

mv "$OUT.tmp" "$OUT"
chmod 644 "$OUT"
printf 'ok (%s bytes)\n' "$(wc -c <"$OUT" | tr -d ' ')"

SHA="$(sha256sum "$OUT" | awk '{print $1}')"
{
    echo "repo    = $REPO"
    echo "commit  = $COMMIT"
    echo "fetched = $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "note    = pinned recorder core; do not edit, re-fetch instead"
    echo
    echo "$SHA  vendor/$NAME"
} > "$ROOT/vendor/SOURCE"
chmod 644 "$ROOT/vendor/SOURCE"

echo
echo "Wrote $ROOT/vendor/SOURCE"
echo "Commit vendor/SOURCE, not the JavaScript:"
echo "  git add vendor/SOURCE && git commit -m 'pin recorder core at ${COMMIT:0:12}'"

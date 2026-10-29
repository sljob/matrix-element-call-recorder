#!/bin/bash
# ==========================================================================
#  Matrix Element Call Recorder Plugin v2.2.0 (nginx) — Launcher
#
#  Thin wrapper: validates host prerequisites, parses CLI flags or reads
#  INSTALL_ACTION from the answers file, enforces safe file permissions on
#  answers, and hands execution over to installer/install.py.
#
#  Modes:
#    --check      Verify server readiness & missing components (read-only)
#    --install    Install or upgrade the plugin (Module API + backend)
#    --repair     Re-apply mounts, nginx snippet & Element module registration
#    --uninstall  Cleanly remove plugin (preserves recordings & profile-spk)
# ==========================================================================
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHASE=""
ANSWERS=""
EXTRA_ARGS=()

R=$'\033[0m'; B=$'\033[1m'; C=$'\033[36m'; Y=$'\033[33m'; E=$'\033[31m'

usage() {
    cat <<'EOF'
Usage:
  bash install.sh [--check|--install|--repair|--uninstall] [--answers] <answers.env>

Modes (CLI flag overrides INSTALL_ACTION in the answers file):
  --check        Read-only discovery, component verification & installation plan.
  --install      Install/upgrade overlay services, nginx snippet & Element ESM module.
  --repair       Re-apply Element Web module (/modules/recorder), config & routes
                 (alias: --reapply-ui).
  --uninstall    Remove plugin containers, routes & Element module (keeps recordings
                 and bot E2EE session profile-spk).

Options:
  -a, --answers <file>   Path to the answers file (default: ./answers.env if present)
  -y, --yes              Non-interactive mode (auto-accept prompts where safe)
  -h, --help             Show this help message

Examples:
  cp answers.example /root/answers.env && chmod 600 /root/answers.env
  bash install.sh --check     /root/answers.env
  bash install.sh --install   /root/answers.env
  bash install.sh --repair    /root/answers.env
  bash install.sh --uninstall /root/answers.env
  bash install.sh --answers   /root/answers.env   # uses INSTALL_ACTION from file
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check|--install|--repair|--uninstall)
            PHASE="${1#--}"
            shift
            ;;
        --reapply-ui)
            PHASE="repair"
            shift
            ;;
        -a|--answers)
            if [ $# -lt 2 ]; then
                printf '%smissing file path after %s%s\n\n' "$E" "$1" "$R" >&2
                usage
                exit 2
            fi
            ANSWERS="$2"
            shift 2
            ;;
        --answers=*)
            ANSWERS="${1#*=}"
            shift
            ;;
        -y|--yes)
            EXTRA_ARGS+=("--yes")
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --*)
            printf '%sunknown option: %s%s\n\n' "$E" "$1" "$R" >&2
            usage
            exit 2
            ;;
        *)
            if [ -z "$ANSWERS" ]; then
                ANSWERS="$1"
            else
                printf '%sunexpected argument: %s%s\n\n' "$E" "$1" "$R" >&2
                usage
                exit 2
            fi
            shift
            ;;
    esac
done

# Default answers file location if omitted on CLI
if [ -z "$ANSWERS" ] && [ -f "$HERE/answers.env" ]; then
    ANSWERS="$HERE/answers.env"
fi

if [ -z "$ANSWERS" ]; then
    printf '%sanswers file not given%s\n\n' "$E" "$R" >&2
    usage
    exit 2
fi

if [ ! -f "$ANSWERS" ]; then
    printf '%sanswers file not found: %s%s\n' "$E" "$ANSWERS" "$R" >&2
    exit 1
fi

# Read a key=value from the answers file without sourcing arbitrary shell code
read_answer_key() {
    local key="$1" file="$2"
    awk -F= -v k="$key" '
        /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
        {
            kname = $1
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", kname)
            if (kname == k) {
                val = substr($0, index($0, "=") + 1)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", val)
                gsub(/^["'\'']|["'\'']$/, "", val)
                print val
            }
        }
    ' "$file" | tail -n 1
}

# Resolve mode from INSTALL_ACTION (or legacy PHASE) in answers file when not passed on CLI
if [ -z "$PHASE" ]; then
    PHASE="$(read_answer_key "INSTALL_ACTION" "$ANSWERS")"
    if [ -z "$PHASE" ]; then
        PHASE="$(read_answer_key "PHASE" "$ANSWERS")"
    fi
    PHASE="${PHASE:-check}"
fi

case "$PHASE" in
    check|install|repair|uninstall) ;;
    reapply-ui) PHASE="repair" ;;
    *)
        printf '%sunknown mode: %s (expected: check | install | repair | uninstall)%s\n\n' \
            "$E" "$PHASE" "$R" >&2
        usage
        exit 2
        ;;
esac

# A world-readable answers file leaks recorder bot passwords and Synapse secrets.
# Refuse on 644/755; auto-tighten or instruct operator.
perms="$(stat -c '%a' "$ANSWERS" 2>/dev/null || stat -f '%Lp' "$ANSWERS" 2>/dev/null || echo "")"
case "$perms" in
    600|400|"") ;;
    *)
        printf '%sanswers file %s has insecure mode %s — run: chmod 600 %s%s\n' \
            "$E" "$ANSWERS" "$perms" "$ANSWERS" "$R" >&2
        exit 1
        ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    printf '%srun as root%s (docker socket and Element Web module mounts require root)\n' \
        "$E" "$R" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    printf '%spython3 is required on the host (apt-get install -y python3)%s\n' "$E" "$R" >&2
    exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'; then
    printf '%spython 3.8+ required, found: %s%s\n' "$E" "$(python3 -V 2>&1)" "$R" >&2
    exit 1
fi

printf '%sMatrix Element Call Recorder Plugin v2.0%s — mode %s%s%s, answers %s\n\n' \
    "$B" "$R" "$C" "$PHASE" "$R" "$ANSWERS"

exec python3 "$HERE/installer/install.py" \
    --phase "$PHASE" \
    --answers "$ANSWERS" \
    "${EXTRA_ARGS[@]}"

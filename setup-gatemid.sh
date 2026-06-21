#!/usr/bin/env bash
# ============================================================================
# setup-gatemid.sh — Configure coding agents to use a Gatemid proxy
#
# Switches Claude Code / OpenCode to point at a gatemid URL + API key with
# all models routed through team-smart-router (deepseek-pro as fallback).
#
# Usage:
#   ./setup-gatemid.sh               interactive install
#   ./setup-gatemid.sh --uninstall   revert changes
#   ./setup-gatemid.sh --help        this message
#
# Environment variables:
#   GATEMID_URL      base URL of the proxy (default: http://localhost:4000)
#   GATEMID_API_KEY  API key for the proxy
#
# Examples:
#   GATEMID_URL=https://my-gatemid.com/v1 GATEMID_API_KEY=sk-xxx ./setup-gatemid.sh
# ============================================================================

set -euo pipefail

# ── Colours & helpers ─────────────────────────────────────────────────────

RESET="\033[0m"
BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
GREY="\033[90m"

ok()   { echo -e "  ${GREEN}✔${RESET} $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET} $1"; }
err()  { echo -e "  ${RED}✖${RESET} $1"; }
info() { echo -e "  ${CYAN}ℹ${RESET} $1"; }
header() { echo -e "\n${BOLD}── $1 ──${RESET}\n"; }

# ── OS detection ──────────────────────────────────────────────────────────

detect_os() {
    case "$(uname -s)" in
        Darwin*)  echo "macos" ;;
        Linux*)   echo "linux" ;;
        CYGWIN*|MINGW*|MSYS*) echo "windows" ;;
        *)        echo "unknown" ;;
    esac
}

OS=$(detect_os)

# ── Path helpers ──────────────────────────────────────────────────────────

expand_home() {
    local p="$1"
    echo "${p/#\~/$HOME}"
}

# ── JSON helpers (via python3, always available on dev machines) ─────────

json_set_env() {
    local file="$1" url="$2" key="$3"
    python3 -c "
import json, sys
fp = sys.argv[1]
url = sys.argv[2]
key = sys.argv[3]
try:
    with open(fp) as f:
        cfg = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}
env = cfg.get('env', {})
env['ANTHROPIC_BASE_URL'] = url.rstrip('/')
env['ANTHROPIC_API_KEY'] = key
env['ANTHROPIC_MODEL'] = 'team-smart-router'
env['ANTHROPIC_DEFAULT_HAIKU_MODEL'] = 'team-smart-router'
env['ANTHROPIC_DEFAULT_SONNET_MODEL'] = 'team-smart-router'
env['ANTHROPIC_DEFAULT_OPUS_MODEL'] = 'deepseek-pro'
cfg['env'] = env
with open(fp, 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
" "$file" "$url" "$key"
}

json_remove_gatemid_env() {
    local file="$1"
    python3 -c "
import json, sys
fp = sys.argv[1]
try:
    with open(fp) as f:
        cfg = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    sys.exit(0)
env = cfg.get('env', {})
gatemid_keys = {
    'ANTHROPIC_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
    'ANTHROPIC_DEFAULT_OPUS_MODEL',
}
dirty = False
for k in list(env.keys()):
    if k in gatemid_keys:
        del env[k]
        dirty = True
if not dirty:
    sys.exit(1)
cfg['env'] = env
with open(fp, 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
" "$file"
}

# ── Backup ─────────────────────────────────────────────────────────────────

backup_file() {
    local path="$1"
    [[ -f "$path" ]] || return 1
    local ts
    ts=$(date +%Y%m%d_%H%M%S)
    local backup="${path}.backup.${ts}"
    cp -p "$path" "$backup"
    ok "backed up → ${backup/$HOME/\~}"
    echo "$backup"
}

# ── Claude Code ───────────────────────────────────────────────────────────

CLAUDE_SETTINGS="$HOME/.claude/settings.json"

install_claude_code() {
    local url="$1" key="$2"
    mkdir -p "$(dirname "$CLAUDE_SETTINGS")"

    if [[ -f "$CLAUDE_SETTINGS" ]]; then
        backup_file "$CLAUDE_SETTINGS"
    fi

    json_set_env "$CLAUDE_SETTINGS" "$url" "$key"
    ok "wrote ${CLAUDE_SETTINGS/$HOME/\~}"
}

uninstall_claude_code() {
    if [[ ! -f "$CLAUDE_SETTINGS" ]]; then
        info "no Claude Code settings found"
        return
    fi

    if json_remove_gatemid_env "$CLAUDE_SETTINGS"; then
        ok "gatemid env vars removed from Claude Code settings"
    else
        info "no gatemid env vars to remove from Claude Code"
    fi
}

# ── OpenCode ──────────────────────────────────────────────────────────────

# OpenCode uses env vars (OPENAI_ENDPOINT, OPENAI_API_KEY).
# We inject them into the user's shell rc file.

detect_rc_file() {
    local shell
    shell=$(basename "${SHELL:-bash}")

    case "$shell" in
        zsh)
            echo "$HOME/.zshrc"
            ;;
        bash)
            if [[ "$OS" == "macos" ]]; then
                echo "$HOME/.bash_profile"
            else
                echo "$HOME/.bashrc"
            fi
            ;;
        *)
            echo "$HOME/.profile"
            ;;
    esac
}

RC_FILE=$(detect_rc_file)
OPENCODE_MARKER_START="# --- gatemid proxy (added by setup-gatemid.sh) ---"
OPENCODE_MARKER_END="# --- end gatemid proxy ---"

install_opencode() {
    local url="$1" key="$2"

    # Sanitise: strip trailing slash
    url="${url%/}"

    # Backup rc file if it exists
    if [[ -f "$RC_FILE" ]]; then
        backup_file "$RC_FILE"
    fi

    # Write env block to rc file
    {
        echo ""
        echo "$OPENCODE_MARKER_START"
        echo "export OPENAI_ENDPOINT='$url'"
        echo "export OPENAI_API_KEY='$key'"
        echo "export OPENAI_BASE_URL='$url'"
        echo "$OPENCODE_MARKER_END"
    } >> "$RC_FILE"

    ok "env vars appended to ${RC_FILE/$HOME/\~}"
    echo ""
    info "After installing, launch OpenCode and select a model from"
    info "the OpenAI provider. The 'team-smart-router' model should"
    info "appear in the model list."
}

uninstall_opencode() {
    local removed=false

    if [[ -f "$RC_FILE" ]]; then
        # Create a temp file without the marker block
        local tmp
        tmp=$(mktemp)
        local inside=false
        while IFS= read -r line; do
            if [[ "$line" == "$OPENCODE_MARKER_START" ]]; then
                inside=true
                continue
            fi
            if $inside; then
                if [[ "$line" == "$OPENCODE_MARKER_END" ]]; then
                    inside=false
                    # strip trailing blank lines before the block
                    removed=true
                    continue
                fi
                continue
            fi
            echo "$line" >> "$tmp"
        done < "$RC_FILE"

        if $removed; then
            # Trim trailing blank lines from temp, then write back
            # Use awk to strip trailing empty lines
            awk 'NF { last=NR } { lines[NR]=$0 } END { for(i=1;i<=last;i++) print lines[i] }' "$tmp" > "${tmp}.clean"
            mv "${tmp}.clean" "$RC_FILE"
            ok "gatemid env vars removed from ${RC_FILE/$HOME/\~}"
        else
            rm -f "$tmp"
        fi
    fi

    if ! $removed; then
        info "no gatemid config found for OpenCode"
    fi
}

# ── Agent helpers ─────────────────────────────────────────────────────────

agent_name() {
    case "$1" in
        1) echo "Claude Code" ;;
        2) echo "OpenCode" ;;
    esac
}

agent_install() {
    case "$1" in
        1) install_claude_code "$2" "$3" ;;
        2) install_opencode "$2" "$3" ;;
    esac
}

agent_uninstall() {
    case "$1" in
        1) uninstall_claude_code ;;
        2) uninstall_opencode ;;
    esac
}

# ── Interactive prompts ───────────────────────────────────────────────────

pick_agent() {
    local mode="${1:-install}"
    local verb
    [[ "$mode" == "uninstall" ]] && verb="unconfigure" || verb="configure"

    echo ""
    echo "Supported coding agents:"
    echo "  1. Claude Code"
    echo "  2. OpenCode"
    echo "  q. Quit"
    echo ""

    local choice
    read -rp "Select an agent to ${verb} (number): " choice

    case "$choice" in
        q|Q) return 1 ;;
        1|2)
            AGENT_KEY=$choice
            return 0
            ;;
        *)
            warn "invalid selection"
            pick_agent "$mode"
            ;;
    esac
}

get_url_and_key() {
    local url key

    url="${GATEMID_URL:-http://localhost:4000}"
    if [[ -z "$url" ]]; then
        read -rp "Gatemid URL [http://localhost:4000]: " url
        url="${url:-http://localhost:4000}"
    fi

    key="${GATEMID_API_KEY:-sk-local-dev-key}"
    while [[ -z "$key" ]]; do
        read -rsp "Gatemid API key: " key
        echo ""
        if [[ -z "$key" ]]; then
            warn "API key is required"
        fi
    done

    GATEMID_URL="$url"
    GATEMID_API_KEY="$key"
}

mask_key() {
    local k="$1"
    local len=${#k}
    if (( len > 8 )); then
        local shown="${k: -4}"
        printf '••••%s' "$shown"
    elif (( len > 4 )); then
        local shown="${k: -4}"
        printf '••••%s' "$shown"
    else
        printf '••••'
    fi
}

confirm() {
    local msg="$1"
    local ans
    read -rp "${msg} [Y/n]: " ans
    [[ -z "$ans" || "$ans" =~ ^[Yy] ]]
}

# ── Help ──────────────────────────────────────────────────────────────────

show_help() {
    cat <<'HELP'
Usage:  ./setup-gatemid.sh [OPTIONS]

Configure a coding agent to point at a Gatemid proxy.

All models are routed through "team-smart-router"; the Opus / heavy
fallback model is "deepseek-pro".

Options:
  -u, --uninstall    Remove gatemid proxy configuration
  -h, --help         Show this help message

Environment variables:
  GATEMID_URL      Base URL of the proxy (default: http://localhost:4000)
  GATEMID_API_KEY  API key for the proxy

Examples:
  ./setup-gatemid.sh                                              # interactive install
  ./setup-gatemid.sh --uninstall                                  # revert
  GATEMID_URL=https://my-gatemid.com/v1 GATEMID_API_KEY=sk-xxx ./setup-gatemid.sh  # non-interactive
HELP
}

# ── Main ──────────────────────────────────────────────────────────────────

main() {
    local uninstall=false

    # Parse flags
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --uninstall|-u) uninstall=true; shift ;;
            --help|-h)      show_help; exit 0 ;;
            *)              err "unknown option: $1"; show_help; exit 1 ;;
        esac
    done

    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║   Gatemid Proxy Setup for Coding Agents  ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"

    if $uninstall; then
        header "Uninstalling gatemid proxy"

        if ! pick_agent "uninstall"; then
            echo ""
            exit 0
        fi

        agent_uninstall "$AGENT_KEY"
        echo ""
        ok "$(agent_name "$AGENT_KEY") gatemid config removed."
        echo "   Restart your shell (or start a new terminal) for changes to take effect."
        exit 0
    fi

    header "Installing gatemid proxy"

    if ! pick_agent "install"; then
        echo ""
        exit 0
    fi

    get_url_and_key

    local name
    name=$(agent_name "$AGENT_KEY")
    echo ""
    echo "Configuring ${name} …"
    echo "  URL       = ${GATEMID_URL}"
    echo "  Key       = $(mask_key "$GATEMID_API_KEY")"
    echo "  Models    → team-smart-router (fallback: deepseek-pro)"
    echo ""

    if ! confirm "Proceed?"; then
        echo "  cancelled"
        exit 1
    fi

    agent_install "$AGENT_KEY" "$GATEMID_URL" "$GATEMID_API_KEY"

    echo ""
    ok "${name} is now configured for gatemid proxy."
    echo "   Start a new session (or reload your shell) for changes to take effect."
}

main "$@"

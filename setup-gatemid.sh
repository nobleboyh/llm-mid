#!/usr/bin/env bash
# ============================================================================
# setup-gatemid.sh — Configure coding agents to use a Gatemid proxy
#
# Switches Claude Code / OpenCode to point at a gatemid URL + API key with
# all models routed through team-smart-router (configurable per-tier defaults).
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
env['ANTHROPIC_DEFAULT_OPUS_MODEL'] = 'team-smart-router'
cfg['env'] = env
with open(fp, 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
" "$file" "$url" "$key"
}

json_set_env_with_tiers() {
    local file="$1" url="$2" key="$3"
    local haiku="${4:-team-smart-router}"
    local sonnet="${5:-team-smart-router}"
    local opus="${6:-team-smart-router}"
    local reasoning="${7:-team-smart-router}"
    python3 -c "
import json, sys
fp = sys.argv[1]
url = sys.argv[2]
key = sys.argv[3]
haiku = sys.argv[4]
sonnet = sys.argv[5]
opus = sys.argv[6]
reasoning = sys.argv[7]
try:
    with open(fp) as f:
        cfg = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}
env = cfg.get('env', {})
env['ANTHROPIC_BASE_URL'] = url.rstrip('/')
env['ANTHROPIC_API_KEY'] = key
env['ANTHROPIC_MODEL'] = 'team-smart-router'
env['ANTHROPIC_DEFAULT_HAIKU_MODEL'] = haiku
env['ANTHROPIC_DEFAULT_SONNET_MODEL'] = sonnet
env['ANTHROPIC_DEFAULT_OPUS_MODEL'] = opus
env['ANTHROPIC_DEFAULT_REASONING_MODEL'] = reasoning
cfg['env'] = env
with open(fp, 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
" "$file" "$url" "$key" "$haiku" "$sonnet" "$opus" "$reasoning"
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
    'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_REASONING_MODEL',
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
    local haiku="${3:-team-smart-router}"
    local sonnet="${4:-team-smart-router}"
    local opus="${5:-deepseek-pro}"
    local reasoning="${6:-team-smart-router}"
    mkdir -p "$(dirname "$CLAUDE_SETTINGS")"

    if [[ -f "$CLAUDE_SETTINGS" ]]; then
        backup_file "$CLAUDE_SETTINGS"
    fi

    json_set_env_with_tiers "$CLAUDE_SETTINGS" "$url" "$key" "$haiku" "$sonnet" "$opus" "$reasoning"
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
        1) install_claude_code "$2" "$3" "${TIER_HAIKU:-team-smart-router}" "${TIER_SONNET:-team-smart-router}" "${TIER_OPUS:-team-smart-router}" "${TIER_REASONING:-team-smart-router}" ;;
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

pick_tier_models() {
    echo ""
    echo "  Model tiers let Claude Code select the best model for each"
    echo "  request complexity level. Defaults route everything through"
    echo "  'team-smart-router' (auto-classifies), but you can pin a tier"
    echo "  to a specific model alias (e.g. gemini-pro, deepseek-flash)."
    echo ""

    if ! confirm "Customize per-tier models?"; then
        TIER_HAIKU="team-smart-router"
        TIER_SONNET="team-smart-router"
        TIER_OPUS="team-smart-router"
        TIER_REASONING="team-smart-router"
        info "Using default tier model mapping (all team-smart-router)"
        return
    fi

    # Dynamically list model aliases from litellm_config.yaml
    local detected_models=()
    if [[ -f litellm_config.yaml ]]; then
        while IFS= read -r line; do
            if [[ "$line" =~ ^[[:space:]]*-[[:space:]]*model_name:[[:space:]]*(.+) ]]; then
                local name="${BASH_REMATCH[1]}"
                if [[ "$name" != "ragas-eval" && "$name" != "team-smart-router" ]]; then
                    detected_models+=("$name")
                fi
            fi
        done < litellm_config.yaml
    fi

    # Fallback list if parsing failed or file doesn't exist
    if [[ ${#detected_models[@]} -eq 0 ]]; then
        detected_models=(
            "gemini-flash" "gemini-pro"
            "deepseek-flash" "deepseek-pro"
            "claude-sonnet" "claude-fable" "claude-opus"
            "openai-gpt4o" "openai-o3"
            "copilot-gpt4" "copilot-codex"
            "github-llama"
        )
    fi

    echo ""
    echo "  Available models:"
    _print_model_menu_with "${detected_models[@]}"
    echo ""
    echo "  Type the number or the model name."
    echo ""

    TIER_HAIKU=$(_pick_gatemid_model "SIMPLE (Haiku) tier" "team-smart-router" "${detected_models[@]}")
    TIER_SONNET=$(_pick_gatemid_model "MEDIUM (Sonnet) tier" "team-smart-router" "${detected_models[@]}")
    TIER_OPUS=$(_pick_gatemid_model "COMPLEX (Opus) tier" "team-smart-router" "${detected_models[@]}")
    TIER_REASONING=$(_pick_gatemid_model "REASONING tier" "team-smart-router" "${detected_models[@]}")

    echo ""
    echo "  Your tier mapping:"
    echo "    SIMPLE    (Haiku)   → ${TIER_HAIKU}"
    echo "    MEDIUM    (Sonnet)  → ${TIER_SONNET}"
    echo "    COMPLEX   (Opus)    → ${TIER_OPUS}"
    echo "    REASONING           → ${TIER_REASONING}"
}

# ── Numbered model picker helpers (bash 3.2 compatible) ──────────────────────

_print_model_menu_with() {
    local models=("$@") i=0 m
    for m in "${models[@]}"; do
        i=$((i + 1))
        echo "    ${i}) $m"
    done
}

_model_num_to_name_with() {
    local num="$1"
    shift
    local models=("$@") i=0 m
    for m in "${models[@]}"; do
        i=$((i + 1))
        [[ "$i" -eq "$num" ]] && echo "$m" && return 0
    done
    return 1
}

_model_is_valid_with() {
    local candidate="$1" m
    shift
    for m in "$@"; do
        [[ "$m" == "$candidate" ]] && return 0
    done
    return 1
}

_find_default_num_with() {
    local target="$1"
    shift
    local models=("$@") i=0 m
    i=0
    for m in "${models[@]}"; do
        i=$((i + 1))
        [[ "$m" == "$target" ]] && echo "$i" && return 0
    done
    echo "1"
}

_pick_gatemid_model() {
    local prompt="$1" default="$2" rest=("${@:3}")
    local models=("${rest[@]}")
    local val resolved default_num
    default_num=$(_find_default_num_with "$default" "${models[@]}")
    while true; do
        read -rp "  ${prompt} [${default_num}] ${default}: " val
        if [[ -z "$val" ]]; then
            echo "$default"
            return
        fi

        # Try as number first
        if [[ "$val" =~ ^[0-9]+$ ]]; then
            resolved=$(_model_num_to_name_with "$val" "${models[@]}")
            if [[ -n "$resolved" ]]; then
                echo "$resolved"
                return
            fi
        fi

        # Try as model name
        if _model_is_valid_with "$val" "${models[@]}"; then
            echo "$val"
            return
        fi

        # Invalid — show help
        echo -e "  ${YELLOW}⚠${RESET} '${val}' is not a valid choice. Pick a number (or name) from:"
        _print_model_menu_with "${models[@]}"
        echo ""
    done
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

All models are routed through "team-smart-router" by default.
During install you can customise per-tier models (SIMPLE/MEDIUM/COMPLEX/REASONING)
to pin a specific model alias instead of using the auto-router.

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
    echo -e "${BOLD}╔═══════════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║     GateMid Proxy Setup — Coding Agents           ║${RESET}"
    echo -e "${BOLD}║     Auto: team-smart-router  |  Custom tiers: ✔   ║${RESET}"
    echo -e "${BOLD}╚═══════════════════════════════════════════════════╝${RESET}"

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
    pick_tier_models

    local name
    name=$(agent_name "$AGENT_KEY")
    echo ""
    echo "Configuring ${name} …"
    echo "  URL              = ${GATEMID_URL}"
    echo "  Key              = $(mask_key "$GATEMID_API_KEY")"
    echo "  Auto model       = team-smart-router"
    echo "  SIMPLE (Haiku)   → ${TIER_HAIKU:-team-smart-router}"
    echo "  MEDIUM (Sonnet)  → ${TIER_SONNET:-team-smart-router}"
    echo "  COMPLEX (Opus)   → ${TIER_OPUS:-team-smart-router}"
    echo "  REASONING        → ${TIER_REASONING:-team-smart-router}"
    echo ""

    if ! confirm "Proceed?"; then
        echo "  cancelled"
        exit 1
    fi

    agent_install "$AGENT_KEY" "$GATEMID_URL" "$GATEMID_API_KEY"

    echo ""
    ok "${name} is now configured for gatemid proxy."
    echo "   Auto model: team-smart-router (classifies & routes by complexity)"
    if [[ "${TIER_HAIKU:-}" != "team-smart-router" || "${TIER_SONNET:-}" != "team-smart-router" || "${TIER_OPUS:-}" != "team-smart-router" || "${TIER_REASONING:-}" != "team-smart-router" ]]; then
        echo "   Custom tier models:"
        echo "     SIMPLE    → ${TIER_HAIKU:-team-smart-router}"
        echo "     MEDIUM    → ${TIER_SONNET:-team-smart-router}"
        echo "     COMPLEX   → ${TIER_OPUS:-team-smart-router}"
        echo "     REASONING → ${TIER_REASONING:-team-smart-router}"
    fi
    echo "   Start a new session (or reload your shell) for changes to take effect."
}

main "$@"

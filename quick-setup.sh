#!/usr/bin/env bash
# ============================================================================
# quick-setup.sh — One-shot GateMid project bootstrap
#
# Guides you through:
#   1. Provider selection (Gemini, DeepSeek, Anthropic, OpenAI, GitHub Copilot…)
#   2. API key configuration per provider
#   3. Model tier assignment (SIMPLE / MEDIUM / COMPLEX / REASONING)
#   4. Writing .env + litellm_config.yaml
#   5. Spinning up docker compose
#   6. Optionally configuring a coding agent (via setup-gatemid.sh)
#
# Usage:
#   ./quick-setup.sh
#   ./quick-setup.sh --help
# ============================================================================

set -euo pipefail

# ── Colours ────────────────────────────────────────────────────────────────

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

# ── Utility helpers ────────────────────────────────────────────────────────

mask_key() {
    local k="$1" len=${#1}
    if (( len > 8 )); then echo "····${k: -4}"
    elif (( len > 4 )); then echo "····${k: -4}"
    else echo "····"
    fi
}

confirm() {
    local msg="$1" ans
    read -rp "  ${msg} [Y/n]: " ans
    [[ -z "$ans" || "$ans" =~ ^[Yy] ]]
}

pick_with_default() {
    local prompt="$1" default="$2"
    local val
    read -rp "  ${prompt} [${default}]: " val
    echo "${val:-$default}"
}

# _print_model_menu — numbered list of ENABLED_MODELS
_print_model_menu() {
    local i=0 m
    for m in "${ENABLED_MODELS[@]}"; do
        i=$((i + 1))
        echo "    ${i}) $m"
    done
}

# _model_num_to_name <num> — returns model name by number, or empty if out of range
_model_num_to_name() {
    local num="$1" i=0 m
    for m in "${ENABLED_MODELS[@]}"; do
        i=$((i + 1))
        if [[ "$i" -eq "$num" ]]; then
            echo "$m"
            return 0
        fi
    done
    echo ""
    return 0
}

# _model_is_valid <model> — returns 0 if model is in ENABLED_MODELS
_model_is_valid() {
    local candidate="$1" m
    for m in "${ENABLED_MODELS[@]}"; do
        [[ "$m" == "$candidate" ]] && return 0
    done
    return 1
}

# _find_default_num <model_name> — returns the menu number for a default model name
_find_default_num() {
    local target="$1" i=0 m
    for m in "${ENABLED_MODELS[@]}"; do
        i=$((i + 1))
        [[ "$m" == "$target" ]] && echo "$i" && return 0
    done
    echo "1"
}

# pick_model_with_default <prompt> <default_model_name> — numbered menu + text input
pick_model_with_default() {
    local prompt="$1" default="$2" val resolved default_num
    default_num=$(_find_default_num "$default")
    while true; do
        read -rp "  ${prompt} [${default_num}] ${default}: " val
        val="${val:-$default_num}"

        # Try as number first
        if [[ "$val" =~ ^[0-9]+$ ]]; then
            resolved=$(_model_num_to_name "$val")
            if [[ -n "$resolved" ]]; then
                echo "$resolved"
                return
            fi
        fi

        # Try as model name
        if _model_is_valid "$val"; then
            echo "$val"
            return
        fi

        # Invalid — show help
        echo -e "  ${YELLOW}⚠${RESET} '${val}' is not a valid choice. Pick a number (or name) from:"
        _print_model_menu
        echo ""
    done
}

# ── Gemin check (for Ragas eval) ────────────────────────────────────────────

_gemini_is_configured() {
    # Must be both: toggled on in provider selection AND have a valid key
    local enabled=false
    local p
    for p in "${ENABLED_PROVIDERS[@]}"; do
        [[ "$p" == "gemini" ]] && enabled=true && break
    done
    $enabled && [[ -n "$GEMINI_API_KEY" && "$GEMINI_API_KEY" != *"your-gemini"* ]]
}

# ── Provider registry ──────────────────────────────────────────────────────
#
# Each entry: short_name|Display Label|ENV_VAR|comma-separated model aliases
# If ENV_VAR is empty, the provider needs no API key (e.g. GitHub Copilot).

PROVIDER_REGISTRY=(
  "gemini|Gemini (Google)|GEMINI_API_KEY|gemini-flash,gemini-pro"
  "deepseek|DeepSeek|DEEPSEEK_API_KEY|deepseek-flash,deepseek-pro"
  "anthropic|Anthropic / Claude|ANTHROPIC_API_KEY|claude-sonnet,claude-fable,claude-opus"
  "openai|OpenAI (Codex CLI)|OPENAI_API_KEY|openai-gpt4o,openai-o3"
  "copilot|GitHub Copilot||copilot-gpt4,copilot-codex"
  "github-models|GitHub Models (Marketplace)|GITHUB_API_KEY|github-llama"
  "ollama|Ollama (local)||ollama"
  "llamacpp|llama.cpp (local)||llamacpp"
  "lmstudio|LM Studio (local)||lmstudio"
  "omlx|oMLX (Apple Silicon)||omlx"
)

# Full model list with their litellm backend mapping
# (portable case function — bash 3.2 compatible)
model_backend() {
    case "$1" in
        gemini-flash)    echo "gemini/gemini-2.5-flash" ;;
        gemini-pro)      echo "gemini/gemini-2.5-pro" ;;
        deepseek-flash)  echo "deepseek/deepseek-v4-flash" ;;
        deepseek-pro)    echo "deepseek/deepseek-v4-pro" ;;
        claude-sonnet)   echo "anthropic/claude-sonnet-4-6" ;;
        claude-fable)    echo "anthropic/claude-fable-5" ;;
        claude-opus)     echo "anthropic/claude-opus-4-8" ;;
        openai-gpt4o)    echo "openai/gpt-4o" ;;
        openai-o3)       echo "openai/o3-mini" ;;
        copilot-gpt4)    echo "github_copilot/gpt-4" ;;
        copilot-codex)   echo "github_copilot/gpt-5.1-codex" ;;
        github-llama)    echo "github/Llama-3.2-11B-Vision-Instruct" ;;
        ollama)          echo "ollama/${OLLAMA_MODEL:-llama3}" ;;
        llamacpp)        echo "openai/${LLAMACPP_MODEL:-llama-3.2-3b}" ;;
        lmstudio)        echo "lm_studio/${LMSTUDIO_MODEL:-model}" ;;
        omlx)            echo "openai/${OMLX_MODEL:-llama}" ;;
        *)               echo "unknown/$1" ;;
    esac
}

# Which env var each model needs (or empty for none)
model_env() {
    case "$1" in
        gemini-flash|gemini-pro)         echo "GEMINI_API_KEY" ;;
        deepseek-flash|deepseek-pro)     echo "DEEPSEEK_API_KEY" ;;
        claude-sonnet|claude-fable|claude-opus) echo "ANTHROPIC_API_KEY" ;;
        openai-gpt4o|openai-o3)          echo "OPENAI_API_KEY" ;;
        copilot-gpt4|copilot-codex)      echo "" ;;
        github-llama)                    echo "GITHUB_API_KEY" ;;
        ollama|llamacpp|lmstudio|omlx)   echo "" ;;
        *)                               echo "" ;;
    esac
}

# local_api_base_for <model> — echoes the api_base for a local provider alias,
# or empty string if the model isn't a local provider. Shared by the primary
# deployment and the order=2 fallback deployment so both get a working endpoint.
local_api_base_for() {
    case "$1" in
        ollama)   echo "${OLLAMA_API_BASE:-http://localhost:11434}" ;;
        llamacpp) echo "${LLAMACPP_API_BASE:-http://localhost:8080}/v1" ;;
        lmstudio) echo "${LMSTUDIO_API_BASE:-http://localhost:1234}" ;;
        omlx)     echo "${OMLX_API_BASE:-http://localhost:8000}/v1" ;;
        *)        echo "" ;;
    esac
}

# local_api_key_for <model> — echoes the dummy api_key used by local providers.
local_api_key_for() {
    case "$1" in
        ollama) echo "ollama" ;;
        *)      echo "sk-no-key" ;;
    esac
}

# ── Local Provider Configuration ─────────────────────────────────────────
# Called after assign_fallbacks to collect endpoint URLs and model names.
# These are stored as env vars in .env and used in write_litellm_config.
configure_local_endpoints() {
    local provider_idx=0
    for p in ollama llamacpp lmstudio omlx; do
        if [[ " ${ENABLED_PROVIDERS[*]} " =~ " ${p} " ]]; then
            provider_idx=$((provider_idx + 1))
        fi
    done
    if [[ "$provider_idx" -eq 0 ]]; then
        return
    fi

    header "Local Provider Configuration"

    echo ""
    echo "  For each local provider, enter the endpoint URL and the model name."
    echo "  Model name is free-text — type whatever your local server serves."
    echo ""

    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " ollama " ]]; then
        echo "  ── Ollama ──"
        OLLAMA_API_BASE=$(pick_with_default "  Endpoint URL" "http://localhost:11434")
        OLLAMA_MODEL=$(pick_with_default "  Model name (e.g. llama3.1)" "llama3")
        echo ""
    fi

    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " llamacpp " ]]; then
        echo "  ── llama.cpp ──"
        LLAMACPP_API_BASE=$(pick_with_default "  Endpoint URL" "http://localhost:8080")
        LLAMACPP_MODEL=$(pick_with_default "  Model name (e.g. llama-3.2-3b)" "llama-3.2-3b")
        echo ""
    fi

    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " lmstudio " ]]; then
        echo "  ── LM Studio ──"
        LMSTUDIO_API_BASE=$(pick_with_default "  Endpoint URL" "http://localhost:1234")
        LMSTUDIO_MODEL=$(pick_with_default "  Model name (e.g. deepseek-coder-v2)" "model")
        echo ""
    fi

    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " omlx " ]]; then
        echo "  ── oMLX (Apple Silicon) ──"
        OMLX_API_BASE=$(pick_with_default "  Endpoint URL" "http://localhost:8000")
        OMLX_MODEL=$(pick_with_default "  Model name (e.g. llama)" "llama")
        echo ""
    fi

    echo ""
    echo "  ⚠  If GateMid runs in Docker and Ollama/llama.cpp/oMLX runs on the host,"
    echo "     use host.docker.internal instead of localhost, e.g.:"
    echo "     http://host.docker.internal:11434"
    echo ""

    ok "local providers configured"
}
# Space-separated "model|fallback" pairs, e.g. "gemini-flash|deepseek-flash"
FALLBACK_PAIRS=""

# Returns the fallback model for a given primary model, or empty string if none.
# Always returns 0 so $() callers don't trigger set -e on bash 3.2 (macOS).
get_fallback() {
    local model="$1" pair m fb
    for pair in $FALLBACK_PAIRS; do
        m="${pair%%|*}"
        fb="${pair##*|}"
        if [[ "$m" == "$model" ]]; then
            echo "$fb"
            return 0
        fi
    done
    echo ""
    return 0
}

# Tells whether a model has a fallback configured (and it's still ENABLED).
_has_fallback() {
    local fb fb
    fb=$(get_fallback "$1")
    [[ -n "$fb" ]] && _model_is_valid "$fb"
}

# ── Step 1: Provider Selection ─────────────────────────────────────────────

select_providers() {
    header "Provider Selection"

    echo "  Choose which LLM providers to enable."
    echo -e "  Type the number to toggle a provider on/off, then press Enter when done."
    echo -e "  ${YELLOW}Note:${RESET} Gemini (Google) is required for Ragas eval scoring —"
    echo -e "  if you skip it or leave the API key blank, eval will be disabled."
    echo ""

    # Use space-separated string instead of bash array (bash 3.2 compat)
    local provider_count="${#PROVIDER_REGISTRY[@]}"
    toggle_state=""
    for ((i=0; i<provider_count; i++)); do toggle_state+=" off"; done
    toggle_state="${toggle_state# }"

    # Read existing .env for defaults
    if [[ -f .env ]]; then
        # shellcheck source=/dev/null
        source .env 2>/dev/null || true
    fi

    local idx line short label
    local first_iteration=true
    # Each iteration prints: 1(header) + 1(blank) + provider_count + 1(blank) + 1(footer) + 1(blank) + 1(prompt)
    # + 1(feedback) + 1(blank) = provider_count + 8 lines
    local lines_per_block=$((provider_count + 8))
    while true; do
        # Replace previous menu instead of appending new one
        if ! $first_iteration; then
            printf "\033[%dA\033[J" "$lines_per_block"
        fi
        first_iteration=false

        echo "  Current selection:"
        echo ""
        idx=0
        for line in "${PROVIDER_REGISTRY[@]}"; do
            short="${line%%|*}"
            label="${line#*|}"
            label="${label%%|*}"
            idx=$((idx + 1))

            # Extract toggle state at position idx (1-based for display)
            if _get_toggle "$idx" "on"; then
                echo -e "  ${GREEN}[✓]${RESET}  $idx) $label"
            else
                echo -e "  ${GREY}[ ]${RESET}  $idx) $label"
            fi
        done
        echo ""
        echo -e "  ${GREY}   d) done — continue${RESET}"
        echo ""

        read -rp "  Toggle (number, or d): " choice
        if [[ "$choice" == "d" ]]; then
            echo ""
            break
        elif [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= provider_count )); then
            # Extract label for feedback
            reg_entry="${PROVIDER_REGISTRY[$((choice - 1))]}"
            label="${reg_entry#*|}"
            label="${label%%|*}"

            if _get_toggle "$choice" "on"; then
                _set_toggle "$choice" "off"
                echo -e "  ${YELLOW}✕${RESET} $label disabled"
            else
                _set_toggle "$choice" "on"
                echo -e "  ${GREEN}✔${RESET} $label enabled"
            fi
            echo ""
        else
            echo -e "  ${YELLOW}Invalid input — type a number (1-${provider_count}) or 'd' for done${RESET}"
            echo ""
        fi
    done

    # Build the list of enabled providers and their models
    ENABLED_PROVIDERS=()
    ENABLED_MODELS=()
    local i=0
    for line in "${PROVIDER_REGISTRY[@]}"; do
        i=$((i + 1))
        if _get_toggle "$i" "on"; then
            local short="${line%%|*}"
            local models_str="${line##*|}"
            ENABLED_PROVIDERS+=("$short")
            local m
            for m in $(echo "$models_str" | tr ',' ' '); do
                ENABLED_MODELS+=("$m")
            done
        fi
    done

    if [[ ${#ENABLED_PROVIDERS[@]} -eq 0 ]]; then
        err "No providers selected — enabling Gemini as fallback."
        ENABLED_PROVIDERS=("gemini")
        ENABLED_MODELS=("gemini-flash" "gemini-pro")
    fi

    echo "  Enabled providers: ${ENABLED_PROVIDERS[*]}"
    echo "  Available models:  ${ENABLED_MODELS[*]}"
}

# ── Toggle helpers (bash 3.2 compatible — no associative arrays) ────────────

# _get_toggle <index(1-based)> <expected_value> — returns 0 if toggle at index matches
_get_toggle() {
    local idx="$1" expected="$2"
    local val
    val=$(echo "$toggle_state" | cut -d' ' -f"$idx")
    [[ "$val" == "$expected" ]]
}

# _set_toggle <index(1-based)> <value>
_set_toggle() {
    local idx="$1" newval="$2"
    local parts=($toggle_state)
    parts[$((idx - 1))]="$newval"
    toggle_state="${parts[*]}"
}

# ── Step 2: API Keys ───────────────────────────────────────────────────────

collect_api_keys() {
    header "API Keys"

    echo "  Leave blank to keep existing values from .env (if any)."
    echo ""

    # Gemini key
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " gemini " ]]; then
        local gemini="${GEMINI_API_KEY:-}"
        read -rp "  GEMINI_API_KEY    (current: $(mask_key "$gemini")): " inp
        GEMINI_API_KEY="${inp:-$gemini}"
        if [[ -z "$GEMINI_API_KEY" || "$GEMINI_API_KEY" == *"your-gemini"* ]]; then
            warn "GEMINI_API_KEY is missing or still a placeholder"
        fi
    fi

    # DeepSeek key
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " deepseek " ]]; then
        local deepseek="${DEEPSEEK_API_KEY:-}"
        read -rp "  DEEPSEEK_API_KEY  (current: $(mask_key "$deepseek")): " inp
        DEEPSEEK_API_KEY="${inp:-$deepseek}"
        if [[ -z "$DEEPSEEK_API_KEY" || "$DEEPSEEK_API_KEY" == *"your-deepseek"* ]]; then
            warn "DEEPSEEK_API_KEY is missing or still a placeholder"
        fi
    fi

    # Anthropic key
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " anthropic " ]]; then
        local anthro="${ANTHROPIC_API_KEY:-}"
        read -rp "  ANTHROPIC_API_KEY (current: $(mask_key "$anthro")): " inp
        ANTHROPIC_API_KEY="${inp:-$anthro}"
        if [[ -z "$ANTHROPIC_API_KEY" || "$ANTHROPIC_API_KEY" == *"your-anthropic"* ]]; then
            warn "ANTHROPIC_API_KEY is missing or still a placeholder"
        fi
    fi

    # OpenAI key
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " openai " ]]; then
        local openai="${OPENAI_API_KEY:-}"
        read -rp "  OPENAI_API_KEY    (current: $(mask_key "$openai")): " inp
        OPENAI_API_KEY="${inp:-$openai}"
        if [[ -z "$OPENAI_API_KEY" || "$OPENAI_API_KEY" == *"your-openai"* ]]; then
            warn "OPENAI_API_KEY is missing or still a placeholder"
        fi
    fi

    # GitHub Models key (optional — fine to leave blank)
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " github-models " ]]; then
        local gitkey="${GITHUB_API_KEY:-}"
        read -rp "  GITHUB_API_KEY    (optional, current: $(mask_key "$gitkey")): " inp
        GITHUB_API_KEY="${inp:-$gitkey}"
    fi

    # Hugging Face token
    local hf="${HF_TOKEN:-}"
    read -rp "  HF_TOKEN          (optional, current: $(mask_key "$hf")): " inp
    HF_TOKEN="${inp:-$hf}"

    echo ""
    info "GitHub Copilot requires no API key — it authenticates via your GitHub session."

    # Check Gemini availability for Ragas eval
    if _gemini_is_configured; then
        ok "Gemini API key found — Ragas eval scoring will be available"
    else
        warn "No valid GEMINI_API_KEY — Ragas eval scoring (embeddings) will be disabled."
        warn "The eval worker container will be started in idle mode."
    fi
}

# ── Step 3: Model Tier Assignment ──────────────────────────────────────────

assign_tiers() {
    header "Model Tier Assignment"

    echo "  Choose which model handles each complexity tier."
    echo "  Type the number or the model name."
    echo ""
    echo "  Available models (from selected providers):"
    _print_model_menu
    echo ""
    echo "  Each tier below maps to one of the above model aliases."
    echo ""

    # Suggest defaults: pick first available models for each tier
    local default_simple="${ENABLED_MODELS[0]:-gemini-flash}"
    local default_medium="${ENABLED_MODELS[0]:-gemini-flash}"
    local default_complex="${ENABLED_MODELS[1]:-${ENABLED_MODELS[0]}}"
    local default_reasoning="${ENABLED_MODELS[1]:-${ENABLED_MODELS[0]}}"

    local valid=false
    while ! $valid; do
        TIER_SIMPLE=$(pick_model_with_default "SIMPLE tier" "$default_simple")
        TIER_MEDIUM=$(pick_model_with_default "MEDIUM tier" "$default_medium")
        TIER_COMPLEX=$(pick_model_with_default "COMPLEX tier" "$default_complex")
        TIER_REASONING=$(pick_model_with_default "REASONING tier" "$default_reasoning")

        echo ""
        echo "  Your tier mapping:"
        echo "    SIMPLE    → ${TIER_SIMPLE}"
        echo "    MEDIUM    → ${TIER_MEDIUM}"
        echo "    COMPLEX   → ${TIER_COMPLEX}"
        echo "    REASONING → ${TIER_REASONING}"
        echo ""

        if confirm "Is this correct?"; then
            valid=true
        else
            echo ""
        fi
    done
}

# ── Step 3b: Model Fallback Assignment ──────────────────────────────────────
# For each enabled model, the user picks which model to auto-fallback to
# when the primary API key fails. The fallback is registered under the same
# model_name with order=2, so the Router tries it transparently.

assign_fallbacks() {
    header "Model Fallback Assignment (auto-failover)"

    echo "  For each enabled model, pick which OTHER model to auto-fallback"
    echo "  to when the primary API key fails (401/429/timeout/cooldown)."
    echo "  The fallback runs under the SAME model_name — the complexity"
    echo "  router doesn't need any config changes."
    echo ""
    echo "  Type a number or model name, or leave blank for no fallback."
    echo ""
    _print_model_menu
    echo ""

    FALLBACK_PAIRS=""
    local m fb prompt default_num
    for m in "${ENABLED_MODELS[@]}"; do
        # Suggest first different-provider model as polite default
        default_num=$(_find_default_num "$m")  # the model itself
        local suggestion=""
        local _i=0 _mm
        for _mm in "${ENABLED_MODELS[@]}"; do
            _i=$((_i + 1))
            if [[ "$_mm" != "$m" ]]; then
                suggestion="$_mm"
                break
            fi
        done
        if [[ -n "$suggestion" ]]; then
            prompt="  Fallback for ${m}"
            read -rp "${prompt} (default: ${suggestion}): " fb
            fb="${fb:-$suggestion}"
        else
            read -rp "  Fallback for ${m} (blank = none): " fb
        fi

        # Resolve numeric input to model name
        if [[ "$fb" =~ ^[0-9]+$ ]]; then
            local resolved
            resolved=$(_model_num_to_name "$fb")
            if [[ -n "$resolved" ]]; then
                fb="$resolved"
            fi
        fi

        if [[ -z "$fb" ]]; then
            info "No fallback for ${m}"
        elif _model_is_valid "$fb" && [[ "$fb" != "$m" ]]; then
            FALLBACK_PAIRS="${FALLBACK_PAIRS} ${m}|${fb}"
            ok "${m} → ${fb}"
        elif [[ "$fb" == "$m" ]]; then
            warn "A model cannot fallback to itself — no fallback for ${m}"
        else
            warn "'${fb}' is not available — no fallback for ${m}"
        fi
        echo ""
    done
}

# ── Step 4: Write files ────────────────────────────────────────────────────

write_env() {
    header "Writing .env"

    # Build .env from enabled providers
    cat > .env <<ENV
# GateMid — API Keys (generated by quick-setup.sh)
ENV

    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " gemini " ]]; then
        echo "GEMINI_API_KEY=${GEMINI_API_KEY}" >> .env
    fi
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " deepseek " ]]; then
        echo "DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}" >> .env
    fi
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " anthropic " ]]; then
        echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" >> .env
    fi
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " openai " ]]; then
        echo "OPENAI_API_KEY=${OPENAI_API_KEY}" >> .env
    fi
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " github-models" ]]; then
        echo "GITHUB_API_KEY=${GITHUB_API_KEY}" >> .env
    fi

    # Local provider endpoint URLs and model names
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " ollama " ]]; then
        echo "OLLAMA_API_BASE=${OLLAMA_API_BASE:-http://localhost:11434}" >> .env
        echo "OLLAMA_MODEL=${OLLAMA_MODEL:-llama3}" >> .env
    fi
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " llamacpp " ]]; then
        echo "LLAMACPP_API_BASE=${LLAMACPP_API_BASE:-http://localhost:8080}" >> .env
        echo "LLAMACPP_MODEL=${LLAMACPP_MODEL:-llama-3.2-3b}" >> .env
    fi
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " lmstudio " ]]; then
        echo "LMSTUDIO_API_BASE=${LMSTUDIO_API_BASE:-http://localhost:1234}" >> .env
        echo "LMSTUDIO_MODEL=${LMSTUDIO_MODEL:-model}" >> .env
    fi
    if [[ " ${ENABLED_PROVIDERS[*]} " =~ " omlx " ]]; then
        echo "OMLX_API_BASE=${OMLX_API_BASE:-http://localhost:8000}" >> .env
        echo "OMLX_MODEL=${OMLX_MODEL:-llama}" >> .env
    fi

    if _gemini_is_configured; then
        echo "RAGAS_EVAL_ENABLED=true" >> .env
    else
        echo "# Gemini key missing — Ragas eval is disabled" >> .env
        echo "RAGAS_EVAL_ENABLED=false" >> .env
    fi

    cat >> .env <<ENV
GATEWAY_MASTER_KEY=sk-local-dev-key

# Hugging Face token (optional — speeds up model downloads)
HF_TOKEN=${HF_TOKEN}
ENV
    ok ".env written"
}

write_litellm_config() {
    header "Writing litellm_config.yaml"

    # We'll write a config that only includes enabled models
    cat > litellm_config.yaml <<YAML
# GateMid — Team Smart Router + Headroom Compression
# LiteLLM Proxy Configuration
# Generated by quick-setup.sh

model_list:
YAML

    # ── Provider models ──────────────────────────────────────────
    for m in "${ENABLED_MODELS[@]}"; do
        local backend
        backend=$(model_backend "$m")
        local env_var
        env_var=$(model_env "$m")
        local copilot_flag=false

        # Check if this is a copilot model (no key needed)
        if [[ "$m" == copilot-* ]]; then
            copilot_flag=true
        fi

        # Check if this is a local provider
        local is_local=false
        if [[ -n "$(local_api_base_for "$m")" ]]; then
            is_local=true
        fi

        # Check if this is a codex / responses mode model
        local mode_responses=false
        if [[ "$m" == copilot-codex ]]; then
            mode_responses=true
        fi

        cat >> litellm_config.yaml <<MODEL
  - model_name: ${m}
    litellm_params:
      model: ${backend}
MODEL

        if [[ -n "$env_var" && "$copilot_flag" == false && "$is_local" == false ]]; then
            echo "      api_key: \"os.environ/${env_var}\"" >> litellm_config.yaml
        fi

        # ── Local provider: add api_base + dummy api_key ──
        if [[ "$is_local" == true ]]; then
            echo "      api_base: $(local_api_base_for "$m")" >> litellm_config.yaml
            echo "      api_key: \"$(local_api_key_for "$m")\"" >> litellm_config.yaml
        fi

        # All deployments are excluded from health checks (init-007):
        # /health must never trigger a live, billed provider completion.
        if [[ "$mode_responses" == true ]]; then
            cat >> litellm_config.yaml <<MODEL
    model_info:
      mode: responses
      disable_background_health_check: true
MODEL
        else
            cat >> litellm_config.yaml <<MODEL
    model_info:
      disable_background_health_check: true
MODEL
        fi

        echo "" >> litellm_config.yaml

        # ── Fallback deployment (same model_name, different provider) ──
        # If the primary API key fails, the Router auto-fallbacks to the
        # order=2 deployment under the same model_name.
        local fallback_m
        fallback_m=$(get_fallback "$m")
        if [[ -n "$fallback_m" ]] && _model_is_valid "$fallback_m"; then
            local fb_backend
            fb_backend=$(model_backend "$fallback_m")
            local fb_env
            fb_env=$(model_env "$fallback_m")
            cat >> litellm_config.yaml <<MODEL
  - model_name: ${m}
    litellm_params:
      model: ${fb_backend}
MODEL
            if [[ -n "$fb_env" ]]; then
                echo "      api_key: \"os.environ/${fb_env}\"" >> litellm_config.yaml
            fi

            # ── Local fallback: same api_base/dummy-key treatment as primary ──
            local fb_api_base
            fb_api_base=$(local_api_base_for "$fallback_m")
            if [[ -n "$fb_api_base" ]]; then
                echo "      api_base: ${fb_api_base}" >> litellm_config.yaml
                echo "      api_key: \"$(local_api_key_for "$fallback_m")\"" >> litellm_config.yaml
            fi

            echo "      order: 2" >> litellm_config.yaml
            cat >> litellm_config.yaml <<MODEL
    model_info:
      disable_background_health_check: true
MODEL
            echo "" >> litellm_config.yaml
        fi
    done

    # ── Ragas Eval Model (requires Gemini for embeddings) ─────────
    if _gemini_is_configured; then
        # Ask user which model to use as the LLM-as-judge
        if [[ ${#ENABLED_MODELS[@]} -gt 0 ]]; then
            echo ""
            echo "  Ragas LLM-as-judge — pick a model for evaluating responses:"
            _print_model_menu
            echo ""
            local judge_model
            judge_model=$(pick_model_with_default "Judge model" "${ENABLED_MODELS[0]}")
        else
            local judge_model="deepseek-flash"
        fi

        local judge_backend
        judge_backend=$(model_backend "$judge_model")
        judge_backend="${judge_backend:-deepseek/deepseek-v4-flash}"

        local judge_env
        judge_env=$(model_env "$judge_model")

        cat >> litellm_config.yaml <<YAML
  # Ragas Eval Model (LLM-as-judge — routes through LiteLLM)
  # The eval worker calls this with model="ragas-eval". The RagasLogger
  # callback skips logging for this model prefix, preventing an eval loop.

  # Primary: user's chosen judge model
  - model_name: ragas-eval
    litellm_params:
      model: ${judge_backend}
      order: 1
YAML
        if [[ -n "$judge_env" ]]; then
            echo "      api_key: \"os.environ/${judge_env}\"" >> litellm_config.yaml
        fi
        cat >> litellm_config.yaml <<MODEL
    model_info:
      disable_background_health_check: true
MODEL
        echo "" >> litellm_config.yaml

        # ── Ragas eval fallback deployment ──────────────────────────
        # Auto-fallback to the other provider if primary judge fails.
        local ragas_fb="" ragas_fb_backend="" ragas_fb_env=""
        if [[ "$judge_env" == "DEEPSEEK_API_KEY" ]] && _gemini_is_configured; then
            ragas_fb="gemini-flash"
            ragas_fb_backend="gemini/gemini-2.5-flash"
            ragas_fb_env="GEMINI_API_KEY"
        elif [[ -n "$judge_env" && "$judge_env" != "DEEPSEEK_API_KEY" ]] && \
             [[ " ${ENABLED_PROVIDERS[*]} " =~ " deepseek " ]]; then
            ragas_fb="deepseek-flash"
            ragas_fb_backend="deepseek/deepseek-v4-flash"
            ragas_fb_env="DEEPSEEK_API_KEY"
        fi
        if [[ -n "$ragas_fb" ]]; then
            cat >> litellm_config.yaml <<YAML
  # Fallback: ${ragas_fb} (if primary judge times out)
  - model_name: ragas-eval
    litellm_params:
      model: ${ragas_fb_backend}
      api_key: "os.environ/${ragas_fb_env}"
      order: 2
    model_info:
      disable_background_health_check: true

YAML
        fi
        ok "Ragas eval configured with judge model: ${judge_model}"
    else
        echo "# Ragas eval model skipped — no GEMINI_API_KEY configured" >> litellm_config.yaml
        echo "" >> litellm_config.yaml
        info "Skipping ragas-eval model entry (Gemini embeddings key not available)"
    fi

    # ── Team Smart Router ────────────────────────────────────────
    cat >> litellm_config.yaml <<YAML
  # Team Smart Router — auto-classifies and routes
  - model_name: team-smart-router
    litellm_params:
      model: auto_router/complexity_router
      complexity_router_config:
        tiers:
          SIMPLE: ${TIER_SIMPLE}
          MEDIUM: ${TIER_MEDIUM}
          COMPLEX: ${TIER_COMPLEX}
          REASONING: ${TIER_REASONING}
        token_thresholds:
          simple: 100
          complex: 2000
        dimension_weights:
          tokenCount: 0.05
          codePresence: 0.10
          reasoningMarkers: 0.30
          technicalTerms: 0.10
          simpleIndicators: 0.15
          multiStepPatterns: 0.05
          questionComplexity: 0.05
        tier_boundaries:
          simple_medium: 0.15
          medium_complex: 0.3
          complex_reasoning: 0.55
      complexity_router_default_model: ${TIER_SIMPLE}
    model_info:
      disable_background_health_check: true

YAML

    # ── LiteLLM settings (retries, cooldowns, timeouts) ──────────
    cat >> litellm_config.yaml <<YAML
litellm_settings:
  drop_params: true
  callbacks: ['proxy.callback.ragas_callback', 'proxy.callbacks.local_provider_failure.local_provider_failure_logger']
  num_retries: 1                    # try each deployment once before falling back
  request_timeout: 60               # fail per-attempt if no response in 30s
  allowed_fails: 3                  # cooldown model after 3 failures in a minute
  cooldown_time: 60                 # skip cooldowned model for 60 seconds

YAML

    # ── Router-level fallback chains (built from get_fallback pairs) ──
    local _has_fb=false _m _fb
    for _m in "${ENABLED_MODELS[@]}"; do
        if _has_fallback "$_m"; then
            _has_fb=true
            break
        fi
    done
    if $_has_fb; then
        echo "router_settings:" >> litellm_config.yaml
        echo "  # Cross-model fallbacks — reached only if ALL deployments" >> litellm_config.yaml
        echo "  # under the primary model_name fail." >> litellm_config.yaml
        echo "  fallbacks:" >> litellm_config.yaml
        for _m in "${ENABLED_MODELS[@]}"; do
            _fb=$(get_fallback "$_m")
            if [[ -n "$_fb" ]] && _model_is_valid "$_fb"; then
                echo "    - \"${_m}\": [\"${_fb}\"]" >> litellm_config.yaml
            fi
        done
        echo "  routing_strategy: simple-shuffle" >> litellm_config.yaml
        echo "" >> litellm_config.yaml
    fi

    cat >> litellm_config.yaml <<YAML
general_settings:
  master_key: "os.environ/GATEWAY_MASTER_KEY"
  # Serve /health from cached results instead of live-probing every
  # deployment (init-007). Background loop still runs but probes nothing
  # (see model_info.disable_background_health_check on each deployment).
  background_health_checks: true
YAML
    ok "litellm_config.yaml written"
}

# ── Step 5: Docker Compose ─────────────────────────────────────────────────

start_compose() {
    header "Starting Docker Compose"

    if ! command -v docker &>/dev/null; then
        err "docker not found — please install Docker Desktop first"
        return 1
    fi

    echo "  Bringing up GateMid services (litellm + redis + eval-worker)…"
    echo ""

    docker compose up -d --build

    echo ""

    # Wait for health
    local max_attempts=30 i=0
    echo -n "  Waiting for litellm proxy to become healthy "
    while ! docker compose exec litellm curl -sf -H "Authorization: Bearer sk-local-dev-key" http://localhost:4000/health &>/dev/null; do
        echo -n "."
        sleep 2
        i=$((i + 1))
        if (( i >= max_attempts )); then
            echo ""
            warn "litellm healthcheck timed out — check 'docker compose logs litellm'"
            break
        fi
    done
    echo ""
    ok "GateMid is up at http://localhost:4000"
}

# ── Step 6: Agent setup ────────────────────────────────────────────────────

offer_agent_setup() {
    header "Coding Agent Configuration (optional)"

    if confirm "Do you want to configure a coding agent to use this GateMid proxy?"; then
        local script="./setup-gatemid.sh"
        if [[ -x "$script" ]]; then
            echo ""
            echo "  Launching setup-gatemid.sh…"
            echo ""
            "$script"
        else
            echo ""
            echo "  setup-gatemid.sh not found — showing manual setup instead."
            show_agent_instructions
        fi
    else
        info "Skipping agent setup."
        show_agent_instructions
    fi
}

show_agent_instructions() {
    echo ""
    header "Manual Agent Configuration"

    echo "  Point your coding agent CLI to the GateMid proxy:"
    echo ""
    echo "  ── Claude Code ──────────────────────────────────────"
    echo "    export ANTHROPIC_BASE_URL=http://localhost:4000"
    echo "    export ANTHROPIC_API_KEY=sk-local-dev-key"
    echo "    export ANTHROPIC_MODEL=<your-chosen-model>"
    echo ""
    echo "  ── Codex CLI (OpenAI) ──────────────────────────────"
    echo "    export OPENAI_BASE_URL=http://localhost:4000"
    echo "    export OPENAI_API_KEY=sk-local-dev-key"
    echo ""
    echo "  ── GitHub Copilot (VS Code) ────────────────────────"
    echo '    Add to settings.json:'
    echo '    "github.copilot.advanced": {'
    echo '      "debug.overrideProxyUrl": "http://localhost:4000",'
    echo '      "debug.testOverrideProxyUrl": "http://localhost:4000"'
    echo '    }'
    echo ""

    if ! confirm "Configure Claude Code now?"; then
        info "You can configure agents later — see instructions above."
        return
    fi

    echo ""
    echo "  Which model should Claude Code use?"
    echo "  Available: ${ENABLED_MODELS[*]}"
    echo ""
    local default_model="${TIER_COMPLEX:-${ENABLED_MODELS[0]}}"
    local model
    read -rp "  Model [${default_model}]: " model
    model="${model:-$default_model}"

    cat <<SHELL

  Add these to your ~/.zshrc or ~/.bashrc:

    export ANTHROPIC_BASE_URL=http://localhost:4000
    export ANTHROPIC_API_KEY=sk-local-dev-key
    export ANTHROPIC_MODEL=${model}

  Or run them now in your current shell:

    export ANTHROPIC_BASE_URL=http://localhost:4000
    export ANTHROPIC_API_KEY=sk-local-dev-key
    export ANTHROPIC_MODEL=${model}

SHELL
}

# ── Summary ────────────────────────────────────────────────────────────────

print_summary() {
    header "Setup Complete"

    echo "  GateMid project is ready!"
    echo ""
    echo "  Enabled providers:"
    for p in "${ENABLED_PROVIDERS[@]}"; do
        echo "    • $p"
    done
    echo ""
    echo "  Model tiers:"
    echo "    SIMPLE    → ${TIER_SIMPLE}"
    echo "    MEDIUM    → ${TIER_MEDIUM}"
    echo "    COMPLEX   → ${TIER_COMPLEX}"
    echo "    REASONING → ${TIER_REASONING}"
    echo ""
    echo "  Endpoint:  http://localhost:4000"
    echo "  API key:   sk-local-dev-key"
    echo "  Docs:      https://github.com/your-org/gatemid  (or README.md)"
    echo ""

    if confirm "Open README.md for more info?"; then
        if command -v less &>/dev/null; then
            less README.md
        elif command -v cat &>/dev/null; then
            cat README.md
        fi
    fi
}

# ── Help ───────────────────────────────────────────────────────────────────

show_help() {
    cat <<'HELP'
Usage:  ./quick-setup.sh [OPTIONS]

One-shot bootstrap for the GateMid project.

Guides you through:
  • Provider selection (Gemini, DeepSeek, Anthropic, OpenAI, GitHub Copilot, GitHub Models)
  • API key entry for selected providers
  • Model tier assignment for the Smart Router
  • Writing .env and litellm_config.yaml
  • Starting Docker Compose services
  • (Optional) Configuring Claude Code / Codex / GitHub Copilot

Options:
  -h, --help     Show this help message
HELP
}

# ── Main ───────────────────────────────────────────────────────────────────

main() {
    # Parse flags
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help|-h) show_help; exit 0 ;;
            *) err "unknown option: $1"; show_help; exit 1 ;;
        esac
    done

    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║     GateMid — Quick Project Setup         ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
    echo ""
    echo "  This will configure your project from scratch."
    echo "  Existing .env and litellm_config.yaml will be OVERWRITTEN."
    echo ""

    if ! confirm "Continue?"; then
        echo "  cancelled"
        exit 0
    fi

    cd "$(dirname "$0")"  # ensure we're in the project root

    select_providers
    collect_api_keys
    assign_tiers
    assign_fallbacks
    configure_local_endpoints
    write_env
    write_litellm_config
    start_compose
    offer_agent_setup
    print_summary
}

main "$@"

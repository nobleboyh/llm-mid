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
        [[ "$i" -eq "$num" ]] && echo "$m" && return 0
    done
    return 1
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
        *)                               echo "" ;;
    esac
}

# ── Step 1: Provider Selection ─────────────────────────────────────────────

select_providers() {
    header "Provider Selection"

    echo "  Choose which LLM providers to enable."
    echo "  Type the number to toggle a provider on/off, then press Enter when done."
    echo "  ${YELLOW}Note:${RESET} Gemini (Google) is required for Ragas eval scoring —"
    echo "  if you skip it or leave the API key blank, eval will be disabled."
    echo ""

    # Use space-separated string instead of bash array (bash 3.2 compat)
    toggle_state="off off off off off off"

    # Read existing .env for defaults
    if [[ -f .env ]]; then
        # shellcheck source=/dev/null
        source .env 2>/dev/null || true
    fi

    local idx line short label
    while true; do
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
        elif [[ "$choice" =~ ^[1-6]$ ]]; then
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
            echo -e "  ${YELLOW}Invalid input — type a number (1-6) or 'd' for done${RESET}"
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

        if [[ -n "$env_var" && "$copilot_flag" == false ]]; then
            echo "      api_key: \"os.environ/${env_var}\"" >> litellm_config.yaml
        fi

        if [[ "$mode_responses" == true ]]; then
            cat >> litellm_config.yaml <<MODEL
    model_info:
      mode: responses
MODEL
        fi

        echo "" >> litellm_config.yaml
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
  - model_name: ragas-eval
    litellm_params:
      model: ${judge_backend}
YAML
        if [[ -n "$judge_env" ]]; then
            echo "      api_key: \"os.environ/${judge_env}\"" >> litellm_config.yaml
        fi
        echo "" >> litellm_config.yaml
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

litellm_settings:
  drop_params: true
  callbacks: ['proxy.callback.ragas_callback']

general_settings:
  master_key: "os.environ/GATEWAY_MASTER_KEY"
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
    write_env
    write_litellm_config
    start_compose
    offer_agent_setup
    print_summary
}

main "$@"

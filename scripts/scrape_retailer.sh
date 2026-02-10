#!/usr/bin/env bash
# scrape_retailer.sh - Scrape retailer sale pages using Firecrawl CLI
# Usage:
#   scrape_retailer.sh <retailer>       Scrape all sale URLs for a retailer
#   scrape_retailer.sh all              Scrape all enabled retailers
#   scrape_retailer.sh --url <URL>      Discovery mode: scrape a single URL
#   scrape_retailer.sh --product <URL>  Scrape a single product page for size info

set -euo pipefail

SKILL_DIR="$HOME/.claude/skills/sale-monitor"
CONFIG_DIR="$SKILL_DIR/config"
RETAILERS_DIR="$CONFIG_DIR/retailers"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="${SALE_MONITOR_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
OUTPUT_DIR="$PROJECT_DIR/output"
LOG_DIR="$PROJECT_DIR/logs"

# Ensure directories exist
mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

# Log file for this run
LOG_FILE="$LOG_DIR/scrape_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

# Load API key: check env first, then extract from shell profile as fallback
if [ -z "${FIRECRAWL_API_KEY:-}" ]; then
    for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.zprofile" "$HOME/.bash_profile"; do
        if [ -f "$rc" ]; then
            key=$(sed -n 's/^export FIRECRAWL_API_KEY="\{0,1\}\(fc-[a-f0-9]*\)"\{0,1\}/\1/p' "$rc" 2>/dev/null | head -1)
            if [ -n "$key" ]; then
                export FIRECRAWL_API_KEY="$key"
                break
            fi
        fi
    done
fi
if [ -z "${FIRECRAWL_API_KEY:-}" ]; then
    echo "ERROR: FIRECRAWL_API_KEY not set. Run: export FIRECRAWL_API_KEY=fc-YOUR_KEY" >&2
    exit 1
fi

# Parse a JSON field using python (available on macOS)
json_get() {
    python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('$1', '$2')) if isinstance(d.get('$1'), (dict,list)) else d.get('$1', '$2'))"
}

json_array_len() {
    python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d) if isinstance(d, list) else 0)"
}

json_array_get() {
    python3 -c "import json,sys; d=json.load(sys.stdin); print(d[$1] if isinstance(d, list) and $1 < len(d) else '')"
}

# Scrape a single URL with given options
scrape_url() {
    local url="$1"
    local output_file="$2"
    local wait_for="${3:-5000}"
    local only_main="${4:-true}"

    local args=()
    args+=("scrape" "$url" "--format" "markdown")
    args+=("--wait-for" "$wait_for")
    args+=("-o" "$output_file")
    args+=("-k" "$FIRECRAWL_API_KEY")

    if [ "$only_main" = "true" ]; then
        args+=("--only-main-content")
    fi

    echo "  Scraping: $url" >&2
    if firecrawl "${args[@]}" 2>/dev/null; then
        local size
        size=$(wc -c < "$output_file" 2>/dev/null || echo 0)
        echo "  -> Saved $size bytes to $output_file" >&2
        echo "$size"
    else
        echo "  -> FAILED to scrape $url" >&2
        echo "0"
    fi
}

# Append pagination param to URL
add_page_param() {
    local url="$1"
    local param="$2"
    local page="$3"

    if [[ "$url" == *"?"* ]]; then
        echo "${url}&${param}=${page}"
    else
        echo "${url}?${param}=${page}"
    fi
}

# Scrape a single retailer
scrape_retailer() {
    local retailer_name="$1"
    local config_file="$RETAILERS_DIR/${retailer_name}.json"

    if [ ! -f "$config_file" ]; then
        echo "ERROR: Config not found: $config_file" >&2
        exit 1
    fi

    local display_name wait_for only_main page_param max_pages
    display_name=$(cat "$config_file" | json_get "name" "$retailer_name")
    wait_for=$(cat "$config_file" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('scrape_options',{}).get('wait_for', 5000))")
    only_main=$(cat "$config_file" | python3 -c "import json,sys; d=json.load(sys.stdin); print(str(d.get('scrape_options',{}).get('only_main_content', True)).lower())")
    page_param=$(cat "$config_file" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('pagination',{}).get('param', 'page'))")
    max_pages=$(cat "$config_file" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('pagination',{}).get('max_pages', 3))")

    # Get sale URLs
    local num_urls
    num_urls=$(cat "$config_file" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('sale_urls', [])))")

    echo "=== Scraping $display_name ($num_urls categories, up to $max_pages pages each) ===" >&2

    local total_files=0
    local output_files=()

    for ((i=0; i<num_urls; i++)); do
        local base_url
        base_url=$(cat "$config_file" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['sale_urls'][$i])")

        # Derive category name: use url_labels[i] if present, else from URL path
        local category
        category=$(cat "$config_file" | python3 -c "import json,sys; d=json.load(sys.stdin); labels=d.get('url_labels',[]); print(labels[$i] if $i<len(labels) else '')" 2>/dev/null)
        if [ -z "$category" ]; then
            category=$(echo "$base_url" | python3 -c "import sys; u=sys.stdin.read().strip(); parts=u.rstrip('/').split('/'); print(parts[-1].split('?')[0] if parts else 'main')")
        fi

        # Read per-category pagination overrides (if any)
        local cat_max_pages stop_when stop_threshold
        cat_max_pages=$(cat "$config_file" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('pagination_overrides',{}).get('$category',{}).get('max_pages', 0))")
        stop_when=$(cat "$config_file" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('pagination_overrides',{}).get('$category',{}).get('stop_when', ''))")
        stop_threshold=$(cat "$config_file" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('pagination_overrides',{}).get('$category',{}).get('threshold', 0.5))")

        # Use override max_pages if set, else global
        local effective_max_pages
        if [ "$cat_max_pages" -gt 0 ] 2>/dev/null; then
            effective_max_pages="$cat_max_pages"
            echo "  [$category] pagination override: max_pages=$effective_max_pages, stop_when=$stop_when" >&2
        else
            effective_max_pages="$max_pages"
        fi

        for ((p=1; p<=effective_max_pages; p++)); do
            local url output_file
            if [ "$p" -eq 1 ]; then
                url="$base_url"
            else
                url=$(add_page_param "$base_url" "$page_param" "$p")
            fi

            output_file="$OUTPUT_DIR/${retailer_name}_${category}_p${p}.md"

            local bytes
            bytes=$(scrape_url "$url" "$output_file" "$wait_for" "$only_main")

            # Stop pagination if page returned very little content (< 500 bytes)
            if [ "$bytes" -lt 500 ] && [ "$p" -gt 1 ]; then
                echo "  -> Page $p has minimal content, stopping pagination" >&2
                rm -f "$output_file"
                break
            fi

            output_files+=("$output_file")
            total_files=$((total_files + 1))

            # Early-stop check via page_checker.py (if stop_when is configured)
            if [ -n "$stop_when" ]; then
                local checker_args=("--mode" "$stop_when" "--file" "$output_file")
                if [ "$stop_when" = "seen_before" ]; then
                    checker_args+=("--latest" "$PROJECT_DIR/results/latest.json" "--threshold" "$stop_threshold")
                fi
                if ! python3 "$PROJECT_DIR/scripts/page_checker.py" "${checker_args[@]}"; then
                    echo "  -> page_checker ($stop_when): stop signal on page $p, halting pagination" >&2
                    break
                fi
            fi
        done
    done

    echo "=== $display_name: scraped $total_files pages ===" >&2
    # Output file paths (one per line) for Claude to read
    for f in "${output_files[@]}"; do
        echo "$f"
    done
}

# Discovery mode - scrape any URL to examine its structure
discovery_scrape() {
    local url="$1"
    local output_file="$OUTPUT_DIR/discovery_$(date +%s).md"

    echo "=== Discovery Mode ===" >&2
    scrape_url "$url" "$output_file" 5000 true
    echo "$output_file"
}

# Product page scrape for size verification
product_scrape() {
    local url="$1"
    local output_file="$OUTPUT_DIR/product_$(date +%s).md"

    echo "=== Product Page Scrape ===" >&2
    scrape_url "$url" "$output_file" 5000 false
    echo "$output_file"
}

# Main
case "${1:-}" in
    --url)
        [ -z "${2:-}" ] && { echo "Usage: $0 --url <URL>" >&2; exit 1; }
        discovery_scrape "$2"
        ;;
    --product)
        [ -z "${2:-}" ] && { echo "Usage: $0 --product <URL>" >&2; exit 1; }
        product_scrape "$2"
        ;;
    all)
        for config_file in "$RETAILERS_DIR"/*.json; do
            retailer=$(basename "$config_file" .json)
            enabled=$(cat "$config_file" | python3 -c "import json,sys; d=json.load(sys.stdin); print(str(d.get('enabled', True)).lower())")
            if [ "$enabled" = "true" ]; then
                scrape_retailer "$retailer"
            fi
        done
        ;;
    "")
        echo "Usage: $0 <retailer|all|--url URL|--product URL>" >&2
        echo "Available retailers:" >&2
        for config_file in "$RETAILERS_DIR"/*.json; do
            echo "  - $(basename "$config_file" .json)" >&2
        done
        exit 1
        ;;
    *)
        scrape_retailer "$1"
        ;;
esac

#!/usr/bin/env python3
"""Parse SSENSE sale markdown files into structured JSON + self-contained HTML viewer."""

import argparse
import glob
import json
import os
import re
import shutil
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Category keywords (order matters - first match wins)
CATEGORY_RULES = [
    ("shoes", ["boot", "shoe", "sneaker", "loafer", "sandal", "mule", "slipper",
               "derby", "oxford", "clog", "espadrille", "pump", "flat", "trainer"]),
    ("outerwear", ["coat", "jacket", "blazer", "parka", "vest", "gilet", "cape",
                   "poncho", "anorak", "windbreaker", "bomber", "overcoat", "peacoat"]),
    ("bottoms", ["trouser", "pant", "jean", "short", "skirt", "legging", "jogger", "chino"]),
    ("tops", ["shirt", "t-shirt", "tee", "polo", "sweater", "hoodie", "cardigan",
              "tank", "blouse", "henley", "sweatshirt", "top", "knit", "pullover",
              "jersey", "debardeur", "mock neck"]),
    ("accessories", ["bag", "belt", "wallet", "scarf", "hat", "cap", "glove",
                     "sunglasses", "tie", "ring", "necklace", "bracelet", "earring",
                     "keychain", "pouch", "tote", "backpack", "clutch", "case",
                     "watch", "socks", "sock", "beanie", "umbrella"]),
]

# SSENSE product block regex
# Format: [![slug - Name](<Base64-Image-Removed>)\\\n\\\nBRAND\\\n\\\n\\\nProduct Name\\\n\\\n\\\n$SALE\\\n\\\n$ORIGINAL](url)
SSENSE_PRODUCT_RE = re.compile(
    r'\[!\[.*?\]\(.*?\)\\\\\n'        # image link opening
    r'\\\\\n'                          # \\
    r'(.+?)\\\\\n'                     # brand name (group 1)
    r'\\\\\n'                          # \\
    r'\\\\\n'                          # \\
    r'(.+?)\\\\\n'                     # product name (group 2)
    r'\\\\\n'                          # \\
    r'\\\\\n'                          # \\
    r'\$([0-9,]+)\\\\\n'              # sale price (group 3)
    r'\\\\\n'                          # \\
    r'\$([0-9,]+)\]'                   # original price (group 4)
    r'\((https://www\.ssense\.com/[^\)]+)\)',  # url (group 5)
    re.MULTILINE
)

def parse_ssense_products_simple(text):
    """Parse SSENSE markdown by splitting on product link blocks.

    Each block looks like (with literal \\\\ as line-continuation markers):
        [![slug - Name](<Base64-Image-Removed>)\\\\
        \\\\
        BRAND\\\\
        \\\\
        \\\\
        Product Name\\\\
        \\\\
        \\\\
        $SALE_PRICE\\\\
        \\\\
        $ORIGINAL_PRICE](https://www.ssense.com/...)
    """
    products = []
    blocks = re.split(r'(?=\[!\[)', text)

    for block in blocks:
        if not block.strip():
            continue

        # Extract URL — may be on same line as last price: $680](url)
        url_match = re.search(r'\]\((https://www\.ssense\.com/[^\)]+)\)', block)
        if not url_match:
            continue
        url = url_match.group(1)

        # Clean lines: strip whitespace and trailing backslashes (literal \\ in file)
        clean_lines = []
        for line in block.split('\n'):
            # Strip trailing backslash escapes (file has literal \\)
            cleaned = line.strip()
            while cleaned.endswith('\\'):
                cleaned = cleaned[:-1]
            cleaned = cleaned.strip()
            if not cleaned:
                continue
            # Remove the ](url) suffix if present on a price line
            cleaned = re.sub(r'\]\(https://www\.ssense\.com/[^\)]+\)$', '', cleaned)
            cleaned = cleaned.strip()
            if cleaned:
                clean_lines.append(cleaned)

        # Filter out image tag line, "SALE ONLY", section headers
        filtered = []
        for line in clean_lines:
            if line.startswith('[!['):
                continue
            if line.startswith('#'):
                continue
            if line.upper() == 'SALE ONLY':
                continue
            filtered.append(line)

        # Extract prices (lines matching $NNN)
        prices = []
        text_lines = []
        for line in filtered:
            price_match = re.match(r'^\$([0-9,]+)$', line)
            if price_match:
                prices.append(int(price_match.group(1).replace(',', '')))
            else:
                text_lines.append(line)

        if len(prices) >= 2 and len(text_lines) >= 1:
            sale_price = prices[0]
            original_price = prices[1]
            brand = text_lines[0]
            name = text_lines[1] if len(text_lines) > 1 else text_lines[0]

            # If brand == name (only one text line), infer brand from URL
            if brand == name:
                url_brand = re.search(r'/product/([^/]+)/', url)
                if url_brand:
                    brand = url_brand.group(1).replace('-', ' ').title()

            products.append({
                "brand": brand,
                "name": name,
                "sale_price": sale_price,
                "original_price": original_price,
                "url": url,
            })

    return products


def parse_mrporter_products(text):
    """Parse MR PORTER markdown into product dicts.

    Product blocks are markdown links ending with ](product_url).
    Content lines (separated by backslash-newlines) contain:
        BRAND, Product Name, $original_price, XX% off, $sale_price
    """
    products = []

    # Find all product URLs: ](https://www.mrporter.com/.../product/...)
    url_re = re.compile(
        r'\]\((https://www\.mrporter\.com/[^\)]*?/product/[^\)]+)\)'
    )

    for url_match in url_re.finditer(text):
        url = url_match.group(1)
        end_pos = url_match.start()  # position of the ] before (url)

        # Walk backwards to find the matching [ bracket
        depth = 1
        pos = end_pos - 1
        while pos >= 0 and depth > 0:
            if text[pos] == ']':
                depth += 1
            elif text[pos] == '[':
                depth -= 1
            pos -= 1
        if depth != 0:
            continue
        start_pos = pos + 1  # position of the opening [

        raw_content = text[start_pos + 1:end_pos]  # content between [ and ]

        # Extract image URL before stripping
        img_match = re.search(r'!\[.*?\]\((https://www\.mrporter\.com/variants/images/[^)]+)\)', raw_content)
        image_url = img_match.group(1) if img_match else None

        # Strip image tags from content
        raw_content = re.sub(r'!\[.*?\]\(.*?\)', '', raw_content, flags=re.DOTALL)

        # Clean: split on \\+newline, strip backslashes and whitespace
        lines = re.split(r'\\+\s*\n|\\+\s*\\+', raw_content)
        clean = []
        for line in lines:
            s = line.strip().strip('\\').strip()
            if s:
                clean.append(s)

        # Extract prices and text lines
        prices = []
        text_lines = []
        for line in clean:
            price_m = re.match(r'^\$([0-9,]+)$', line)
            if price_m:
                prices.append(int(price_m.group(1).replace(',', '')))
            elif re.match(r'^\d+% off$', line):
                continue  # skip "50% off" lines
            elif line in ('FINAL SALE', 'FURTHER REDUCED'):
                continue
            else:
                text_lines.append(line)

        # Need at least 2 prices (original + sale) and 1 text line (brand)
        if len(prices) < 2 or len(text_lines) < 1:
            continue

        # MR PORTER order: original_price first, sale_price second
        original_price = prices[0]
        sale_price = prices[1]

        brand = text_lines[0]
        name = text_lines[1] if len(text_lines) > 1 else brand

        # Fallback: construct image URL from product ID in URL
        if not image_url:
            pid_match = re.search(r'/product/(?:[^/]+/)+(\d+)$', url)
            if pid_match:
                image_url = f"https://www.mrporter.com/variants/images/{pid_match.group(1)}/in/w358_q60.jpg"

        products.append({
            "brand": brand,
            "name": name,
            "sale_price": sale_price,
            "original_price": original_price,
            "url": url,
            "image_url": image_url,
        })

    return products


def parse_2ndstreet_products(text):
    """Parse 2ndStreet JP markdown into product dicts.

    Product blocks are markdown links ending with ](https://en.2ndstreet.jp/goods/detail/...).
    Content lines (separated by backslash-newlines) contain:
        optional XX% OFF, Brand, Description, Size, Condition, ¥price
    Single price in JPY — no original/sale pair.
    """
    products = []

    # Find all product URLs
    url_re = re.compile(
        r'\]\((https://en\.2ndstreet\.jp/goods/detail/[^\)]+)\)'
    )

    for url_match in url_re.finditer(text):
        url = url_match.group(1)
        end_pos = url_match.start()  # position of ] before (url)

        # Walk backwards to find matching [ bracket
        depth = 1
        pos = end_pos - 1
        while pos >= 0 and depth > 0:
            if text[pos] == ']':
                depth += 1
            elif text[pos] == '[':
                depth -= 1
            pos -= 1
        if depth != 0:
            continue
        start_pos = pos + 1  # position of opening [

        raw_content = text[start_pos + 1:end_pos]  # content between [ and ]

        # Extract image URL before stripping — use full-size instead of thumbnail
        img_match = re.search(r'!\[.*?\]\((https://cdn2\.2ndstreet\.jp/img/pc/goods/[^)]+)\)', raw_content)
        image_url = img_match.group(1).replace('_tn.jpg', '.jpg') if img_match else None

        # Strip image tags from content
        raw_content = re.sub(r'!\[.*?\]\(.*?\)', '', raw_content, flags=re.DOTALL)

        # Clean: split on \\+newline, strip backslashes and whitespace
        lines = re.split(r'\\+\s*\n|\\+\s*\\+', raw_content)
        clean = []
        for line in lines:
            s = line.strip().strip('\\').strip()
            # Strip markdown list prefix "- "
            if s.startswith('- '):
                s = s[2:].strip()
            if s and s != '-':
                clean.append(s)

        # Extract discount, price, and text lines
        discount_pct = 0
        price = None
        text_lines = []

        for line in clean:
            # Match "XX% OFF" (possibly with leading dash or whitespace)
            disc_m = re.match(r'^-?\s*(\d+)%\s*OFF$', line, re.IGNORECASE)
            if disc_m:
                discount_pct = int(disc_m.group(1))
                continue

            # Match ¥ price (e.g. ¥14,190)
            price_m = re.match(r'^[¥￥]([0-9,]+)$', line)
            if price_m:
                price = int(price_m.group(1).replace(',', ''))
                continue

            # Skip condition/size metadata lines
            if line.startswith('Item Condition:'):
                continue
            if re.match(r'^Size\s', line):
                continue

            text_lines.append(line)

        if price is None or len(text_lines) < 1:
            continue

        brand = text_lines[0]
        name = text_lines[1] if len(text_lines) > 1 else brand

        # Back-calculate original price from discount
        if discount_pct > 0:
            original_price = round(price / (1 - discount_pct / 100))
        else:
            original_price = price

        products.append({
            "brand": brand,
            "name": name,
            "sale_price": price,
            "original_price": original_price,
            "discount_pct": discount_pct,
            "url": url,
            "currency": "JPY",
            "retailer_type": "secondhand",
            "image_url": image_url,
        })

    return products


def infer_category(product_name):
    """Infer product category from name keywords."""
    name_lower = product_name.lower()
    for category, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in name_lower:
                return category
    return "other"


def parse_file(filepath):
    """Parse a single markdown file and return list of product dicts."""
    with open(filepath, 'r', encoding='utf-8') as f:
        text = f.read()

    # Skip "no sale products" pages
    if 'no sale products to display' in text.lower():
        return []

    # Skip very small files (anti-bot blocks, empty pages)
    if len(text) < 500:
        return []

    # Determine retailer from filename
    basename = os.path.basename(filepath)
    retailer = basename.split('_')[0] if '_' in basename else 'unknown'

    if retailer == '2ndstreet':
        products = parse_2ndstreet_products(text)
    elif retailer == 'mrporter':
        products = parse_mrporter_products(text)
    else:
        products = parse_ssense_products_simple(text)

    # Enrich each product
    for p in products:
        p['retailer'] = retailer
        p.setdefault('image_url', None)
        # 2ndStreet parser sets discount_pct/currency/retailer_type itself
        if 'discount_pct' not in p:
            if p['original_price'] > 0:
                p['discount_pct'] = round((1 - p['sale_price'] / p['original_price']) * 100)
            else:
                p['discount_pct'] = 0
        p.setdefault('currency', 'USD')
        p.setdefault('retailer_type', 'standard')
        p['category'] = infer_category(p['name'])

    return products


def load_preferences(prefs_path=None):
    """Load preferences from config file."""
    if prefs_path is None:
        prefs_path = os.path.expanduser(
            '~/.claude/skills/sale-monitor/config/preferences.json'
        )
    if os.path.exists(prefs_path):
        with open(prefs_path) as f:
            return json.load(f)
    return {}


def _load_cortex_state():
    """Load Cortex feedback state for sale-monitor items. Returns {} on failure."""
    try:
        cortex_export = os.path.expanduser('~/CLAUDE/Cortex/scripts/export_feedback.py')
        if not os.path.exists(cortex_export):
            return {}
        import importlib.util
        spec = importlib.util.spec_from_file_location("export_feedback", cortex_export)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.export_sale_monitor_state()
    except Exception:
        return {}


SSENSE_IMAGE_CACHE_PATH = os.path.expanduser('~/CLAUDE/sale-monitor/cache/ssense_images.json')


def fetch_jpy_usd_rate():
    """Fetch current JPY→USD exchange rate from frankfurter.app (free, no key)."""
    try:
        req = urllib.request.Request(
            'https://api.frankfurter.app/latest?from=JPY&to=USD',
            headers={'User-Agent': 'sale-monitor/1.0'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        rate = data['rates']['USD']
        print(f'  JPY→USD rate: {rate} (1 JPY = ${rate})')
        return rate
    except Exception as e:
        print(f'  Warning: could not fetch JPY→USD rate: {e}')
        return None


def convert_jpy_to_usd(products, rate):
    """Convert JPY-denominated products to USD, preserving original prices."""
    converted = 0
    for p in products:
        if p.get('currency') != 'JPY':
            continue
        p['sale_price_jpy'] = p['sale_price']
        p['original_price_jpy'] = p['original_price']
        p['sale_price'] = round(p['sale_price'] * rate)
        p['original_price'] = round(p['original_price'] * rate)
        p['currency'] = 'USD'
        converted += 1
    if converted:
        print(f'  Converted {converted} JPY products to USD')


def _load_firecrawl_key():
    """Load Firecrawl API key from env or shell profile."""
    key = os.environ.get('FIRECRAWL_API_KEY', '')
    if key:
        return key
    for rc in ['~/.zshrc', '~/.bashrc', '~/.zprofile', '~/.bash_profile']:
        path = os.path.expanduser(rc)
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    m = re.match(r'^export FIRECRAWL_API_KEY="?(fc-[a-f0-9]+)"?', line)
                    if m:
                        return m.group(1)
    return ''


def _fetch_ssense_product_code(url, api_key):
    """Scrape a single SSENSE product page via Firecrawl and extract productCode."""
    payload = json.dumps({
        "url": url,
        "formats": ["markdown"],
        "waitFor": 5000,
        "onlyMainContent": True,
    }).encode()

    req = urllib.request.Request(
        'https://api.firecrawl.dev/v1/scrape',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        md = body.get('data', {}).get('markdown', '')
        # productCode appears as a standalone line: 6 digits + letter + 6 digits
        m = re.search(r'^\s*(\d{6}[A-Z]\d{6})\s*$', md, re.MULTILINE)
        return m.group(1) if m else None
    except Exception as e:
        print(f'    Warning: Firecrawl request failed for {url}: {e}')
        return None


def resolve_ssense_images(products):
    """Resolve image URLs for SSENSE products using a persistent cache.

    For each SSENSE product missing an image_url:
      1. Check the local cache (keyed by product URL ID)
      2. On cache miss, scrape the product page via Firecrawl to get the productCode
      3. Construct the image URL and cache the result
    """
    ssense_products = [p for p in products if p.get('retailer') == 'ssense' and not p.get('image_url')]
    if not ssense_products:
        return

    # Load cache
    cache = {}
    if os.path.exists(SSENSE_IMAGE_CACHE_PATH):
        with open(SSENSE_IMAGE_CACHE_PATH) as f:
            cache = json.load(f)

    # Check cache first
    to_fetch = []
    for p in ssense_products:
        pid = p['url'].rstrip('/').split('/')[-1]
        if pid in cache:
            p['image_url'] = cache[pid]
        else:
            to_fetch.append((p, pid))

    cached_count = len(ssense_products) - len(to_fetch)
    if cached_count:
        print(f'  SSENSE images: {cached_count} from cache')

    if not to_fetch:
        return

    # Need API key for fetching
    api_key = _load_firecrawl_key()
    if not api_key:
        print(f'  SSENSE images: {len(to_fetch)} need fetching but no FIRECRAWL_API_KEY found, skipping')
        return

    print(f'  SSENSE images: fetching {len(to_fetch)} product pages...')
    fetched = 0
    for i, (p, pid) in enumerate(to_fetch):
        code = _fetch_ssense_product_code(p['url'], api_key)
        if code:
            image_url = f'https://img.ssensemedia.com/images/{code}_1/x.jpg'
            p['image_url'] = image_url
            cache[pid] = image_url
            fetched += 1
            print(f'    [{i+1}/{len(to_fetch)}] {pid} -> {code}')
        else:
            print(f'    [{i+1}/{len(to_fetch)}] {pid} -> no productCode found')
        # Small delay between requests
        if i < len(to_fetch) - 1:
            time.sleep(0.5)

    print(f'  SSENSE images: {fetched}/{len(to_fetch)} resolved')

    # Save updated cache
    os.makedirs(os.path.dirname(SSENSE_IMAGE_CACHE_PATH), exist_ok=True)
    with open(SSENSE_IMAGE_CACHE_PATH, 'w') as f:
        json.dump(cache, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description='Parse SSENSE sale markdown into JSON')
    parser.add_argument('--input-dir', default=None,
                        help='Directory with scraped .md files (default: ~/CLAUDE/sale-monitor/output)')
    parser.add_argument('--min-discount', type=int, default=None,
                        help='Minimum discount %% (default: from preferences.json)')
    parser.add_argument('--output-dir', default=None,
                        help='Directory for JSON output (default: ~/CLAUDE/sale-monitor/results)')
    parser.add_argument('--viewer-dir', default=None,
                        help='Directory for HTML viewer (default: ~/CLAUDE/sale-monitor/viewer)')
    parser.add_argument('--prefs', default=None,
                        help='Path to preferences.json')
    args = parser.parse_args()

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    input_dir = args.input_dir or os.path.join(project_dir, 'output')
    output_dir = args.output_dir or os.path.join(project_dir, 'results')
    viewer_dir = args.viewer_dir or os.path.join(project_dir, 'viewer')

    # Load preferences
    prefs = load_preferences(args.prefs)
    min_discount = args.min_discount or prefs.get('min_discount_pct', 50)

    # Find all markdown files
    md_files = sorted(glob.glob(os.path.join(input_dir, '*.md')))
    if not md_files:
        print(f'No .md files found in {input_dir}')
        return

    print(f'Parsing {len(md_files)} files from {input_dir}...')

    # Parse all files
    all_products = []
    retailers = set()
    for filepath in md_files:
        products = parse_file(filepath)
        all_products.extend(products)
        if products:
            retailers.add(products[0]['retailer'])

    print(f'  Total products parsed: {len(all_products)}')

    # Deduplicate by URL (p2 often repeats p1)
    seen_urls = set()
    unique_products = []
    for p in all_products:
        if p['url'] not in seen_urls:
            seen_urls.add(p['url'])
            unique_products.append(p)

    print(f'  After dedup: {len(unique_products)}')

    # Filter by discount threshold (secondhand items always pass)
    filtered = [p for p in unique_products
                if p.get('retailer_type') == 'secondhand' or p['discount_pct'] >= min_discount]
    filtered.sort(key=lambda p: p['discount_pct'], reverse=True)

    print(f'  After filtering (>={min_discount}% off): {len(filtered)}')

    # Convert JPY prices to USD
    jpy_usd_rate = fetch_jpy_usd_rate()
    if jpy_usd_rate:
        convert_jpy_to_usd(filtered, jpy_usd_rate)

    # Resolve SSENSE images (cache-first, Firecrawl for misses)
    resolve_ssense_images(filtered)

    # Embed Cortex feedback state (graceful fallback if unavailable)
    cortex_state = _load_cortex_state()
    if cortex_state:
        for p in filtered:
            status = cortex_state.get(p['url'])
            if status:
                p['cortex_status'] = status
        print(f'  Cortex sync: {sum(1 for p in filtered if "cortex_status" in p)} items with feedback')

    # Build output JSON
    now = datetime.now(timezone.utc)
    timestamp = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    file_timestamp = now.strftime('%Y-%m-%d_%H%M%S')

    meta = {
        "scraped_at": timestamp,
        "retailers": sorted(retailers),
        "total_parsed": len(unique_products),
        "total_filtered": len(filtered),
        "min_discount_pct": min_discount,
    }
    if jpy_usd_rate:
        meta["jpy_usd_rate"] = jpy_usd_rate

    result = {
        "meta": meta,
        "products": filtered,
    }

    # Write timestamped JSON
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, f'{file_timestamp}.json')
    with open(json_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f'  Wrote {json_path}')

    # Update latest.json symlink
    latest_path = os.path.join(output_dir, 'latest.json')
    if os.path.islink(latest_path) or os.path.exists(latest_path):
        os.remove(latest_path)
    os.symlink(os.path.basename(json_path), latest_path)
    print(f'  Updated {latest_path} -> {os.path.basename(json_path)}')

    # Generate self-contained deals.html
    generate_deals_html(result, viewer_dir)
    print(f'  Wrote {os.path.join(viewer_dir, "deals.html")}')

    print(f'\nDone! {len(filtered)} deals ready.')
    print(f'  Open: {os.path.join(viewer_dir, "deals.html")}')


def generate_deals_html(data, viewer_dir):
    """Generate a self-contained HTML file with inlined JSON data."""
    # Read the template
    template_path = os.path.join(viewer_dir, 'index.html')
    if not os.path.exists(template_path):
        print(f'  Warning: {template_path} not found, skipping deals.html generation')
        return

    with open(template_path, 'r') as f:
        template = f.read()

    # Inject data by replacing the placeholder
    json_str = json.dumps(data)
    html = template.replace(
        'const EMBEDDED_DATA = null;',
        f'const EMBEDDED_DATA = {json_str};'
    )

    deals_path = os.path.join(viewer_dir, 'deals.html')
    with open(deals_path, 'w') as f:
        f.write(html)


if __name__ == '__main__':
    main()

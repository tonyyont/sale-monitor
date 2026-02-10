#!/usr/bin/env python3
"""Check scraped page content to decide whether to continue pagination.

Two modes:
  --mode no_discount   Exit 1 (stop) if no products on the page have discounts.
  --mode seen_before   Exit 1 (stop) if >threshold of product URLs already exist in latest.json.

Exit 0 = continue scraping, exit 1 = stop pagination.
Diagnostic output goes to stderr.
"""

import argparse
import json
import re
import sys


def check_no_discount(text):
    """Return True if page has at least one discounted product."""
    # Match "XX% OFF" pattern used by 2ndStreet
    discounts = re.findall(r'\d+%\s*OFF', text, re.IGNORECASE)
    count = len(discounts)
    print(f"  page_checker: no_discount — found {count} discounted items", file=sys.stderr)
    return count > 0


def check_seen_before(text, latest_path, threshold):
    """Return True if page has enough new (unseen) products to continue."""
    # Extract product URLs from page
    page_urls = set(re.findall(
        r'https://en\.2ndstreet\.jp/goods/detail/[^\s\)\]]+', text
    ))
    if not page_urls:
        print("  page_checker: seen_before — no product URLs found on page", file=sys.stderr)
        return False  # no products = stop

    # Load known URLs from latest.json
    known_urls = set()
    try:
        with open(latest_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for p in data.get('products', []):
            known_urls.add(p.get('url', ''))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  page_checker: seen_before — could not load {latest_path}: {e}", file=sys.stderr)
        return True  # can't check = keep going

    overlap = page_urls & known_urls
    ratio = len(overlap) / len(page_urls) if page_urls else 0
    print(
        f"  page_checker: seen_before — {len(overlap)}/{len(page_urls)} URLs already known "
        f"({ratio:.0%}, threshold {threshold:.0%})",
        file=sys.stderr,
    )
    return ratio < threshold  # continue if overlap is below threshold


def main():
    parser = argparse.ArgumentParser(description='Page content checker for pagination control')
    parser.add_argument('--mode', required=True, choices=['no_discount', 'seen_before'])
    parser.add_argument('--file', required=True, help='Scraped markdown file to check')
    parser.add_argument('--latest', default=None, help='Path to latest.json (for seen_before mode)')
    parser.add_argument('--threshold', type=float, default=0.5,
                        help='Overlap ratio to trigger stop (for seen_before mode)')
    args = parser.parse_args()

    try:
        with open(args.file, 'r', encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        print(f"  page_checker: file not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    if args.mode == 'no_discount':
        should_continue = check_no_discount(text)
    elif args.mode == 'seen_before':
        latest = args.latest or ''
        if not latest:
            print("  page_checker: --latest required for seen_before mode", file=sys.stderr)
            sys.exit(1)
        should_continue = check_seen_before(text, latest, args.threshold)

    sys.exit(0 if should_continue else 1)


if __name__ == '__main__':
    main()

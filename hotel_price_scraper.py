#!/usr/bin/env python3
"""
Hotel Price Scraper - Section L Ueno Hirokoji & Section L Akihabara
Supports Booking.com, Agoda, Hotels.com

Usage:
    python hotel_price_scraper.py 2025-08-10 2025-08-13
    python hotel_price_scraper.py 2025-08-10 2025-08-13 --portal agoda
    python hotel_price_scraper.py 2025-08-10 2025-08-13 --output my_prices.csv

Requirements:
    pip install playwright
    playwright install chromium
"""

import argparse
import asyncio
import csv
import os
import re
import sys
from datetime import datetime, date
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ── Hotel definitions ─────────────────────────────────────────────────────────
HOTELS = [
    {
        "name": "Section L Ueno Hirokoji",
        "booking": "https://www.booking.com/hotel/jp/section-l-ueno-hirokoji.en-gb.html",
        "agoda":   "https://www.agoda.com/section-l-ueno-hirokoji/hotel/tokyo-jp.html",
        "hotels_com": "https://www.hotels.com/ho573817/",
    },
    {
        "name": "Section L Akihabara",
        "booking": "https://www.booking.com/hotel/jp/section-l-akihabara.en-gb.html",
        "agoda":   "https://www.agoda.com/section-l-akihabara/hotel/tokyo-jp.html",
        "hotels_com": "https://www.hotels.com/ho2375654/",
    },
]

# ── Portal strategies ─────────────────────────────────────────────────────────
PORTALS = {
    "booking": {
        "label": "Booking.com",
        "build_url": lambda base, ci, co: (
            f"{base}?checkin={ci}&checkout={co}"
            f"&group_adults=1&no_rooms=1&group_children=0&selected_currency=JPY"
        ),
        "dismiss": [
            "[data-testid='accept-all-button']",
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept')",
            "[aria-label='Dismiss sign-in info.']",
            ".bui-modal__close",
        ],
        "price_selectors": [
            "[data-testid='price-and-discounted-price']",
            "[data-testid='price-per-night']",
            ".prco-valign-middle-helper",
            ".bui-price-display__value",
            ".hprt-price-price",
            ".prco-inline-block-maker-helper",
            ".bui-price-display",
            "[class*='priceWrapper']",
        ],
        "cta": [
            "text=Check availability",
            "text=See availability",
            "[data-testid='availability-cta-btn']",
        ],
        "price_patterns": [r"[¥￥]\s*([\d,]+)", r"JPY\s*([\d,]+)"],
    },
    "agoda": {
        "label": "Agoda",
        "build_url": lambda base, ci, co: (
            f"{base}?checkIn={ci}&checkOut={co}&adults=1&children=0&rooms=1"
        ),
        "dismiss": [
            "[data-element-name='cookie-banner-accept']",
            "button:has-text('Accept')",
        ],
        "price_selectors": [
            "[data-selenium='display-price']",
            ".PropertyCardPrice__Value",
            ".pd-price",
            "[class*='price'][class*='amount']",
            "[data-testid='price']",
            ".CopyColor-cheapest-price",
        ],
        "cta": [],
        "price_patterns": [r"[¥￥]\s*([\d,]+)", r"JPY\s*([\d,]+)", r"([\d,]{4,})"],
    },
    "hotels_com": {
        "label": "Hotels.com",
        "build_url": lambda base, ci, co: (
            f"{base}?q-check-in={ci}&q-check-out={co}&q-rooms=1&q-room-0-adults=1"
        ),
        "dismiss": [
            "button:has-text('Accept')",
            "#onetrust-accept-btn-handler",
        ],
        "price_selectors": [
            "[data-stid='section-price-summary']",
            "[class*='price-lock']",
            "[data-testid='price']",
            ".Price",
        ],
        "cta": [],
        "price_patterns": [r"[¥￥]\s*([\d,]+)", r"JPY\s*([\d,]+)", r"([\d,]{4,})"],
    },
}


def parse_date(s):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {s!r}. Use YYYY-MM-DD.")


def first_price(text, patterns, min_val=500):
    for pat in patterns:
        for m in re.finditer(pat, text):
            val = int(m.group(1).replace(",", ""))
            if val >= min_val:
                return val
    return None


async def dismiss_overlays(page, selectors):
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await page.wait_for_timeout(500)
        except Exception:
            pass


async def collect_price(page, price_selectors, patterns):
    for sel in price_selectors:
        try:
            elems = page.locator(sel)
            n = await elems.count()
            for i in range(min(n, 6)):
                txt = await elems.nth(i).inner_text(timeout=1500)
                p = first_price(txt, patterns)
                if p:
                    return p, sel
        except Exception:
            continue
    return None, None


async def scrape_one(browser, hotel, portal_key, checkin, checkout):
    strategy = PORTALS[portal_key]
    base_url  = hotel[portal_key]
    ci, co    = checkin.isoformat(), checkout.isoformat()
    url       = strategy["build_url"](base_url, ci, co)
    nights    = (checkout - checkin).days

    row = {
        "hotel":              hotel["name"],
        "portal":             strategy["label"],
        "checkin":            ci,
        "checkout":           co,
        "nights":             nights,
        "price_per_night_JPY": None,
        "total_price_JPY":    None,
        "currency":           "JPY",
        "status":             "pending",
        "scraped_at":         datetime.now().isoformat(timespec="seconds"),
        "url":                url,
    }

    ctx = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-GB",
        timezone_id="Asia/Tokyo",
        viewport={"width": 1440, "height": 900},
    )
    page = await ctx.new_page()
    await page.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )

    try:
        print(f"\n  [{hotel['name']}]  portal={strategy['label']}")
        print(f"   URL: {url[:100]}")

        # Step 1: load page
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        print("   ✓ DOM ready")

        # Step 2: dismiss overlays
        await dismiss_overlays(page, strategy["dismiss"])

        # Step 3: wait for network to settle
        try:
            await page.wait_for_load_state("networkidle", timeout=12_000)
            print("   ✓ Network idle")
        except Exception:
            print("   ! Network did not idle, continuing")

        # Step 4: first price attempt
        await page.wait_for_timeout(1000)
        price, matched_sel = await collect_price(
            page, strategy["price_selectors"], strategy["price_patterns"]
        )

        # Step 5: scroll + click CTA + retry
        if price is None:
            print("   … Scrolling and trying CTA …")
            await page.evaluate("window.scrollBy(0, Math.floor(document.body.scrollHeight/2))")
            await page.wait_for_timeout(1500)
            for cta in strategy.get("cta", []):
                try:
                    btn = page.locator(cta).first
                    if await btn.is_visible(timeout=1200):
                        await btn.click()
                        print(f"   … Clicked: {cta}")
                        await page.wait_for_timeout(3000)
                        break
                except Exception:
                    pass
            price, matched_sel = await collect_price(
                page, strategy["price_selectors"], strategy["price_patterns"]
            )

        # Step 6: full-page text grep (last resort)
        if price is None:
            print("   … Full-page text grep …")
            body = await page.inner_text("body")
            price = first_price(body, strategy["price_patterns"])
            if price:
                matched_sel = "body-text-grep"

        # Record result
        if price:
            if price > 80_000 and nights > 1:
                row["total_price_JPY"]    = price
                row["price_per_night_JPY"] = round(price / nights)
            else:
                row["price_per_night_JPY"] = price
                row["total_price_JPY"]    = price * nights
            row["status"] = f"ok [{matched_sel}]"
            print(f"   ✓ ¥{row['price_per_night_JPY']:,}/night  ·  ¥{row['total_price_JPY']:,} total")
        else:
            row["status"] = "not_found"
            print("   ✗ No price found")

        # Save debug screenshot
        safe = hotel["name"].replace(" ", "_")
        await page.screenshot(path=f"{safe}_{portal_key}.png", full_page=False)

    except PlaywrightTimeoutError:
        row["status"] = "timeout"
        print("   ✗ Timeout")
    except Exception as e:
        row["status"] = f"error: {str(e)[:100]}"
        print(f"   ✗ {e}")
    finally:
        await ctx.close()

    return row


async def run(checkin, checkout, portal, output):
    print(f"\n{'='*60}")
    print(f"  Section L Hotel Price Scraper")
    print(f"  Check-in  : {checkin}   Check-out : {checkout}")
    print(f"  Nights    : {(checkout-checkin).days}   Portal: {PORTALS[portal]['label']}")
    print(f"  Output    : {output}")
    print(f"{'='*60}")

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        for hotel in HOTELS:
            r = await scrape_one(browser, hotel, portal, checkin, checkout)
            results.append(r)
            await asyncio.sleep(2)
        await browser.close()

    # Write CSV
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    fields = ["hotel","portal","checkin","checkout","nights",
              "price_per_night_JPY","total_price_JPY","currency",
              "status","scraped_at","url"]
    with open(output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    for r in results:
        ppn = f"¥{r['price_per_night_JPY']:,}" if r["price_per_night_JPY"] else "N/A"
        tot = f"¥{r['total_price_JPY']:,}"    if r["total_price_JPY"]    else "N/A"
        print(f"  {r['hotel']}")
        print(f"    Per night : {ppn:>12}    Total : {tot}")
        print(f"    Status    : {r['status']}")
    print(f"\n  CSV saved → {output}")
    print(f"{'='*60}\n")
    return results


def main():
    ap = argparse.ArgumentParser(description="Section L hotel price scraper")
    ap.add_argument("checkin",   help="Check-in  date YYYY-MM-DD")
    ap.add_argument("checkout",  help="Check-out date YYYY-MM-DD")
    ap.add_argument("--portal",  choices=list(PORTALS.keys()), default="booking")
    ap.add_argument("--output",  default="hotel_prices.csv")
    args = ap.parse_args()

    ci = parse_date(args.checkin)
    co = parse_date(args.checkout)
    if ci >= co:
        sys.exit("Error: checkout must be after checkin.")

    asyncio.run(run(ci, co, args.portal, args.output))


if __name__ == "__main__":
    main()

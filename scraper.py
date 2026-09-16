"""
WICOM UK scraper - core logic.

How it works:
1. Read the list of manufacturers from the site's menu.
2. For each manufacturer, open its category page (100 products per page)
   and keep going to the next page until there are no new products.
3. From each product card, take: code, description, original price,
   discounted price. The manufacturer comes from the category we are in.
"""

import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://wicom.com/uk/"
PAGE_SIZE = 100          # products per listing page (site allows 36/48/100)
MAX_PAGES = 500          # safety cap per manufacturer

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def make_session():
    """A requests session that retries automatically on temporary errors."""
    retry = Retry(
        total=4,
        backoff_factor=1.5,  # waits 1.5s, 3s, 6s, 12s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


def fetch(session, url, delay=1.0):
    """Download a page, with a small random pause to be polite to the site."""
    if delay > 0:
        time.sleep(delay + random.uniform(0, delay / 2))
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.text


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def slugify(text):
    """'WIC 7302028-A' -> 'wic-7302028-a' (same style as the site's URLs)."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_price(element):
    """Turn a Magento price element into a number (or None)."""
    if element is None:
        return None
    amount = element.get("data-price-amount")
    if amount:
        return round(float(amount), 2)
    match = re.search(r"\d[\d,]*\.?\d*", element.get_text(" ", strip=True))
    if match:
        return round(float(match.group().replace(",", "")), 2)
    return None


def split_code_and_description(title, product_url):
    """
    The product title looks like 'AP-4921 CYTIVA PALL,ACRODISC ...'.
    The URL contains the code as a slug: '.../s/ap-4921/...'.
    We find how many words at the start of the title match that slug.
    """
    slug_match = re.search(r"/s/([^/]+)/", product_url or "")
    words = title.split()
    if slug_match:
        slug = slug_match.group(1)
        for i in range(1, len(words) + 1):
            if slugify(" ".join(words[:i])) == slug:
                code = " ".join(words[:i])
                description = " ".join(words[i:]) or code
                return code, description
    # Fallback: first word is the code
    if words:
        return words[0], " ".join(words[1:]) or words[0]
    return "", ""


def parse_listing_page(html, manufacturer):
    """Return a list of product dicts found on one listing page."""
    soup = BeautifulSoup(html, "lxml")
    rows = []

    for item in soup.select("li.product-item"):
        link = item.select_one("a.product-item-link") or item.select_one(
            "a[href*='/catalog/product/view/']"
        )
        if link is None:
            continue

        url = link.get("href", "")
        title = link.get_text(" ", strip=True)
        code, description = split_code_and_description(title, url)

        final_price = parse_price(item.select_one('[data-price-type="finalPrice"]'))
        old_price = parse_price(item.select_one('[data-price-type="oldPrice"]'))

        # If there is an "old price", the item is discounted.
        if old_price is not None:
            original, discounted = old_price, final_price
        else:
            original, discounted = final_price, None

        rows.append({
            "Manufacturer": manufacturer,
            "Product Code": code,
            "Description": description,
            "Original Price (EUR)": original,
            "Discounted Price (EUR)": discounted,
            "Product URL": url,
        })

    return rows


# ---------------------------------------------------------------------------
# Scraping steps
# ---------------------------------------------------------------------------

def get_manufacturers(session=None):
    """Read the manufacturer list from the 'Manufacturers' menu."""
    session = session or make_session()
    soup = BeautifulSoup(fetch(session, BASE_URL, delay=0), "lxml")

    brands = {}
    for menu_link in soup.select('a[href$="/hersteller/"]'):
        parent = menu_link.find_parent("li")
        if parent is None:
            continue
        for a in parent.select("ul a[href]"):
            name = a.get_text(strip=True)
            href = a["href"]
            # skip the single-letter "A", "B", "C" headings
            if href.startswith("#") or len(name) <= 1:
                continue
            brands[name] = href
        if brands:
            break

    return [{"name": n, "url": u} for n, u in sorted(brands.items())]


def scrape_manufacturer(session, brand, max_pages=MAX_PAGES, delay=1.0):
    """Scrape all pages for one manufacturer."""
    products = []
    seen_urls = set()

    for page in range(1, max_pages + 1):
        url = f"{brand['url']}?product_list_limit={PAGE_SIZE}&p={page}"
        rows = parse_listing_page(fetch(session, url, delay), brand["name"])

        new_rows = [r for r in rows if r["Product URL"] not in seen_urls]
        # Magento repeats the last page when you go past the end -> stop.
        if not new_rows:
            break

        for r in new_rows:
            seen_urls.add(r["Product URL"])
        products.extend(new_rows)

        if len(rows) < PAGE_SIZE:  # last page
            break

    return products


def scrape_batch(brands, max_pages=MAX_PAGES, workers=3, delay=1.0, on_done=None):
    """
    Scrape a batch of manufacturers in parallel.
    on_done(brand_name, n_products, error) is called after each manufacturer.
    Returns (products, errors).
    """
    session = make_session()
    products, errors = [], []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(scrape_manufacturer, session, b, max_pages, delay): b
            for b in brands
        }
        for future in as_completed(futures):
            brand = futures[future]
            try:
                rows = future.result()
                products.extend(rows)
                if on_done:
                    on_done(brand["name"], len(rows), None)
            except Exception as exc:  # keep going if one brand fails
                errors.append({"Manufacturer": brand["name"], "Error": str(exc)})
                if on_done:
                    on_done(brand["name"], 0, exc)

    return products, errors


def deduplicate(products):
    """Remove duplicates (same product listed under two categories)."""
    unique = {}
    for p in products:
        unique.setdefault(p["Product URL"], p)
    return list(unique.values())

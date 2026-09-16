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

STORE = "de"  # which WICOM store to scrape: "de", "uk", "en", ...


def set_store(code):
    """Switch store, e.g. set_store("de")."""
    global STORE
    STORE = code.strip().strip("/").lower()


def base_url():
    return f"https://wicom.com/{STORE}/"
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


class WrongStoreError(Exception):
    """The site redirected us away from the UK store."""


def add_store_param(url):
    return url  # left as a hook; forcing a store code caused redirects


def fetch(session, url, delay=1.0):
    """Download a page, with a small random pause to be polite to the site."""
    if delay > 0:
        time.sleep(delay + random.uniform(0, delay / 2))
    response = session.get(add_store_param(url), timeout=30)
    response.raise_for_status()
    if f"/{STORE}/" not in response.url:
        raise WrongStoreError(f"Redirected to {response.url} (asked for {url})")
    return response.text


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def slugify(text):
    """'WIC 7302028-A' -> 'wic-7302028-a' (same style as the site's URLs)."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


PACK_SIZE = re.compile(r",\s*(\d+\s*\*\s*[\d.,]+\s*[^,]*)$")


def clean_code(code):
    """'Z3AF / 7000103752' -> 'Z3AF'   'TR642E - 7100347562' -> 'TR642E'"""
    return re.split(r"\s+[/-]\s*|\s*/\s+", code.strip())[0].strip()


def clean_description(text):
    """
    '/ 7000103752 3M,Z3AF/2 BACK PLATE KIT FOR VERSAFLO,1 * 1 KIT'
      -> ('Z3AF/2 BACK PLATE KIT FOR VERSAFLO', '1 * 1 KIT')
    """
    text = text.strip()
    # 1) take the pack size off the end: ',1 * 50 items'
    pack = ""
    match = PACK_SIZE.search(text)
    if match:
        pack = match.group(1).strip()
        text = text[:match.start()]
    # 2) drop a leftover secondary number at the start: '/ 7000103752 '
    text = re.sub(r"^[/-]?\s*\d{6,}\s+", "", text)
    # 3) drop an upper-case brand prefix: '3M,'  'CYTIVA PALL,'
    if "," in text:
        prefix, rest = text.split(",", 1)
        if prefix.strip() and prefix == prefix.upper() and len(prefix) <= 25 and rest.strip():
            text = rest
    return text.strip(" ,"), pack


def parse_price(element):
    """Turn a Magento price element into a number (or None)."""
    if element is None:
        return None
    amount = element.get("data-price-amount")
    if amount:
        return round(float(amount), 2)
    match = re.search(r"\d[\d.,]*", element.get_text(" ", strip=True))
    if not match:
        return None
    number = match.group().rstrip(".,")
    if re.search(r",\d{1,2}$", number):          # German: 1.234,56
        number = number.replace(".", "").replace(",", ".")
    else:                                         # English: 1,234.56
        number = number.replace(",", "")
    return round(float(number), 2)


def read_code_and_description(link, product_url):
    """
    The product name looks like:  <strong>CODE<br/>DESCRIPTION</strong>
    so the line break tells us exactly where the code ends.
    """
    lines = [t.strip() for t in link.get_text("\n").split("\n") if t.strip()]
    if len(lines) >= 2:
        return lines[0], " ".join(lines[1:])
    return split_code_and_description(" ".join(lines), product_url)


def split_code_and_description(title, product_url):
    """
    The product title looks like 'AP-4921 CYTIVA PALL,ACRODISC ...'.
    The URL contains the code as a slug: '.../s/ap-4921/...'.
    We find how many words at the start of the title match that slug.
    """
    slug_match = (re.search(r"/s/([^/]+)/", product_url or "")
                  or re.search(r"/([^/?]+)\.html", product_url or ""))
    words = title.split()
    if slug_match:
        slug = re.sub(r"[^a-z0-9]", "", slug_match.group(1))
        for i in range(1, len(words) + 1):
            if re.sub(r"[^a-z0-9]", "", " ".join(words[:i]).lower()) == slug:
                code = " ".join(words[:i])
                description = " ".join(words[i:]) or code
                return code, description
    # Fallback: first word is the code
    if words:
        return words[0], " ".join(words[1:]) or words[0]
    return "", ""


PRODUCT_LINK = ("a.product-item-link, a.product-item-photo, "
                "a[href*='/catalog/product/view/']")


def is_product_url(url):
    """Real product pages: '/catalog/product/view/...' or '/de/agi-5068-0008.html'."""
    return url.startswith("http") and (
        "/catalog/product/view/" in url or url.split("?")[0].endswith(".html")
    )


def find_product_cards(soup):
    """
    Find one 'card' element per product.
    First try the standard Magento card; if the theme is different,
    walk up from each product link to the nearest <li> or <div> with a price.
    """
    cards = soup.select("ol.product-items > li.product-item")
    if cards:
        return cards

    cards, seen = [], set()
    for link in soup.select(PRODUCT_LINK):
        card = link
        for _ in range(8):  # climb a few levels at most
            if card.parent is None:
                break
            card = card.parent
            if card.name in ("li", "div") and card.select_one("[data-price-type], .price"):
                break
        if id(card) not in seen:
            seen.add(id(card))
            cards.append(card)
    return cards


def pick_title_link(card):
    """The product link with the most text (the image link has no text)."""
    links = card.select(PRODUCT_LINK)
    if not links:
        return None
    return max(links, key=lambda a: len(a.get_text(strip=True)))


def diagnose(url):
    """Small report to help fix the parser if the site layout changes."""
    response = make_session().get(add_store_param(url), timeout=30)
    html = response.text
    soup = BeautifulSoup(html, "lxml")
    first = soup.select_one("li.product-item a.product-item-photo")
    snippet = ""
    if first is not None:
        box = first
        for _ in range(4):
            box = box.parent or box
        snippet = str(box)[:4000]
    return {
        "page_title": soup.title.get_text(strip=True) if soup.title else "",
        "html_length": len(html),
        "product_links": len(soup.select("li.product-item a.product-item-photo")),
        "li.product-item": len(soup.select("li.product-item")),
        "price elements": len(soup.select("[data-price-type]")),
        "sub-category links": len(soup.select("a[href$='.html']")),
        "products_parsed": len(parse_listing_page(html, "test")),
        "first_3_codes": [r["Product Code"] for r in parse_listing_page(html, "test")[:3]],
        "page_links": sorted({a["href"] for a in soup.select("a[href*='p=']")
                              if re.search(r"[?&]p=\d+", a["href"])})[:10],
        "requested_url": url,
        "landed_on": response.url,
        "redirects": [r.headers.get("Location") for r in response.history],
        "html_snippet": snippet or html[:4000],
    }


def parse_listing_page(html, manufacturer):
    """Return a list of product dicts found on one listing page."""
    soup = BeautifulSoup(html, "lxml")
    rows = []

    for item in find_product_cards(soup):
        link = pick_title_link(item)
        if link is None:
            continue

        url = link.get("href", "")
        if not is_product_url(url):
            continue  # skip empty template cards (wishlist/compare sidebar)
        code, description = read_code_and_description(link, url)
        # the site sometimes puts the extra number on the code line
        extra = code[len(clean_code(code)):].strip()
        code = clean_code(code)
        description, pack_size = clean_description(f"{extra} {description}")

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
            "Pack Size": pack_size,
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
    soup = BeautifulSoup(fetch(session, base_url(), delay=0), "lxml")

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
            if href.rstrip("/").endswith("hersteller"):  # the overview page itself
                continue
            brands[name] = href
        if brands:
            break

    return [{"name": n, "url": u} for n, u in sorted(brands.items())]


def scrape_manufacturer(session, brand, max_pages=MAX_PAGES, delay=1.0):
    """Scrape all pages for one manufacturer, 100 products per page
    (same link format the site uses: ?p=3&product_list_limit=100)."""
    pattern = f"{brand['url']}?p={{page}}&product_list_limit=100"
    return scrape_pages(session, brand, pattern, max_pages, delay)


def scrape_pages(session, brand, pattern, max_pages, delay):
    """Go page by page until a page brings no new products."""
    products = []
    seen_urls = set()

    for page in range(1, max_pages + 1):
        rows = parse_listing_page(fetch(session, pattern.format(page=page), delay), brand["name"])
        new_rows = [r for r in rows if r["Product URL"] not in seen_urls]
        # Magento repeats the last page when you go past the end -> stop.
        if not new_rows:
            break
        for r in new_rows:
            seen_urls.add(r["Product URL"])
        products.extend(new_rows)

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

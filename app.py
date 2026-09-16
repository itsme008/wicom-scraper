"""
WICOM UK Scraper - web app.

Run locally:  streamlit run app.py
Flow: 1) load manufacturers  2) run a small test  3) approve  4) full run in batches
"""

import base64
from datetime import datetime

import pandas as pd
import streamlit as st

from scraper import deduplicate, diagnose, get_manufacturers, scrape_batch

st.set_page_config(page_title="WICOM UK Scraper", page_icon="🧪", layout="wide")
st.title("🧪 WICOM UK Product Scraper")
st.caption("Scrapes manufacturer, product code, description and prices from wicom.com/uk")

# Remember things between button clicks
for key, default in {
    "brands": None,
    "test_passed": False,
    "test_df": None,
    "full_df": None,
}.items():
    st.session_state.setdefault(key, default)


def to_csv(df):
    return df.to_csv(index=False).encode("utf-8-sig")  # utf-8-sig opens nicely in Excel


def download_section(df, file_name, key):
    """A download button plus a plain link as a backup."""
    data = to_csv(df)
    st.download_button("⬇ Download CSV", data, file_name=file_name,
                       mime="text/csv", key=key)
    b64 = base64.b64encode(data).decode()
    st.markdown(
        f'<a href="data:text/csv;base64,{b64}" download="{file_name}">'
        f"Button not working? Click here to download {file_name}</a>",
        unsafe_allow_html=True,
    )


# ---------------- Sidebar settings ----------------
with st.sidebar:
    st.header("Settings")
    workers = st.slider("Parallel workers", 1, 5, 3,
                        help="More = faster, but heavier on the website.")
    delay = st.slider("Delay between requests (seconds)", 0.5, 5.0, 1.0, 0.5)
    batch_size = st.number_input("Manufacturers per batch", 1, 50, 5)
    with st.expander("🔧 Diagnose a page"):
        diag_url = st.text_input("Page URL", "https://wicom.com/uk/sale.html")
        if st.button("Diagnose"):
            try:
                report = diagnose(diag_url)
                snippet = report.pop("html_snippet")
                st.json(report)
                st.code(snippet, language="html")
            except Exception as exc:
                st.error(str(exc))
    if st.button("Reset everything"):
        st.session_state.clear()
        st.rerun()

# ---------------- Step 1: manufacturers ----------------
st.subheader("Step 1 — Load manufacturers")
if st.button("Load manufacturer list"):
    with st.spinner("Reading the site menu..."):
        try:
            st.session_state.brands = get_manufacturers()
        except Exception as exc:
            st.error(f"Could not load manufacturers: {exc}")

brands = st.session_state.brands
if not brands:
    st.info("Click the button above to start.")
    st.stop()

st.success(f"Found {len(brands)} manufacturers.")
brand_names = [b["name"] for b in brands]

# ---------------- Step 2: test run ----------------
st.subheader("Step 2 — Test run (required)")
test_brands = st.multiselect("Manufacturers to test", brand_names, default=brand_names[:2])
test_pages = st.number_input("Pages per manufacturer (36 products each)", 1, 3, 1)

if st.button("Run test", type="primary", disabled=not test_brands):
    selected = [b for b in brands if b["name"] in test_brands]
    with st.spinner("Running test..."):
        products, errors = scrape_batch(selected, max_pages=test_pages,
                                        workers=workers, delay=delay)
    st.session_state.test_df = pd.DataFrame(deduplicate(products))
    st.session_state.test_passed = False
    for e in errors:
        st.warning(f"{e['Manufacturer']}: {e['Error']}")

test_df = st.session_state.test_df
if test_df is not None:
    if test_df.empty:
        st.error("The test found no products. The site layout may have changed.")
    else:
        st.write(f"Test found **{len(test_df)}** products. Check that the columns look right:")
        st.dataframe(test_df, use_container_width=True)
        download_section(test_df, "wicom_test.csv", key="dl_test")
        if st.checkbox("✅ The test results look correct", value=st.session_state.test_passed):
            st.session_state.test_passed = True

# ---------------- Step 3: full run ----------------
st.subheader("Step 3 — Full run in batches")
if not st.session_state.test_passed:
    st.info("🔒 Run a test and confirm the results to unlock the full run.")
    st.stop()

full_brands = st.multiselect("Manufacturers to scrape", brand_names, default=brand_names)

if st.button("Start full run", type="primary", disabled=not full_brands):
    selected = [b for b in brands if b["name"] in full_brands]
    batches = [selected[i:i + batch_size] for i in range(0, len(selected), batch_size)]

    all_products, all_errors = [], []
    progress = st.progress(0.0, text="Starting...")
    log = st.empty()
    done_count = 0

    for n, batch in enumerate(batches, start=1):
        messages = []

        def on_done(name, count, error):
            messages.append(f"❌ {name}: {error}" if error else f"✔ {name}: {count} products")

        products, errors = scrape_batch(batch, workers=workers, delay=delay, on_done=on_done)
        all_products.extend(products)
        all_errors.extend(errors)
        done_count += len(batch)

        progress.progress(done_count / len(selected),
                          text=f"Batch {n}/{len(batches)} done — {len(all_products)} products so far")
        log.text("\n".join(messages))
        # save after every batch so nothing is lost if something breaks later
        st.session_state.full_df = pd.DataFrame(deduplicate(all_products))

    st.success(f"Finished: {len(st.session_state.full_df)} unique products.")
    if all_errors:
        st.warning("Some manufacturers failed (you can re-run just those):")
        st.dataframe(pd.DataFrame(all_errors))

full_df = st.session_state.full_df
if full_df is not None and not full_df.empty:
    st.dataframe(full_df, use_container_width=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    download_section(full_df, f"wicom_products_{stamp}.csv", key="dl_full")

# WICOM UK Scraper

Web app that scrapes Manufacturer, Product Code, Description, Original Price and Discounted Price from https://wicom.com/uk/.

## Use it
1. Load manufacturers
2. Run a small test and tick "results look correct"
3. Full run unlocks -> runs in batches -> download CSV

## Run locally
    pip install -r requirements.txt
    streamlit run app.py

## Deploy (no code download needed for users)
**Replit:** import this folder/repo, press Run, then Deploy (the `.replit` file is already set up).
**Streamlit Community Cloud (free):** push to GitHub -> share.streamlit.io -> New app -> pick `app.py`.

## Notes
- Prices are in EUR, excl. VAT (as shown on the site).
- Discounted Price is empty when the item isn't on sale; both prices are empty for quote-only items.

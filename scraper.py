import asyncio
from playwright.async_api import async_playwright
import pandas as pd
import json
import html
import os
import re
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _normalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    # handle protocol-relative urls
    if u.startswith("//"):
        return "https:" + u
    return u


def _normalize_name(n: str) -> str:
    if not n:
        return ""
    return re.sub(r"\s+", " ", n.strip()).lower()

def _extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        if url.startswith('//'):
            url = 'https:' + url
        if not url.startswith('http'):
            url = 'http://' + url
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.startswith('www.'):
            host = host[4:]
        return host
    except Exception:
        return ""
    

START_URL = "https://www.ycombinator.com/companies"
DICE_LOCAL = "Search Companies _ Dice.com.htm"
DICE_URL = "https://www.dice.com/companies"


async def scrape():
    async with async_playwright() as p:
        # Determine headless behavior:
        # - If HEADLESS env var is set, respect it (0/false => headless=False)
        # - Otherwise default to headless in CI (GITHUB_ACTIONS or CI env present),
        #   and non-headless locally for easier debugging.
        headless_env = os.getenv('HEADLESS')
        if headless_env is not None:
            headless = not (headless_env.lower() in ('0', 'false'))
        else:
            headless = bool(os.getenv('GITHUB_ACTIONS')) or bool(os.getenv('CI'))

        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()

        print("Loading Companies pages...")
        await page.goto(START_URL, wait_until="networkidle")

        # --- Select sorting: Launch Date ---
        # The page has a select with an option value 'YCCompany_By_Launch_Date_production'.
        # Try to set that option so companies are listed by launch date before extracting.
        try:
            await page.wait_for_selector('select', timeout=5000)
            await page.select_option('select', value='YCCompany_By_Launch_Date_production')
            # Wait for network activity and a short delay for DOM to update
            await page.wait_for_load_state('networkidle')
            await page.wait_for_timeout(800)
            print("Selected 'Launch Date' sorting.")
        except Exception as e:
            print("Could not set Launch Date sorting:", e)

        # Scroll para cargar más empresas
        last_height = 0
        while True:
            height = await page.evaluate("() => document.body.scrollHeight")
            if height == last_height:
                break
            last_height = height
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(1000)

        print("Extracting company cards (YC)...")

        cards = await page.query_selector_all('a[href^="/companies/"]')
        print(f"Companies found: {len(cards)}")

        # Limit: only collect the most recent N companies to reduce workload
        # Read from environment if provided (useful for CI). Default to 20.
        try:
            MAX_COMPANIES = int(os.getenv('MAX_COMPANIES', '4'))
        except Exception:
            MAX_COMPANIES = 4
        # Keep the first MAX_COMPANIES entries from the listing (adjust if you prefer last N)
        cards = cards[:MAX_COMPANIES]
        print(f"Limiting to {len(cards)} companies (max {MAX_COMPANIES}).")

        results = []

        for card in cards:

            # --- NAME ---
            name_el = await card.query_selector("span")
            name = await name_el.inner_text() if name_el else ""

            # --- LOCATION ---
            spans = await card.query_selector_all("span")
            location = ""
            if len(spans) > 1:
                try:
                    location = await spans[1].inner_text()
                except:
                    location = ""

            # --- DESCRIPTION ---
            desc_el = await card.query_selector('div.mb-1\\.5.text-sm')
            description = await desc_el.inner_text() if desc_el else ""

            # --- URL ---
            url = await card.get_attribute("href")
            full_url = f"https://www.ycombinator.com{url}"

            # --- Open company page to extract website and LinkedIn links ---
            company_linkedin = ""
            company_website = ""
            founders_linkedin = []

            try:
                cp = await browser.new_page()
                await cp.goto(full_url, wait_until="networkidle")
                # Small per-field source tracking for debugging
                company_linkedin_source = ""
                company_website_source = ""
                founders_linkedin_sources = []

                # Retry loop: sometimes the server-rendered data-page element is hydrated slightly after load
                state_el = None
                raw = None
                for attempt in range(3):
                    state_el = await cp.query_selector('div[id^="ycdc_new/pages/Companies/ShowPage-react-component-"]')
                    if not state_el:
                        state_el = await cp.query_selector('div[data-page]')
                    if state_el:
                        raw = await state_el.get_attribute('data-page')
                        if raw:
                            break
                    await cp.wait_for_timeout(300)
                # Try to extract structured data embedded in the page (YC uses a data-page JSON)
                try:
                    if raw:
                        try:
                            parsed = json.loads(html.unescape(raw))
                            props = parsed.get('props', {}) or {}
                            # company-level links
                            company_obj = props.get('company', {}) or {}
                            cl = company_obj.get('linkedin_url', '') or company_obj.get('linkedin', '')
                            if cl:
                                company_linkedin = _normalize_url(cl)
                                company_linkedin_source = 'json'

                            cw = company_obj.get('website', '') or company_obj.get('url', '')
                            if cw:
                                company_website = _normalize_url(cw)
                                company_website_source = 'json'

                            # founders: try both props.founders and company.founders
                            f_list = props.get('founders') or company_obj.get('founders') or []
                            if f_list:
                                for f in f_list:
                                    lk = f.get('linkedin_url') or f.get('linkedin') or f.get('linkedinUrl')
                                    if lk:
                                        founders_linkedin.append(_normalize_url(lk))
                                        founders_linkedin_sources.append('json')
                        except Exception:
                            # fall through to anchor scraping
                            pass
                except Exception:
                    pass

                # Fallback: DOM anchors if structured data didn't yield results
                if not company_linkedin:
                    try:
                        c_link_el = await cp.query_selector('a[href*="linkedin.com/company"]')
                        company_linkedin = await c_link_el.get_attribute('href') if c_link_el else ""
                        if company_linkedin:
                            company_linkedin = _normalize_url(company_linkedin)
                            company_linkedin_source = 'dom'
                    except:
                        company_linkedin = ""
                company_linkedin = _normalize_url(company_linkedin)

                if not company_website:
                    try:
                        web_el = await cp.query_selector('a[aria-label="Company website"]')
                        if web_el:
                            company_website = await web_el.get_attribute('href') or ""
                            if company_website:
                                company_website = _normalize_url(company_website)
                                company_website_source = 'dom'
                        else:
                            anchors = await cp.query_selector_all('a[href^="http"]')
                            for a in anchors:
                                href = await a.get_attribute('href')
                                if href and 'linkedin.com' not in href:
                                    company_website = href
                                    break
                    except:
                        company_website = ""
                company_website = _normalize_url(company_website)

                # Parse JSON-LD (<script type="application/ld+json">) as an additional fallback
                if not company_linkedin or not company_website:
                    try:
                        scripts = await cp.query_selector_all('script[type="application/ld+json"]')
                        for s in scripts:
                            txt = await s.text_content() or ''
                            try:
                                j = json.loads(txt)
                            except Exception:
                                continue
                            # JSON-LD could be a list
                            items = j if isinstance(j, list) else [j]
                            for it in items:
                                # Organization entries often have sameAs with social links
                                same = it.get('sameAs') or []
                                if isinstance(same, str):
                                    same = [same]
                                for url_candidate in same:
                                    if 'linkedin.com/company' in url_candidate and not company_linkedin:
                                        company_linkedin = _normalize_url(url_candidate)
                                        company_linkedin_source = 'json-ld'
                                    if (url_candidate.startswith('http') or url_candidate.startswith('//')) and not company_website and 'linkedin.com' not in url_candidate:
                                        company_website = _normalize_url(url_candidate)
                                        company_website_source = 'json-ld'
                                # Sometimes url field is present
                                if not company_website and it.get('url'):
                                    company_website = _normalize_url(it.get('url'))
                                    company_website_source = 'json-ld'
                    except Exception:
                        pass

                if not founders_linkedin:
                    try:
                        founder_els = await cp.query_selector_all('a[href*="linkedin.com/in"]')
                        seen = set()
                        for f in founder_els:
                            href = await f.get_attribute('href')
                            if href and href not in seen:
                                seen.add(href)
                                founders_linkedin.append(_normalize_url(href))
                                founders_linkedin_sources.append('dom')
                    except:
                        founders_linkedin = []

                # Final fallback: regex scan of full HTML content for linkedin links
                if (not company_linkedin) or (not founders_linkedin):
                    try:
                        page_html = await cp.content()
                        if not company_linkedin:
                            m = re.search(r'https?://(?:www\.)?linkedin\.com/company[0-9A-Za-z_\-./?=&%]+', page_html)
                            if m:
                                company_linkedin = _normalize_url(m.group(0))
                                company_linkedin_source = 'regex'
                        if not founders_linkedin:
                            matches = re.findall(r'https?://(?:www\.)?linkedin\.com/in[0-9A-Za-z_\-./?=&%]+', page_html)
                            seen = set(founders_linkedin)
                            for mm in matches:
                                n = _normalize_url(mm)
                                if n not in seen:
                                    founders_linkedin.append(n)
                                    founders_linkedin_sources.append('regex')
                    except Exception:
                        pass

                await cp.close()
            except Exception as e:
                print(f"Error opening company page {full_url}:", e)

            # Provide a single 'Company Website' field: prefer the company website, otherwise company LinkedIn
            company_website_value = company_website or company_linkedin or ""

            results.append({
                "name": name.strip(),
                "Company Website": company_website_value,
                "Tech Hiring Platforms": "ycombinator.com"
            })
        # --- Now also try Dice (local file fallback to live site) ---
        dice_results = []
        try:
            # Use a fresh browser instance for Dice to avoid depending on the YC browser state
            # Reuse the same headless decision (respect HEADLESS or CI detection)
            dice_browser = await p.chromium.launch(headless=headless)
            dp = await dice_browser.new_page()
            # Always load the live Dice companies listing
            dice_start = DICE_URL
            print(f"Loading live Dice site: {DICE_URL}")
            await dp.goto(dice_start, wait_until="networkidle")
            # basic scroll to load cards
            last_h = 0
            for _ in range(10):
                h = await dp.evaluate("() => document.body.scrollHeight")
                if h == last_h:
                    break
                last_h = h
                await dp.mouse.wheel(0, 3000)
                await dp.wait_for_timeout(800)

            # try several selectors for company cards on Dice
            card_selectors = ['company-card', 'a[href*="/company"]', 'a[href*="/companies"]', 'div.company-card', 'a[data-testid="company-card"]']
            cards = []
            for sel in card_selectors:
                els = await dp.query_selector_all(sel)
                if els:
                    cards = els
                    break

            print(f"Dice cards found: {len(cards)}")
            MAX_DICE = 20
            cards = cards[:MAX_DICE]

            for card in cards:
                # try to get href from the card or inner anchor
                href = None
                try:
                    href = await card.get_attribute('href')
                except:
                    href = None
                if not href:
                    a = await card.query_selector('a')
                    href = await a.get_attribute('href') if a else None
                if not href:
                    # skip if no link
                    continue
                if href.startswith('/'):
                    full = 'https://www.dice.com' + href
                else:
                    full = href

                name = ''
                website = ''
                try:
                    cpage = await dice_browser.new_page()
                    await cpage.goto(full, wait_until='networkidle')
                    # Try JSON-LD first
                    try:
                        scripts = await cpage.query_selector_all('script[type="application/ld+json"]')
                        for s in scripts:
                            txt = await s.text_content() or ''
                            try:
                                j = json.loads(txt)
                            except Exception:
                                continue
                            # extract name and url
                            if not name and isinstance(j, dict) and j.get('name'):
                                name = j.get('name')
                            if not website and isinstance(j, dict) and j.get('url'):
                                website = _normalize_url(j.get('url'))
                    except Exception:
                        pass

                    # DOM fallbacks
                    if not name:
                        h1 = await cpage.query_selector('h1')
                        if h1:
                            name = (await h1.inner_text()).strip()
                    if not website:
                        # common link selectors
                        web_el = await cpage.query_selector('a[aria-label="Company website"]')
                        if web_el:
                            website = _normalize_url(await web_el.get_attribute('href') or '')
                        else:
                            anchors = await cpage.query_selector_all('a[href^="http"]')
                            for a in anchors:
                                href2 = await a.get_attribute('href')
                                if href2 and 'dice.com' not in href2 and 'linkedin.com' not in href2:
                                    website = _normalize_url(href2)
                                    break
                    await cpage.close()
                except Exception:
                    pass

                if name:
                    dice_results.append({
                        'name': name.strip(),
                        'Company Website': website or '',
                        'Tech Hiring Platforms': 'dice.com'
                    })

            await dp.close()
            await dice_browser.close()
        except Exception as e:
            print('Error scraping Dice:', e)

        # combine YC results (from earlier) with dice_results
        combined = results + dice_results

        # close browser and return combined results
        await browser.close()
        return combined


if __name__ == "__main__":
    companies = asyncio.run(scrape())
    print(f"Saving {len(companies)} companies to CSV (name + Company Website)...")
    df = pd.DataFrame(companies)
    if df.empty:
        df = pd.DataFrame(columns=['name', 'Company Website'])
    else:
        # Normalize column names if needed
        if 'Company' in df.columns and 'name' not in df.columns:
            df.rename(columns={'Company': 'name'}, inplace=True)
        if 'Company Website' not in df.columns and 'CompanyWebsite' in df.columns:
            df.rename(columns={'CompanyWebsite': 'Company Website'}, inplace=True)
        df = df[[c for c in ['name', 'Company Website', 'Tech Hiring Platforms'] if c in df.columns]]

    # Load existing CSV (prefer Companies.csv, fallback to yc_companies.csv)
    existing_csv = 'Companies.csv' if Path('Companies.csv').exists() else ('yc_companies.csv' if Path('yc_companies.csv').exists() else None)
    if existing_csv:
        existing_df = pd.read_csv(existing_csv, sep=';', dtype=str, keep_default_na=False).fillna("")
    else:
        existing_df = pd.DataFrame(columns=['name', 'Company Website'])

    # Ensure existing has required columns
    if 'name' not in existing_df.columns and existing_df.shape[1] >= 1:
        existing_df.rename(columns={existing_df.columns[0]: 'name'}, inplace=True)
    if 'Company Website' not in existing_df.columns:
        if 'CompanyWebsite' in existing_df.columns:
            existing_df.rename(columns={'CompanyWebsite': 'Company Website'}, inplace=True)
        else:
            existing_df['Company Website'] = ''
    # If an older 'highlight' column exists, rename it to the new 'Is a new company?' column
    if 'highlight' in existing_df.columns and 'Is a new company?' not in existing_df.columns:
        existing_df.rename(columns={'highlight': 'Is a new company?'}, inplace=True)

    # Clean up any leftover temporary columns from older runs
    for _tmp in ['__name_norm', '__domain']:
        if _tmp in existing_df.columns:
            existing_df.drop(columns=[_tmp], inplace=True)

    # Ensure existing_df has the 'Tech Hiring Platforms' column
    if 'Tech Hiring Platforms' not in existing_df.columns:
        existing_df['Tech Hiring Platforms'] = ''

    # Prepare for duplicate detection (compute sets without adding temporary DataFrame columns)
    existing_names = set(existing_df['name'].astype(str).apply(_normalize_name).tolist())
    existing_domains = set(existing_df['Company Website'].astype(str).apply(_extract_domain).tolist())

    # Filter new rows: not present by name or domain
    new_rows = []
    seen_names = set()
    seen_domains = set()
    for _, r in df.iterrows():
        name = str(r.get('name', '')).strip()
        site = str(r.get('Company Website', '')).strip()
        tech_platform = str(r.get('Tech Hiring Platforms', '')).strip()
        name_norm = _normalize_name(name)
        domain = _extract_domain(site)
        if not name and not site:
            continue
        # Skip if exists by name or domain
        if name_norm and name_norm in existing_names:
            continue
        if domain and domain in existing_domains:
            continue
        # Skip duplicates within this run
        if name_norm and name_norm in seen_names:
            continue
        if domain and domain in seen_domains:
            continue
        seen_names.add(name_norm)
        seen_domains.add(domain)
        new_rows.append({'name': name, 'Company Website': site, 'Is a new company?': 'Yes', 'Tech Hiring Platforms': tech_platform})

    # Ensure existing_df has the 'Is a new company?' column and reset all previous 'Yes' flags.
    # Only companies added in the current run will be marked 'Yes'.
    if 'Is a new company?' not in existing_df.columns:
        existing_df['Is a new company?'] = 'No'
    else:
        # Reset all existing entries to 'No' so only current-run additions get 'Yes'
        existing_df['Is a new company?'] = 'No' 

    # Append new rows and place them at the top so newly added companies appear first in the table.
    if new_rows:
        appended_df = pd.concat([pd.DataFrame(new_rows), existing_df], ignore_index=True)
    else:
        appended_df = existing_df.copy()

    # Add numbering column where the most recent company has the highest number.
    appended_df = appended_df.reset_index(drop=True)
    total = len(appended_df)
    # Insert 'No.' column at position 0 with values total, total-1, ..., 1
    appended_df.insert(0, 'No.', list(range(total, 0, -1)))
    # Ensure columns order includes our important columns first (with 'No.' first)
    cols = [c for c in ['No.', 'name', 'Company Website', 'Tech Hiring Platforms', 'Is a new company?'] if c in appended_df.columns]
    appended_df = appended_df[cols + [c for c in appended_df.columns if c not in cols]]

    # Do not drop duplicates across the full dataset to avoid accidentally removing manual entries
    # (we already prevented adding duplicate new rows earlier by checking existing names/domains).

    # Save CSV (semicolon-delimited) and report newly added count
    new_count = len([r for r in new_rows])
    try:
        # Ensure we don't keep any temporary columns that might persist from older runs
        appended_df.drop(columns=['__name_norm', '__domain'], inplace=True, errors='ignore')

        # --- Generate HTML report (overwritten on each run) ---
        try:
            html_out = 'Companies.html'
            # Ensure the five columns exist so the table always has the same structure (include numbering)
            required_cols = ['No.', 'name', 'Company Website', 'Tech Hiring Platforms', 'Is a new company?']
            for c in required_cols:
                if c not in appended_df.columns:
                    appended_df[c] = ''

            rows_html = []
            for _, row in appended_df.iterrows():
                name = html.escape(str(row.get('name', '') or ''))
                website = str(row.get('Company Website', '') or '')
                if website:
                    website_esc = html.escape(website, quote=True)
                    website_html = f'<a href="{website_esc}" target="_blank" rel="noopener noreferrer" class="text-blue-600 hover:underline">{html.escape(website)}</a>'
                else:
                    website_html = ''
                platform = html.escape(str(row.get('Tech Hiring Platforms', '') or ''))
                is_new = str(row.get('Is a new company?', '') or 'No')
                # subtle shading for newly added companies (minimal, professional)
                is_new_flag = is_new.strip().lower() == 'yes'
                tr_class = 'bg-gray-50 even:bg-gray-100' if is_new_flag else 'bg-white even:bg-gray-50'
                # bold the 'Yes' text for new companies, keep 'No' plain
                is_new_html = '<strong>Yes</strong>' if is_new_flag else html.escape('No')
                number = html.escape(str(row.get('No.', '') or ''))
                rows_html.append(
                    f'<tr class="{tr_class}">'
                    f'<td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">{number}</td>'
                    f'<td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">{name}</td>'
                    f'<td class="px-6 py-4 text-sm text-gray-500">{website_html}</td>'
                    f'<td class="px-6 py-4 text-sm text-gray-500">{platform}</td>'
                    f'<td class="px-6 py-4 text-sm text-gray-500">{is_new_html}</td>'
                    f'</tr>'
                )

            # Use Costa Rica time (GMT-6). Costa Rica does not observe DST, so a fixed offset is used.
            tz_cr = timezone(timedelta(hours=-6))
            now = datetime.now(tz_cr).strftime('%Y-%m-%d %H:%M GMT-6')
            html_content = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<script src="https://cdn.tailwindcss.com"></script>
<title>Companies</title>
</head>
<body class="bg-gray-100 p-6">
<div class="max-w-6xl mx-auto">
  <div class="mb-6 flex items-baseline justify-between">
    <h1 class="text-2xl font-bold">Companies</h1>
    <div class="text-sm text-gray-600">Updated: {now} — Added {new_count} new</div>
  </div>
  <div class="overflow-x-auto bg-white shadow rounded-lg">
    <table class="min-w-full divide-y divide-gray-200">
      <thead class="bg-gray-50">
        <tr>
          <th class="px-6 py-3 text-left text-xs font-bold text-gray-500 uppercase tracking-wider">No.</th>
          <th class="px-6 py-3 text-left text-xs font-bold text-gray-500 uppercase tracking-wider">Name</th>
          <th class="px-6 py-3 text-left text-xs font-bold text-gray-500 uppercase tracking-wider">Company Website</th>
          <th class="px-6 py-3 text-left text-xs font-bold text-gray-500 uppercase tracking-wider">Tech Hiring Platforms</th>
          <th class="px-6 py-3 text-left text-xs font-bold text-gray-500 uppercase tracking-wider">Is a new company?</th>
        </tr>
      </thead>
      <tbody class="bg-white divide-y divide-gray-200">
        {''.join(rows_html)}
      </tbody>
    </table>
  </div>
</div>
</body>
</html>'''

            with open(html_out, 'w', encoding='utf-8') as f:
                f.write(html_content)
            print(f"Saved HTML report to {html_out}. Added {new_count} new companies.")
        except Exception as e:
            print("Warning: could not write HTML report:", e)

    except Exception as e:
        print("Error while preparing report:", e)
        raise

    print("Done!")

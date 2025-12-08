import asyncio
from playwright.async_api import async_playwright
import pandas as pd
import json
import html
import os
import re
from datetime import datetime


def _normalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    # handle protocol-relative urls
    if u.startswith("//"):
        return "https:" + u
    return u

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

        print("Loading YC companies page...")
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
            MAX_COMPANIES = int(os.getenv('MAX_COMPANIES', '20'))
        except Exception:
            MAX_COMPANIES = 20
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
                "Company Website": company_website_value
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
                        'Company Website': website or ''
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
    # Ensure only the requested columns are written (name and Company Website)
    df = df[["name", "Company Website"]]
    try:
        # Use semicolon as separator for better Excel compatibility in some locales
        df.to_csv("yc_companies.csv", index=False, sep=';')
        print("Saved to yc_companies.csv (semicolon-separated)")
    except PermissionError:
        alt = f"yc_companies_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        df.to_csv(alt, index=False, sep=';')
        print(f"yc_companies.csv is locked; saved to {alt} (semicolon-separated) instead")

    print("Done!")

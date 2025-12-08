import asyncio
from playwright.async_api import async_playwright
import pandas as pd
import json
import html
import re


def _normalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    # handle protocol-relative urls
    if u.startswith("//"):
        return "https:" + u
    return u

START_URL = "https://www.ycombinator.com/companies"


async def scrape():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
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

        print("Extracting company cards...")

        cards = await page.query_selector_all('a[href^="/companies/"]')
        print(f"Companies found: {len(cards)}")

        # Limit: only collect the most recent N companies to reduce workload
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

            results.append({
                "name": name.strip(),
                "location": location.strip(),
                "description": description.strip(),
                "url": full_url,
                "company_linkedin": company_linkedin,
                "company_website": company_website,
                "founder_linkedin": ";".join(founders_linkedin)
            })

        await browser.close()
        return results


if __name__ == "__main__":
    companies = asyncio.run(scrape())
    print(f"Saving {len(companies)} companies to CSV...")

    df = pd.DataFrame(companies)
    df.to_csv("yc_companies.csv", index=False)

    print("Done!")

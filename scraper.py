import asyncio
from playwright.async_api import async_playwright
import pandas as pd

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
                # company LinkedIn (company profile URL)
                try:
                    c_link_el = await cp.query_selector('a[href*="linkedin.com/company"]')
                    company_linkedin = await c_link_el.get_attribute('href') if c_link_el else ""
                except:
                    company_linkedin = ""

                # company website (aria-label present on YC company pages)
                try:
                    web_el = await cp.query_selector('a[aria-label="Company website"]')
                    if web_el:
                        company_website = await web_el.get_attribute('href') or ""
                    else:
                        # fallback: find first external link that is not linkedin
                        anchors = await cp.query_selector_all('a[href^="http"]')
                        for a in anchors:
                            href = await a.get_attribute('href')
                            if href and 'linkedin.com' not in href:
                                company_website = href
                                break
                except:
                    company_website = ""

                # founder(s) linkedin: look for profile links (linkedin.com/in)
                try:
                    founder_els = await cp.query_selector_all('a[href*="linkedin.com/in"]')
                    seen = set()
                    for f in founder_els:
                        href = await f.get_attribute('href')
                        if href and href not in seen:
                            seen.add(href)
                            founders_linkedin.append(href)
                except:
                    founders_linkedin = []

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

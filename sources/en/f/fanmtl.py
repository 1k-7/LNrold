# -*- coding: utf-8 -*-
import logging
import time
from urllib.parse import urlparse, parse_qs 
from bs4 import BeautifulSoup
from lncrawl.models import Chapter
from lncrawl.core.crawler import Crawler

# KEY CHANGE: Use curl_cffi to spoof browser TLS fingerprint
# This bypasses Cloudflare blocking standard 'requests'
from curl_cffi import requests

# Import Selenium for fallback solving
from lncrawl.webdriver.local import create_local
from selenium.webdriver import ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logger = logging.getLogger(__name__)

class FanMTLCrawler(Crawler):
    has_mtl = True
    base_url = "https://www.fanmtl.com/"

    def initialize(self):
        # [TURBO] 60 threads for downloading
        self.init_executor(60) 
        
        # 1. Setup the RUNNER (curl_cffi)
        # 'impersonate' makes the TLS handshake look exactly like Chrome 120
        self.runner = requests.Session(impersonate="chrome120")
        
        self.runner.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.fanmtl.com/",
            "Upgrade-Insecure-Requests": "1",
            # User agent is handled automatically by impersonate, but we set a fallback
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

        # Expose runner to the bot for cover downloading
        self.scraper = self.runner

        self.cookies_synced = False
        self.cleaner.bad_css.update({'div[align="center"]'})
        logger.info("FanMTL Strategy: curl_cffi (TLS Spoofing) -> Selenium Solver (Fallback)")

    def refresh_cookies(self, url):
        """Launches a stealthy headless browser to solve Cloudflare."""
        logger.warning(f"🔒 Launching Browser Solver for: {url}")
        driver = None
        try:
            options = ChromeOptions()
            options.add_argument("--no-sandbox") 
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1920,1080")
            
            # STEALTH: Hide automation flags
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            
            driver = create_local(headless=True, options=options)
            
            # STEALTH: Patch navigator.webdriver to undefined
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    })
                """
            })

            driver.get(url)
            time.sleep(5)

            # --- SOLVER LOGIC ---
            try:
                # 1. Look for Cloudflare Challenge iframe
                iframe = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[src*='challenge'], iframe[src*='turnstile']"))
                )
                if iframe:
                    logger.info("Browser: Found Cloudflare challenge. Attempting click...")
                    driver.switch_to.frame(iframe)
                    
                    # 2. Click logic
                    checkbox = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='checkbox'], .mark, body"))
                    )
                    driver.execute_script("arguments[0].click();", checkbox)
                    
                    driver.switch_to.default_content()
                    time.sleep(5)
            except Exception:
                pass
            # --------------------

            # Wait if still stuck
            if "Just a moment" in driver.title:
                logger.info("Browser: Waiting for redirect...")
                time.sleep(10)

            cookies = driver.get_cookies()
            ua = driver.execute_script("return navigator.userAgent")
            
            found_cf = False
            for cookie in cookies:
                # Convert Selenium cookie to requests/curl_cffi cookie
                self.runner.cookies.set(
                    cookie['name'], 
                    cookie['value'], 
                    domain=cookie.get('domain', ''),
                    path=cookie.get('path', '/')
                )
                if cookie['name'] == 'cf_clearance':
                    found_cf = True
            
            if ua:
                self.runner.headers['User-Agent'] = ua
            
            if found_cf:
                logger.info("✅ Solver Success! Cookies synced.")
                self.cookies_synced = True
            else:
                logger.warning("⚠️ Browser finished but 'cf_clearance' missing. (Title: %s)", driver.title)
            
        except Exception as e:
            logger.critical(f"❌ Browser Solver Failed: {e}")
        finally:
            if driver:
                try: driver.quit()
                except: pass

    def get_soup_safe(self, url, headers=None):
        """Smart wrapper: curl_cffi -> Solver -> Retry"""
        retries = 0
        while True:
            try:
                # STEP 1: Fast Request (TLS Spoofed)
                response = self.runner.get(url, headers=headers, timeout=20)
                
                # Check for Challenge Page
                if response.status_code in [403, 503] and ("just a moment" in response.text.lower() or "challenge" in response.text.lower()):
                    if retries == 0:
                        logger.warning("⛔ Access blocked (403/503). Activating Solver...")
                        self.refresh_cookies(url)
                        retries += 1
                        continue
                    else:
                        # If we still fail after solving, the IP might be dirty
                        raise Exception("Cloudflare Loop (Solver failed)")

                response.raise_for_status()
                return self.make_soup(response)

            except Exception as e:
                msg = str(e).lower()
                if "404" in msg:
                    logger.error(f"Permanent Error (404): {url}")
                    return self.make_soup("<html></html>")

                if retries < 2:
                    logger.warning(f"Request Error: {e}. Retrying...")
                    time.sleep(2)
                    retries += 1
                    continue
                
                logger.error(f"Failed to fetch {url} after retries.")
                return self.make_soup("<html></html>")

    def read_novel_info(self):
        logger.debug("Visiting %s", self.novel_url)
        
        soup = self.get_soup_safe(self.novel_url)

        possible_title = soup.select_one("h1.novel-title")
        if possible_title:
            self.novel_title = possible_title.text.strip()
        else:
            meta_title = soup.select_one('meta[property="og:title"]')
            self.novel_title = meta_title.get("content").strip() if meta_title else "Unknown Title"

        img_tag = soup.select_one("figure.cover img") or soup.select_one(".fixed-img img")
        if img_tag:
            url = img_tag.get("src")
            if "placeholder" in str(url) and img_tag.get("data-src"):
                url = img_tag.get("data-src")
            self.novel_cover = self.absolute_url(url)

        author_tag = soup.select_one('.novel-info .author span[itemprop="author"]')
        self.novel_author = author_tag.text.strip() if author_tag else "Unknown"

        summary_div = soup.select_one(".summary .content")
        self.novel_synopsis = summary_div.get_text("\n\n").strip() if summary_div else ""

        self.volumes = [{"id": 1, "title": "Volume 1"}]
        self.chapters = []

        self.parse_chapter_list(soup)

        pagination_links = soup.select('.pagination a[data-ajax-update="#chpagedlist"]')
        if pagination_links:
            try:
                last_page = pagination_links[-1]
                href = last_page.get("href")
                common_url = self.absolute_url(href).split("?")[0]
                query = parse_qs(urlparse(href).query)
                page_params = query.get("page", ["0"])
                page_count = int(page_params[0]) + 1
                wjm = query.get("wjm", [""])[0]
                
                # Headers for AJAX pagination
                ajax_headers = {"X-Requested-With": "XMLHttpRequest"}

                for page in range(page_count):
                    url = f"{common_url}?page={page}&wjm={wjm}"
                    page_soup = self.get_soup_safe(url, headers=ajax_headers)
                    self.parse_chapter_list(page_soup)
                    
            except Exception as e:
                logger.error(f"Pagination failed: {e}")

        self.chapters.sort(key=lambda x: x["id"] if isinstance(x, dict) else getattr(x, "id", 0))

    def parse_chapter_list(self, soup):
        if not soup: return
        for a in soup.select("ul.chapter-list li a"):
            try:
                url = self.absolute_url(a["href"])
                self.chapters.append(Chapter(
                    id=len(self.chapters) + 1,
                    volume=1,
                    url=url,
                    title=a.select_one(".chapter-title").text.strip(),
                ))
            except: pass

    def download_chapter_body(self, chapter):
        try:
            soup = self.get_soup_safe(chapter["url"])
            body = soup.select_one("#chapter-article .chapter-content")
            return self.cleaner.extract_contents(body).strip() if body else ""
        except Exception:
            return ""

# -*- coding: utf-8 -*-
import logging
import time
import random
import requests
import cloudscraper
from urllib.parse import urlparse, parse_qs 
from bs4 import BeautifulSoup
from lncrawl.models import Chapter
from lncrawl.core.crawler import Crawler

# Import Selenium
from lncrawl.webdriver.local import create_local
from selenium.webdriver import ChromeOptions, ActionChains
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
        
        # 1. Setup Standard Requests (Primary)
        self.runner = requests.Session()
        
        # 2. Setup Cloudscraper (Secondary)
        # This handles many Cloudflare challenges natively without a browser
        self.scraper_sess = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'linux', 'desktop': True}
        )
        
        # Common Headers
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Referer": "https://www.fanmtl.com/"
        }
        self.runner.headers.update(headers)
        self.scraper_sess.headers.update(headers)
        
        # Optimize connection pool
        adapter = requests.adapters.HTTPAdapter(pool_connections=60, pool_maxsize=60)
        self.runner.mount("https://", adapter)
        self.runner.mount("http://", adapter)

        self.scraper = self.runner
        self.cleaner.bad_css.update({'div[align="center"]'})
        logger.info("FanMTL Strategy: Requests -> Cloudscraper -> Selenium (Triple Fallback)")

    def human_click(self, driver, element):
        """Attempts multiple clicking strategies to fool detection."""
        try:
            # Strategy A: ActionChains (Simulate Mouse)
            action = ActionChains(driver)
            action.move_to_element_with_offset(element, random.randint(-3, 3), random.randint(-3, 3))
            action.pause(random.uniform(0.1, 0.3))
            action.click()
            action.perform()
        except Exception:
            try:
                # Strategy B: Direct JS Click
                driver.execute_script("arguments[0].click();", element)
            except Exception:
                pass

    def fetch_with_browser(self, url):
        """
        Launches Selenium with 'New Headless' mode to act as a real browser.
        """
        logger.warning(f"🔒 Launching Browser Solver for: {url}")
        driver = None
        try:
            options = ChromeOptions()
            options.add_argument("--no-sandbox") 
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1920,1080")
            
            # CRITICAL: Use new headless mode (passes detection better)
            options.add_argument("--headless=new") 
            
            # Anti-Detection
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            options.add_argument(f"--user-agent={self.runner.headers['User-Agent']}")
            
            # We pass headless=False to create_local to prevent it from adding 
            # the old '--headless' flag, since we added '--headless=new' manually.
            driver = create_local(headless=False, options=options)
            
            # Patch navigator.webdriver
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            })

            driver.get(url)
            
            # --- RETRY LOOP ---
            for attempt in range(3):
                time.sleep(4 + attempt)
                
                if "Just a moment" not in driver.title:
                    logger.info("Browser: Page loaded successfully!")
                    break

                logger.info(f"Browser: Attempting Solve #{attempt+1}...")
                
                try:
                    # Switch to iframe
                    iframe = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[src*='challenge'], iframe[src*='turnstile']"))
                    )
                    driver.switch_to.frame(iframe)
                    
                    # Find and Click
                    checkbox = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='checkbox'], .mark, body"))
                    )
                    self.human_click(driver, checkbox)
                    
                    # Switch back
                    driver.switch_to.default_content()
                except Exception:
                    driver.switch_to.default_content()

            # Result
            if "Just a moment" in driver.title:
                logger.error("Browser: Failed to pass challenge.")
            
            # Extract Data
            html = driver.page_source
            cookies = driver.get_cookies()
            return html, cookies

        except Exception as e:
            logger.critical(f"❌ Browser Failed: {e}")
            return None, None
        finally:
            if driver:
                try: driver.quit()
                except: pass

    def get_soup_safe(self, url, headers=None):
        retries = 0
        while True:
            try:
                # LAYER 1: Standard Requests (Fast)
                req_headers = self.runner.headers.copy()
                if headers: req_headers.update(headers)
                
                response = self.runner.get(url, headers=req_headers, timeout=15)
                
                # Check for Block
                if response.status_code in [403, 503] or "just a moment" in response.text.lower():
                    if retries == 0:
                        logger.warning("⛔ Requests blocked. Trying Cloudscraper...")
                        
                        # LAYER 2: Cloudscraper (Medium)
                        try:
                            cf_response = self.scraper_sess.get(url, headers=req_headers, timeout=20)
                            if cf_response.status_code == 200 and "just a moment" not in cf_response.text.lower():
                                logger.info("✅ Cloudscraper bypassed protection!")
                                # Sync cookies to main runner
                                self.runner.cookies.update(self.scraper_sess.cookies)
                                return self.make_soup(cf_response)
                        except Exception as cfe:
                            logger.warning(f"Cloudscraper failed: {cfe}")

                        # LAYER 3: Selenium (Slow Fallback)
                        logger.warning("⛔ Cloudscraper failed. Switching to Selenium...")
                        html_source, cookies = self.fetch_with_browser(url)
                        
                        if html_source and cookies:
                            # Sync cookies
                            for cookie in cookies:
                                self.runner.cookies.set(
                                    cookie['name'], cookie['value'], 
                                    domain=cookie.get('domain', ''), path=cookie.get('path', '/')
                                )
                                # Also update Cloudscraper for future luck
                                self.scraper_sess.cookies.set(
                                    cookie['name'], cookie['value'], 
                                    domain=cookie.get('domain', ''), path=cookie.get('path', '/')
                                )
                            return self.make_soup(html_source)
                        
                        retries += 1
                        continue
                    else:
                        raise Exception("All bypass methods failed.")

                response.raise_for_status()
                return self.make_soup(response)

            except Exception as e:
                if "404" in str(e):
                    return self.make_soup("<html></html>")
                
                if retries < 2:
                    logger.warning(f"Fetch error: {e}. Retrying...")
                    time.sleep(2)
                    retries += 1
                    continue
                
                logger.error(f"Failed to fetch {url}")
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

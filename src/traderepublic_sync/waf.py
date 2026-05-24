"""AWS WAF token acquisition.

Trade Republic's API rejects requests without a valid ``x-aws-waf-token``
header. The token is issued by AWS WAF after the browser solves a
JavaScript challenge, so we drive a headless browser to obtain one.

Two backends are supported; install the matching extra:
    pip install traderepublic-sync[playwright]   # recommended (lighter)
    pip install traderepublic-sync[selenium]
"""

import logging
import time

logger = logging.getLogger(__name__)


def get_waf_token(method: str = "playwright") -> str:
    """Fetch AWS WAF token using the specified method.

    Args:
        method: "playwright" or "selenium".

    Returns:
        The WAF token string, or "" if acquisition failed.
    """
    dispatch = {
        "selenium": _waf_token_selenium,
        "playwright": _waf_token_playwright,
    }
    func = dispatch.get(method)
    if not func:
        logger.error("Unknown waf_method: %r. Use: selenium, playwright", method)
        return ""
    return func()


def _waf_token_selenium() -> str:
    """Fetch WAF token using Selenium + headless Chrome."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        raise ImportError(
            "selenium not installed. Install with: pip install traderepublic-sync[selenium]"
        )

    logger.info("Fetching WAF token with Selenium...")
    options = Options()
    options.add_argument("--headless=new")
    try:
        driver = webdriver.Chrome(options=options)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined })"},
        )
        driver.get("https://app.traderepublic.com/")
        time.sleep(5)

        waf_token = None
        for cookie in driver.get_cookies():
            if "aws-waf-token" in cookie.get("name", ""):
                waf_token = cookie["value"]
                break
        if not waf_token:
            try:
                waf_token = driver.execute_script(
                    "return window.AWSWafIntegration && window.AWSWafIntegration.getToken();"
                )
            except Exception:
                pass

        driver.quit()

        if waf_token:
            logger.info("WAF token obtained (Selenium)")
            return waf_token
        else:
            logger.warning("WAF token not found in cookies or JS context.")
            return ""
    except Exception as e:
        logger.error("Selenium error: %s", e)
        return ""


def _waf_token_playwright() -> str:
    """Fetch WAF token using Playwright (lighter than Selenium)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "playwright not installed. Install with: "
            "pip install traderepublic-sync[playwright] && playwright install chromium"
        )

    logger.info("Fetching WAF token with Playwright...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined })"
            )

            waf_token = None
            page.goto("https://app.traderepublic.com/", wait_until="domcontentloaded")

            for _ in range(10):
                page.wait_for_timeout(1000)
                cookies = context.cookies()
                for cookie in cookies:
                    if "aws-waf-token" in cookie.get("name", ""):
                        waf_token = cookie["value"]
                        break
                if waf_token:
                    break

            if not waf_token:
                try:
                    waf_token = page.evaluate(
                        "() => window.AWSWafIntegration && window.AWSWafIntegration.getToken()"
                    )
                except Exception:
                    pass

            browser.close()

            if waf_token:
                logger.info("WAF token obtained (Playwright)")
                return waf_token
            else:
                logger.warning("WAF token not found in cookies or JS context.")
                return ""
    except Exception as e:
        logger.error("Playwright error: %s", e)
        return ""

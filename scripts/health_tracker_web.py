"""scripts/health_tracker_web.py — 用 Playwright 繞過 Garmin API 429 限制。

備援（API 版為主，見 T19）：健康數據與活動同步的主路線已改為
``scripts/health_tracker.py``（官方 garminconnect API，含 429 retry + token
快取，2026-07-11 驗證可直接取得活動）。本檔案保留為官方 API 路線失效時的
手動備援，不再是唯一寫入者，目前無排程、無測試。

流程：
1. 嘗試從快取 cookies 恢復 session（避免每次重新登入）
2. Session 失效時，用 Playwright 無頭瀏覽器重新登入
3. 若需要 MFA，透過 input() 提示使用者輸入驗證碼
4. 用 requests + cookies 呼叫 Garmin Connect 內部 REST API
5. 寫入 Notion Health DB

執行方式::

    python scripts/health_tracker_web.py
"""

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests

# 讓腳本在任何目錄都能正確 import 專案模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from config import (
    ACTIVITY_DB_ID,
    GARMIN_EMAIL,
    GARMIN_PASSWORD,
    HEALTH_DB_ID,
    NOTION_API_KEY,
    get_activity_api_key,
)
from models import ActivityData, HealthData
from notion_client_helper import (
    create_page,
    get_notion_client,
    page_exists_for_date,
    query_pages,
    prop_date,
    prop_number,
    prop_rich_text,
    prop_select,
    prop_title,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# ── 常數 ──────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_COOKIE_DIR = _PROJECT_ROOT / ".garmin_web_cookies"
_COOKIE_FILE = _COOKIE_DIR / "cookies.json"

_GARMIN_BASE = "https://connect.garmin.com"
_GARMIN_SIGNIN = "https://connect.garmin.com/signin/"
_GARMIN_DASHBOARD = "https://connect.garmin.com/modern/home"

# 瀏覽器 User-Agent（模擬 Chrome）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Garmin Connect 內部 API 所需的 headers
_API_HEADERS = {
    "NK": "NT",
    "X-app-ver": "4.79.0",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": _USER_AGENT,
    "Referer": "https://connect.garmin.com/modern/",
    "Origin": "https://connect.garmin.com",
}


# ── Data Transfer Objects ──────────────────────────────────────────────────────
# HealthData 和 ActivityData 已移至 models.py，透過頂部 import 使用。


# ── Cookie 管理 ────────────────────────────────────────────────────────────────


def _save_cookies(cookies: List[Dict]) -> None:
    """將 cookies 儲存到本地 JSON 檔案。

    Args:
        cookies: Playwright context.cookies() 回傳的 cookie 清單。
    """
    _COOKIE_DIR.mkdir(exist_ok=True)
    _COOKIE_FILE.write_text(json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Cookies 已儲存至 %s", _COOKIE_FILE)


def _load_cookies() -> Optional[List[Dict]]:
    """從本地 JSON 檔案載入 cookies。

    Returns:
        Cookie 清單；檔案不存在或格式錯誤時回傳 None。
    """
    if not _COOKIE_FILE.exists():
        return None
    try:
        cookies = json.loads(_COOKIE_FILE.read_text(encoding="utf-8"))
        logger.info("載入快取 cookies（%d 筆）", len(cookies))
        return cookies
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("讀取 cookies 失敗：%s", exc)
        return None


def _build_session(cookies: List[Dict]) -> requests.Session:
    """建立帶有 Garmin cookies 的 requests Session。

    支援 Cookie-Editor 匯出格式（含多餘欄位如 hostOnly、storeId 等）。

    Args:
        cookies: Playwright 或 Cookie-Editor 格式的 cookie 清單。

    Returns:
        已設定 cookies 和 headers 的 Session。
    """
    session = requests.Session()
    session.headers.update(_API_HEADERS)

    # 直接用 name:value 對設定 cookies（最可靠，避免 domain matching 問題）
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if name and value is not None:
            session.cookies.set(name, str(value))

    return session


def _is_session_valid(session: requests.Session) -> bool:
    """測試 session 是否仍然有效。

    依序嘗試多個端點，只要有一個回傳有效 JSON 資料即視為有效。

    Args:
        session: 已設定 cookies 的 requests Session。

    Returns:
        Session 有效回傳 True，否則回傳 False。
    """
    test_urls = [
        # 用戶資料（需要登入才有 displayName）
        f"{_GARMIN_BASE}/proxy/userprofile-service/userprofile/personal-information",
        # 用戶設定（另一個簡單的認證端點）
        f"{_GARMIN_BASE}/proxy/userprofile-service/userprofile/user-settings",
    ]

    for url in test_urls:
        try:
            resp = session.get(url, timeout=15)
            logger.debug("Session 驗證 %s → HTTP %d", url.split("/")[-1], resp.status_code)
            if resp.status_code == 200:
                # HTTP 200 = 已認證（未登入時 Garmin 會 redirect 到 SSO，不回 200）
                # 不檢查 body 是否為空，因為 personal-information 對某些帳號可能回傳 {}
                logger.info("Session 有效（HTTP 200 from %s）", url.split("/")[-1])
                return True
            if resp.status_code in (401, 403):
                logger.info("Session 無效（HTTP %d from %s）", resp.status_code, url.split("/")[-1])
                # 繼續嘗試其他端點
        except Exception as exc:
            logger.debug("Session 驗證端點失敗：%s", exc)

    logger.warning("所有 session 驗證端點均無 HTTP 200 回應，視為無效")
    return False


# ── curl_cffi 直接登入（繞過 Cloudflare TLS 指紋偵測）────────────────────────

_SSO_PAGE_URL = (
    "https://sso.garmin.com/portal/sso/en-US/sign-in"
    "?clientId=GarminConnect"
    "&service=https%3A%2F%2Fconnect.garmin.com%2Fapp"
)
_SSO_LOGIN_API = (
    "https://sso.garmin.com/portal/api/login"
    "?clientId=GarminConnect"
    "&locale=en-US"
    "&service=https%3A%2F%2Fconnect.garmin.com%2Fapp"
)
_SSO_MFA_API = (
    "https://sso.garmin.com/portal/api/mfa"
    "?clientId=GarminConnect"
    "&locale=en-US"
    "&service=https%3A%2F%2Fconnect.garmin.com%2Fapp"
)


def _curl_session_to_cookie_list(cf_session: object) -> List[Dict]:
    """將 curl_cffi session cookies 轉換為 Playwright 格式的清單。

    Args:
        cf_session: curl_cffi.requests.Session 物件。

    Returns:
        Playwright 格式的 cookie 清單。
    """
    result = []
    cookies = cf_session.cookies  # type: ignore[attr-defined]

    # curl_cffi cookies 可能是 dict-like 或 jar-like
    try:
        # 先嘗試 dict 形式（{name: value}）
        for name, value in cookies.items():
            result.append({"name": name, "value": value, "domain": "connect.garmin.com", "path": "/"})
        if result:
            return result
    except Exception:
        pass

    try:
        # 嘗試 Jar 形式（有 .name, .value 屬性的物件）
        for cookie in cookies.jar:
            result.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": getattr(cookie, "domain", None) or "connect.garmin.com",
                "path": getattr(cookie, "path", None) or "/",
            })
        if result:
            return result
    except Exception:
        pass

    try:
        # 嘗試把整個 jar 轉 dict
        d = dict(cookies)
        for name, value in d.items():
            result.append({"name": name, "value": str(value), "domain": "connect.garmin.com", "path": "/"})
    except Exception as exc:
        logger.warning("Cookie 提取全部方式失敗：%s", exc)

    return result


def _curl_cffi_login() -> Optional[List[Dict]]:
    """用 curl_cffi 模擬 Chrome TLS 指紋登入 Garmin Connect。

    curl_cffi 可繞過 Cloudflare 的 TLS 指紋偵測。
    支援 MFA：若收到 MFA 要求，透過 input() 提示使用者輸入驗證碼。

    Returns:
        登入成功後的 cookies 清單；失敗時回傳 None。
    """
    try:
        from curl_cffi import requests as cf_requests
    except ImportError:
        logger.warning("curl_cffi 未安裝，跳過此登入方式")
        return None

    logger.info("嘗試 curl_cffi 登入（Chrome TLS 指紋模擬）...")

    try:
        session = cf_requests.Session(impersonate="chrome131")
        session.headers.update({
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

        # ── Step 1：GET SSO 頁面，初始化 cookies（含 Cloudflare cf_clearance）
        logger.info("取得 SSO 頁面（初始化 cookies）...")
        resp = session.get(_SSO_PAGE_URL, timeout=30)
        logger.info("SSO 頁面 HTTP %d", resp.status_code)
        if resp.status_code not in (200, 302):
            logger.warning("SSO 頁面回傳 %d，繼續嘗試...", resp.status_code)

        time.sleep(2)  # 等待 Cloudflare challenge 完成

        # ── Step 2：POST 登入憑證 ────────────────────────────────────────────
        logger.info("提交登入憑證...")
        login_resp = session.post(
            _SSO_LOGIN_API,
            json={
                "username": GARMIN_EMAIL,
                "password": GARMIN_PASSWORD,
                "rememberMe": False,
                "captchaToken": "",
            },
            headers={
                "Origin": "https://sso.garmin.com",
                "Referer": _SSO_PAGE_URL,
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=30,
        )
        logger.info("登入 API HTTP %d", login_resp.status_code)

        if login_resp.status_code not in (200, 201, 302):
            logger.warning(
                "登入 API 失敗 HTTP %d：%s",
                login_resp.status_code,
                login_resp.text[:300],
            )
            return None

        # ── Step 3：解析登入回應 ─────────────────────────────────────────────
        try:
            resp_data = login_resp.json()
        except Exception:
            resp_data = {}

        logger.info("登入回應 JSON keys：%s", list(resp_data.keys()) if isinstance(resp_data, dict) else type(resp_data).__name__)
        logger.info("登入回應內容：%s", str(resp_data)[:400])

        # 偵測 MFA 要求
        mfa_needed = (
            resp_data.get("mfaRequired")
            or resp_data.get("isMfaRequired")
            or resp_data.get("type") == "MFA"
            or login_resp.status_code == 403
        )

        if mfa_needed:
            logger.info("偵測到 MFA 要求")
            print("\n[Garmin MFA] 帳號啟用了兩步驟驗證。")
            print("[Garmin MFA] 請檢查 email 或驗證器，輸入 6 位數驗證碼：")
            mfa_code = input("驗證碼：").strip()

            mfa_resp = session.post(
                _SSO_MFA_API,
                json={"mfaCode": mfa_code},
                headers={
                    "Origin": "https://sso.garmin.com",
                    "Referer": _SSO_PAGE_URL,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                },
                timeout=30,
            )
            logger.info("MFA API HTTP %d", mfa_resp.status_code)
            if mfa_resp.status_code not in (200, 201, 302):
                logger.error("MFA 驗證失敗：%s", mfa_resp.text[:200])
                return None
            try:
                resp_data = mfa_resp.json()
            except Exception:
                resp_data = {}

        # ── Step 4：跟隨 redirect 取得 connect.garmin.com session ────────────
        service_ticket_url = (
            resp_data.get("serviceTicketUrl")
            or resp_data.get("redirectUrl")
            or resp_data.get("url")
        )

        if service_ticket_url:
            logger.info("跟隨 service ticket redirect：%s", service_ticket_url[:80])
            ticket_resp = session.get(service_ticket_url, timeout=30)
            logger.info("Service ticket redirect HTTP %d", ticket_resp.status_code)
            # 繼續跟隨 redirect 直到 connect.garmin.com
            if "connect.garmin.com" not in ticket_resp.url:
                # 可能還有一跳
                final_url = ticket_resp.url
                logger.info("最終 URL：%s", final_url)
        else:
            # 嘗試直接造訪 connect.garmin.com（session cookies 可能已足夠）
            logger.info("無 redirect URL，嘗試直接造訪 connect.garmin.com...")
            session.get(f"{_GARMIN_BASE}/modern/home", timeout=30)

        # ── Step 5：轉換 cookies 格式 ─────────────────────────────────────────
        cookies = _curl_session_to_cookie_list(session)
        logger.info("curl_cffi 收集到 %d 個 cookies", len(cookies))

        if not cookies:
            logger.warning("沒有收集到任何 cookies")
            return None

        return cookies

    except Exception as exc:
        logger.error("curl_cffi 登入失敗：%s", exc)
        return None


# ── Playwright 登入 ────────────────────────────────────────────────────────────


def _playwright_login() -> Optional[List[Dict]]:
    """用 Playwright 無頭瀏覽器登入 Garmin Connect，回傳 cookies。

    支援 MFA：若出現驗證碼輸入欄，會用 input() 提示使用者互動輸入。

    Returns:
        登入成功後的 Playwright cookies 清單；失敗時回傳 None。
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    except ImportError:
        logger.error("Playwright 未安裝，請執行：pip install playwright && playwright install chromium")
        return None

    logger.info("啟動 Playwright 登入流程...")

    # 直接前往 SSO 頁面（避免 iframe 複雜性）
    _SSO_URL = (
        "https://sso.garmin.com/portal/sso/en-US/sign-in"
        "?clientId=GarminConnect"
        "&service=https%3A%2F%2Fconnect.garmin.com%2Fapp"
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            slow_mo=100,  # 操作間加入 100ms 延遲，更接近人工行為
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        # 移除 webdriver 屬性避免被偵測為 bot
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()

        # 攔截網路請求，記錄 auth 相關 API 呼叫（debug 用）
        _auth_responses: List[Dict] = []

        def _on_request(request: object) -> None:  # type: ignore[override]
            try:
                url = request.url  # type: ignore[attr-defined]
                if any(kw in url for kw in ["api/login", "api/v1", "oauth", "authenticate"]):
                    method = request.method  # type: ignore[attr-defined]
                    headers = dict(request.headers)  # type: ignore[attr-defined]
                    try:
                        body = request.post_data  # type: ignore[attr-defined]
                    except Exception:
                        body = None
                    _auth_responses.append({"type": "req", "method": method, "url": url, "body": body, "headers": headers})
            except Exception:
                pass

        def _on_response(response: object) -> None:  # type: ignore[override]
            try:
                url = response.url  # type: ignore[attr-defined]
                if any(kw in url for kw in ["api/login", "api/v1", "oauth", "authenticate"]):
                    status = response.status  # type: ignore[attr-defined]
                    try:
                        body = response.text()  # type: ignore[attr-defined]
                    except Exception:
                        body = ""
                    _auth_responses.append({"type": "resp", "url": url, "status": status, "body": body[:300]})
            except Exception:
                pass

        page.on("request", _on_request)
        page.on("response", _on_response)

        try:
            # ── Step 1：開啟 SSO 登入頁（直接，非 iframe）──────────────────────
            logger.info("開啟 Garmin SSO 登入頁...")
            page.goto(_SSO_URL, wait_until="networkidle", timeout=30000)
            logger.info("目前 URL：%s", page.url)

            # ── Step 2：輸入 Email ──────────────────────────────────────────────
            email_selectors = [
                "#email",
                "input[name='username']",
                "input[type='email']",
                "#username",
                "input[placeholder*='mail']",
                "input[placeholder*='Email']",
            ]
            email_el = None
            for sel in email_selectors:
                try:
                    email_el = page.wait_for_selector(sel, timeout=8000, state="visible")
                    if email_el:
                        logger.info("找到 Email 欄位（selector: %s）", sel)
                        break
                except PWTimeoutError:
                    continue

            if not email_el:
                logger.error("找不到 Email 輸入欄位，截圖 debug.png 供排查")
                page.screenshot(path=str(_PROJECT_ROOT / "debug.png"))
                browser.close()
                return None

            # 模擬人工輸入 email
            email_el.click()
            email_el.fill("")
            page.keyboard.type(GARMIN_EMAIL, delay=50)
            logger.info("輸入 Email 完畢")
            time.sleep(0.5)

            # ── Step 3：嘗試點「繼續」或直接看密碼欄是否已顯示 ────────────────
            # 先按 Tab 移出 email 欄（觸發驗證）
            page.keyboard.press("Tab")
            time.sleep(0.5)

            # 等待密碼欄出現（若是單頁表單），或等待「繼續」按鈕
            password_el = None
            try:
                password_el = page.wait_for_selector(
                    "input[type='password'], #password, input[name='password']",
                    timeout=5000,
                    state="visible",
                )
                logger.info("密碼欄已可見（單頁表單）")
            except PWTimeoutError:
                # 密碼欄還沒出現，嘗試點「繼續」按鈕
                logger.info("密碼欄未顯示，尋找繼續按鈕...")
                continue_selectors = [
                    "button[type='submit']",
                    "input[type='submit']",
                    "button#login-btn-usernameSubmit",
                    "button.btn-primary",
                    "[data-testid='button-continue']",
                ]
                for sel in continue_selectors:
                    try:
                        btn = page.query_selector(sel)
                        if btn and btn.is_visible():
                            logger.info("點擊繼續按鈕（selector: %s）", sel)
                            btn.click()
                            time.sleep(1)
                            break
                    except Exception:
                        pass
                else:
                    # 也嘗試在 email 欄按 Enter
                    page.keyboard.press("Enter")
                    logger.info("在 Email 欄按 Enter（嘗試觸發繼續）")
                    time.sleep(1)

                # 再等密碼欄
                try:
                    password_el = page.wait_for_selector(
                        "input[type='password'], #password, input[name='password']",
                        timeout=15000,
                        state="visible",
                    )
                    logger.info("繼續後密碼欄出現")
                except PWTimeoutError:
                    logger.error("等待密碼欄逾時，截圖 debug.png 供排查")
                    page.screenshot(path=str(_PROJECT_ROOT / "debug.png"))
                    browser.close()
                    return None

            # ── Step 4：輸入密碼 ────────────────────────────────────────────────
            password_el.click()
            password_el.fill("")
            page.keyboard.type(GARMIN_PASSWORD, delay=50)
            logger.info("輸入密碼完畢")
            time.sleep(0.5)

            # ── Step 5：提交（先試按鈕 click，備用 Enter）──────────────────────
            submit_selectors = [
                "button[type='submit']",
                "#login-btn-signin",
                "input[type='submit']",
                "button.btn-primary",
                "[data-testid='button-signin']",
            ]
            submit_clicked = False
            for sel in submit_selectors:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        logger.info("點擊提交按鈕（selector: %s）", sel)
                        submit_clicked = True
                        break
                except Exception:
                    pass

            if not submit_clicked:
                password_el.press("Enter")
                logger.info("用 Enter 鍵提交")

            # 等待頁面反應（React SPA 用 XHR/fetch，不一定有傳統導航）
            time.sleep(4)
            logger.info("提交後目前 URL：%s", page.url)

            # ── 截圖並讀取頁面文字（debug：確認登入狀態）────────────────────
            page.screenshot(path=str(_PROJECT_ROOT / "debug_after_submit.png"))
            page_text = page.inner_text("body")[:800]
            logger.info("提交後頁面內容（前 800 字）：%s", page_text.replace("\n", " "))
            # 印出攔截到的 auth API 呼叫（完整細節）
            for r in _auth_responses:
                if r.get("type") == "req":
                    logger.info(
                        "auth 請求：%s %s | body=%s | headers=%s",
                        r["method"], r["url"], r.get("body", ""),
                        {k: v for k, v in r.get("headers", {}).items() if k.lower() in ("content-type", "x-csrf-token", "origin", "referer", "x-requested-with")},
                    )
                else:
                    logger.info("auth 回應：[%d] %s | body=%s", r.get("status"), r["url"], r.get("body", ""))

            # ── Step 6：偵測 MFA 頁面 ───────────────────────────────────────────
            time.sleep(2)
            mfa_selectors = [
                "input[name='code']",
                "input[name='verificationCode']",
                "input[id*='mfa']",
                "input[id*='otp']",
                "input[placeholder*='code']",
                "input[placeholder*='Code']",
                "input[autocomplete='one-time-code']",
            ]
            for sel in mfa_selectors:
                try:
                    mfa_el = page.query_selector(sel)
                    if mfa_el and mfa_el.is_visible():
                        logger.info("偵測到 MFA 驗證碼欄位（selector: %s）", sel)
                        print("\n[Garmin MFA] 帳號啟用了兩步驟驗證。")
                        print("[Garmin MFA] 請檢查 email 或驗證器，輸入 6 位數驗證碼：")
                        mfa_code = input("驗證碼：").strip()
                        mfa_el.fill(mfa_code)
                        try:
                            with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
                                mfa_el.press("Enter")
                        except Exception:
                            page.click("button[type='submit']")
                            time.sleep(3)
                        logger.info("MFA 提交後 URL：%s", page.url)
                        break
                except Exception:
                    pass

            # ── Step 7：等待最終跳轉至 connect.garmin.com ────────────────────────
            try:
                page.wait_for_url(
                    lambda url: url.startswith("https://connect.garmin.com/") and "sign" not in url,
                    timeout=20000,
                )
                logger.info("已到達 connect.garmin.com，URL：%s", page.url)
            except PWTimeoutError:
                current_url = page.url
                logger.warning("等待 connect.garmin.com 逾時，目前 URL：%s", current_url)
                if "sso.garmin.com" in current_url:
                    page.screenshot(path=str(_PROJECT_ROOT / "debug.png"))
                    logger.error("仍在 SSO 頁，登入失敗，截圖已存至 debug.png")
                    browser.close()
                    return None
                # 如果已在 connect.garmin.com 的其他路徑也接受
                logger.info("接受目前 URL：%s", current_url)

            # ── Step 8：等待 connect.garmin.com 的 JS 完全初始化並設定 cookies ─
            page.wait_for_load_state("networkidle", timeout=15000)
            time.sleep(2)

            # ── 收集所有 cookies ────────────────────────────────────────────────
            cookies = context.cookies()
            logger.info("收集到 %d 個 cookies", len(cookies))

            browser.close()
            return cookies

        except Exception as exc:
            logger.error("Playwright 登入過程發生錯誤：%s", exc)
            try:
                page.screenshot(path=str(_PROJECT_ROOT / "debug.png"))
            except Exception:
                pass
            browser.close()
            return None


# ── OAuth Token 取得（從 localStorage）────────────────────────────────────────

_CONNECT_API_BASE = "https://connectapi.garmin.com"
_TOKEN_CACHE_FILE = _COOKIE_DIR / "oauth_token.json"


def _save_oauth_token(token: str) -> None:
    """將 OAuth token 快取到檔案。"""
    _COOKIE_DIR.mkdir(exist_ok=True)
    import json as _json
    _TOKEN_CACHE_FILE.write_text(_json.dumps({"token": token}), encoding="utf-8")
    logger.info("OAuth token 已快取")


def _load_oauth_token() -> Optional[str]:
    """從快取檔案載入 OAuth token。"""
    if not _TOKEN_CACHE_FILE.exists():
        return None
    try:
        import json as _json
        data = _json.loads(_TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
        return data.get("token")
    except Exception:
        return None


def _find_chrome_exe() -> Optional[str]:
    """尋找 Windows 上的 Chrome 可執行檔路徑。"""
    import os
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), r"Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    return None


def _extract_oauth_token_via_cdp_auto(cookies: List[Dict], port: int = 9223) -> Optional[str]:
    """自動啟動 Chrome、注入 cookies、取得 OAuth token（全自動，無需手動操作）。

    流程：
    1. 啟動 Chrome（headless=new 模式，使用臨時 user-data-dir）
    2. 透過 CDP 注入 Garmin Connect cookies
    3. 導航到 connect.garmin.com/app/
    4. 等待 React 初始化並從 localStorage 取出 Bearer token

    Args:
        cookies: 已知有效的 Garmin Connect cookies。
        port: Chrome 遠端偵錯埠號（預設 9223，避免與現有 Chrome 衝突）。

    Returns:
        OAuth access token 字串；失敗時回傳 None。
    """
    import subprocess
    import tempfile
    import threading
    import time as _time

    chrome_exe = _find_chrome_exe()
    if not chrome_exe:
        logger.debug("找不到 Chrome 執行檔，跳過自動 CDP 流程")
        return None

    # 使用臨時目錄作為 Chrome user-data-dir（避免污染用戶設定）
    tmp_dir = tempfile.mkdtemp(prefix="garmin_chrome_")
    logger.info("啟動 Chrome CDP（%s，port %d）...", Path(chrome_exe).name, port)

    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            [
                chrome_exe,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={tmp_dir}",
                "--remote-allow-origins=*",  # Chrome 新版本需要此旗標才能 WebSocket CDP 連線
                # 不使用 headless（避免 Cloudflare 偵測），視窗會短暫出現後自動關閉
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1,1",  # 最小化視窗大小
                "--window-position=9999,9999",  # 移到螢幕外
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 等待 Chrome 啟動並監聽 CDP
        for _ in range(20):
            _time.sleep(0.5)
            try:
                r = requests.get(f"http://localhost:{port}/json/version", timeout=2)
                if r.status_code == 200:
                    logger.info("Chrome CDP 已就緒")
                    break
            except Exception:
                pass
        else:
            logger.warning("Chrome CDP 啟動逾時")
            return None

        token = _cdp_inject_cookies_and_get_token(cookies, port)
        return token

    except Exception as exc:
        logger.warning("自動 Chrome CDP 失敗：%s", exc)
        return None
    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def _cdp_inject_cookies_and_get_token(cookies: List[Dict], port: int) -> Optional[str]:
    """透過 CDP WebSocket 注入 cookies、導航到 /app/、取出 localStorage token。

    Args:
        cookies: Garmin Connect cookies 清單。
        port: Chrome CDP 端口號。

    Returns:
        OAuth access token 或 None。
    """
    try:
        import websocket  # type: ignore[import]
        import threading
        import json as _json
    except ImportError:
        logger.debug("websocket-client 未安裝（pip install websocket-client）")
        return None

    # 先建立新分頁
    try:
        r = requests.put(f"http://localhost:{port}/json/new", timeout=5)
        tab_info = r.json()
        ws_url = tab_info.get("webSocketDebuggerUrl", "")
        logger.debug("CDP 新分頁 ws：%s", ws_url[:60])
    except Exception as exc:
        logger.debug("CDP 建立分頁失敗：%s", exc)
        return None

    result_holder: List[Optional[str]] = [None]
    msg_id = [0]
    done = threading.Event()
    pending_commands: dict = {}  # id -> description

    def send(ws: object, method: str, params: dict) -> int:
        msg_id[0] += 1
        cmd_id = msg_id[0]
        import json as _json
        ws.send(_json.dumps({"id": cmd_id, "method": method, "params": params}))  # type: ignore[attr-defined]
        return cmd_id

    # CDP 訊息狀態機
    state = [0]  # 0=init, 1=page enabled, 2=network enabled, 3=cookies set, 4=navigating, 5=done
    nav_id = [0]
    eval_id = [0]

    def on_open(ws: object) -> None:
        logger.info("CDP WebSocket 連線成功")
        send(ws, "Page.enable", {})
        send(ws, "Network.enable", {})

    def on_message(ws: object, msg: str) -> None:
        import json as _json
        try:
            data = _json.loads(msg)
        except Exception:
            return

        msg_data_id = data.get("id")
        event = data.get("method", "")

        # 頁面導航事件（含 URL）
        if event == "Page.frameNavigated":
            frame = data.get("params", {}).get("frame", {})
            url = frame.get("url", "")
            if frame.get("parentId") is None:  # 只關注主框架
                logger.info("CDP 頁面導航：%s", url[:80])
            return

        # 頁面載入完成事件
        if event in ("Page.loadEventFired", "Page.domContentEventFired"):
            # 取得目前 URL
            send(ws, "Runtime.evaluate", {
                "expression": "window.location.href",
                "returnByValue": True,
            })
            # 等 React 初始化，輪詢 localStorage
            def poll_token() -> None:
                import time as _t
                import json as _j
                for attempt in range(40):
                    _t.sleep(1.5)
                    if done.is_set():
                        return
                    eid = send(ws, "Runtime.evaluate", {
                        "expression": """
                            (function(){
                                var r={};
                                for(var i=0;i<localStorage.length;i++){
                                    var k=localStorage.key(i);
                                    r[k]=localStorage.getItem(k);
                                }
                                return JSON.stringify(r);
                            })()
                        """,
                        "returnByValue": True,
                    })
                    eval_id[0] = eid
            t = threading.Thread(target=poll_token, daemon=True)
            t.start()
            return

        if msg_data_id is None:
            return

        # Network.enable 完成後設定 cookies
        if state[0] < 3 and msg_data_id > 0:
            state[0] += 1
            if state[0] == 2:
                # 轉換 cookies 格式
                cdp_cookies = []
                for c in cookies:
                    domain = c.get("domain", "connect.garmin.com")
                    cdp_cookies.append({
                        "name": c["name"],
                        "value": c["value"],
                        "domain": domain.lstrip("."),
                        "path": c.get("path", "/"),
                        "secure": c.get("secure", False),
                        "httpOnly": c.get("httpOnly", False),
                        "sameSite": "None",
                    })
                send(ws, "Network.setCookies", {"cookies": cdp_cookies})
            elif state[0] == 3:
                # 導航到 /app/
                nav_id[0] = send(ws, "Page.navigate", {
                    "url": "https://connect.garmin.com/app/"
                })
                logger.info("CDP 已注入 %d 個 cookies，開始導航到 /app/...", len(cookies))
            return

        # Runtime.evaluate 回應
        if msg_data_id == eval_id[0]:
            result = data.get("result", {}).get("result", {})
            value = result.get("value")
            if value:
                try:
                    ls_data = _json.loads(value)
                    if ls_data:
                        logger.info("CDP localStorage 共 %d key：%s", len(ls_data), list(ls_data.keys())[:10])
                    # 搜尋 access_token
                    found = False
                    for k, v in ls_data.items():
                        if not v:
                            continue
                        try:
                            inner = _json.loads(v)
                            if isinstance(inner, dict) and "access_token" in inner:
                                result_holder[0] = inner["access_token"]
                                logger.info("從 localStorage[%s] 取得 access_token", k)
                                done.set()
                                found = True
                                return
                        except Exception:
                            pass
                        if isinstance(k, str) and ("token" in k.lower() or "bearer" in k.lower() or "oauth" in k.lower()):
                            if isinstance(v, str) and len(v) > 20:
                                result_holder[0] = v
                                logger.info("從 localStorage[%s] 取得 token（key=%s）", k, k)
                                done.set()
                                found = True
                                return
                    if not found and ls_data:
                        logger.debug("localStorage 無 token（keys: %s）", list(ls_data.keys()))
                except Exception as exc:
                    logger.debug("localStorage 解析錯誤：%s", exc)
        return

    def on_error(ws: object, err: object) -> None:
        logger.warning("CDP WebSocket 錯誤：%s", err)

    try:
        ws_app = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
        )
        t = threading.Thread(target=ws_app.run_forever, daemon=True)
        t.start()
        # 最多等 45 秒（React app 初始化需要一些時間）
        done.wait(timeout=45)
        ws_app.close()
        return result_holder[0]
    except Exception as exc:
        logger.debug("CDP WebSocket 執行失敗：%s", exc)
        return None


def _extract_oauth_token_via_cdp(port: int = 9222) -> Optional[str]:
    """透過已開啟的 Chrome CDP（遠端偵錯）從 localStorage 取出 OAuth access token。

    使用方式：先用 --remote-debugging-port=9222 啟動 Chrome，
    登入 connect.garmin.com，再執行此函式。

    Args:
        port: Chrome 遠端偵錯埠號（預設 9222）。

    Returns:
        Access token 字串；失敗時回傳 None。
    """
    try:
        r = requests.get(f"http://localhost:{port}/json", timeout=5)
        tabs = r.json()
        # 找 connect.garmin.com 的分頁
        garmin_tabs = [t for t in tabs if "connect.garmin.com" in t.get("url", "")]
        if not garmin_tabs:
            logger.warning("CDP：找不到已開啟的 connect.garmin.com 分頁")
            return None
        target = garmin_tabs[0]
        ws_url = target.get("webSocketDebuggerUrl", "")
        logger.info("CDP 連接分頁：%s", target.get("url", "")[:60])
    except Exception as exc:
        logger.debug("CDP 連接失敗（Chrome 未以偵錯模式開啟？）：%s", exc)
        return None

    try:
        import websocket  # type: ignore[import]
        import threading

        result_holder: List[Optional[str]] = [None]
        done = threading.Event()

        def on_message(ws: object, msg: str) -> None:
            try:
                import json as _json
                data = _json.loads(msg)
                if "result" in data and "result" in data["result"]:
                    value = data["result"]["result"].get("value")
                    if value:
                        result_holder[0] = value
            except Exception:
                pass
            finally:
                done.set()

        def on_open(ws: object) -> None:
            import json as _json
            # 執行 JS 取出所有 token 相關的 localStorage 項目
            js_code = """
                (function() {
                    var result = {};
                    for (var i = 0; i < localStorage.length; i++) {
                        var k = localStorage.key(i);
                        result[k] = localStorage.getItem(k);
                    }
                    return JSON.stringify(result);
                })()
            """
            cmd = _json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": js_code, "returnByValue": True},
            })
            ws.send(cmd)  # type: ignore[attr-defined]

        ws_client = websocket.WebSocketApp(
            ws_url,
            on_message=on_message,
            on_open=on_open,
        )
        t = threading.Thread(target=ws_client.run_forever, daemon=True)
        t.start()
        done.wait(timeout=10)
        ws_client.close()

        if result_holder[0]:
            import json as _json
            try:
                ls_data = _json.loads(result_holder[0])
                logger.info("CDP localStorage 共 %d 個 key", len(ls_data))
                for k, v in ls_data.items():
                    if v:
                        logger.debug("  %s: %s", k, str(v)[:60])
                # 找 access_token
                for k, v in ls_data.items():
                    if v:
                        try:
                            inner = _json.loads(v)
                            if isinstance(inner, dict) and "access_token" in inner:
                                token = inner["access_token"]
                                logger.info("從 localStorage key=%s 取得 access_token", k)
                                return token
                        except Exception:
                            if "access_token" in str(k).lower() or "bearer" in str(k).lower():
                                return v
            except Exception as exc:
                logger.warning("CDP localStorage 解析失敗：%s", exc)
    except ImportError:
        logger.debug("websocket-client 未安裝，無法使用 CDP（pip install websocket-client）")
    except Exception as exc:
        logger.warning("CDP 取 token 失敗：%s", exc)

    return None


def _extract_oauth_token_via_browser(cookies: List[Dict]) -> Optional[str]:
    """注入 cookies 到 Playwright 瀏覽器，等 React app 初始化後從 localStorage 取 OAuth token。

    新版 Garmin Connect (/app/) 使用 OAuth Bearer token 呼叫 connectapi.garmin.com。
    Token 存在 localStorage，不在 cookies 裡。

    Args:
        cookies: 已知有效的 Garmin Connect cookies（含 SESSIONID 等）。

    Returns:
        OAuth access token 字串；失敗時回傳 None。
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    except ImportError:
        logger.warning("Playwright 未安裝，無法取得 OAuth token")
        return None

    logger.info("啟動 Playwright 取得 OAuth token（從 localStorage）...")

    with sync_playwright() as pw:
        for launch_kwargs in [
            {"channel": "chrome", "headless": True},
            {"headless": True},
        ]:
            try:
                browser = pw.chromium.launch(**launch_kwargs)
                break
            except Exception:
                continue
        else:
            logger.warning("無法啟動瀏覽器取得 OAuth token")
            return None

        context = browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )

        # 注入已知有效的 cookies
        playwright_cookies = []
        for c in cookies:
            domain = c.get("domain", "connect.garmin.com")
            # Playwright 需要 domain 開頭有 dot 的形式，且不能是 hostOnly
            host_only = c.get("hostOnly", False)
            pw_cookie: Dict = {
                "name": c["name"],
                "value": c["value"],
                "domain": domain if domain.startswith(".") or host_only else f".{domain}",
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
                "sameSite": "None",
            }
            if host_only:
                pw_cookie["domain"] = domain.lstrip(".")
            playwright_cookies.append(pw_cookie)

        try:
            context.add_cookies(playwright_cookies)
        except Exception as exc:
            logger.warning("注入 cookies 部分失敗：%s，繼續嘗試...", exc)

        page = context.new_page()
        token = None

        try:
            # 造訪 /app/ 頁面，讓 React app 初始化並完成 OAuth 換票
            logger.info("載入 connect.garmin.com/app/ 觸發 OAuth 初始化...")
            page.goto("https://connect.garmin.com/app/", wait_until="domcontentloaded", timeout=30000)

            # 等待 React app 初始化（JS bundle 載入 + OAuth token 寫入 localStorage）
            # 輪詢 localStorage，最多等 30 秒
            for _ in range(30):
                try:
                    token = page.evaluate("""() => {
                        // garth/Connect app stores token in various keys
                        const keys = [
                            'garmin_connect_access_token',
                            'access_token',
                            'gauth_access_token',
                            'token',
                        ];
                        for (const k of keys) {
                            const v = localStorage.getItem(k);
                            if (v) return v;
                        }
                        // Try to find any key with 'token' in the name
                        for (let i = 0; i < localStorage.length; i++) {
                            const k = localStorage.key(i);
                            if (k && k.toLowerCase().includes('token')) {
                                return JSON.stringify({key: k, value: localStorage.getItem(k)});
                            }
                        }
                        return null;
                    }""")
                    if token:
                        logger.info("從 localStorage 找到 token（key/value）：%s", str(token)[:80])
                        break
                except Exception:
                    pass
                time.sleep(1)

            if not token:
                # 列出所有 localStorage keys 供 debug
                try:
                    all_keys = page.evaluate("() => Object.keys(localStorage)")
                    logger.info("localStorage 所有 keys：%s", all_keys)
                except Exception:
                    pass
                logger.warning("未能從 localStorage 取得 OAuth token")

        except Exception as exc:
            logger.error("取得 OAuth token 時發生錯誤：%s", exc)
        finally:
            browser.close()

    return token


def _build_api_session(cookies: List[Dict], oauth_token: Optional[str] = None) -> requests.Session:
    """建立帶有 Garmin Connect cookies（及可選 OAuth token）的 requests Session。

    若提供 oauth_token，同時設定 Authorization: Bearer header，
    用於呼叫 connectapi.garmin.com。

    Args:
        cookies: Cookie 清單。
        oauth_token: OAuth access token（可選）。

    Returns:
        已設定 headers 和 cookies 的 Session。
    """
    session = requests.Session()
    session.headers.update(_API_HEADERS)
    if oauth_token:
        # 解析 token：可能是 JSON 格式 {"key": "...", "value": "..."}
        try:
            import json as _json
            parsed = _json.loads(oauth_token)
            if isinstance(parsed, dict) and "value" in parsed:
                raw_token = parsed["value"]
                # value 可能本身又是 JSON
                try:
                    inner = _json.loads(raw_token)
                    actual = inner.get("access_token") or inner.get("token") or raw_token
                except Exception:
                    actual = raw_token
                session.headers["Authorization"] = f"Bearer {actual}"
                logger.info("設定 Bearer token（from JSON key=%s）", parsed.get("key"))
            else:
                session.headers["Authorization"] = f"Bearer {oauth_token}"
                logger.info("設定 Bearer token（直接字串）")
        except Exception:
            session.headers["Authorization"] = f"Bearer {oauth_token}"
            logger.info("設定 Bearer token（直接字串）")
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if name and value is not None:
            session.cookies.set(name, str(value))
    return session


# ── Garmin Connect API 呼叫 ────────────────────────────────────────────────────


def _get_display_name_from_page(cookies: List[Dict]) -> Optional[str]:
    """從 /app/ 頁面的 window.VIEWER_SOCIAL_PROFILE 取得 displayName（UUID 格式）。

    不需要 API，直接解析 HTML 內嵌的用戶資料。

    Args:
        cookies: Cookie 清單。

    Returns:
        displayName 字串（UUID 或 slug）；失敗時回傳 None。
    """
    import re as _re
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT})
    for c in cookies:
        session.cookies.set(c.get("name", ""), c.get("value", ""))
    try:
        r = session.get(f"{_GARMIN_BASE}/app/", timeout=15)
        m = _re.search(r'"displayName"\s*:\s*"([^"]+)"', r.text)
        if m:
            display_name = m.group(1)
            logger.info("displayName（from /app/ HTML）：%s", display_name)
            return display_name
    except Exception as exc:
        logger.debug("從 /app/ 取 displayName 失敗：%s", exc)
    return None


def _get_display_name(session: requests.Session) -> Optional[str]:
    """從 Garmin Connect API 取得帳號的 display name。

    優先嘗試 connectapi.garmin.com（需 Bearer token），備用舊版 proxy 端點。

    Args:
        session: 已設定 cookies（及可選 Bearer token）的 requests Session。

    Returns:
        Display name 字串；取得失敗時回傳 None。
    """
    urls = [
        f"{_CONNECT_API_BASE}/userprofile-service/userprofile/personal-information",
        f"{_GARMIN_BASE}/proxy/userprofile-service/userprofile/personal-information",
    ]
    for url in urls:
        result = _api_get(session, url)
        if isinstance(result, dict) and result:
            display_name = result.get("displayName") or result.get("userName")
            if display_name:
                logger.info("Display name：%s", display_name)
                return display_name
    return None


def _api_get(session: requests.Session, url: str, params: Optional[Dict] = None) -> Optional[object]:
    """通用 GET 輔助函式，附帶 status code logging。

    Args:
        session: 已設定 cookies 的 requests Session。
        url: 完整 API URL。
        params: 查詢參數（可選）。

    Returns:
        JSON 解析後的物件（dict 或 list）；失敗時回傳 None。
    """
    try:
        resp = session.get(url, params=params, timeout=15)
        logger.debug("GET %s → HTTP %d", url, resp.status_code)
        if resp.status_code == 200:
            data = resp.json()
            # 非空才回傳（空 {} 或 [] 視為失敗，繼續嘗試下一個端點）
            if data:
                return data
            return data  # 仍回傳，讓呼叫方決定
        # 印出前 200 字方便排查
        logger.warning("API 失敗 HTTP %d：%s ... (url=%s)", resp.status_code, resp.text[:200], url)
    except Exception as exc:
        logger.warning("API 呼叫例外（%s）：%s", url, exc)
    return None


def _fetch_daily_summary(session: requests.Session, target_date: str, display_name: Optional[str] = None) -> Optional[Dict]:
    """抓取當日總覽（steps、calories、stress、body battery 等）。

    優先嘗試 connectapi.garmin.com，備用舊版 proxy 端點。

    Args:
        session: 已設定 cookies（及可選 Bearer token）的 requests Session。
        target_date: ISO 格式日期字串，例如 ``'2024-01-15'``。
        display_name: 用戶 displayName（connectapi 端點需要）。

    Returns:
        API 回傳的 dict；失敗時回傳 None。
    """
    urls = []
    if display_name:
        urls.append(f"{_CONNECT_API_BASE}/usersummary-service/usersummary/daily/{display_name}")
    urls.append(f"{_CONNECT_API_BASE}/usersummary-service/usersummary/daily/{target_date}")
    urls.append(f"{_GARMIN_BASE}/proxy/usersummary-service/usersummary/daily/{target_date}")
    for url in urls:
        result = _api_get(session, url, params={"calendarDate": target_date})
        if isinstance(result, dict) and result:
            return result
    return None


def _fetch_sleep(session: requests.Session, target_date: str, display_name: Optional[str] = None) -> Optional[Dict]:
    """抓取睡眠資料。優先 connectapi，備用舊版 proxy。"""
    urls = []
    if display_name:
        urls.append(f"{_CONNECT_API_BASE}/wellness-service/wellness/sleep/{display_name}")
    urls.append(f"{_CONNECT_API_BASE}/wellness-service/wellness/sleep")
    urls.append(f"{_GARMIN_BASE}/proxy/wellness-service/wellness/sleep")
    for url in urls:
        result = _api_get(session, url, params={"date": target_date})
        if isinstance(result, dict) and result:
            return result
    return None


def _fetch_rhr(session: requests.Session, display_name: str, target_date: str) -> Optional[Dict]:
    """抓取安靜心率（Resting Heart Rate）資料。優先 connectapi，備用舊版 proxy。"""
    urls = [
        f"{_CONNECT_API_BASE}/userstats-service/wellness/daily/{display_name}",
        f"{_GARMIN_BASE}/proxy/userstats-service/wellness/daily/{display_name}",
    ]
    for url in urls:
        result = _api_get(session, url, params={"fromDate": target_date, "untilDate": target_date})
        if isinstance(result, dict) and result:
            return result
    return None


def _fetch_body_battery(session: requests.Session, target_date: str, display_name: Optional[str] = None) -> Optional[List]:
    """抓取 Body Battery 時序資料。優先 connectapi，備用舊版 proxy。"""
    urls = []
    if display_name:
        urls.append(f"{_CONNECT_API_BASE}/wellness-service/wellness/bodyBattery/valuesForDay/{display_name}")
    urls.append(f"{_CONNECT_API_BASE}/wellness-service/wellness/bodyBattery/valuesForDay")
    urls.append(f"{_GARMIN_BASE}/proxy/wellness-service/wellness/bodyBattery/valuesForDay")
    for url in urls:
        result = _api_get(session, url, params={"date": target_date})
        if isinstance(result, list) and result:
            return result
    return None


# ── 數據解析 ───────────────────────────────────────────────────────────────────


def _parse_steps(summary: Optional[Dict]) -> Optional[int]:
    """從 daily summary 解析步數。"""
    if not summary:
        return None
    val = summary.get("totalSteps")
    if val is not None:
        logger.info("步數：%d", int(val))
        return int(val)
    return None


def _parse_calories(summary: Optional[Dict]) -> Optional[int]:
    """從 daily summary 解析活動消耗卡路里。"""
    if not summary:
        return None
    val = summary.get("activeKilocalories") or summary.get("totalKilocalories")
    if val is not None:
        logger.info("活動消耗：%d kcal", int(val))
        return int(val)
    return None


def _parse_stress(summary: Optional[Dict]) -> Optional[int]:
    """從 daily summary 解析平均壓力值。"""
    if not summary:
        return None
    val = summary.get("averageStressLevel")
    if val is not None and int(val) > 0:
        logger.info("平均壓力：%d", int(val))
        return int(val)
    return None


def _parse_body_battery_from_summary(summary: Optional[Dict]) -> Optional[int]:
    """從 daily summary 解析 body battery（最低值）。"""
    if not summary:
        return None
    val = summary.get("bodyBatteryLowestValue") or summary.get("bodyBatteryEndLevel")
    if val is not None:
        logger.info("身體電量（摘要）：%d", int(val))
        return int(val)
    return None


def _parse_body_battery_from_timeseries(data: Optional[List]) -> Optional[int]:
    """從 Body Battery 時序資料解析當日結束時的數值。"""
    if not data:
        return None
    last = data[-1]
    if isinstance(last, dict):
        val = last.get("value") or last.get("bodyBatteryLevel")
        if val is not None:
            logger.info("身體電量（時序末）：%d", int(val))
            return int(val)
    return None


def _parse_sleep_hours(sleep_data: Optional[Dict]) -> Optional[float]:
    """從睡眠 API 回傳解析睡眠時數。"""
    if not sleep_data:
        return None
    daily = sleep_data.get("dailySleepDTO") or sleep_data
    sleep_sec = daily.get("sleepTimeSeconds")
    if sleep_sec is not None:
        hours = round(sleep_sec / 3600, 2)
        logger.info("睡眠時數：%.2f 小時", hours)
        return hours
    return None


def _parse_sleep_score(sleep_data: Optional[Dict]) -> Optional[int]:
    """從睡眠 API 回傳解析睡眠分數。"""
    if not sleep_data:
        return None
    daily = sleep_data.get("dailySleepDTO") or sleep_data
    score = (
        daily.get("sleepScores", {}).get("overall", {}).get("value")
        if isinstance(daily.get("sleepScores"), dict)
        else daily.get("sleepScore") or daily.get("overallSleepScore")
    )
    if score is not None:
        logger.info("睡眠分數：%d", int(score))
        return int(score)
    return None


def _parse_rhr(rhr_data: Optional[Dict]) -> Optional[int]:
    """從 RHR API 回傳解析安靜心率。"""
    if not rhr_data:
        return None
    # 格式：{"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [...]}}}
    metrics = (
        rhr_data.get("allMetrics", {})
        .get("metricsMap", {})
        .get("WELLNESS_RESTING_HEART_RATE", [])
    )
    if metrics:
        val = metrics[0].get("value")
        if val is not None:
            logger.info("安靜心率：%d bpm", int(val))
            return int(val)
    return None


# ── 主要資料收集 ────────────────────────────────────────────────────────────────


def collect_health_data(
    session: requests.Session,
    target_date: str,
    display_name: Optional[str] = None,
) -> HealthData:
    """呼叫所有 Garmin Connect 內部 API，收集並解析健康數據。

    單一 API 失敗不中斷整體流程。

    Args:
        session: 已登入的 requests Session（含 cookies 及可選 Bearer token）。
        target_date: ISO 格式日期字串，例如 ``'2024-01-15'``。
        display_name: 用戶 displayName（可從 /app/ HTML 取得）。

    Returns:
        :class:`HealthData` 各欄位失敗時為 None。
    """
    logger.info("開始抓取健康數據（日期：%s）", target_date)

    # displayName 未知時嘗試從 API 取得
    if not display_name:
        display_name = _get_display_name(session)

    summary = _fetch_daily_summary(session, target_date, display_name)
    sleep_data = _fetch_sleep(session, target_date, display_name)
    bb_timeseries = _fetch_body_battery(session, target_date, display_name)

    rhr_value: Optional[int] = None
    if display_name:
        rhr_data = _fetch_rhr(session, display_name, target_date)
        rhr_value = _parse_rhr(rhr_data)

    # Body battery 優先從 timeseries 取（更準確），否則用 summary
    body_battery = _parse_body_battery_from_timeseries(bb_timeseries)
    if body_battery is None:
        body_battery = _parse_body_battery_from_summary(summary)

    return HealthData(
        steps=_parse_steps(summary),
        resting_heart_rate=rhr_value,
        sleep_hours=_parse_sleep_hours(sleep_data),
        sleep_score=_parse_sleep_score(sleep_data),
        stress=_parse_stress(summary),
        body_battery=body_battery,
        calories=_parse_calories(summary),
    )


# ── 寫入 Notion ────────────────────────────────────────────────────────────────


def write_to_notion(health: HealthData, record_date: str) -> bool:
    """將健康數據寫入 Notion Health DB（含防重複寫入）。

    Args:
        health: 已抓取的健康摘要。
        record_date: 記錄日期，ISO 格式。

    Returns:
        寫入成功（或已存在跳過）回傳 True，失敗回傳 False。
    """
    client = get_notion_client(NOTION_API_KEY)

    if page_exists_for_date(client, HEALTH_DB_ID, record_date):
        logger.info("健康記錄已存在（%s），跳過寫入。", record_date)
        return True

    properties: Dict[str, object] = {
        "Name": prop_title(f"Health {record_date}"),
        "Date": prop_date(record_date),
    }

    field_map: Dict[str, Optional[float]] = {
        "Steps":           health.steps,
        "Resting HR":      health.resting_heart_rate,
        "Sleep Hours":     health.sleep_hours,
        "Sleep Score":     health.sleep_score,
        "Stress Avg":      health.stress,
        "Body Battery":    health.body_battery,
        "Active Calories": health.calories,
        "Training Readiness": health.training_readiness,
        "Recovery Time":   health.recovery_time,
        "HRV":             health.hrv_last_night,
        "HRV Weekly Avg":  health.hrv_weekly_avg,
        "Acute Load":      health.acute_load,
        "Fitness Age":     health.fitness_age,
    }

    for field_name, value in field_map.items():
        if value is not None:
            properties[field_name] = prop_number(float(value))

    result = create_page(client, HEALTH_DB_ID, properties)
    return result is not None


# ── 活動數據 ───────────────────────────────────────────────────────────────────

_ACTIVITIES_REST_URL = (
    "https://connect.garmin.com/gc-api/activitylist-service"
    "/activities/search/activities"
)

_ACTIVITIES_JS = """
async ([restUrl, limit]) => {
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (!meta) return { error: 'csrf_missing' };
    const csrf = meta.getAttribute('content');

    return new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.open('GET', restUrl + '?limit=' + limit + '&start=0', true);
        xhr.withCredentials = true;
        xhr.setRequestHeader('Accept', 'application/json');
        xhr.setRequestHeader('NK', 'NT');
        xhr.setRequestHeader('Connect-Csrf-Token', csrf);
        xhr.onload = () => {
            try { resolve(JSON.parse(xhr.responseText)); }
            catch(e) { resolve({ error: 'parse_failed', raw: xhr.responseText.slice(0, 200) }); }
        };
        xhr.onerror = () => resolve({ error: 'onerror' });
        xhr.send();
    });
}
"""


def _parse_pace(duration_sec: float, distance_m: float) -> Optional[str]:
    """計算配速（min:sec / km）。

    Args:
        duration_sec: 持續時間（秒）。
        distance_m: 距離（公尺）。

    Returns:
        配速字串，如 ``'6:51'``；距離為零時回傳 None。
    """
    if not distance_m or distance_m <= 0:
        return None
    pace_sec_per_km = duration_sec / (distance_m / 1000)
    minutes = int(pace_sec_per_km // 60)
    seconds = int(pace_sec_per_km % 60)
    return f"{minutes}:{seconds:02d}"


def _parse_activity(raw: Dict) -> ActivityData:
    """將 Garmin REST API 的活動 JSON 轉為 ActivityData。

    Args:
        raw: 單筆活動的 API 回應 dict。

    Returns:
        解析後的 :class:`ActivityData`。
    """
    distance_m = raw.get("distance") or 0
    duration_sec = raw.get("duration") or 0
    distance_km = round(distance_m / 1000, 2) if distance_m else None
    duration_min = round(duration_sec / 60, 1) if duration_sec else None

    activity_type = (raw.get("activityType") or {}).get("typeKey")
    is_run = activity_type in ("running", "track_running", "trail_running", "treadmill_running")
    pace = _parse_pace(duration_sec, distance_m) if is_run else None

    return ActivityData(
        activity_id=raw.get("activityId"),
        activity_name=raw.get("activityName"),
        activity_type=activity_type,
        start_time=raw.get("startTimeLocal"),
        distance_km=distance_km,
        duration_min=duration_min,
        pace=pace,
        avg_hr=raw.get("averageHR"),
        max_hr=raw.get("maxHR"),
        calories=raw.get("calories"),
        avg_cadence=raw.get("averageRunningCadenceInStepsPerMinute"),
        avg_power=raw.get("avgPower"),
        elevation_gain=raw.get("elevationGain"),
        training_effect_aerobic=raw.get("aerobicTrainingEffect"),
        training_effect_anaerobic=raw.get("anaerobicTrainingEffect"),
        vo2max=raw.get("vO2MaxValue"),
        training_load=raw.get("activityTrainingLoad"),
        avg_stride_length=raw.get("avgStrideLength"),
        avg_vertical_oscillation=raw.get("avgVerticalOscillation"),
        avg_ground_contact_time=raw.get("avgGroundContactTime"),
    )


def fetch_activities_via_playwright_page(
    page: object,
    limit: int = 20,
) -> List[ActivityData]:
    """在已登入的 Playwright page 上透過 XHR 取得活動清單。

    Args:
        page: 已導航到 connect.garmin.com/app/ 的 Playwright page。
        limit: 最多回傳幾筆活動（預設 20）。

    Returns:
        :class:`ActivityData` 清單，按時間由新到舊排序。
    """
    try:
        result = page.evaluate(  # type: ignore[attr-defined]
            _ACTIVITIES_JS, [_ACTIVITIES_REST_URL, limit]
        )
    except Exception as exc:
        logger.error("瀏覽器內活動 API 呼叫失敗：%s", exc)
        return []

    if not result or not isinstance(result, list):
        logger.error("活動 API 回傳異常：%s", str(result)[:200])
        return []

    activities = [_parse_activity(raw) for raw in result]
    logger.info("取得 %d 筆活動", len(activities))
    return activities


def _activity_exists_in_notion(
    client: object,
    database_id: str,
    activity_id: int,
) -> bool:
    """檢查 Notion DB 中是否已有該 activity_id 的記錄。

    用 Activity ID（rich_text 欄位）做唯一性檢查，避免重複寫入。

    Args:
        client: Notion Client。
        database_id: Activity DB ID。
        activity_id: Garmin 活動 ID。

    Returns:
        已存在回傳 True。
    """
    filter_dict = {
        "property": "Activity ID",
        "rich_text": {"equals": str(activity_id)},
    }
    pages = query_pages(client, database_id, filter_dict=filter_dict, page_size=1)
    return len(pages) > 0


def write_activities_to_notion(
    activities: List[ActivityData],
) -> int:
    """將活動清單寫入 Notion Activity DB（跳過已存在的）。

    Args:
        activities: 活動清單。

    Returns:
        新寫入的筆數。
    """
    if not ACTIVITY_DB_ID or ACTIVITY_DB_ID == "placeholder":
        logger.warning("ACTIVITY_DB_ID 尚未設定，跳過活動寫入。")
        return 0

    # Activity DB 在獨立 workspace，須用專屬 key（get_activity_api_key，2026-07 事故教訓）
    client = get_notion_client(get_activity_api_key())
    written = 0

    for act in activities:
        if not act.activity_id:
            continue

        if _activity_exists_in_notion(client, ACTIVITY_DB_ID, act.activity_id):
            logger.debug("活動已存在：%s（%s）", act.activity_name, act.activity_id)
            continue

        # 日期：從 start_time 截取 YYYY-MM-DD
        act_date = act.start_time[:10] if act.start_time else None

        properties: Dict[str, object] = {
            "Name": prop_title(act.activity_name or "Activity"),
        }
        if act_date:
            properties["Date"] = prop_date(act_date)
        if act.activity_type:
            properties["Type"] = prop_select(act.activity_type)
        if act.activity_id:
            properties["Activity ID"] = prop_rich_text(str(act.activity_id))

        # Number 欄位
        number_fields: Dict[str, Optional[float]] = {
            "Distance (km)": act.distance_km,
            "Duration (min)": act.duration_min,
            "Avg HR": float(act.avg_hr) if act.avg_hr else None,
            "Max HR": float(act.max_hr) if act.max_hr else None,
            "Calories": float(act.calories) if act.calories else None,
            "Avg Cadence": act.avg_cadence,
            "Avg Power": float(act.avg_power) if act.avg_power else None,
            "Elevation Gain": float(act.elevation_gain) if act.elevation_gain is not None else None,
            "TE Aerobic": act.training_effect_aerobic,
            "TE Anaerobic": act.training_effect_anaerobic,
            "VO2 Max": float(act.vo2max) if act.vo2max else None,
            "Training Load": act.training_load,
            "Stride Length": act.avg_stride_length,
            "Vertical Oscillation": act.avg_vertical_oscillation,
            "Ground Contact Time": act.avg_ground_contact_time,
        }
        for field_name, value in number_fields.items():
            if value is not None:
                properties[field_name] = prop_number(float(value))

        # Pace 用 rich_text
        if act.pace:
            properties["Pace"] = prop_rich_text(act.pace)

        result = create_page(client, ACTIVITY_DB_ID, properties)
        if result:
            written += 1
            logger.info("寫入活動：%s（%s，%.1f km）", act.activity_name, act_date, act.distance_km or 0)

    return written


# ── 主程式 ─────────────────────────────────────────────────────────────────────


def _interactive_browser_login() -> Optional[List[Dict]]:
    """互動式瀏覽器登入：開啟真實 Chrome 視窗，等待使用者手動完成登入。

    適用於首次設定或 cookies 過期時。登入一次後，cookies 可使用數週。
    支援 MFA、CAPTCHA（使用者在瀏覽器中手動完成）。

    Returns:
        登入成功後的 cookies 清單；失敗或使用者取消時回傳 None。
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    except ImportError:
        logger.error("Playwright 未安裝")
        return None

    print("\n" + "=" * 60)
    print("需要手動登入 Garmin Connect")
    print("=" * 60)
    print("即將開啟瀏覽器視窗。請：")
    print("  1. 在瀏覽器中完成 Garmin Connect 登入")
    print("  2. 若出現驗證碼請完成驗證")
    print("  3. 若收到 Email 驗證碼請輸入")
    print("  4. 等待跳轉到 Garmin Connect 首頁後，腳本將自動繼續")
    print("  5. 瀏覽器視窗會在登入完成後自動關閉")
    print("=" * 60)
    try:
        input("按 Enter 開啟瀏覽器（或 3 秒後自動開啟）...")
    except EOFError:
        pass  # 非互動環境，直接繼續
    print("開啟瀏覽器...")

    logger.info("開啟互動式瀏覽器登入...")

    with sync_playwright() as pw:
        # 嘗試用已安裝的 Chrome，備用 Chromium
        for launch_kwargs in [
            {"channel": "chrome", "headless": False},
            {"headless": False},
        ]:
            try:
                browser = pw.chromium.launch(**launch_kwargs)
                logger.info("瀏覽器啟動成功：%s", launch_kwargs)
                break
            except Exception as exc:
                logger.warning("瀏覽器啟動失敗（%s）：%s", launch_kwargs, exc)
                continue
        else:
            logger.error("無法啟動任何瀏覽器")
            return None

        context = browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        try:
            page.goto(_GARMIN_SIGNIN, wait_until="domcontentloaded", timeout=30000)
            logger.info("瀏覽器已開啟登入頁，等待使用者完成登入...")
            print("\n瀏覽器已開啟，請在瀏覽器中完成登入...")

            # 等待使用者完成登入（URL 跳轉到 connect.garmin.com 主頁）
            page.wait_for_url(
                lambda url: (
                    url.startswith("https://connect.garmin.com/")
                    and "sign" not in url
                    and "signin" not in url
                    and "login" not in url
                ),
                timeout=300000,  # 最多等 5 分鐘讓使用者完成登入
            )
            logger.info("偵測到登入成功，URL：%s", page.url)
            print("登入成功！正在擷取 cookies...")

            # 等待頁面完全載入
            page.wait_for_load_state("networkidle", timeout=15000)
            time.sleep(2)

            cookies = context.cookies()
            logger.info("收集到 %d 個 cookies", len(cookies))

            browser.close()
            return cookies

        except PWTimeoutError:
            logger.warning("等待登入逾時（5 分鐘）")
            print("\n等待逾時。若已完成登入，可重新執行腳本。")
            browser.close()
            return None
        except Exception as exc:
            logger.error("互動式登入發生錯誤：%s", exc)
            browser.close()
            return None


def _has_user_data(session: requests.Session) -> bool:
    """確認 session 能取得真實用戶資料（非空 {} 回應）。

    用於偵測「cookies 已過期導致 API 均回傳 {}」的情況。
    與 _is_session_valid 不同，這裡需要 API 回傳非空 JSON。

    Args:
        session: 已設定 cookies 的 requests Session。

    Returns:
        能取得實際資料回傳 True，否則回傳 False。
    """
    try:
        resp = session.get(
            f"{_GARMIN_BASE}/proxy/userprofile-service/userprofile/personal-information",
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
                return True
    except Exception:
        pass
    return False


def _extract_jwt_from_cookies(cookies: List[Dict]) -> Optional[str]:
    """從 cookies 取出 JWT_WEB 值，嘗試當作 Bearer token 使用。

    JWT_WEB 是 Garmin SSO 核發的 JWT，包含角色與過期時間。
    某些 connectapi.garmin.com 端點可接受此 token 作為 Bearer。

    Args:
        cookies: Cookie 清單。

    Returns:
        JWT 字串；找不到時回傳 None。
    """
    for c in cookies:
        if c.get("name") == "JWT_WEB":
            val = c.get("value", "")
            if val and val.startswith("eyJ"):
                logger.info("找到 JWT_WEB cookie（長度 %d）", len(val))
                return val
    return None


def _test_bearer_token(session: requests.Session, token: str, display_name: Optional[str] = None) -> bool:
    """測試 Bearer token 是否能成功呼叫 connectapi.garmin.com。

    Args:
        session: 基礎 requests.Session（含 cookies）。
        token: 待測試的 Bearer token 字串。
        display_name: 用戶 displayName（可選，用於建構測試 URL）。

    Returns:
        能取得非空資料回傳 True，否則回傳 False。
    """
    test_session = requests.Session()
    test_session.headers.update(_API_HEADERS)
    test_session.headers["Authorization"] = f"Bearer {token}"
    # 複製 cookies
    for name, value in session.cookies.items():
        test_session.cookies.set(name, value)

    test_urls = [
        f"{_CONNECT_API_BASE}/userprofile-service/userprofile/personal-information",
    ]
    if display_name:
        from datetime import date as _date
        today = _date.today().isoformat()
        test_urls.insert(0, f"{_CONNECT_API_BASE}/usersummary-service/usersummary/daily/{display_name}?calendarDate={today}")

    for url in test_urls:
        try:
            resp = test_session.get(url, timeout=10)
            logger.info("Bearer token 測試 %s → HTTP %d", url.split("/")[-1].split("?")[0], resp.status_code)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, (dict, list)):
                    logger.info("Bearer token 有效，取得非空資料")
                    return True
                logger.info("Bearer token 接受（HTTP 200）但回傳空資料：%s", str(data)[:60])
        except Exception as exc:
            logger.debug("Bearer token 測試端點失敗：%s", exc)
    return False


def get_garmin_session() -> Optional[requests.Session]:
    """取得有效的 Garmin Connect session（含 OAuth Bearer token）。

    流程：
    1. 從快取 cookies 建立 session
    2. 嘗試用 JWT_WEB cookie 作為 Bearer token（最快，無需瀏覽器）
    3. 嘗試 Chrome CDP 從 localStorage 取 OAuth token（需 --remote-debugging-port=9222）
    4. 快取的 OAuth token
    5. 以純 cookie session 繼續（資料可能為空）
    6. cookies 過期（401/403）→ 互動式瀏覽器重新登入

    Returns:
        已認證的 requests.Session（含 Bearer token）；失敗時回傳 None。
    """
    cached_cookies = _load_cookies()
    if cached_cookies:
        session = _build_session(cached_cookies)
        if _is_session_valid(session):
            logger.info("Cookie session 有效，嘗試取得 Bearer token...")

            # ── 方法 1：JWT_WEB cookie 直接當 Bearer token ────────────────
            jwt_token = _extract_jwt_from_cookies(cached_cookies)
            if jwt_token:
                logger.info("嘗試 JWT_WEB 作為 Bearer token...")
                display_name = _get_display_name_from_page(cached_cookies)
                if _test_bearer_token(session, jwt_token, display_name):
                    logger.info("JWT_WEB Bearer token 有效！")
                    return _build_api_session(cached_cookies, jwt_token)
                logger.info("JWT_WEB 無效（connectapi 不接受），繼續嘗試其他方式...")

            # ── 方法 2：自動啟動 Chrome CDP（全自動，繞過 Cloudflare）────────
            cdp_token = _extract_oauth_token_via_cdp_auto(cached_cookies)
            if cdp_token:
                logger.info("自動 CDP 取得 OAuth token，快取並建立 session...")
                _save_oauth_token(cdp_token)
                return _build_api_session(cached_cookies, cdp_token)

            # ── 方法 3：已開啟的 Chrome CDP（port 9222）──────────────────────
            cdp_token = _extract_oauth_token_via_cdp()
            if cdp_token:
                logger.info("CDP 取得 OAuth token，快取並建立 session...")
                _save_oauth_token(cdp_token)
                return _build_api_session(cached_cookies, cdp_token)

            # ── 方法 3：快取的 OAuth token ────────────────────────────────
            cached_token = _load_oauth_token()
            if cached_token:
                logger.info("使用快取的 OAuth token...")
                return _build_api_session(cached_cookies, cached_token)

            # ── 方法 4：純 cookie session（資料可能為空，但不阻塞）──────────
            logger.warning(
                "無法取得 Bearer token。\n"
                "若要取得完整健康數據，請：\n"
                "  1. 用以下指令開啟 Chrome：\n"
                '     chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\\temp\\chrome-debug"\n'
                "  2. 在 Chrome 中登入 connect.garmin.com/app/\n"
                "  3. 重新執行此腳本"
            )
            return session

        # session 無效（401/403），需要重新登入
        logger.info("快取 session 已失效（401/403），需要重新登入...")
        _COOKIE_FILE.unlink(missing_ok=True)
        # 同時清除 OAuth token 快取
        if _TOKEN_CACHE_FILE.exists():
            _TOKEN_CACHE_FILE.unlink(missing_ok=True)

    # 互動式瀏覽器登入（首次或 cookies 過期時）
    cookies = _interactive_browser_login()
    if not cookies:
        logger.error("瀏覽器登入失敗")
        return None

    _save_cookies(cookies)
    session = _build_session(cookies)

    if not _is_session_valid(session):
        logger.error("登入後 session 仍無效")
        return None

    # 登入後嘗試取得 Bearer token
    jwt_token = _extract_jwt_from_cookies(cookies)
    if jwt_token and _test_bearer_token(session, jwt_token):
        return _build_api_session(cookies, jwt_token)

    cdp_token = _extract_oauth_token_via_cdp_auto(cookies)
    if cdp_token:
        _save_oauth_token(cdp_token)
        return _build_api_session(cookies, cdp_token)

    cdp_token = _extract_oauth_token_via_cdp()
    if cdp_token:
        _save_oauth_token(cdp_token)
        return _build_api_session(cookies, cdp_token)

    return session


_GQL_URL = "https://connect.garmin.com/gc-api/graphql-gateway/graphql"

# JavaScript 健康數據抓取腳本（在瀏覽器內執行）
_HEALTH_JS = """
async ([date, gqlUrl]) => {
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (!meta) return { error: 'csrf_missing', url: window.location.href };
    const csrf = meta.getAttribute('content');

    function xhrGql(query) {
        return new Promise((resolve) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', gqlUrl, true);
            xhr.withCredentials = true;
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.setRequestHeader('Accept', 'application/json');
            xhr.setRequestHeader('NK', 'NT');
            xhr.setRequestHeader('Connect-Csrf-Token', csrf);
            xhr.onload = () => {
                try { resolve(JSON.parse(xhr.responseText)); }
                catch(e) { resolve(null); }
            };
            xhr.onerror = () => resolve(null);
            xhr.send(JSON.stringify({ query }));
        });
    }

    const [summaryR, sleepR, trainingR, hrvR, fitnessR] = await Promise.all([
        xhrGql(`{ userDailySummary(startDate: "${date}", endDate: "${date}") {
            totalSteps restingHeartRate averageStressLevel
            bodyBatteryMostRecentValue activeKilocalories } }`),
        xhrGql(`{ sleepScalar(date: "${date}", sleepOnly: false) }`),
        xhrGql(`{ trainingReadinessScalar(calendarDate: "${date}") }`),
        xhrGql(`{ heartRateVariabilityScalar(startDate: "${date}", endDate: "${date}") }`),
        xhrGql(`{ fitnessAgeScalar(startDate: "${date}", endDate: "${date}") }`),
    ]);

    return {
        summary: summaryR && summaryR.data,
        sleep: sleepR && sleepR.data,
        training: trainingR && trainingR.data,
        hrv: hrvR && hrvR.data,
        fitness: fitnessR && fitnessR.data,
        url: window.location.href,
    };
}
"""


def _collect_health_via_playwright_page(page: object, target_date: str) -> HealthData:
    """瀏覽器內執行 XHR + GraphQL，取得所有健康數據。

    在已登入的 Playwright page 上執行 JavaScript，使用瀏覽器的 session
    和 CSRF token 呼叫 /gc-api/graphql-gateway/graphql。

    Args:
        page: 已導航到 connect.garmin.com/app/ 的 Playwright page 物件。
        target_date: ISO 格式日期字串，例如 ``'2026-04-01'``。

    Returns:
        :class:`HealthData`，各欄位在失敗時為 None。
    """
    try:
        result = page.evaluate(_HEALTH_JS, [target_date, _GQL_URL])  # type: ignore[attr-defined]
        logger.debug("瀏覽器 API 回傳：%s", str(result)[:500])
    except Exception as exc:
        logger.error("瀏覽器內 API 呼叫失敗：%s", exc)
        return HealthData()

    if not result or result.get("error"):
        logger.error("API 回傳錯誤：%s（url=%s）", result.get("error") if result else "null", result.get("url") if result else "")
        return HealthData()

    # 解析 userDailySummary（返回陣列，取第一筆）
    summary_list = (result.get("summary") or {}).get("userDailySummary") or []
    summary = summary_list[0] if summary_list else {}

    # 解析 sleepScalar
    sleep_scalar = (result.get("sleep") or {}).get("sleepScalar") or {}
    sleep_dto = sleep_scalar.get("dailySleepDTO") or {}

    steps = summary.get("totalSteps")
    rhr = summary.get("restingHeartRate")
    stress = summary.get("averageStressLevel")
    body_battery = summary.get("bodyBatteryMostRecentValue")
    calories = summary.get("activeKilocalories")

    sleep_sec = sleep_dto.get("sleepTimeSeconds")
    sleep_hours = round(sleep_sec / 3600, 2) if sleep_sec else None
    sleep_scores = sleep_dto.get("sleepScores") or {}
    sleep_score = (sleep_scores.get("overall") or {}).get("value") if isinstance(sleep_scores, dict) else None

    # 解析 trainingReadinessScalar（陣列，取 validSleep=True 的那筆，否則取第一筆）
    tr_list = (result.get("training") or {}).get("trainingReadinessScalar") or []
    tr = next((t for t in tr_list if t.get("validSleep")), tr_list[0] if tr_list else {})
    training_readiness = tr.get("score")
    training_readiness_level = tr.get("level")
    recovery_time = tr.get("recoveryTime")
    acute_load = tr.get("acuteLoad")

    # 解析 heartRateVariabilityScalar
    hrv_data = (result.get("hrv") or {}).get("heartRateVariabilityScalar") or {}
    hrv_summaries = hrv_data.get("hrvSummaries") or []
    hrv_entry = hrv_summaries[0] if hrv_summaries else {}
    hrv_last_night = hrv_entry.get("lastNightAvg")
    hrv_weekly_avg = hrv_entry.get("weeklyAvg")
    hrv_status = hrv_entry.get("status")

    # 解析 fitnessAgeScalar
    fa_list = (result.get("fitness") or {}).get("fitnessAgeScalar") or []
    fa_entry = fa_list[0] if fa_list else {}
    fa_values = fa_entry.get("values") or {}
    fitness_age = fa_values.get("fitnessAge")

    logger.info("─ 步數：%s", f"{steps:,}" if steps else "N/A")
    logger.info("─ 安靜心率：%s", f"{rhr} bpm" if rhr else "N/A")
    logger.info("─ 睡眠時數：%s", f"{sleep_hours:.2f} 小時" if sleep_hours else "N/A")
    logger.info("─ 睡眠分數：%s", str(sleep_score) if sleep_score else "N/A")
    logger.info("─ 平均壓力：%s", str(stress) if stress else "N/A")
    logger.info("─ 身體電量：%s", str(body_battery) if body_battery else "N/A")
    logger.info("─ 活動消耗：%s", f"{calories} kcal" if calories else "N/A")
    logger.info("─ 訓練準備度：%s（%s）", training_readiness or "N/A", training_readiness_level or "N/A")
    logger.info("─ 恢復時間：%s 小時", recovery_time if recovery_time is not None else "N/A")
    logger.info("─ HRV 昨晚：%s ms（週均 %s ms，%s）", hrv_last_night or "N/A", hrv_weekly_avg or "N/A", hrv_status or "N/A")
    logger.info("─ 急性負荷：%s", acute_load or "N/A")
    logger.info("─ 體適能年齡：%s", f"{fitness_age:.1f}" if fitness_age else "N/A")

    return HealthData(
        steps=int(steps) if steps is not None else None,
        resting_heart_rate=int(rhr) if rhr is not None else None,
        sleep_hours=sleep_hours,
        sleep_score=int(sleep_score) if sleep_score is not None else None,
        stress=int(stress) if stress is not None else None,
        body_battery=int(body_battery) if body_battery is not None else None,
        calories=int(calories) if calories is not None else None,
        training_readiness=int(training_readiness) if training_readiness is not None else None,
        training_readiness_level=training_readiness_level,
        recovery_time=int(recovery_time) if recovery_time is not None else None,
        hrv_last_night=int(hrv_last_night) if hrv_last_night is not None else None,
        hrv_weekly_avg=int(hrv_weekly_avg) if hrv_weekly_avg is not None else None,
        hrv_status=hrv_status,
        acute_load=int(acute_load) if acute_load is not None else None,
        fitness_age=round(fitness_age, 1) if fitness_age is not None else None,
    )


def get_health_data_via_playwright(
    target_date: str,
) -> Optional[tuple]:
    """Playwright 瀏覽器登入並取得健康數據 + 活動清單。

    流程：
    1. 嘗試用快取 cookies 直接載入 /app/home
    2. 若被重導到 SSO，開啟互動式登入視窗
    3. 登入成功後儲存 cookies
    4. 同一 session 取得健康數據和活動清單
    5. 關閉瀏覽器，回傳 (HealthData, List[ActivityData])

    Args:
        target_date: ISO 格式日期字串，例如 ``'2026-04-01'``。

    Returns:
        ``(HealthData, List[ActivityData])``；失敗時回傳 None。
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    except ImportError:
        logger.error("Playwright 未安裝，請執行：pip install playwright && playwright install chromium")
        return None

    logger.info("啟動 Playwright 瀏覽器（健康數據抓取）...")

    with sync_playwright() as pw:
        # 優先使用真實 Chrome（TLS 指紋更自然），備用 Chromium
        browser = None
        for kwargs in [{"channel": "chrome", "headless": True}, {"headless": True}]:
            try:
                browser = pw.chromium.launch(**kwargs)
                logger.info("瀏覽器啟動：%s", kwargs)
                break
            except Exception:
                continue
        if browser is None:
            logger.error("無法啟動瀏覽器")
            return None

        context = browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )

        # 注入快取 cookies（若有）
        cached_cookies = _load_cookies() or []
        if cached_cookies:
            pw_cookies = []
            for c in cached_cookies:
                domain = c.get("domain", "connect.garmin.com")
                host_only = c.get("hostOnly", False)
                pw_cookies.append({
                    "name": c["name"],
                    "value": c["value"],
                    "domain": domain if domain.startswith(".") or host_only else f".{domain}",
                    "path": c.get("path", "/"),
                    "secure": c.get("secure", False),
                    "httpOnly": c.get("httpOnly", False),
                    "sameSite": "None",
                })
            try:
                context.add_cookies(pw_cookies)
                logger.info("已注入 %d 個快取 cookies", len(pw_cookies))
            except Exception as exc:
                logger.warning("注入 cookies 失敗：%s", exc)

        page = context.new_page()

        # 嘗試直接載入 app
        try:
            page.goto("https://connect.garmin.com/app/home", wait_until="domcontentloaded", timeout=20000)
            logger.info("已載入，目前 URL：%s", page.url[:80])
        except Exception as exc:
            logger.warning("載入 /app/home 失敗：%s", exc)

        # 若被重導到 SSO，開啟互動式登入
        if "sso.garmin.com" in page.url or "signin" in page.url or "sign-in" in page.url:
            logger.info("需要重新登入，開啟互動式視窗...")
            browser.close()
            browser = pw.chromium.launch(channel="chrome", headless=False) if True else None
            for kwargs in [{"channel": "chrome", "headless": False}, {"headless": False}]:
                try:
                    browser = pw.chromium.launch(**kwargs)
                    break
                except Exception:
                    continue
            if browser is None:
                logger.error("無法啟動互動式瀏覽器")
                return None

            context = browser.new_context(user_agent=_USER_AGENT, viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.goto("https://connect.garmin.com/signin/", wait_until="domcontentloaded", timeout=30000)

            print("\n" + "=" * 60)
            print("請在瀏覽器中完成 Garmin Connect 登入（含 MFA）")
            print("登入後等待自動跳轉到首頁，腳本將自動繼續")
            print("=" * 60)

            try:
                page.wait_for_url(
                    lambda url: url.startswith("https://connect.garmin.com/") and "sign" not in url,
                    timeout=300000,
                )
                logger.info("登入成功，URL：%s", page.url[:80])
            except PWTimeoutError:
                logger.error("等待登入逾時")
                browser.close()
                return None

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(2)

            # 儲存新 cookies
            new_cookies = context.cookies()
            _save_cookies(new_cookies)
            logger.info("已儲存 %d 個新 cookies", len(new_cookies))

        # 等待 app 完全初始化（CSRF token 需要頁面 JS 執行後才出現）
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(2)

        health = _collect_health_via_playwright_page(page, target_date)

        # 同一 session 取得活動清單
        activities = fetch_activities_via_playwright_page(page, limit=20)

        browser.close()
        return health, activities


def main() -> None:
    """健康與活動追蹤主程式（Playwright 網頁版）。"""
    logger.info("=" * 60)
    logger.info("健康與活動追蹤腳本啟動（Playwright 網頁版）")
    logger.info("=" * 60)

    config.validate_config()

    # 前一天的日期（Garmin 資料通常在隔天才完整同步）
    target_date = (date.today() - timedelta(days=1)).isoformat()
    logger.info("抓取日期：%s", target_date)

    result = get_health_data_via_playwright(target_date)
    if result is None:
        logger.error("無法取得 Garmin 數據，腳本終止。")
        sys.exit(1)

    health, activities = result

    # ── 健康數據摘要 ──
    logger.info("── 健康數據 ──")
    logger.info("─ 步數：%s",     f"{health.steps:,}" if health.steps else "N/A")
    logger.info("─ 安靜心率：%s", f"{health.resting_heart_rate} bpm" if health.resting_heart_rate else "N/A")
    logger.info("─ 睡眠時數：%s", f"{health.sleep_hours} 小時" if health.sleep_hours else "N/A")
    logger.info("─ 睡眠分數：%s", str(health.sleep_score) if health.sleep_score else "N/A")
    logger.info("─ 平均壓力：%s", str(health.stress) if health.stress else "N/A")
    logger.info("─ 身體電量：%s", str(health.body_battery) if health.body_battery else "N/A")
    logger.info("─ 活動消耗：%s", f"{health.calories} kcal" if health.calories else "N/A")
    logger.info("─ 訓練準備度：%s（%s）", health.training_readiness or "N/A", health.training_readiness_level or "N/A")
    logger.info("─ 恢復時間：%s 小時", health.recovery_time if health.recovery_time is not None else "N/A")
    logger.info("─ HRV：%s ms（週均 %s ms，%s）", health.hrv_last_night or "N/A", health.hrv_weekly_avg or "N/A", health.hrv_status or "N/A")
    logger.info("─ 急性負荷：%s", health.acute_load or "N/A")
    logger.info("─ 體適能年齡：%s", health.fitness_age or "N/A")

    # ── 活動摘要 ──
    if activities:
        logger.info("── 近期活動（%d 筆）──", len(activities))
        for act in activities[:5]:
            logger.info(
                "  %s | %s | %.1f km | %s | HR %s | TE %.1f",
                (act.start_time or "")[:10],
                act.activity_type or "?",
                act.distance_km or 0,
                act.pace or "-",
                act.avg_hr or "-",
                act.training_effect_aerobic or 0,
            )

    # ── 寫入 Notion ──
    if HEALTH_DB_ID and HEALTH_DB_ID != "placeholder":
        success = write_to_notion(health, target_date)
        if success:
            logger.info("✓ 健康數據已寫入 Notion")
        else:
            logger.error("✗ 健康數據寫入 Notion 失敗")
    else:
        logger.warning("HEALTH_DB_ID 尚未設定，跳過健康寫入。")

    if activities:
        written = write_activities_to_notion(activities)
        logger.info("✓ 新寫入 %d 筆活動到 Notion", written)

    logger.info("腳本完成")


if __name__ == "__main__":
    main()

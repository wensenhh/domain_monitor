import os
import multiprocessing
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


def _clean_url(value: str) -> str:
    s = "" if value is None else str(value).strip()
    if not s:
        return s
    if "://" not in s:
        return "http://" + s
    return s


def _parse_float_seconds(text: str | None) -> float | None:
    if not text:
        return None
    s = str(text).strip()
    if not s or s in {"-", "--"}:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _safe_filename(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    return s[:200] if s else "domain"


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _detect_real_proxy_ip(page, *, timeout_ms: int) -> str | None:
    try:
        page.goto("https://httpbin.org/ip", wait_until="domcontentloaded", timeout=timeout_ms)
        raw = None
        try:
            raw = page.locator("pre").first.inner_text(timeout=timeout_ms)
        except Exception:
            raw = page.locator("body").first.inner_text(timeout=timeout_ms)
        m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", raw or "")
        return m.group(1) if m else None
    except Exception:
        return None


def _handle_click_verify(page, *, base_url: str, timeout_ms: int) -> bool:
    for _ in range(4):
        try:
            if page.locator("text=网站当前访问量较大").count() == 0 and page.locator("text=点击按钮继续访问").count() == 0:
                if page.locator("text=点击验证").count() == 0:
                    return False
        except Exception:
            return False

        btn = page.locator(
            "button:has-text('继续访问'), button:has-text('继续'), button:has-text('点击'), button, input[type=button], input[type=submit], .btn"
        ).first
        try:
            if btn.count() == 0 or not btn.is_visible():
                page.wait_for_timeout(800)
                continue
        except Exception:
            page.wait_for_timeout(800)
            continue

        try:
            try:
                btn.scroll_into_view_if_needed(timeout=timeout_ms)
            except Exception:
                pass
            try:
                btn.hover(timeout=timeout_ms)
            except Exception:
                pass
            btn.click(timeout=timeout_ms)
        except Exception:
            page.wait_for_timeout(800)
            continue
        try:
            page.wait_for_timeout(600)
        except Exception:
            pass
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            try:
                page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception:
                pass
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        try:
            if page.locator("input[name='url'], input#url, input[name='host'], input#host").count() > 0:
                return True
        except Exception:
            pass
        page.wait_for_timeout(800)
    return True


def _extract_rows(page, *, base_url: str, screenshot_path: str | None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    tables = page.locator("table")
    for i in range(tables.count()):
        tbl = tables.nth(i)
        headers: list[str] = []
        if tbl.locator("thead tr").count() > 0:
            headers = [h.strip() for h in tbl.locator("thead tr").nth(0).locator("th,td").all_text_contents()]
        elif tbl.locator("tbody tr").count() > 0:
            headers = [h.strip() for h in tbl.locator("tbody tr").nth(0).locator("th,td").all_text_contents()]

        header_l = [h.lower() for h in headers]
        key_hits = sum(1 for k in ["下载", "连接", "dns", "总", "状态", "ip"] if any(k in h for h in header_l))
        if key_hits < 4:
            continue

        if any("区域/运营商" in h or "最快" in h or "最慢" in h or "平均" in h for h in headers):
            continue

        header_map = {j: (headers[j].lower() if j < len(headers) else "") for j in range(max(len(headers), 0))}
        body_rows = tbl.locator("tbody tr")
        start_row = 0
        if headers and body_rows.count() > 0:
            first_row_header_like = [t.strip().lower() for t in body_rows.nth(0).locator("th").all_text_contents()]
            if first_row_header_like:
                start_row = 1

        for r in range(start_row, body_rows.count()):
            row = body_rows.nth(r)
            row_data = row.evaluate(
                r"""
                (node) => {
                  const cells = Array.from(node.querySelectorAll('td,th'));
                  const texts = cells.map((c) => (c.innerText || '').trim());
                  return { texts, html: node.outerHTML || '' };
                }
                """
            )
            texts = [str(c).strip() for c in (row_data.get("texts") or []) if str(c).strip()]
            if not texts:
                continue
            text_join = " ".join(texts)
            raw_html = str(row_data.get("html") or "")

            operator = None
            region = None
            download_time = None
            connect_time = None
            dns_time = None
            total_time = None
            status_code = None
            ip_location = None
            response_ip = None

            for idx, tx in enumerate(texts):
                key = header_map.get(idx, "")
                if "运营" in key:
                    operator = tx
                elif "地区" in key or "区域" in key:
                    region = tx
                elif "下载" in key:
                    download_time = _parse_float_seconds(tx)
                elif "连接" in key:
                    connect_time = _parse_float_seconds(tx)
                elif "dns" in key or "解析" in key:
                    dns_time = _parse_float_seconds(tx)
                elif "总" in key:
                    total_time = _parse_float_seconds(tx)
                elif "状态" in key or "http" in key:
                    m = re.search(r"\b\d{3}\b|失败|错误|超时|无响应|未解析|--", tx)
                    status_code = m.group(0) if m else tx.strip()
                elif "响应ip" in key or ("ip" in key and "响应" in key):
                    m = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", tx)
                    response_ip = m.group(0) if m else response_ip
                elif ("ip" in key) and ("所在" in key or "地区" in key or "位置" in key):
                    ip_location = tx

            if not response_ip:
                m = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text_join)
                response_ip = m.group(0) if m else None
            if not ip_location and response_ip:
                m2 = re.search(rf"{re.escape(response_ip)}\s*(.+)", text_join)
                ip_location = m2.group(1).strip() if m2 else None

            if not status_code:
                m_sc = re.search(r"\b\d{3}\b|失败|错误|超时|无响应|未解析", text_join)
                status_code = m_sc.group(0) if m_sc else None

            has_any = any(v is not None for v in [download_time, connect_time, dns_time, total_time, status_code, response_ip, region, operator])
            if not has_any:
                continue

            detect_node_location = ""
            if region:
                detect_node_location += region.strip()
            if operator:
                detect_node_location += operator.strip()
            detect_node_location = detect_node_location or (texts[0] if texts else "")
            detect_node_location = detect_node_location[:255] if detect_node_location else None

            results.append(
                {
                    "isp": (operator or "").strip() or None,
                    "detect_node_location": detect_node_location,
                    "ip_location": ip_location,
                    "response_ip": response_ip,
                    "status_code": status_code or "--",
                    "download_time": download_time,
                    "connect_time": connect_time,
                    "dns_time": dns_time,
                    "total_time": total_time,
                    "raw": {
                        "source": "itdog",
                        "base_url": base_url,
                        "screenshot": screenshot_path,
                        "row_html": raw_html,
                        "texts": texts,
                    },
                }
            )
    if not results:
        raise RuntimeError(f"itdog results empty, screenshot={screenshot_path or ''}")

    uniq: list[dict[str, Any]] = []
    seen = set()
    for r in results:
        key = (
            str(r.get("isp") or "").strip(),
            str(r.get("detect_node_location") or "").strip(),
            str(r.get("response_ip") or "").strip(),
            str(r.get("status_code") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq


def _run_playwright_impl(
    domain: str,
    *,
    base_url: str,
    proxy: str,
    headless: bool,
    screenshot_enabled: bool,
    screenshot_dir: str,
    nav_timeout_ms: int,
    action_timeout_ms: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError("playwright is not installed") from e

    base_url = (base_url or "https://www.itdog.cn/http/").strip()
    proxy_s = (proxy or "").strip()
    url_in = _clean_url(domain)
    timings_ms: dict[str, float] = {}
    real_proxy_ip = None

    overall_t0 = time.monotonic()
    pw = None
    browser = None
    context = None
    page = None
    screenshot_path = None
    try:
        pw = sync_playwright().start()
        t0 = time.monotonic()
        browser = pw.chromium.launch(
            headless=headless,
            proxy={"server": proxy_s} if proxy_s else None,
            args=["--disable-http2", "--disable-quic", "--disable-blink-features=AutomationControlled"],
        )
        timings_ms["browser_launch_ms"] = (time.monotonic() - t0) * 1000.0
        t1 = time.monotonic()
        context = browser.new_context(
            locale="zh-CN",
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        timings_ms["context_create_ms"] = (time.monotonic() - t1) * 1000.0

        blocked_types = {"image", "media"} if screenshot_enabled else {"image", "media", "font", "stylesheet"}
        blocked_hosts = {
            "doubleclick.net",
            "googlesyndication.com",
            "google-analytics.com",
            "googletagmanager.com",
            "adservice.google.com",
            "gstatic.com",
        }

        def handle_route(route, request):
            try:
                if request.resource_type in blocked_types:
                    return route.abort()
                host = urlparse(request.url).hostname or ""
                for h in blocked_hosts:
                    if host.endswith(h):
                        return route.abort()
            except Exception:
                pass
            return route.continue_()

        context.route("**/*", handle_route)
        page = context.new_page()
        page.set_default_navigation_timeout(nav_timeout_ms)
        page.set_default_timeout(action_timeout_ms)

        if proxy_s:
            t2 = time.monotonic()
            real_proxy_ip = _detect_real_proxy_ip(page, timeout_ms=nav_timeout_ms)
            timings_ms["proxy_detect_ms"] = (time.monotonic() - t2) * 1000.0

        t3 = time.monotonic()
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=min(nav_timeout_ms, 20000))
        except PlaywrightTimeoutError:
            try:
                page.goto(base_url, wait_until="commit", timeout=min(nav_timeout_ms, 15000))
            except Exception:
                pass
        timings_ms["goto_ms"] = (time.monotonic() - t3) * 1000.0

        _handle_click_verify(page, base_url=base_url, timeout_ms=nav_timeout_ms)

        input_loc = page.locator(
            "input[name='url'], input#url, input[name='host'], input#host, input[placeholder*='http'], input[placeholder*='域名']"
        ).first
        if input_loc.count() == 0:
            _handle_click_verify(page, base_url=base_url, timeout_ms=nav_timeout_ms)
            input_loc = page.locator("input[name='url'], input#url, input[name='host'], input#host").first
        if input_loc.count() == 0:
            debug_path = None
            try:
                _ensure_dir(screenshot_dir)
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                filename = f"itdog_verify_{_safe_filename(url_in)}_{ts}.png"
                debug_path = os.path.join(screenshot_dir, filename)
                page.screenshot(path=debug_path, full_page=True)
            except Exception:
                debug_path = None
            raise RuntimeError(f"itdog input not found, screenshot={debug_path or ''}")

        t4 = time.monotonic()
        input_loc.fill(url_in)
        btn = page.locator("button[onclick*=\"check_form('fast')\"], button:has-text('检测'), button:has-text('开始')").first
        if btn.count() == 0:
            btn = page.locator("input[type=button][value*='检测'], input[type=submit][value*='检测']").first
        if btn.count() == 0:
            raise RuntimeError("itdog start button not found")
        btn.click()
        timings_ms["fill_and_click_ms"] = (time.monotonic() - t4) * 1000.0

        t5 = time.monotonic()
        page.wait_for_selector("table, tr", timeout=max(10000, action_timeout_ms))
        deadline = time.monotonic() + max(90.0, (nav_timeout_ms / 1000.0) + 30.0)
        last_cnt = -1
        stable = 0
        while time.monotonic() < deadline:
            try:
                if page.locator("text=网站当前访问量较大").count() > 0:
                    _handle_click_verify(page, base_url=base_url, timeout_ms=nav_timeout_ms)
            except Exception:
                pass
            try:
                cnt = page.locator("tr.node_tr").count()
            except Exception:
                cnt = 0
            if cnt == last_cnt and cnt > 0:
                stable += 1
            else:
                stable = 0
                last_cnt = cnt
            if stable >= 3 and cnt > 0:
                break
            page.wait_for_timeout(1000)
        timings_ms["wait_table_ms"] = (time.monotonic() - t5) * 1000.0

        if screenshot_enabled:
            _ensure_dir(screenshot_dir)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"itdog_{_safe_filename(url_in)}_{ts}.png"
            screenshot_path = os.path.join(screenshot_dir, filename)
            t6 = time.monotonic()
            try:
                page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                screenshot_path = None
            timings_ms["screenshot_ms"] = (time.monotonic() - t6) * 1000.0

        t7 = time.monotonic()
        results = _extract_rows(page, base_url=base_url, screenshot_path=screenshot_path)
        timings_ms["extract_rows_ms"] = (time.monotonic() - t7) * 1000.0

        for r in results:
            raw = r.get("raw")
            if isinstance(raw, dict):
                raw["real_proxy_ip"] = real_proxy_ip
        timings_ms["total_platform_ms"] = (time.monotonic() - overall_t0) * 1000.0
        return results, {
            "real_proxy_ip": real_proxy_ip,
            "screenshot": screenshot_path,
            "timings_ms": timings_ms,
            "itdog_stats": {"rows": len(results)},
            "incomplete": False,
        }
    finally:
        t8 = time.monotonic()
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        timings_ms["close_ms"] = (time.monotonic() - t8) * 1000.0


def _child_run(conn, kwargs: dict[str, Any]):
    try:
        res = _run_playwright_impl(**kwargs)
        conn.send({"ok": True, "res": res})
    except Exception as e:
        conn.send({"ok": False, "etype": type(e).__name__, "error": str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run(
    domain: str,
    *,
    base_url: str,
    proxy: str,
    headless: bool,
    screenshot_enabled: bool,
    screenshot_dir: str,
    nav_timeout_ms: int,
    action_timeout_ms: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    kwargs = {
        "domain": domain,
        "base_url": base_url,
        "proxy": proxy,
        "headless": headless,
        "screenshot_enabled": screenshot_enabled,
        "screenshot_dir": screenshot_dir,
        "nav_timeout_ms": nav_timeout_ms,
        "action_timeout_ms": action_timeout_ms,
    }
    proc = ctx.Process(target=_child_run, args=(child_conn, kwargs), daemon=True)
    proc.start()
    child_conn.close()
    timeout_seconds = max(90.0, (nav_timeout_ms / 1000.0) + (action_timeout_ms / 1000.0) + 60.0)
    t0 = time.monotonic()
    try:
        if parent_conn.poll(timeout_seconds):
            try:
                msg = parent_conn.recv()
            except EOFError:
                msg = None
        else:
            msg = None
    finally:
        try:
            parent_conn.close()
        except Exception:
            pass
    if msg is None:
        try:
            proc.terminate()
        except Exception:
            pass
        proc.join(timeout=5)
        raise RuntimeError(f"itdog timeout after {round(time.monotonic() - t0, 1)}s")
    proc.join(timeout=5)
    if msg.get("ok"):
        return msg["res"]
    raise RuntimeError(f"itdog failed: {msg.get('etype')}: {msg.get('error')}")

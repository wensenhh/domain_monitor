import re
import time
from typing import Any
from urllib.parse import urlparse


def _clean_url(value: str) -> str:
    s = "" if value is None else str(value).strip()
    if not s:
        return s
    if "://" not in s:
        return "http://" + s
    return s


def _parse_float_ms(text: str) -> float | None:
    s = "" if text is None else str(text).strip()
    if not s or s in {"-", "--"}:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except Exception:
        return None
    if "ms" in s.lower():
        return v / 1000.0
    return v


def _detect_real_proxy_ip(page, *, timeout_ms: int) -> str | None:
    try:
        page.goto("https://httpbin.org/ip", wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            raw = page.locator("pre").first.inner_text(timeout=timeout_ms)
        except Exception:
            raw = page.locator("body").first.inner_text(timeout=timeout_ms)
        m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", raw or "")
        if m:
            return m.group(1)
    except Exception:
        return None
    return None


def _extract_table(page) -> tuple[list[str], list[list[str]]]:
    return page.evaluate(
        """() => {
          const table = document.querySelector('#tbSort');
          if (!table) return {headers: [], rows: []};
          const headers = Array.from(table.querySelectorAll('thead th')).map(th => (th.innerText || '').trim());
          const rows = Array.from(table.querySelectorAll('tbody tr')).map(tr => {
            const tds = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').replace(/\\s+/g,' ').trim());
            return tds;
          }).filter(r => r.length > 0);
          return {headers, rows};
        }"""
    )


def _pick_cell(headers: list[str], row: list[str], keys: list[str]) -> str | None:
    for i, h in enumerate(headers):
        for k in keys:
            if k in h:
                if i < len(row):
                    v = (row[i] or "").strip()
                    return v or None
    return None


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
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError("playwright is not installed") from e

    base_url = (base_url or "https://tool.chinaz.com/speedtest/").strip()
    proxy_s = (proxy or "").strip()
    domain = _clean_url(domain)
    timings_ms: dict[str, float] = {}
    real_proxy_ip = None

    overall_t0 = time.monotonic()
    pw = None
    browser = None
    context = None
    page = None
    try:
        pw = sync_playwright().start()
        t0 = time.monotonic()
        browser = pw.chromium.launch(headless=headless, proxy={"server": proxy_s} if proxy_s else None)
        timings_ms["browser_launch_ms"] = (time.monotonic() - t0) * 1000.0
        t1 = time.monotonic()
        context = browser.new_context(locale="zh-CN")
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
        page.goto(base_url, wait_until="domcontentloaded")
        timings_ms["goto_ms"] = (time.monotonic() - t3) * 1000.0

        input_loc = page.locator("input#host, input[name=host], input[placeholder*='域名'], input[placeholder*='http']").first
        t4 = time.monotonic()
        input_loc.fill(domain)
        btn = page.locator(
            "input[type=button][value*='检测'], input[type=submit][value*='检测'], button:has-text('检测'), button:has-text('开始')"
        ).first
        btn.click()
        timings_ms["fill_and_click_ms"] = (time.monotonic() - t4) * 1000.0

        t5 = time.monotonic()
        last_ready = -1
        stable_ready = 0
        headers: list[str] = []
        rows: list[list[str]] = []
        deadline = time.monotonic() + max(90.0, (nav_timeout_ms / 1000.0) + 30.0)
        while time.monotonic() < deadline:
            obj = _extract_table(page)
            headers = obj.get("headers") or []
            rows = obj.get("rows") or []
            rows = [r for r in rows if r and r[0] and r[0] != "暂无数据"]

            ready = 0
            for r in rows:
                if len(r) >= 7:
                    status = (r[2] or "").strip()
                    if status and status not in {"--", "-"}:
                        ready += 1
            if ready == last_ready and ready > 0:
                stable_ready += 1
            else:
                stable_ready = 0
                last_ready = ready

            if len(rows) >= 20 and ready >= max(10, int(len(rows) * 0.2)) and stable_ready >= 3:
                break
            page.wait_for_timeout(1000)
        timings_ms["wait_table_ms"] = (time.monotonic() - t5) * 1000.0

        if not rows:
            raise RuntimeError("chinaz results empty")

        results: list[dict[str, Any]] = []
        for r in rows:
            detect_node_location = _pick_cell(headers, r, ["检测点", "监测点", "节点", "地区"])
            isp = _pick_cell(headers, r, ["运营商"])
            response_ip = _pick_cell(headers, r, ["响应IP", "解析IP"])
            ip_location = _pick_cell(headers, r, ["响应IP位置", "解析IP归属地", "归属地", "IP归属"])
            if response_ip:
                m = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3})(?:\s+(.*))?$", response_ip)
                if m:
                    response_ip = m.group(1)
                    if not ip_location:
                        ip_location = (m.group(2) or "").strip() or None
            status_code = _pick_cell(headers, r, ["状态码", "状态"])
            total_time = _parse_float_ms(_pick_cell(headers, r, ["总耗时", "总时间", "耗时"]) or "")
            dns_time = _parse_float_ms(_pick_cell(headers, r, ["DNS", "解析时间"]) or "")
            connect_time = _parse_float_ms(_pick_cell(headers, r, ["连接时间", "连接"]) or "")
            download_time = _parse_float_ms(_pick_cell(headers, r, ["下载时间", "下载"]) or "")

            results.append(
                {
                    "isp": isp,
                    "detect_node_location": detect_node_location,
                    "ip_location": ip_location,
                    "response_ip": response_ip,
                    "status_code": status_code,
                    "download_time": download_time,
                    "connect_time": connect_time,
                    "dns_time": dns_time,
                    "total_time": total_time,
                    "raw": {
                        "source": "chinaz_cs",
                        "headers": headers,
                        "row": r,
                        "proxy": proxy_s,
                        "real_proxy_ip": real_proxy_ip,
                        "base_url": base_url,
                    },
                }
            )

        timings_ms["total_platform_ms"] = (time.monotonic() - overall_t0) * 1000.0
        return results, {
            "real_proxy_ip": real_proxy_ip,
            "timings_ms": timings_ms,
            "chinaz_stats": {"rows": len(results)},
            "incomplete": False,
        }
    finally:
        t6 = time.monotonic()
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
        timings_ms["close_ms"] = (time.monotonic() - t6) * 1000.0

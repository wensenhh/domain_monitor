import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse


def _parse_float(text: str | None):
    if not text:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _safe_filename(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    return s[:200] if s else "domain"


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _pick_first_visible_locator(page, selectors: list[str]):
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            continue
    return None


def _extract_ip(text: str):
    m = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    return m.group(0) if m else None


def _detect_real_proxy_ip(page, timeout_ms: int):
    try:
        resp = page.request.get("https://httpbin.org/ip", timeout=timeout_ms)
        data = resp.json() if resp.ok else {}
        origin = str((data or {}).get("origin") or "").strip()
        if not origin:
            return None
        ip = origin.split(",")[0].strip()
        return ip or None
    except Exception:
        return None


def _is_suspicious_region(text: str | None) -> bool:
    if not text:
        return True
    s = text.strip()
    if not s:
        return True
    if len(s) >= 40:
        return True
    if re.fullmatch(r"[A-Za-z0-9._%\\-]{20,}", s or ""):
        return True
    if s.startswith("-") or ("=" in s) or ("%2F" in s) or ("/" in s and "google.com" not in s):
        return True
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", s):
        return True
    return False


def _split_location(text: str | None):
    if not text:
        return ("未知", "未知", "未知", "未知")
    parts = re.split(r"[·\s\-]", text.strip())
    parts = [p for p in parts if p]
    country = parts[0] if len(parts) > 0 else "未知"
    province = parts[1] if len(parts) > 1 else "未知"
    city = parts[2] if len(parts) > 2 else "未知"
    isp = parts[3] if len(parts) > 3 else "未知"
    return (country, province, city, isp)


def _wait_until_ready(page):
    done = False
    for _ in range(120):
        try:
            progress = page.locator("text=当前进度")
            if progress.count() > 0 and "100%" in progress.first.inner_text():
                done = True
                break
        except Exception:
            pass
        rows = page.locator("tr.node_tr")
        if rows.count() > 0:
            done = True
            break
        time.sleep(1)
    if not done:
        page.wait_for_selector("tr.node_tr", timeout=30000)


def _node_stats(page):
    return page.evaluate(
        r"""
        () => {
          const rows = Array.from(document.querySelectorAll('tr.node_tr'));
          let total = rows.length;
          let loading = 0;
          let http_final = 0;
          let http_non_placeholder = 0;
          for (const row of rows) {
            const node = row.getAttribute('node') || '';
            const real = node ? document.getElementById(`real_ip_${node}`) : null;
            const hover = node ? document.getElementById(`hover_button_${node}`) : null;
            const http = node ? document.getElementById(`http_code_${node}`) : null;

            const realLoading = !!(real && (real.querySelector('.spinner-border,.spinner-grow,.sr-only,[role=\"status\"]') || /loading/i.test(real.textContent || '')));
            let hoverHidden = false;
            if (hover) {
              const style = window.getComputedStyle(hover);
              hoverHidden = style && style.display === 'none';
            }
            if (realLoading || hoverHidden) loading += 1;

            const httpText = (http && http.textContent ? http.textContent : '').trim();
            if (httpText && httpText !== '--') http_non_placeholder += 1;
            if (/^\d{3}$/.test(httpText) || /(失败|错误|超时|无响应|未解析)/i.test(httpText)) http_final += 1;
          }
          return { total, loading, http_final, http_non_placeholder };
        }
        """
    )


def _stabilize_results(page):
    stable = 0
    last_total = -1
    last_loading = -1
    last_http_final = -1
    last_http_non_placeholder = -1
    final_stats = {"total": 0, "loading": 0, "http_final": 0, "http_non_placeholder": 0}
    for _ in range(90):
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(800)
        try:
            stats = _node_stats(page) or {}
        except Exception:
            stats = {}
        total = int(stats.get("total") or 0)
        loading = int(stats.get("loading") or 0)
        http_final = int(stats.get("http_final") or 0)
        http_non_placeholder = int(stats.get("http_non_placeholder") or 0)
        final_stats = {"total": total, "loading": loading, "http_final": http_final, "http_non_placeholder": http_non_placeholder}

        if (
            total == last_total
            and loading == last_loading
            and http_final == last_http_final
            and http_non_placeholder == last_http_non_placeholder
            and total > 0
        ):
            stable += 1
        else:
            stable = 0
        last_total = total
        last_loading = loading
        last_http_final = http_final
        last_http_non_placeholder = http_non_placeholder

        if total > 0 and loading == 0 and stable >= 3:
            break
    page.wait_for_timeout(800)
    incomplete = bool(final_stats.get("total")) and int(final_stats.get("loading") or 0) > 0
    final_stats["incomplete"] = incomplete
    return final_stats


def _click_sections(page):
    for label in ["全部节点", "中国地区", "海外地区"]:
        _try_click_section(page, label)


def _try_click_section(page, label: str, *, retries: int = 3) -> bool:
    loc = page.get_by_text(label, exact=False)
    if loc.count() == 0:
        return False
    for _ in range(max(retries, 1)):
        try:
            loc.first.scroll_into_view_if_needed(timeout=1000)
        except Exception:
            pass
        try:
            loc.first.click(timeout=5000, force=True)
            page.wait_for_timeout(800)
            return True
        except Exception:
            page.wait_for_timeout(500)
            continue
    return False


def _dedupe_results(results: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen = set()
    for r in results:
        raw = r.get("raw") if isinstance(r, dict) else None
        node_id = None
        if isinstance(raw, dict):
            html = raw.get("row_html")
            if isinstance(html, str):
                m = re.search(r'node="(\d+)"', html)
                node_id = m.group(1) if m else None
        key = (
            node_id,
            str(r.get("detect_node_location") or "").strip(),
            str(r.get("response_ip") or "").strip(),
            str(r.get("status_code") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _extract_rows(page, *, screenshot_path: str | None, base_url: str):
    results: list[dict] = []
    tables = page.locator("table")
    for i in range(tables.count()):
        tbl = tables.nth(i)
        headers: list[str] = []
        if tbl.locator("thead tr").count() > 0:
            headers = [h.strip() for h in tbl.locator("thead tr").nth(0).locator("th,td").all_text_contents()]
        else:
            if tbl.locator("tbody tr").count() > 0:
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
        if headers and tbl.locator("tbody tr").count() > 0:
            first_row_header_like = [t.strip().lower() for t in tbl.locator("tbody tr").nth(0).locator("th").all_text_contents()]
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
                    download_time = _parse_float(tx)
                elif "连接" in key:
                    connect_time = _parse_float(tx)
                elif "dns" in key or "解析" in key:
                    dns_time = _parse_float(tx)
                elif "总" in key:
                    total_time = _parse_float(tx)
                elif "状态" in key or "http" in key:
                    m = re.search(r"\b\d{3}\b|失败|错误|--", tx)
                    status_code = m.group(0) if m else tx.strip()
                elif "响应ip" in key or ("ip" in key and "响应" in key):
                    response_ip = _extract_ip(tx)
                elif ("ip" in key) and ("所在" in key or "地区" in key or "位置" in key):
                    ip_location = tx

            if (operator is None or region is None) and texts:
                first = texts[0]
                isp_match = re.search(r"(电信|联通|移动|广电|教育网)", first)
                if isp_match and operator is None:
                    operator = isp_match.group(1)
                if re.search(r"[\u4e00-\u9fff]", first):
                    region_guess = re.sub(r"(电信|联通|移动|广电|教育网)", "", first).strip()
                    if region is None and region_guess:
                        region = region_guess or region

            tokens = {"--", "失败", "错误", "超时", "无响应", "未解析", "timeout", "Timeout"}
            token_hit = any(t in text_join for t in tokens)
            has_times = any(v is not None for v in [download_time, connect_time, dns_time, total_time])
            has_status = status_code is not None
            has_identity = response_ip is not None or operator is not None or region is not None
            if not (has_times or has_status or has_identity or token_hit):
                continue

            if any(re.match(r"^HTTP/\d\.\d", t) for t in texts):
                continue
            if re.search(r"^\s*HTTP/\d\.\d", text_join):
                continue

            if not response_ip:
                response_ip = _extract_ip(text_join) or None
            if not ip_location and response_ip:
                m2 = re.search(rf"{re.escape(response_ip)}\s*(.+)", text_join)
                ip_location = m2.group(1).strip() if m2 else None

            if not response_ip:
                m_ip = re.search(r'id="real_ip_\d+".*?>(\d{1,3}(?:\.\d{1,3}){3})<', raw_html, flags=re.S)
                response_ip = m_ip.group(1) if m_ip else response_ip

            if not ip_location or ip_location == "--":
                m_loc = re.search(r'<td[^>]*class="ip_address[^"]*"[^>]*title="([^"]+)"', raw_html)
                ip_location = m_loc.group(1).strip() if m_loc else ip_location

            if not status_code or status_code == "--":
                m_sc = re.search(r'id="http_code_\d+"\s*>\s*(\d{3})\s*<', raw_html)
                status_code = m_sc.group(1) if m_sc else status_code

            if total_time is None:
                m_tt = re.search(r'id="all_time_\d+".*?>(?:<[^>]+>)*\s*([0-9]+(?:\.[0-9]+)?)\s*s', raw_html, flags=re.S)
                total_time = _parse_float(m_tt.group(1)) if m_tt else total_time
            if dns_time is None:
                m_dt = re.search(r'id="dns_time_\d+".*?>\s*([0-9]+(?:\.[0-9]+)?)\s*s', raw_html, flags=re.S)
                dns_time = _parse_float(m_dt.group(1)) if m_dt else dns_time
            if connect_time is None:
                m_ct = re.search(r'id="connect_time_\d+".*?>\s*([0-9]+(?:\.[0-9]+)?)\s*s', raw_html, flags=re.S)
                connect_time = _parse_float(m_ct.group(1)) if m_ct else connect_time
            if download_time is None:
                m_dl = re.search(r'id="download_time_\d+".*?>\s*([0-9]+(?:\.[0-9]+)?)\s*s', raw_html, flags=re.S)
                download_time = _parse_float(m_dl.group(1)) if m_dl else download_time

            if _is_suspicious_region(region):
                cleaned = None
                if ip_location:
                    cleaned = ip_location.replace("/google.com", "").replace("/", " ").strip()
                region = cleaned or None

            country, province, city, isp2 = _split_location(ip_location)

            detect_node_location = ""
            if region:
                detect_node_location += region.strip()
            if operator:
                detect_node_location += operator.strip()
            detect_node_location = detect_node_location or (texts[0] if texts else "")
            detect_node_location = detect_node_location[:255] if detect_node_location else None

            isp_value = (operator or "").strip() or (isp2 if isp2 != "未知" else None)
            results.append(
                {
                    "isp": isp_value,
                    "detect_node_location": detect_node_location,
                    "download_time": download_time,
                    "connect_time": connect_time,
                    "dns_time": dns_time,
                    "total_time": total_time,
                    "status_code": status_code,
                    "ip_location": ip_location,
                    "response_ip": response_ip,
                    "raw": {
                        "source": "itdog_http",
                        "base_url": base_url,
                        "screenshot": screenshot_path,
                        "operator": operator,
                        "region": region,
                        "ip_country": country,
                        "ip_province": province,
                        "ip_city": city,
                        "ip_isp": isp2,
                        "row_html": raw_html,
                        "texts": texts,
                    },
                }
            )

    uniq: list[dict] = []
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

    if not uniq:
        raise RuntimeError(f"itdog results empty, screenshot={screenshot_path or ''}")

    return uniq


def run_itdog_http(
    domain: str,
    *,
    base_url: str,
    proxy: str,
    headless: bool,
    screenshot_enabled: bool,
    screenshot_dir: str,
    nav_timeout_ms: int,
    action_timeout_ms: int,
):
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError("playwright is not installed") from e

    base_url = (base_url or "https://www.itdog.cn/http/").strip()
    screenshot_path = None
    domain = domain.strip()
    real_proxy_ip = None
    timings_ms = {}

    pw = None
    browser = None
    context = None
    page = None
    proxy_cfg = {"server": proxy} if proxy else None
    overall_t0 = time.monotonic()
    try:
        pw = sync_playwright().start()
        t0 = time.monotonic()
        browser = pw.chromium.launch(headless=headless, proxy=proxy_cfg)
        timings_ms["browser_launch_ms"] = (time.monotonic() - t0) * 1000.0
        t1 = time.monotonic()
        context = browser.new_context(locale="zh-CN")
        timings_ms["context_create_ms"] = (time.monotonic() - t1) * 1000.0
        if screenshot_enabled:
            blocked_types = {"image", "media"}
        else:
            blocked_types = {"image", "media", "font", "stylesheet"}
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

        if proxy:
            t2 = time.monotonic()
            real_proxy_ip = _detect_real_proxy_ip(page, timeout_ms=nav_timeout_ms)
            timings_ms["proxy_detect_ms"] = (time.monotonic() - t2) * 1000.0

        try:
            t3 = time.monotonic()
            page.goto(base_url, wait_until="domcontentloaded")
            timings_ms["goto_ms"] = (time.monotonic() - t3) * 1000.0
        except Exception as e:
            raise RuntimeError(f"goto itdog page {base_url} failed: {e}") from e

        input_locator = page.locator(
            'input[name="url"], input#url, input[placeholder*="http"], input[placeholder*="域名"], input'
        )
        t4 = time.monotonic()
        input_locator.first.fill(domain)

        btn = page.locator("button[onclick*=\"check_form('fast')\"]")
        if btn.count() == 0:
            btn = page.get_by_role("button", name=re.compile("检测|开始|测速|测试"))
            if btn.count() > 1:
                btn = btn.nth(0)
            elif btn.count() == 0:
                btn = page.locator("button, .btn, .button").first
        btn.click()
        timings_ms["fill_and_click_ms"] = (time.monotonic() - t4) * 1000.0

        try:
            t5 = time.monotonic()
            _wait_until_ready(page)
            timings_ms["wait_ready_ms"] = (time.monotonic() - t5) * 1000.0
        except PlaywrightTimeoutError as e:
            raise RuntimeError(f"waiting itdog page {base_url} timeout: {e}") from e

        t6 = time.monotonic()
        _click_sections(page)
        stable_stats = _stabilize_results(page)
        results = _extract_rows(page, screenshot_path=None, base_url=base_url)
        if len(results) < 120:
            if _try_click_section(page, "全部节点", retries=5):
                stable_stats = _stabilize_results(page)
                results2 = _extract_rows(page, screenshot_path=None, base_url=base_url)
                if len(results2) > len(results):
                    results = results2
        if len(results) < 120:
            merged = list(results)
            if _try_click_section(page, "中国地区", retries=3):
                stable_stats = _stabilize_results(page)
                merged.extend(_extract_rows(page, screenshot_path=None, base_url=base_url))
            if _try_click_section(page, "海外地区", retries=3):
                stable_stats = _stabilize_results(page)
                merged.extend(_extract_rows(page, screenshot_path=None, base_url=base_url))
            results = _dedupe_results(merged)
        timings_ms["stabilize_ms"] = (time.monotonic() - t6) * 1000.0

        if screenshot_enabled:
            _ensure_dir(screenshot_dir)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"itdog_{_safe_filename(domain)}_{ts}.png"
            screenshot_path = os.path.join(screenshot_dir, filename)
            try:
                page.evaluate(
                    """
                    async () => {
                      let last = 0;
                      for (let i=0;i<20;i++) {
                        window.scrollTo(0, document.body.scrollHeight);
                        await new Promise(r => setTimeout(r, 400));
                        const h = document.body.scrollHeight;
                        if (Math.abs(h - last) < 5) break;
                        last = h;
                      }
                    }
                    """
                )
            except Exception:
                pass
            t7 = time.monotonic()
            page.screenshot(path=screenshot_path, full_page=True)
            timings_ms["screenshot_ms"] = (time.monotonic() - t7) * 1000.0

        t8 = time.monotonic()
        results = _dedupe_results(results)
        for r in results:
            raw = r.get("raw")
            if isinstance(raw, dict):
                raw["screenshot"] = screenshot_path
                raw["itdog_stats"] = stable_stats
        results = _dedupe_results(results)
        timings_ms["extract_rows_ms"] = (time.monotonic() - t8) * 1000.0
        for r in results:
            raw = r.get("raw")
            if isinstance(raw, dict):
                raw["real_proxy_ip"] = real_proxy_ip
            else:
                r["raw"] = {"real_proxy_ip": real_proxy_ip}

        incomplete = bool((stable_stats or {}).get("incomplete"))
        return results, {
            "real_proxy_ip": real_proxy_ip,
            "screenshot": screenshot_path,
            "timings_ms": timings_ms,
            "itdog_stats": stable_stats,
            "incomplete": incomplete,
        }
    finally:
        t9 = time.monotonic()
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
        timings_ms["close_ms"] = (time.monotonic() - t9) * 1000.0
        timings_ms["total_platform_ms"] = (time.monotonic() - overall_t0) * 1000.0

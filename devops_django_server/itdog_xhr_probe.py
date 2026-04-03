import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_str(v: Any, limit: int = 400) -> str:
    s = "" if v is None else str(v)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        return s[:limit] + "…"
    return s


def _mask_header_value(name: str, value: str) -> str:
    lname = name.lower()
    if lname in {"cookie", "authorization"}:
        return f"<redacted len={len(value)}>"
    if lname.startswith("x-") and "token" in lname:
        return f"<redacted len={len(value)}>"
    return value


def _pick_first_visible_locator(page, selectors: list[str]):
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            continue
    return None


def _is_itdog_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc
    except Exception:
        return False
    return "itdog.cn" in host


def _extract_signal(body_text: str) -> dict:
    s = body_text
    return {
        "has_node_tr": bool(re.search(r'node_tr"|node_tr\\b', s)),
        "has_node_attr": bool(re.search(r'node="\\d+"', s)),
        "has_http_code_id": bool(re.search(r"http_code_\\d+", s)),
        "has_task_id": bool(re.search(r"(task|report|result)[_-]?id", s, flags=re.I)),
        "has_uuid": bool(re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s, flags=re.I)),
        "has_json_brace": "{" in s and "}" in s,
    }


@dataclass
class XhrRow:
    ts: str
    resource_type: str
    method: str
    url: str
    request_headers: dict
    post_data: str | None
    status: int | None
    response_headers: dict | None
    content_type: str | None
    body_preview: str | None
    signal: dict | None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--base-url", default="https://www.itdog.cn/http/")
    ap.add_argument("--proxy", default=os.environ.get("DEFAULT_PROXY", ""))
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--no-headless", action="store_false", dest="headless")
    ap.add_argument("--timeout-ms", type=int, default=60000)
    ap.add_argument("--observe-seconds", type=int, default=25)
    ap.add_argument("--max-body", type=int, default=600)
    ap.add_argument("--itdog-only", action="store_true", default=True)
    ap.add_argument("--all-hosts", action="store_false", dest="itdog_only")
    ap.add_argument("--include-document", action="store_true", default=True)
    ap.add_argument("--no-document", action="store_false", dest="include_document")
    ap.add_argument("--include-script", action="store_true", default=True)
    ap.add_argument("--no-script", action="store_false", dest="include_script")
    ap.add_argument("--capture-ws-frames", action="store_true", default=True)
    ap.add_argument("--no-capture-ws-frames", action="store_false", dest="capture_ws_frames")
    ap.add_argument("--max-ws-frames", type=int, default=40)
    ap.add_argument("--summary", action="store_true", default=False)
    args = ap.parse_args()

    rows: list[XhrRow] = []
    seen_req_keys: set[tuple[str, str, str]] = set()
    ws_log: list[dict] = []
    ws_frames: list[dict] = []
    console_msgs: list[dict] = []
    page_errors: list[dict] = []
    discovered: dict[str, Any] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context_kwargs: dict[str, Any] = {}
        if args.proxy:
            context_kwargs["proxy"] = {"server": args.proxy}
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.set_default_timeout(args.timeout_ms)

        if args.capture_ws_frames:
            page.add_init_script(
                r"""
                (() => {
                  const NativeWebSocket = window.WebSocket;
                  if (!NativeWebSocket) return;
                  try { console.log('__INIT_WS_PATCH__'); } catch (e) {}
                  const maxLen = 2000;
                  function preview(data) {
                    try {
                      if (data == null) return { kind: 'null', preview: '' };
                      if (typeof data === 'string') {
                        return { kind: 'string', preview: data.slice(0, maxLen), length: data.length };
                      }
                      if (data instanceof ArrayBuffer) {
                        const u8 = new Uint8Array(data);
                        const head = Array.from(u8.slice(0, 16)).map(b => b.toString(16).padStart(2,'0')).join('');
                        return { kind: 'arraybuffer', length: u8.length, head_hex: head };
                      }
                      if (data && data.constructor && data.constructor.name === 'Blob') {
                        return { kind: 'blob', size: data.size, type: data.type || '' };
                      }
                      const s = JSON.stringify(data);
                      return { kind: 'json', preview: (s || '').slice(0, maxLen), length: (s || '').length };
                    } catch (e) {
                      return { kind: 'unknown', preview: '' };
                    }
                  }
                  function log(prefix, url, payload) {
                    try {
                      const obj = preview(payload);
                      console.log(prefix + ' ' + url + ' ' + JSON.stringify(obj));
                    } catch (e) {}
                  }
                  function logEvent(prefix, url, payload) {
                    try {
                      console.log(prefix + ' ' + url + ' ' + JSON.stringify(payload || {}));
                    } catch (e) {}
                  }
                  window.WebSocket = function(url, protocols) {
                    try { console.log('__WS_EVENT__ ' + url + ' ' + JSON.stringify({event:'construct'})); } catch (e) {}
                    const ws = protocols ? new NativeWebSocket(url, protocols) : new NativeWebSocket(url);
                    const u = url;
                    ws.addEventListener('open', () => logEvent('__WS_EVENT__', u, {event:'open'}));
                    ws.addEventListener('close', (ev) => logEvent('__WS_EVENT__', u, {event:'close', code: ev.code, reason: ev.reason, wasClean: ev.wasClean}));
                    ws.addEventListener('error', () => logEvent('__WS_EVENT__', u, {event:'error'}));
                    ws.addEventListener('message', (ev) => log('__WS_RECV__', u, ev.data));
                    const origSend = ws.send;
                    ws.send = function(data) {
                      log('__WS_SEND__', u, data);
                      return origSend.call(ws, data);
                    };
                    return ws;
                  };
                  window.WebSocket.prototype = NativeWebSocket.prototype;
                })();
                """
            )

        def on_console(msg):
            try:
                if len(console_msgs) < 30:
                    console_msgs.append({"ts": _now_ts(), "type": msg.type, "text": (msg.text or "")[:500]})
                if len(ws_frames) >= args.max_ws_frames:
                    return
                text = msg.text or ""
                if text.startswith("__WS_SEND__") or text.startswith("__WS_RECV__"):
                    direction = "send" if text.startswith("__WS_SEND__") else "recv"
                    rest = text.split(" ", 2)
                    url = rest[1] if len(rest) > 1 else None
                    payload = None
                    if len(rest) > 2:
                        try:
                            payload = json.loads(rest[2])
                        except Exception:
                            payload = {"raw": rest[2][:500]}
                    ws_frames.append({"ts": _now_ts(), "direction": direction, "url": url, "payload": payload})
                elif text.startswith("__WS_EVENT__"):
                    rest = text.split(" ", 2)
                    url = rest[1] if len(rest) > 1 else None
                    payload = None
                    if len(rest) > 2:
                        try:
                            payload = json.loads(rest[2])
                        except Exception:
                            payload = {"raw": rest[2][:500]}
                    ws_frames.append({"ts": _now_ts(), "direction": "event", "url": url, "payload": payload})
            except Exception:
                return

        page.on("console", on_console)
        page.on("pageerror", lambda exc: page_errors.append({"ts": _now_ts(), "message": _safe_str(exc, limit=500)}))

        def on_websocket(ws):
            try:
                url = ws.url
            except Exception:
                url = None
            item: dict[str, Any] = {"ts": _now_ts(), "url": url, "frames_sent": 0, "frames_received": 0}

            def on_sent(frame):
                item["frames_sent"] += 1

            def on_recv(frame):
                item["frames_received"] += 1

            try:
                ws.on("framesent", on_sent)
                ws.on("framereceived", on_recv)
            except Exception:
                pass
            ws_log.append(item)

        def record_request(req):
            try:
                rtype = req.resource_type
                url = req.url
                if args.itdog_only and not _is_itdog_url(url):
                    return
                if rtype not in {"xhr", "fetch", "other"} and not re.search(r"/(batch_http|http_ipv6)/", url, flags=re.I):
                    return
                key = (rtype, req.method, url)
                if key in seen_req_keys:
                    return
                seen_req_keys.add(key)
                req_headers = {k: _mask_header_value(k, v) for k, v in (req.all_headers() or {}).items()}
                post_data = req.post_data
                rows.append(
                    XhrRow(
                        ts=_now_ts(),
                        resource_type=rtype,
                        method=req.method,
                        url=url,
                        request_headers=req_headers,
                        post_data=_safe_str(post_data, limit=300) if post_data else None,
                        status=None,
                        response_headers=None,
                        content_type=None,
                        body_preview=None,
                        signal=None,
                    )
                )
            except Exception:
                return

        def on_response(resp):
            try:
                req = resp.request
                rtype = req.resource_type
                allowed = {"xhr", "fetch"}
                if args.include_document:
                    allowed.add("document")
                if args.include_script:
                    allowed.add("script")
                if rtype not in allowed:
                    return
                url = req.url
                if args.itdog_only and not _is_itdog_url(url):
                    return
                seen_req_keys.add((rtype, req.method, url))
                req_headers = {k: _mask_header_value(k, v) for k, v in (req.all_headers() or {}).items()}
                post_data = req.post_data
                status = resp.status
                resp_headers = resp.all_headers()
                content_type = None
                if isinstance(resp_headers, dict):
                    content_type = resp_headers.get("content-type") or resp_headers.get("Content-Type")
                body_preview = None
                signal = None
                want_body = False
                if rtype in {"xhr", "fetch"}:
                    want_body = True
                if rtype == "document" and req.method.upper() == "POST":
                    want_body = True
                if content_type and "json" in content_type.lower():
                    want_body = True
                if re.search(r"/(api|ajax)/", url, flags=re.I):
                    want_body = True
                if rtype == "script" and re.search(r"(api|ajax|json|data)", url, flags=re.I):
                    want_body = True
                if want_body:
                    try:
                        text = resp.text()
                        body_preview = _safe_str(text, limit=args.max_body)
                        signal = _extract_signal(text)
                    except Exception:
                        body_preview = None
                        signal = None
                rows.append(
                    XhrRow(
                        ts=_now_ts(),
                        resource_type=rtype,
                        method=req.method,
                        url=url,
                        request_headers=req_headers,
                        post_data=_safe_str(post_data, limit=300) if post_data else None,
                        status=status,
                        response_headers=resp_headers if isinstance(resp_headers, dict) else None,
                        content_type=content_type,
                        body_preview=body_preview,
                        signal=signal,
                    )
                )
            except Exception:
                return

        page.on("request", record_request)
        page.on("response", on_response)
        page.on("websocket", on_websocket)

        page.goto(args.base_url, wait_until="domcontentloaded")
        input_locator = _pick_first_visible_locator(
            page,
            [
                'input[name="url"]',
                "input#url",
                'input[placeholder*="http"]',
                'input[placeholder*="域名"]',
                "input",
            ],
        )
        if not input_locator:
            print("cannot find input", file=sys.stderr)
            return 2
        input_locator.fill(args.domain)

        btn = _pick_first_visible_locator(page, ['button[onclick*="check_form"]', "button", ".btn", ".button"])
        if not btn:
            print("cannot find button", file=sys.stderr)
            return 2
        btn.click()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=args.timeout_ms)
        except Exception:
            pass

        t0 = time.time()
        while time.time() - t0 < args.observe_seconds:
            time.sleep(1)

        try:
            runtime = page.evaluate(
                r"""
                () => {
                  const out = {};
                  out.has_create_websocket_fn = typeof create_websocket === 'function';
                  out.ws_typeof = typeof ws;
                  try {
                    out.ws_readyState = ws && typeof ws.readyState === 'number' ? ws.readyState : null;
                    out.ws_url = ws && typeof ws.url === 'string' ? ws.url : null;
                  } catch (e) {
                    out.ws_readyState = null;
                    out.ws_url = null;
                  }
                  out.WebSocket_typeof = typeof WebSocket;
                  return out;
                }
                """
            )
            if isinstance(runtime, dict):
                discovered["runtime"] = runtime
        except Exception:
            pass

        post_html = None
        for r in reversed(rows):
            if r.resource_type == "document" and r.method.upper() == "POST" and r.body_preview:
                post_html = r.body_preview
                break
        if post_html:
            m_ws = re.search(r"wss_url\s*=\s*['\"]([^'\"]+)['\"]", post_html)
            m_task = re.search(r"task_id\s*=\s*['\"]([^'\"]+)['\"]", post_html)
            m_mode = re.search(r"check_mode\s*=\s*['\"]([^'\"]+)['\"]", post_html)
            if m_ws:
                discovered["wss_url"] = m_ws.group(1)
            if m_task:
                discovered["task_id"] = m_task.group(1)
            if m_mode:
                discovered["check_mode"] = m_mode.group(1)
            discovered["has_create_websocket"] = "create_websocket" in post_html

        out: dict[str, Any] = {
            "domain": args.domain,
            "base_url": args.base_url,
            "proxy": args.proxy or None,
            "headless": args.headless,
            "observe_seconds": args.observe_seconds,
            "captured": len(rows),
            "websockets": ws_log,
            "ws_frames": ws_frames,
            "console_msgs": console_msgs,
            "page_errors": page_errors,
            "discovered": discovered,
        }
        if not args.summary:
            out["rows"] = [r.__dict__ for r in rows]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import base64
import json
import os
import re
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener


def _clean_url(value: str) -> str:
    s = "" if value is None else str(value).strip()
    if not s:
        return s
    if "://" not in s:
        return "http://" + s
    return s


def _strip_www(url: str) -> str:
    s = (url or "").strip()
    if not s:
        return s
    if "://" not in s:
        return s[4:] if s.lower().startswith("www.") else s
    u = urlparse(s)
    host = (u.hostname or "").strip()
    if host.lower().startswith("www."):
        host = host[4:]
        port = f":{u.port}" if u.port else ""
        path = u.path or ""
        query = f"?{u.query}" if u.query else ""
        return f"{u.scheme}://{host}{port}{path}{query}"
    return s


def _open(req: Request, *, timeout: int, proxy_url: str | None):
    if proxy_url:
        opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    else:
        opener = build_opener(ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def _http_get_proxy(url: str, *, headers: dict[str, str] | None, timeout: int, proxy_url: str | None) -> tuple[int, dict[str, str], bytes]:
    req = Request(url, method="GET", headers=headers or {})
    with _open(req, timeout=timeout, proxy_url=proxy_url) as resp:
        status = getattr(resp, "status", 200)
        hdrs = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
        body = resp.read()
        return status, hdrs, body


def _http_post_form_proxy(
    url: str, *, data: dict[str, Any], headers: dict[str, str] | None, timeout: int, proxy_url: str | None
) -> tuple[int, dict[str, str], bytes]:
    norm: dict[str, Any] = {}
    for k, v in (data or {}).items():
        norm[k] = "" if v is None else v
    payload = urlencode(norm, doseq=True).encode("utf-8")
    h = {"content-type": "application/x-www-form-urlencoded; charset=UTF-8", "content-length": str(len(payload))}
    if headers:
        h.update(headers)
    req = Request(url, data=payload, method="POST", headers=h)
    with _open(req, timeout=timeout, proxy_url=proxy_url) as resp:
        status = getattr(resp, "status", 200)
        hdrs = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
        body = resp.read()
        return status, hdrs, body


def _decode_text(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except Exception:
        return body.decode("utf-8", errors="ignore")


def _detect_real_proxy_ip(timeout_ms: int, *, proxy_url: str | None) -> str | None:
    timeout = max(5, int(timeout_ms / 1000))
    try:
        req = Request("https://httpbin.org/ip", method="GET", headers={"user-agent": "vp-monitor-17ce"})
        with _open(req, timeout=timeout, proxy_url=proxy_url) as resp:
            if getattr(resp, "status", 200) >= 400:
                return None
            data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
        origin = str((data or {}).get("origin") or "").strip()
        if not origin:
            return None
        ip = origin.split(",")[0].strip()
        return ip or None
    except Exception:
        return None


def _parse_single_attr_value(tag: str, attr: str) -> str | None:
    m = re.search(rf"{re.escape(attr)}\\s*=\\s*\"([^\"]*)\"", tag, flags=re.I)
    if m:
        return m.group(1)
    m = re.search(rf"{re.escape(attr)}\\s*=\\s*'([^']*)'", tag, flags=re.I)
    if m:
        return m.group(1)
    m = re.search(rf"{re.escape(attr)}\\s*=\\s*([^\\s>]+)", tag, flags=re.I)
    if m:
        return m.group(1).strip()
    return None


def _parse_checked_values(html: str, *, name: str) -> list[int]:
    out: list[int] = []
    for tag in re.findall(rf"<input\\b[^>]*\\bname=['\\\"]{re.escape(name)}['\\\"][^>]*>", html, flags=re.I):
        if "checked" not in tag.lower():
            continue
        v = _parse_single_attr_value(tag, "value")
        if v is None:
            continue
        try:
            out.append(int(str(v).strip()))
        except Exception:
            continue
    return out


def _parse_checked_radio_label(html: str, *, name: str) -> str | None:
    m = re.search(
        rf"<label>\\s*<input\\b[^>]*\\bname=['\\\"]{re.escape(name)}['\\\"][^>]*\\bchecked\\b[^>]*>\\s*([^<\\s][^<]*)</label>",
        html,
        flags=re.I,
    )
    if not m:
        return None
    v = (m.group(1) or "").strip()
    return v or None


def _parse_hidden_value(html: str, *, element_id: str) -> str | None:
    m = re.search(rf"<input\\b[^>]*\\bid=['\\\"]{re.escape(element_id)}['\\\"][^>]*>", html, flags=re.I)
    if not m:
        return None
    return _parse_single_attr_value(m.group(0), "value")


def _extract_common_js_url(html: str, *, base_url: str) -> str | None:
    m = re.search(r'<script\\b[^>]*\\bsrc="([^"]*?/smedia/js/common\\.js[^"]*)"', html, flags=re.I)
    if not m:
        m = re.search(r"<script\\b[^>]*\\bsrc='([^']*?/smedia/js/common\\.js[^']*)'", html, flags=re.I)
    if not m:
        return None
    src = (m.group(1) or "").strip()
    if not src:
        return None
    if src.startswith("http://") or src.startswith("https://"):
        return src
    base = (base_url or "https://17ce.com/get").strip() or "https://17ce.com/get"
    return "https://" + (urlparse(base).hostname or "17ce.com") + src


def _extract_pro_ids_from_common_js(js: str) -> list[int] | None:
    m_fn = re.search(r"function\\s+getPostdata\\s*\\(", js)
    start = m_fn.start() if m_fn else 0
    snippet = js[start : start + 12000]
    m = re.search(r"pro_ids\\s*:\\s*\\[([^\\]]+)\\]", snippet)
    if not m:
        return None
    raw = m.group(1) or ""
    vals = re.findall(r"\\b\\d+\\b", raw)
    out: list[int] = []
    for v in vals:
        try:
            out.append(int(v))
        except Exception:
            continue
    return out or None


def _get_full_ips(node_ids: list[int], *, referer: str, timeout_s: int, proxy_url: str | None) -> dict[int, dict[str, Any]]:
    if not node_ids:
        return {}
    st, _h, body = _http_post_form_proxy(
        "https://17ce.com/site/getFullIps",
        data={"nodes": node_ids},
        headers={"user-agent": "vp-monitor-17ce", "referer": referer, "x-requested-with": "XMLHttpRequest"},
        timeout=timeout_s,
        proxy_url=proxy_url,
    )
    if st >= 400:
        return {}
    payload = json.loads(_decode_text(body) or "{}")
    if not (payload or {}).get("rt"):
        return {}
    fullips = (payload or {}).get("fullips") or (payload or {}).get("data", {}).get("fullips") or []
    out: dict[int, dict[str, Any]] = {}
    for item in fullips:
        if not isinstance(item, dict):
            continue
        sid_raw = item.get("sid")
        try:
            sid = int(sid_raw)
        except Exception:
            continue
        out[sid] = item
    return out


class WsClient:
    def __init__(self, wss_url: str, *, origin: str, proxy: str | None):
        u = urlparse(wss_url)
        self.host = u.hostname or ""
        self.port = u.port or 443
        self.path = (u.path or "/") + (("?" + u.query) if u.query else "")
        self.origin = origin
        self.proxy = proxy
        self.sock: ssl.SSLSocket | None = None

    def connect(self, *, timeout: int):
        key_b64 = base64.b64encode(os.urandom(16)).decode("ascii")
        ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        raw: socket.socket
        if self.proxy:
            p = urlparse(self.proxy)
            phost = p.hostname or ""
            pport = p.port or (80 if (p.scheme or "http").startswith("http") else 1080)
            raw = socket.create_connection((phost, pport), timeout=timeout)
            req = (
                f"CONNECT {self.host}:{self.port} HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                "Proxy-Connection: Keep-Alive\r\n"
                "\r\n"
            ).encode("utf-8")
            raw.sendall(req)
            resp = b""
            raw.settimeout(max(5, timeout))
            while b"\r\n\r\n" not in resp and len(resp) < 65536:
                chunk = raw.recv(4096)
                if not chunk:
                    break
                resp += chunk
            if b" 200 " not in resp.split(b"\r\n", 1)[0]:
                raise RuntimeError("proxy CONNECT failed")
        else:
            raw = socket.create_connection((self.host, self.port), timeout=timeout)
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(raw, server_hostname=self.host)
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Pragma: no-cache\r\n"
            "Cache-Control: no-cache\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key_b64}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: {self.origin}\r\n"
            f"User-Agent: {ua}\r\n"
            "\r\n"
        ).encode("utf-8")
        tls.sendall(req)
        resp = self._read_http_response(tls, timeout=timeout)
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError("websocket upgrade failed")
        self.sock = tls

    def close(self):
        if not self.sock:
            return
        try:
            self._send_frame(opcode=0x8, payload=b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None

    def _read_http_response(self, sock_obj: ssl.SSLSocket, *, timeout: int) -> bytes:
        data = b""
        sock_obj.settimeout(max(5, timeout))
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = sock_obj.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def _recv_exact(self, n: int) -> bytes:
        if not self.sock:
            raise RuntimeError("not connected")
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("socket closed")
            buf += chunk
        return buf

    def _send_frame(self, *, opcode: int, payload: bytes):
        if not self.sock:
            raise RuntimeError("not connected")
        fin_opcode = 0x80 | (opcode & 0x0F)
        mask_bit = 0x80
        length = len(payload)
        header = bytearray()
        header.append(fin_opcode)
        if length < 126:
            header.append(mask_bit | length)
        elif length < (1 << 16):
            header.append(mask_bit | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(mask_bit | 127)
            header.extend(length.to_bytes(8, "big"))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_frame(self) -> tuple[int | None, bytes | None]:
        if not self.sock:
            raise RuntimeError("not connected")
        try:
            b1b2 = self._recv_exact(2)
        except socket.timeout:
            return None, None
        b1, b2 = b1b2[0], b1b2[1]
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(self._recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._recv_exact(8), "big")
        mask = b""
        if masked:
            mask = self._recv_exact(4)
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def send_json(self, obj: dict[str, Any]):
        s = json.dumps(obj, ensure_ascii=False)
        self._send_frame(opcode=0x1, payload=s.encode("utf-8"))

    def recv_json(self, *, timeout_seconds: float) -> dict[str, Any] | None:
        if not self.sock:
            raise RuntimeError("not connected")
        end = time.time() + max(0.1, timeout_seconds)
        while time.time() < end:
            self.sock.settimeout(max(0.2, min(5.0, end - time.time())))
            opcode, payload = self._recv_frame()
            if opcode is None:
                continue
            if opcode == 0x9:
                self._send_frame(opcode=0xA, payload=payload or b"")
                continue
            if opcode == 0x8:
                return None
            if opcode != 0x1:
                continue
            try:
                text = (payload or b"").decode("utf-8", errors="replace")
                obj = json.loads(text)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
        return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return x if x >= 0 else None
    s = str(v).strip()
    if not s:
        return None
    m = re.search(r"([0-9]+(?:\\.[0-9]+)?)", s)
    if not m:
        return None
    try:
        x = float(m.group(1))
    except Exception:
        return None
    return x if x >= 0 else None


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
    timings_ms: dict[str, float] = {}
    overall_t0 = time.monotonic()
    proxy_s = (proxy or "").strip()
    base = (base_url or "https://17ce.com/get").strip() or "https://17ce.com/get"
    url_in = _clean_url(domain)
    real_proxy_ip = None
    if proxy_s:
        t0 = time.monotonic()
        real_proxy_ip = _detect_real_proxy_ip(nav_timeout_ms, proxy_url=proxy_s)
        timings_ms["proxy_detect_ms"] = (time.monotonic() - t0) * 1000.0

    timeout_s = max(5, int(nav_timeout_ms / 1000))
    t1 = time.monotonic()
    status, _hdrs, body = _http_get_proxy(
        base,
        headers={"user-agent": "vp-monitor-17ce", "accept": "text/html"},
        timeout=timeout_s,
        proxy_url=proxy_s or None,
    )
    if status >= 400:
        raise RuntimeError(f"17ce page fetch failed: {status}")
    html = _decode_text(body)
    timings_ms["fetch_page_ms"] = (time.monotonic() - t1) * 1000.0

    t_val = (_parse_hidden_value(html, element_id="t") or "http").strip().lower() or "http"
    request_method = (_parse_checked_radio_label(html, name="rt") or "GET").strip().upper() or "GET"
    followlocation_raw = (_parse_hidden_value(html, element_id="followlocation") or "3").strip()
    try:
        followlocation = int(followlocation_raw)
    except Exception:
        followlocation = 3

    isps = _parse_checked_values(html, name="isp")
    areas = _parse_checked_values(html, name="area")
    nodetype = [v for v in _parse_checked_values(html, name="ntype") if v != 0]
    if not nodetype:
        nodetype = [1, 2]
    if not isps:
        isps = [0, 1, 2, 6, 7, 8, 17, 18, 19, 3, 4]
    if not areas:
        areas = [0, 1, 2, 3]

    pro_ids: list[int] | None = None
    common_url = _extract_common_js_url(html, base_url=base)
    if common_url:
        try:
            t2 = time.monotonic()
            st2, _h2, js_body = _http_get_proxy(
                common_url,
                headers={"user-agent": "vp-monitor-17ce", "accept": "application/javascript"},
                timeout=timeout_s,
                proxy_url=proxy_s or None,
            )
            if st2 < 400:
                pro_ids = _extract_pro_ids_from_common_js(_decode_text(js_body))
            timings_ms["fetch_common_js_ms"] = (time.monotonic() - t2) * 1000.0
        except Exception:
            pro_ids = None
    if not pro_ids:
        pro_ids = [
            12,
            49,
            79,
            80,
            180,
            183,
            184,
            188,
            189,
            190,
            192,
            193,
            194,
            195,
            196,
            221,
            227,
            235,
            236,
            238,
            241,
            243,
            250,
            346,
            349,
            350,
            351,
            353,
            354,
            355,
            356,
            357,
            239,
            352,
            3,
            5,
            8,
            18,
            27,
            42,
            43,
            46,
            47,
            51,
            56,
            85,
        ]

    def _checkuser(url_value: str) -> dict[str, Any]:
        st, _h, body = _http_post_form_proxy(
            "https://17ce.com/site/checkuser",
            data={"url": url_value, "type": t_val, "isp": 0},
            headers={"user-agent": "vp-monitor-17ce", "referer": base, "x-requested-with": "XMLHttpRequest"},
            timeout=timeout_s,
            proxy_url=proxy_s or None,
        )
        if st >= 400:
            raise RuntimeError(f"17ce checkuser failed: {st}")
        return json.loads(_decode_text(body) or "{}")

    t3 = time.monotonic()
    payload = _checkuser(url_in)
    if not (payload or {}).get("rt"):
        err = (payload or {}).get("data") or {}
        err_s = str(err.get("error") or err)
        if "请输入正确" in err_s or "ip" in err_s.lower() or "域名" in err_s:
            alt = _strip_www(url_in)
            if alt and alt != url_in:
                payload = _checkuser(alt)
                url_in = alt
        if not (payload or {}).get("rt"):
            raise RuntimeError(f"17ce checkuser rejected: {(payload or {}).get('data')}")
    timings_ms["checkuser_ms"] = (time.monotonic() - t3) * 1000.0
    data = (payload or {}).get("data") or {}
    user = str(data.get("user") or "").strip()
    code = str(data.get("code") or "").strip()
    ut = str(data.get("ut") or "").strip()
    if not (user and code and ut):
        raise RuntimeError("17ce checkuser missing auth params")

    fullips = data.get("fullips") or []
    sid_map: dict[int, dict[str, Any]] = {}
    for item in fullips:
        if not isinstance(item, dict):
            continue
        sid_raw = item.get("sid")
        try:
            sid = int(sid_raw)
        except Exception:
            continue
        sid_map[sid] = item

    ws_url = "wss://wsapi.17ce.com:8001/socket/?" + urlencode({"user": user, "code": code, "ut": ut})
    ws = WsClient(ws_url, origin="https://17ce.com", proxy=proxy_s or None)
    results: list[dict[str, Any]] = []
    finished = False
    task_id = None
    frames = 0
    by_type: dict[str, int] = {}
    try:
        t4 = time.monotonic()
        ws.connect(timeout=max(5, int(nav_timeout_ms / 1000)))
        timings_ms["ws_connect_ms"] = (time.monotonic() - t4) * 1000.0

        t5 = time.monotonic()
        login_deadline = time.monotonic() + max(10.0, nav_timeout_ms / 1000)
        while time.monotonic() < login_deadline:
            obj = ws.recv_json(timeout_seconds=2.0)
            if not obj:
                continue
            frames += 1
            msg = str(obj.get("msg") or "").strip().lower()
            if int(obj.get("rt") or 0) == 1 and msg == "login ok":
                break
        else:
            raise RuntimeError("17ce websocket login timeout")
        timings_ms["ws_login_ms"] = (time.monotonic() - t5) * 1000.0

        postdata = {
            "txnid": 1,
            "nodetype": nodetype,
            "num": 2,
            "Url": url_in,
            "TestType": t_val.upper() if t_val != "tracert" else "TraceRT",
            "Host": "",
            "TimeOut": 3 if t_val == "ping" else 10,
            "Request": request_method,
            "NoCache": False,
            "Speed": 0,
            "Cookie": "",
            "Trace": False,
            "Referer": "",
            "UserAgent": "",
            "FollowLocation": followlocation,
            "GetMD5": True,
            "GetResponseHeader": True,
            "MaxDown": 1048576,
            "AutoDecompress": False,
            "type": 1,
            "isps": isps,
            "pro_ids": pro_ids,
            "areas": areas,
            "SnapShot": False,
            "postfield": "",
            "PingCount": 10,
            "PingSize": 32,
            "SrcIP": "",
        }
        t6 = time.monotonic()
        ws.send_json(postdata)
        timings_ms["ws_send_ms"] = (time.monotonic() - t6) * 1000.0

        deadline = time.monotonic() + max(60.0, (action_timeout_ms / 1000) + 30.0)
        while time.monotonic() < deadline:
            obj = ws.recv_json(timeout_seconds=2.0)
            if not obj:
                continue
            frames += 1
            t = str(obj.get("type") or "unknown")
            by_type[t] = by_type.get(t, 0) + 1

            if t == "TaskErr":
                err = obj.get("error")
                raise RuntimeError(f"17ce task error: {err}")

            if t == "TaskAccept":
                d = obj.get("data") or {}
                if isinstance(d, dict) and d:
                    first = next(iter(d.values()))
                    if isinstance(first, dict) and first.get("TaskId") is not None:
                        task_id = first.get("TaskId")
                        nodes_obj = first.get("nodes") or {}
                        node_ids: list[int] = []
                        if isinstance(nodes_obj, dict):
                            for k in nodes_obj.keys():
                                try:
                                    node_ids.append(int(k))
                                except Exception:
                                    continue
                        if node_ids:
                            try:
                                sid_map.update(
                                    _get_full_ips(
                                        node_ids, referer=base, timeout_s=timeout_s, proxy_url=proxy_s or None
                                    )
                                )
                            except Exception:
                                pass
                continue

            if t == "NewData":
                d = obj.get("data") or {}
                if not isinstance(d, dict):
                    continue
                if task_id is not None and d.get("TaskId") not in (None, task_id):
                    continue
                node_id_raw = d.get("NodeID")
                try:
                    node_id = int(node_id_raw)
                except Exception:
                    node_id = None
                node_info = sid_map.get(node_id) if node_id is not None else None
                detect_node_location = None
                isp_name = None
                if isinstance(node_info, dict):
                    detect_node_location = str(node_info.get("fullname") or "").strip() or None
                    isp_name = str(node_info.get("isp") or "").strip() or None
                    if not detect_node_location:
                        detect_node_location = str(node_info.get("name") or "").strip() or None

                src_ip = d.get("SrcIP") or d.get("RealIP") or d.get("RealIp")
                response_ip = str(src_ip).strip() if src_ip not in (None, "") else None
                status_code = d.get("HttpCode")
                if status_code in (None, "") and d.get("ErrMsg") is not None:
                    status_code = "error"
                status_code = str(status_code) if status_code not in (None, "") else "--"

                results.append(
                    {
                        "isp": isp_name,
                        "detect_node_location": detect_node_location,
                        "ip_location": None,
                        "response_ip": response_ip,
                        "status_code": status_code,
                        "download_time": _to_float(d.get("DownTime")),
                        "connect_time": _to_float(d.get("ConnectTime")),
                        "dns_time": _to_float(d.get("NsLookup")),
                        "total_time": _to_float(d.get("TotalTime")),
                        "raw": {
                            "source": "17ce_get",
                            "task_id": task_id,
                            "node_id": node_id,
                            "err_msg": d.get("ErrMsg"),
                            "src_ip": d.get("SrcIP"),
                            "real_proxy_ip": real_proxy_ip,
                            "base_url": base,
                        },
                    }
                )
                continue

            if t == "TaskEnd":
                finished = True
                break

        timings_ms["ws_recv_ms"] = (time.monotonic() - t6) * 1000.0
    finally:
        t9 = time.monotonic()
        try:
            ws.close()
        except Exception:
            pass
        timings_ms["ws_close_ms"] = (time.monotonic() - t9) * 1000.0

    if not results:
        raise RuntimeError("17ce results empty")

    uniq: list[dict[str, Any]] = []
    seen = set()
    for r in results:
        raw = r.get("raw") if isinstance(r, dict) else None
        node_id = raw.get("node_id") if isinstance(raw, dict) else None
        key = (
            node_id,
            str(r.get("detect_node_location") or "").strip(),
            str(r.get("response_ip") or "").strip(),
            str(r.get("status_code") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    timings_ms["total_platform_ms"] = (time.monotonic() - overall_t0) * 1000.0
    return uniq, {
        "real_proxy_ip": real_proxy_ip,
        "timings_ms": timings_ms,
        "17ce_stats": {"frames": frames, "by_type": by_type, "rows": len(uniq), "task_id": task_id},
        "incomplete": not finished,
    }

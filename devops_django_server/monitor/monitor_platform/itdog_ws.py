import hashlib
import json
import os
import re
import base64
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener
import logging


logger = logging.getLogger("monitor")


SALT = "token_20230313000136kwyktxb0tgspm00yo5"


def _now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _md5_16_middle(s: str) -> str:
    h = hashlib.md5(s.encode("utf-8")).hexdigest()
    return h[8:24]


def _urlencode(s: str) -> str:
    out = []
    for ch in s:
        o = ord(ch)
        if (48 <= o <= 57) or (65 <= o <= 90) or (97 <= o <= 122) or ch in "-_.~":
            out.append(ch)
        elif ch == " ":
            out.append("+")
        else:
            out.append("%" + format(o, "02X"))
    return "".join(out)


def _http_get(url: str, *, headers: dict[str, str] | None, timeout: int) -> tuple[int, dict[str, str], bytes]:
    req = Request(url, method="GET", headers=headers or {})
    with _open(req, timeout=timeout, proxy_url=None) as resp:
        status = getattr(resp, "status", 200)
        hdrs = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
        body = resp.read()
        return status, hdrs, body


def _http_post_form(url: str, *, data: dict[str, str], headers: dict[str, str] | None, timeout: int) -> tuple[int, dict[str, str], bytes]:
    payload = "&".join([f"{_urlencode(k)}={_urlencode(v)}" for k, v in data.items()]).encode("utf-8")
    h = {"content-type": "application/x-www-form-urlencoded", "content-length": str(len(payload))}
    if headers:
        h.update(headers)
    req = Request(url, data=payload, method="POST", headers=h)
    with _open(req, timeout=timeout, proxy_url=None) as resp:
        status = getattr(resp, "status", 200)
        hdrs = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
        body = resp.read()
        return status, hdrs, body


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
    url: str, *, data: dict[str, str], headers: dict[str, str] | None, timeout: int, proxy_url: str | None
) -> tuple[int, dict[str, str], bytes]:
    payload = "&".join([f"{_urlencode(k)}={_urlencode(v)}" for k, v in data.items()]).encode("utf-8")
    h = {"content-type": "application/x-www-form-urlencoded", "content-length": str(len(payload))}
    if headers:
        h.update(headers)
    req = Request(url, data=payload, method="POST", headers=h)
    with _open(req, timeout=timeout, proxy_url=proxy_url) as resp:
        status = getattr(resp, "status", 200)
        hdrs = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
        body = resp.read()
        return status, hdrs, body


def _detect_real_proxy_ip(timeout_ms: int, *, proxy_url: str | None):
    timeout = max(5, int(timeout_ms / 1000))
    try:
        req = Request("https://httpbin.org/ip", method="GET", headers={"user-agent": "vp-monitor-itdog-ws"})
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


def _parse_task_info(html: str) -> dict[str, str]:
    m_task = re.search(r"task_id\s*=\s*'([^']+)'", html)
    m_ws = re.search(r"wss_url\s*=\s*'([^']+)'", html)
    m_mode = re.search(r"check_mode\s*=\s*'([^']+)'", html)
    out = {}
    if m_task:
        out["task_id"] = m_task.group(1)
    if m_ws:
        out["wss_url"] = m_ws.group(1)
    if m_mode:
        out["check_mode"] = m_mode.group(1)
    return out


@dataclass
class WsResult:
    frames: int
    by_type: dict[str, int]
    samples: list[dict[str, Any]]
    results: list[dict]
    finished: bool


class WsClient:
    def __init__(self, wss_url: str, *, origin: str, proxy: str | None = None):
        u = urlparse(wss_url)
        self.host = u.hostname or ""
        self.port = u.port or 443
        self.path = (u.path or "/") + (("?" + u.query) if u.query else "")
        self.origin = origin
        self.sock: ssl.SSLSocket | None = None
        self.proxy = proxy

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
            f"Host: {self.host}\r\n"
            "Pragma: no-cache\r\n"
            "Cache-Control: no-cache\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key_b64}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n"
            f"Origin: {self.origin}\r\n"
            f"User-Agent: {ua}\r\n"
            "\r\n"
        ).encode("utf-8")
        tls.sendall(req)
        resp = self._read_http_response(tls, timeout=timeout)
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"websocket upgrade failed")
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

    def _recv_frame(self) -> tuple[int | None, bytes | None]:
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

    def recv_loop(self, *, timeout_seconds: int, max_frames: int) -> WsResult:
        if not self.sock:
            raise RuntimeError("not connected")
        end = time.time() + timeout_seconds
        by_type: dict[str, int] = {}
        samples: list[dict[str, Any]] = []
        results: list[dict] = []
        frames = 0
        finished = False
        while time.time() < end and frames < max_frames:
            self.sock.settimeout(max(0.5, min(5.0, end - time.time())))
            opcode, payload = self._recv_frame()
            if opcode is None:
                continue
            frames += 1
            if opcode == 0x9:
                self._send_frame(opcode=0xA, payload=payload or b"")
                continue
            if opcode == 0x8:
                break
            if opcode != 0x1:
                continue
            try:
                text = (payload or b"").decode("utf-8", errors="replace")
                obj = json.loads(text)
                t = str(obj.get("type") or "unknown")
                by_type[t] = by_type.get(t, 0) + 1
                if len(samples) < 5:
                    samples.append(obj)
                if t == "finished":
                    finished = True
                    break
                if t in {"success", "unknown", "error", "fail"} or True:
                    r = _map_obj_to_result(obj)
                    if r:
                        results.append(r)
            except Exception:
                continue
        return WsResult(frames=frames, by_type=by_type, samples=samples, results=results, finished=finished)


def _map_obj_to_result(obj: dict[str, Any]) -> dict | None:
    detect_node_location = str(obj.get("name") or "").strip() or None
    response_ip = str(obj.get("ip") or "").strip() or None
    status_code = obj.get("http_code")
    status_code = str(status_code) if status_code not in (None, "") else "--"
    total_time = _to_float(obj.get("all_time"))
    dns_time = _to_float(obj.get("dns_time"))
    connect_time = _to_float(obj.get("connect_time"))
    download_time = _to_float(obj.get("download_time"))
    ip_location = str(obj.get("address") or "").strip() or None
    raw = {
        "source": "itdog_ws",
        "node_id": obj.get("node_id"),
        "line": obj.get("line"),
        "name": obj.get("name"),
        "region": obj.get("region"),
        "province": obj.get("province"),
        "head": obj.get("head"),
    }
    return {
        "isp": None,
        "detect_node_location": detect_node_location,
        "ip_location": ip_location,
        "response_ip": response_ip,
        "status_code": status_code,
        "download_time": download_time,
        "connect_time": connect_time,
        "dns_time": dns_time,
        "total_time": total_time,
        "raw": raw,
    }


def _to_float(v: Any):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def run_itdog_ws(
    domain: str,
    *,
    base_url: str | None,
    proxy: str,
    headless: bool,
    screenshot_enabled: bool,
    screenshot_dir: str,
    nav_timeout_ms: int,
    action_timeout_ms: int,
):
    timings_ms: dict[str, float] = {}
    overall_t0 = time.monotonic()
    base = (base_url or "https://www.itdog.cn/http/").strip()
    if not base:
        base = "https://www.itdog.cn/http/"
    if not base.endswith("/"):
        base += "/"
    t1 = time.monotonic()
    env_proxy = (proxy or "").strip()
    real_proxy_ip = None
    logger.info(f"itdog_ws: domain={domain} base_url={base} proxy={env_proxy} headless={headless} screenshot_enabled={screenshot_enabled} screenshot_dir={screenshot_dir} nav_timeout_ms={nav_timeout_ms} action_timeout_ms={action_timeout_ms}")

    if env_proxy:
        t0 = time.monotonic()
        real_proxy_ip = _detect_real_proxy_ip(timeout_ms=nav_timeout_ms, proxy_url=env_proxy)
        timings_ms["proxy_detect_ms"] = (time.monotonic() - t0) * 1000.0

    _http_get_proxy(
        base,
        headers={"user-agent": "vp-monitor-itdog-ws"},
        timeout=max(5, int(nav_timeout_ms / 1000)),
        proxy_url=env_proxy or None,
    )
    timings_ms["goto_ms"] = (time.monotonic() - t1) * 1000.0
    t2 = time.monotonic()
    host_s = urlparse(domain).netloc or domain.replace("https://", "").replace("http://", "")
    status, _, body = _http_post_form_proxy(
        base,
        data={
            "line": "",
            "host": domain,
            "host_s": host_s,
            "check_mode": "fast",
            "ipv4": "",
            "method": "get",
            "referer": "",
            "ua": "",
            "cookies": "",
            "redirect_num": "5",
            "dns_server_type": "isp",
            "dns_server": "",
        },
        headers={"user-agent": "vp-monitor-itdog-ws", "origin": "https://www.itdog.cn", "referer": base},
        timeout=max(10, int(action_timeout_ms / 1000)),
        proxy_url=env_proxy or None,
    )
    timings_ms["fill_and_click_ms"] = (time.monotonic() - t2) * 1000.0
    if status >= 400:
        logger.error(f"post itdog http failed: status={status} body={body}")
        raise RuntimeError("post itdog http failed")
    html = body.decode("utf-8", errors="ignore")
    info = _parse_task_info(html)
    task_id = info.get("task_id")
    if not task_id:
        logger.error(f"task_id not found in itdog response: {html}")
        raise RuntimeError("task_id not found")
    wss_url = info.get("wss_url") or "wss://www.itdog.cn/websockets"
    token = _md5_16_middle(task_id + SALT)
    payload = {"task_id": task_id, "task_token": token}
    logger.info(f"itdog_ws: task_id={task_id} wss_url={wss_url}")
    t3 = time.monotonic()
    client = WsClient(wss_url, origin="https://www.itdog.cn", proxy=env_proxy or None)
    client.connect(timeout=20)
    client.send_json(payload)
    res = client.recv_loop(timeout_seconds=max(30, int(nav_timeout_ms / 1000)), max_frames=8000)
    logger.info(f"itdog_ws: recv_loop finished: finished={res.finished} by_type={res.by_type}")
    client.close()
    timings_ms["stabilize_ms"] = (time.monotonic() - t3) * 1000.0
    incomplete = not res.finished or (res.by_type.get("success", 0) < 100)
    results = res.results
    for r in results:
        raw = r.get("raw")
        if isinstance(raw, dict):
            raw["real_proxy_ip"] = real_proxy_ip
            raw["base_url"] = base
    timings_ms["total_platform_ms"] = (time.monotonic() - overall_t0) * 1000.0
    meta = {
        "real_proxy_ip": real_proxy_ip,
        "screenshot": None,
        "timings_ms": timings_ms,
        "itdog_stats": {"ws_frames": res.frames, "by_type": res.by_type, "incomplete": incomplete},
        "incomplete": incomplete,
    }
    return results, meta

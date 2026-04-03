import base64
import http.cookiejar
import json
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


def _int_env(name: str, default: int) -> int:
    try:
        v = int(str(os.environ.get(name) or "").strip() or default)
        return v
    except Exception:
        return default


def _clean_url(value: str) -> str:
    s = "" if value is None else str(value).strip()
    if not s:
        return s
    if "://" not in s:
        return "http://" + s
    return s


def _host_of(url: str) -> str:
    try:
        u = urlparse(url)
        return (u.hostname or "").strip()
    except Exception:
        return ""


def _build_opener(proxy_url: str):
    jar = http.cookiejar.CookieJar()
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}), HTTPCookieProcessor(jar))
    return opener, jar


def _cookie_header_for_17ce(jar: http.cookiejar.CookieJar) -> str | None:
    parts: list[str] = []
    for c in jar:
        try:
            name = str(getattr(c, "name", "") or "")
            value = str(getattr(c, "value", "") or "")
            domain = str(getattr(c, "domain", "") or "").lstrip(".")
        except Exception:
            continue
        if not name or value is None:
            continue
        if domain == "17ce.com" or domain.endswith(".17ce.com"):
            parts.append(f"{name}={value}")
    s = "; ".join(parts)
    return s or None


def _http_get(opener, url: str, *, timeout: int, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    h = {"user-agent": "Mozilla/5.0", "accept": "*/*"}
    if headers:
        h.update(headers)
    req = Request(url, method="GET", headers=h)
    with opener.open(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        return int(status), resp.read()


def _detect_real_proxy_ip(opener, *, timeout: int) -> str | None:
    try:
        status, body = _http_get(opener, "https://httpbin.org/ip", timeout=timeout)
        if status < 200 or status >= 300:
            return None
        s = (body or b"").decode("utf-8", errors="ignore")
        m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", s)
        if not m:
            return None
        return m.group(1)
    except Exception:
        return None


def _wait_for_proxy_ip_rotation(opener, *, current_ip: str | None, timeout_seconds: int, http_timeout: int) -> str | None:
    if timeout_seconds <= 0:
        return None
    end = time.time() + timeout_seconds
    while time.time() < end:
        ip = _detect_real_proxy_ip(opener, timeout=http_timeout)
        if ip and ip != current_ip:
            return ip
        time.sleep(1.0)
    return None


def _http_post_form(opener, url: str, *, data: list[tuple[str, str]] | dict[str, str], timeout: int, headers: dict[str, str]) -> tuple[int, bytes]:
    payload = urlencode(data).encode("utf-8")
    h = {
        "content-type": "application/x-www-form-urlencoded",
        "content-length": str(len(payload)),
        "user-agent": "Mozilla/5.0",
        "accept": "*/*",
    }
    h.update(headers)
    req = Request(url, data=payload, method="POST", headers=h)
    with opener.open(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        return int(status), resp.read()


def _load_common_js_pro_ids(opener, *, timeout: int) -> list[int]:
    status, body = _http_get(opener, "https://www.17ce.com/smedia/js/common.js?ver=20251221", timeout=timeout)
    if status < 200 or status >= 300:
        return []
    js = (body or b"").decode("utf-8", errors="ignore")
    m = re.search(r"pro_ids\s*:\s*\[([0-9,\s]+)\]", js)
    if not m:
        return []
    return [int(x) for x in re.findall(r"\d+", m.group(1))]


def _checkuser(opener, *, url: str, timeout: int) -> dict[str, Any]:
    headers = {
        "referer": "https://www.17ce.com/get",
        "origin": "https://www.17ce.com",
        "x-requested-with": "XMLHttpRequest",
    }
    status, body = _http_post_form(
        opener,
        "https://www.17ce.com/site/checkuser",
        data={"url": url, "type": "http", "isp": "0"},
        timeout=timeout,
        headers=headers,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"17ce checkuser http status {status}")
    obj = json.loads((body or b"{}").decode("utf-8", errors="ignore") or "{}")
    if not isinstance(obj, dict) or not obj.get("rt"):
        err = ""
        if isinstance(obj, dict):
            data = obj.get("data")
            if isinstance(data, dict):
                err = str(data.get("error") or "")
        raise RuntimeError(f"17ce checkuser failed: {err or 'unknown'}")
    data = obj.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("17ce checkuser data invalid")
    return data


def _get_full_ips(opener, *, node_ids: list[int], timeout: int) -> dict[int, dict[str, Any]]:
    if not node_ids:
        return {}
    headers = {
        "referer": "https://www.17ce.com/get",
        "origin": "https://www.17ce.com",
        "x-requested-with": "XMLHttpRequest",
    }
    payload = [("nodes[]", str(n)) for n in node_ids]
    for i in range(3):
        try:
            status, body = _http_post_form(opener, "https://www.17ce.com/site/getFullIps", data=payload, timeout=timeout, headers=headers)
            if status < 200 or status >= 300:
                time.sleep(0.6 + i * 0.8)
                continue
            obj = json.loads((body or b"{}").decode("utf-8", errors="ignore") or "{}")
            if not isinstance(obj, dict) or not obj.get("rt"):
                time.sleep(0.6 + i * 0.8)
                continue
            fullips = obj.get("fullips") or obj.get("data", {}).get("fullips")
            if not isinstance(fullips, list):
                time.sleep(0.6 + i * 0.8)
                continue
            out: dict[int, dict[str, Any]] = {}
            for x in fullips:
                if not isinstance(x, dict):
                    continue
                try:
                    sid = int(str(x.get("sid")))
                except Exception:
                    continue
                out[sid] = x
            return out
        except Exception:
            time.sleep(0.6 + i * 0.8)
    return {}


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        out = float(v)
        return None if out < 0 else out
    s = str(v).strip()
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    if not m:
        return None
    out = float(m.group(1))
    return None if out < 0 else out


def _find_task_id(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for k in ("TaskId", "TaskID", "taskId", "task_id"):
            v = obj.get(k)
            if v not in (None, ""):
                s = str(v).strip()
                if s:
                    return s
        for v in obj.values():
            found = _find_task_id(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_task_id(v)
            if found:
                return found
    return None


@dataclass
class WsResult:
    frames: int
    by_type: dict[str, int]
    samples: list[dict[str, Any]]
    results: list[dict[str, Any]]
    finished: bool
    task_id: str | None
    task_end: dict[str, Any] | None
    accept_nodes: list[int]
    error: str | None


class WsClient:
    def __init__(self, host: str, *, port: int, path: str, origin: str, proxy: str):
        self.host = host
        self.port = port
        self.path = path
        self.origin = origin
        self.proxy = proxy
        self.sock: ssl.SSLSocket | None = None

    def connect(self, *, timeout: int, cookie_header: str | None = None):
        key_b64 = base64.b64encode(os.urandom(16)).decode("ascii")
        ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        p = urlparse(self.proxy)
        phost = p.hostname or ""
        pport = p.port or 80
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
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(raw, server_hostname=self.host)
        cookie_line = f"Cookie: {cookie_header}\r\n" if cookie_header else ""
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
            f"{cookie_line}"
            f"User-Agent: {ua}\r\n"
            "\r\n"
        ).encode("utf-8")
        tls.sendall(req)
        resp2 = self._read_http_response(tls, timeout=timeout)
        if b" 101 " not in resp2.split(b"\r\n", 1)[0]:
            raise RuntimeError("websocket upgrade failed")
        self.sock = tls

    def wait_login_ok(self, *, timeout_seconds: int) -> str | None:
        if not self.sock:
            return "not connected"
        end = time.time() + timeout_seconds
        while time.time() < end:
            try:
                self.sock.settimeout(max(0.5, min(5.0, end - time.time())))
                opcode, payload = self._recv_frame()
            except socket.timeout:
                continue
            except Exception as e:
                return f"{type(e).__name__}: {e}"
            if opcode is None:
                continue
            if opcode == 0x9:
                try:
                    self._send_frame(opcode=0xA, payload=payload or b"")
                except Exception:
                    pass
                continue
            if opcode == 0x8:
                return "socket closed"
            if opcode != 0x1:
                continue
            try:
                text = (payload or b"").decode("utf-8", errors="replace")
                obj = json.loads(text)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("rt") == 1 and str(obj.get("msg") or "") == "login ok":
                return None
        return "login timeout"

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
        results: list[dict[str, Any]] = []
        accept_nodes: list[int] = []
        frames = 0
        finished = False
        task_id = None
        task_end = None
        ws_error: str | None = None
        while time.time() < end and frames < max_frames:
            try:
                self.sock.settimeout(max(0.5, min(5.0, end - time.time())))
                opcode, payload = self._recv_frame()
            except socket.timeout:
                continue
            except Exception as e:
                ws_error = f"{type(e).__name__}: {e}"
                break
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
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if len(samples) < 8:
                samples.append(obj)
            msg = str(obj.get("msg") or "")
            if obj.get("rt") == 1 and msg == "login ok":
                by_type["login_ok"] = by_type.get("login_ok", 0) + 1
                continue
            t = str(obj.get("type") or "unknown")
            by_type[t] = by_type.get(t, 0) + 1
            if t == "TaskEnd":
                d = obj.get("data")
                task_end = d if isinstance(d, dict) else None
                finished = True
                break
            if t == "TaskAccept":
                found = _find_task_id(obj)
                if found:
                    task_id = found
                data0 = obj.get("data")
                if isinstance(data0, dict):
                    for _, v in data0.items():
                        if not isinstance(v, dict):
                            continue
                        nodes = v.get("nodes")
                        if isinstance(nodes, dict):
                            for k in nodes.keys():
                                try:
                                    accept_nodes.append(int(str(k)))
                                except Exception:
                                    continue
            if t == "NewData":
                data = obj.get("data")
                if isinstance(data, dict):
                    results.append(data)
        accept_nodes = sorted(list({n for n in accept_nodes}))
        if not ws_error and not finished and not results and not accept_nodes:
            ws_error = "ws timeout"
        return WsResult(
            frames=frames,
            by_type=by_type,
            samples=samples,
            results=results,
            finished=finished,
            task_id=task_id,
            task_end=task_end,
            accept_nodes=accept_nodes,
            error=ws_error,
        )


def run_17ce_ws(
    domain: str,
    *,
    base_url: str | None = None,
    proxy: str,
    headless: bool,
    screenshot_enabled: bool,
    screenshot_dir: str,
    nav_timeout_ms: int,
    action_timeout_ms: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    proxy_s = (proxy or "").strip()
    if not proxy_s:
        proxy_s = (os.environ.get("DEFAULT_PROXY") or os.environ.get("PROXY") or "").strip()
    if not proxy_s:
        raise RuntimeError("17ce requires proxy")

    t0 = time.monotonic()
    url = _clean_url(domain)
    opener, jar = _build_opener(proxy_s)
    http_timeout = max(6, int(_int_env("SEVENTEEN_CE_HTTP_TIMEOUT_SECONDS", 8)))
    ws_connect_timeout = max(3, int(_int_env("SEVENTEEN_CE_WS_CONNECT_TIMEOUT_SECONDS", 6)))
    ws_attempts = max(1, int(_int_env("SEVENTEEN_CE_WS_ATTEMPTS", 2)))
    rotate_wait_seconds = int(_int_env("SEVENTEEN_CE_WAIT_IP_ROTATION_SECONDS", 16))
    real_proxy_ip = None
    for i in range(3):
        real_proxy_ip = _detect_real_proxy_ip(opener, timeout=http_timeout)
        if real_proxy_ip:
            break
        time.sleep(0.6 + i * 0.8)

    data = None
    for i in range(2):
        try:
            data = _checkuser(opener, url=url, timeout=http_timeout)
            break
        except Exception:
            time.sleep(0.6 + i * 0.8)
    if not isinstance(data, dict):
        results = [
            {
                "isp": None,
                "detect_node_location": None,
                "ip_location": None,
                "response_ip": None,
                "status_code": "系统异常",
                "download_time": None,
                "connect_time": None,
                "dns_time": None,
                "total_time": None,
                "raw": {
                    "source": "17ce_checkuser_error",
                    "url": url,
                    "proxy": proxy_s,
                    "real_proxy_ip": real_proxy_ip,
                    "error": "checkuser failed after retries",
                },
            }
        ]
        meta_out = {
            "real_proxy_ip": real_proxy_ip,
            "timings_ms": {"total_platform_ms": (time.monotonic() - t0) * 1000.0},
            "seventeence_stats": {"finished": False, "task_id": None, "ws_error": None},
            "incomplete": True,
        }
        return results, meta_out

    user = str(data.get("user") or "").strip()
    code = str(data.get("code") or "").strip()
    ut = str(data.get("ut") or "").strip()
    if not (user and code and ut):
        results = [
            {
                "isp": None,
                "detect_node_location": None,
                "ip_location": None,
                "response_ip": None,
                "status_code": "系统异常",
                "download_time": None,
                "connect_time": None,
                "dns_time": None,
                "total_time": None,
                "raw": {
                    "source": "17ce_checkuser_error",
                    "url": url,
                    "proxy": proxy_s,
                    "real_proxy_ip": real_proxy_ip,
                    "error": "checkuser missing auth fields",
                },
            }
        ]
        meta_out = {
            "real_proxy_ip": real_proxy_ip,
            "timings_ms": {"total_platform_ms": (time.monotonic() - t0) * 1000.0},
            "seventeence_stats": {"finished": False, "task_id": None, "ws_error": None},
            "incomplete": True,
        }
        return results, meta_out

    pro_ids: list[int] = []
    for i in range(3):
        pro_ids = _load_common_js_pro_ids(opener, timeout=http_timeout)
        if pro_ids:
            break
        time.sleep(0.6 + i * 0.8)

    ws_host = "wsapi.17ce.com"
    ws_port = 8001
    ws_path = "/socket/?" + urlencode({"user": user, "code": code, "ut": ut})

    host = _host_of(url)
    postdata = {
        "txnid": 1,
        "nodetype": [1, 2],
        "num": 2,
        "Url": url,
        "TestType": "HTTP",
        "Host": host,
        "TimeOut": 10,
        "Request": "GET",
        "NoCache": False,
        "Speed": 0,
        "Cookie": "",
        "Trace": False,
        "Referer": "",
        "UserAgent": "",
        "FollowLocation": 0,
        "GetMD5": True,
        "GetResponseHeader": True,
        "MaxDown": 1048576,
        "AutoDecompress": False,
        "type": 1,
        "isps": [0, 1, 2, 6, 7, 8, 17, 18, 19, 3, 4],
        "pro_ids": pro_ids,
        "areas": [0, 1, 2, 3],
        "SnapShot": False,
        "postfield": "",
        "PingCount": 10,
        "PingSize": 32,
        "SrcIP": "",
    }

    ws_res: WsResult | None = None
    last_ws_err = None
    cookie_header = _cookie_header_for_17ce(jar)
    for attempt in range(ws_attempts):
        client = WsClient(ws_host, port=ws_port, path=ws_path, origin="https://www.17ce.com", proxy=proxy_s)
        try:
            client.connect(timeout=ws_connect_timeout, cookie_header=cookie_header)
            login_err = client.wait_login_ok(timeout_seconds=10)
            if login_err:
                raise RuntimeError(f"17ce ws login failed: {login_err}")
            client.send_json(postdata)
            ws_timeout = _int_env("SEVENTEEN_CE_WS_TIMEOUT_SECONDS", 12)
            ws_res = client.recv_loop(timeout_seconds=max(6, int(ws_timeout)), max_frames=8000)
            last_ws_err = ws_res.error or "ws timeout"
        except Exception as e:
            last_ws_err = f"{type(e).__name__}: {e}"
            ws_res = None
        finally:
            client.close()
        if ws_res and (ws_res.accept_nodes or ws_res.results):
            break
        if attempt < ws_attempts - 1:
            if last_ws_err and ("timed out" in last_ws_err or "ws timeout" in last_ws_err or "socket closed" in last_ws_err):
                new_ip = _wait_for_proxy_ip_rotation(
                    opener, current_ip=real_proxy_ip, timeout_seconds=rotate_wait_seconds, http_timeout=http_timeout
                )
                if new_ip:
                    real_proxy_ip = new_ip
                    continue
        time.sleep(0.6 + attempt * 0.8)
    if not ws_res or (not ws_res.accept_nodes and not ws_res.results):
        msg = (last_ws_err or (ws_res.error if ws_res else None) or "unknown")[:2000]
        results = [
            {
                "isp": None,
                "detect_node_location": None,
                "ip_location": None,
                "response_ip": None,
                "status_code": "系统异常",
                "download_time": None,
                "connect_time": None,
                "dns_time": None,
                "total_time": None,
                "raw": {
                    "source": "17ce_ws_error",
                    "url": url,
                    "proxy": proxy_s,
                    "real_proxy_ip": real_proxy_ip,
                    "ws_error": msg,
                },
            }
        ]
        meta_out = {
            "real_proxy_ip": real_proxy_ip,
            "timings_ms": {"total_platform_ms": (time.monotonic() - t0) * 1000.0},
            "seventeence_stats": {"ws_error": msg, "finished": False, "task_id": None},
            "incomplete": True,
        }
        return results, meta_out

    node_map: dict[int, dict[str, Any]] = {}
    fullips0 = data.get("fullips") if isinstance(data.get("fullips"), list) else []
    if isinstance(fullips0, list):
        for x in fullips0:
            if not isinstance(x, dict):
                continue
            try:
                sid = int(str(x.get("sid")))
            except Exception:
                continue
            node_map[sid] = x

    if ws_res.accept_nodes:
        node_map.update(_get_full_ips(opener, node_ids=ws_res.accept_nodes, timeout=http_timeout))

    seen_node_ids: set[int] = set()
    results: list[dict[str, Any]] = []
    for item in ws_res.results:
        try:
            node_id_int = int(str(item.get("NodeID")))
        except Exception:
            node_id_int = None
        if node_id_int is not None:
            seen_node_ids.add(node_id_int)
        meta = node_map.get(node_id_int) if node_id_int is not None else None
        isp = str(meta.get("isp")).strip() if isinstance(meta, dict) and meta.get("isp") else None
        detect_node_location = str(meta.get("fullname") or meta.get("name") or "").strip() if isinstance(meta, dict) else ""
        detect_node_location = detect_node_location or None
        prov = str(meta.get("province") or "").strip() if isinstance(meta, dict) else ""
        city = str(meta.get("city") or "").strip() if isinstance(meta, dict) else ""
        ip_location = " ".join([x for x in [prov, city] if x]) or None
        response_ip = str(item.get("SrcIP") or item.get("srcip") or "").strip() or None
        status_code = item.get("HttpCode")
        if status_code not in (None, ""):
            status_code = str(status_code)
        else:
            err_msg = str(item.get("ErrMsg") or "").strip()
            status_code = "系统异常" if err_msg else "--"
        results.append(
            {
                "isp": isp,
                "detect_node_location": detect_node_location,
                "ip_location": ip_location,
                "response_ip": response_ip,
                "status_code": status_code,
                "download_time": _to_float(item.get("DownTime")),
                "connect_time": _to_float(item.get("ConnectTime")),
                "dns_time": _to_float(item.get("NsLookup")),
                "total_time": _to_float(item.get("TotalTime")),
                "raw": {
                    "source": "17ce_ws",
                    "task_id": ws_res.task_id,
                    "node_id": node_id_int,
                    "data": item,
                    "node": meta,
                    "proxy": proxy_s,
                    "real_proxy_ip": real_proxy_ip,
                },
            }
        )

    missing = [nid for nid in ws_res.accept_nodes if nid not in seen_node_ids]
    for nid in missing:
        meta = node_map.get(nid)
        isp = str(meta.get("isp")).strip() if isinstance(meta, dict) and meta.get("isp") else None
        detect_node_location = str(meta.get("fullname") or meta.get("name") or "").strip() if isinstance(meta, dict) else ""
        detect_node_location = detect_node_location or None
        prov = str(meta.get("province") or "").strip() if isinstance(meta, dict) else ""
        city = str(meta.get("city") or "").strip() if isinstance(meta, dict) else ""
        ip_location = " ".join([x for x in [prov, city] if x]) or None
        results.append(
            {
                "isp": isp,
                "detect_node_location": detect_node_location,
                "ip_location": ip_location,
                "response_ip": None,
                "status_code": "超时",
                "download_time": None,
                "connect_time": None,
                "dns_time": None,
                "total_time": None,
                "raw": {
                    "source": "17ce_ws_missing",
                    "task_id": ws_res.task_id,
                    "node_id": nid,
                    "node": meta,
                    "proxy": proxy_s,
                    "real_proxy_ip": real_proxy_ip,
                },
            }
        )

    meta_out = {
        "real_proxy_ip": real_proxy_ip,
        "timings_ms": {"total_platform_ms": (time.monotonic() - t0) * 1000.0},
        "seventeence_stats": {
            "frames": ws_res.frames,
            "by_type": ws_res.by_type,
            "finished": ws_res.finished,
            "task_id": ws_res.task_id,
            "task_end": ws_res.task_end,
            "ws_error": ws_res.error,
        },
        "incomplete": not bool(results),
    }
    return results, meta_out

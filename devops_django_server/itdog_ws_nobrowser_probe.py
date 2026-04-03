import argparse
import base64
import hashlib
import json
import os
import re
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


SALT = "token_20230313000136kwyktxb0tgspm00yo5"


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _md5_16_middle(s: str) -> str:
    h = hashlib.md5(s.encode("utf-8")).hexdigest()
    return h[8:24]


def _http_get(url: str, *, headers: dict[str, str] | None = None, timeout: int = 30) -> tuple[int, dict[str, str], bytes]:
    req = Request(url, method="GET", headers=headers or {})
    with urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        hdrs = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
        body = resp.read()
        return status, hdrs, body


def _http_post_form(
    url: str, *, data: dict[str, str], headers: dict[str, str] | None = None, timeout: int = 30
) -> tuple[int, dict[str, str], bytes]:
    payload = "&".join([f"{_urlencode(k)}={_urlencode(v)}" for k, v in data.items()]).encode("utf-8")
    h = {"content-type": "application/x-www-form-urlencoded", "content-length": str(len(payload))}
    if headers:
        h.update(headers)
    req = Request(url, data=payload, method="POST", headers=h)
    with urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        hdrs = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
        body = resp.read()
        return status, hdrs, body


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


def _parse_task_info(html: str) -> dict[str, str]:
    m_task = re.search(r"task_id\s*=\s*'([^']+)'", html)
    m_ws = re.search(r"wss_url\s*=\s*'([^']+)'", html)
    m_mode = re.search(r"check_mode\s*=\s*'([^']+)'", html)
    if not m_task:
        raise RuntimeError("task_id not found in html")
    out = {"task_id": m_task.group(1)}
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


class WsClient:
    def __init__(self, wss_url: str, *, origin: str = "https://www.itdog.cn"):
        u = urlparse(wss_url)
        if u.scheme != "wss":
            raise ValueError("only wss supported")
        self.host = u.hostname or ""
        self.port = u.port or 443
        self.path = u.path or "/"
        if u.query:
            self.path += "?" + u.query
        self.origin = origin
        self.sock: socket.socket | None = None

    def connect(self, *, timeout: int = 20):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(raw, server_hostname=self.host)
        ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Pragma: no-cache\r\n"
            "Cache-Control: no-cache\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n"
            f"Origin: {self.origin}\r\n"
            f"User-Agent: {ua}\r\n"
            "\r\n"
        ).encode("utf-8")
        tls.sendall(req)
        resp = self._read_http_response(tls, timeout=timeout)
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"websocket upgrade failed: {resp[:200]!r}")
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

    def send_text(self, text: str):
        self._send_frame(opcode=0x1, payload=text.encode("utf-8"))

    def recv_loop(self, *, timeout_seconds: int, max_frames: int = 5000) -> WsResult:
        if not self.sock:
            raise RuntimeError("not connected")
        end = time.time() + timeout_seconds
        by_type: dict[str, int] = {}
        samples: list[dict[str, Any]] = []
        frames = 0
        while time.time() < end and frames < max_frames:
            self.sock.settimeout(max(0.5, min(5.0, end - time.time())))
            opcode, payload = self._recv_frame()
            if opcode is None:
                continue
            frames += 1
            if opcode == 0x9:
                self._send_frame(opcode=0xA, payload=payload or b"")
                continue
            if opcode == 0xA:
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
                    break
            except Exception:
                continue
        return WsResult(frames=frames, by_type=by_type, samples=samples)

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--base-url", default="https://www.itdog.cn/http/")
    ap.add_argument("--timeout-seconds", type=int, default=90)
    ap.add_argument("--http-timeout", type=int, default=40)
    args = ap.parse_args()

    base_url = args.base_url.rstrip("/") + "/"
    status0, _, _ = _http_get(base_url, headers={"user-agent": "vp-monitor-probe"}, timeout=args.http_timeout)
    if status0 >= 400:
        raise SystemExit(f"GET {base_url} failed: {status0}")

    post_data = {
        "line": "",
        "host": args.domain,
        "host_s": urlparse(args.domain).netloc or args.domain.replace("https://", "").replace("http://", ""),
        "check_mode": "fast",
        "ipv4": "",
        "method": "get",
        "referer": "",
        "ua": "",
        "cookies": "",
        "redirect_num": "5",
        "dns_server_type": "isp",
        "dns_server": "",
    }
    status1, _, body1 = _http_post_form(
        base_url,
        data=post_data,
        headers={"user-agent": "vp-monitor-probe", "origin": "https://www.itdog.cn", "referer": base_url},
        timeout=args.http_timeout,
    )
    if status1 >= 400:
        raise SystemExit(f"POST {base_url} failed: {status1}")
    html = body1.decode("utf-8", errors="ignore")
    info = _parse_task_info(html)
    task_id = info["task_id"]
    wss_url = info.get("wss_url") or "wss://www.itdog.cn/websockets"
    token = _md5_16_middle(task_id + SALT)

    payload = json.dumps({"task_id": task_id, "task_token": token}, ensure_ascii=False)
    client = WsClient(wss_url)
    t0 = time.time()
    client.connect(timeout=20)
    client.send_text(payload)
    res = client.recv_loop(timeout_seconds=args.timeout_seconds, max_frames=8000)
    client.close()
    elapsed = time.time() - t0

    out = {
        "ts": _now_ts(),
        "domain": args.domain,
        "task_id": task_id,
        "check_mode": info.get("check_mode"),
        "wss_url": wss_url,
        "task_token": token,
        "elapsed_seconds": round(elapsed, 3),
        "ws_frames": res.frames,
        "by_type": res.by_type,
        "samples": res.samples,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

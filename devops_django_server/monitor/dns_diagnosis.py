from __future__ import annotations

import json
import random
import socket
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from typing import Any
from urllib.parse import urlparse


QTYPE_A = 1
QTYPE_NS = 2
QTYPE_CNAME = 5
QTYPE_SOA = 6
QTYPE_AAAA = 28

RCODE_NAMES = {
    0: "NOERROR",
    1: "FORMERR",
    2: "SERVFAIL",
    3: "NXDOMAIN",
    4: "NOTIMP",
    5: "REFUSED",
}

HOLD_STATUSES = {
    "clienthold",
    "serverhold",
    "inactive",
}


@dataclass
class DiagnosisResult:
    diagnosis_type: str
    confidence: float
    evidence: dict[str, Any]


def normalize_hostname(domain: str) -> str:
    s = "" if domain is None else str(domain).strip()
    if "://" in s:
        host = urlparse(s).hostname or ""
    else:
        host = urlparse("//" + s).hostname or s.split("/", 1)[0]
    return host.strip().strip(".").lower()


def _encode_name(name: str) -> bytes:
    out = bytearray()
    for part in name.strip(".").split("."):
        b = part.encode("idna")
        out.append(len(b))
        out.extend(b)
    out.append(0)
    return bytes(out)


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    next_offset = offset
    seen = set()

    while True:
        if offset >= len(data):
            raise ValueError("dns name out of range")
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                next_offset = offset
            break
        if (length & 0xC0) == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("dns pointer out of range")
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            if ptr in seen:
                raise ValueError("dns pointer loop")
            seen.add(ptr)
            if not jumped:
                next_offset = offset + 2
            offset = ptr
            jumped = True
            continue
        offset += 1
        label = data[offset : offset + length]
        labels.append(label.decode("ascii", errors="ignore"))
        offset += length
        if not jumped:
            next_offset = offset

    return ".".join([p for p in labels if p]), next_offset


def _parse_records(data: bytes, offset: int, count: int) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    for _ in range(count):
        name, offset = _read_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError("dns rr header out of range")
        rtype, rclass, ttl, rdlength = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata_start = offset
        rdata_end = offset + rdlength
        if rdata_end > len(data):
            raise ValueError("dns rr body out of range")
        value: Any = None
        raw = data[rdata_start:rdata_end]
        try:
            if rtype == QTYPE_A and rdlength == 4:
                value = str(IPv4Address(raw))
            elif rtype == QTYPE_AAAA and rdlength == 16:
                value = str(IPv6Address(raw))
            elif rtype in {QTYPE_NS, QTYPE_CNAME}:
                value, _ = _read_name(data, rdata_start)
            elif rtype == QTYPE_SOA:
                mname, p = _read_name(data, rdata_start)
                rname, _ = _read_name(data, p)
                value = {"mname": mname, "rname": rname}
        except Exception:
            value = None
        records.append({"name": name, "type": rtype, "class": rclass, "ttl": ttl, "value": value})
        offset = rdata_end
    return records, offset


def query_dns(resolver: str, name: str, qtype: int, *, timeout_seconds: float) -> dict[str, Any]:
    qid = random.randint(0, 65535)
    packet = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    packet += _encode_name(name)
    packet += struct.pack("!HH", qtype, 1)
    started = time.monotonic()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_seconds)
            sock.sendto(packet, (resolver, 53))
            data, _addr = sock.recvfrom(4096)
    except Exception as e:
        # DNS 诊断不能因为单个递归解析器超时就误判域名异常；这里保留错误证据，后续分类会要求多解析器一致失败。
        return {
            "resolver": resolver,
            "qtype": qtype,
            "rcode": "TIMEOUT",
            "answers": [],
            "authority": [],
            "error": f"{type(e).__name__}: {e}",
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 2),
        }

    try:
        if len(data) < 12:
            raise ValueError("short dns response")
        rid, flags, qdcount, ancount, nscount, arcount = struct.unpack("!HHHHHH", data[:12])
        if rid != qid:
            raise ValueError("dns id mismatch")
        rcode = RCODE_NAMES.get(flags & 0x000F, str(flags & 0x000F))
        offset = 12
        for _ in range(qdcount):
            _qname, offset = _read_name(data, offset)
            offset += 4
        answers, offset = _parse_records(data, offset, ancount)
        authority, offset = _parse_records(data, offset, nscount)
        additional, _offset = _parse_records(data, offset, arcount)
        return {
            "resolver": resolver,
            "qtype": qtype,
            "rcode": rcode,
            "answers": answers,
            "authority": authority,
            "additional": additional,
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 2),
        }
    except Exception as e:
        # 解析响应失败通常是平台/网络证据不足，不直接归因为域名故障。
        return {
            "resolver": resolver,
            "qtype": qtype,
            "rcode": "PARSE_ERROR",
            "answers": [],
            "authority": [],
            "error": f"{type(e).__name__}: {e}",
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 2),
        }


def fetch_rdap_statuses(hostname: str, *, timeout_seconds: int) -> dict[str, Any]:
    ascii_hostname = hostname.encode("idna").decode("ascii")
    url = f"https://rdap.org/domain/{ascii_hostname}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "devops-monitor/rdap"})
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            payload = json.loads(resp.read(200000).decode("utf-8", errors="ignore") or "{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "url": url, "http_status": e.code, "error": f"HTTPError: {e}"}
    except Exception as e:
        # RDAP 是辅助证据，失败时只降级为 DNS 证据判断，避免因为外部接口频控造成误报。
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}
    statuses = [str(s).strip().lower() for s in (payload or {}).get("status", []) if str(s).strip()]
    return {"ok": True, "url": url, "statuses": statuses}


def _has_address_answer(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        if row.get("rcode") != "NOERROR":
            continue
        for rr in row.get("answers") or []:
            if rr.get("type") in {QTYPE_A, QTYPE_AAAA} and rr.get("value"):
                return True
    return False


def _rcode_failures(rows: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        rcode = str(row.get("rcode") or "")
        if rcode and rcode != "NOERROR":
            out.append(rcode)
        elif not row.get("answers"):
            out.append("NO_ANSWER")
    return out


def _extract_ns_names(rows: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        for rr in (row.get("answers") or []) + (row.get("authority") or []):
            if rr.get("type") in {QTYPE_NS, QTYPE_SOA}:
                value = rr.get("value")
                if isinstance(value, dict):
                    value = value.get("mname")
                if value:
                    names.append(str(value).strip(".").lower())
    return sorted(set([n for n in names if n]))


def _matches_registrar_ns(ns_names: list[str], patterns: list[str]) -> bool:
    normalized = [str(p).strip(".").lower() for p in patterns if str(p).strip()]
    for ns in ns_names:
        for pattern in normalized:
            if pattern.startswith("*.") and ns.endswith(pattern[1:]):
                return True
            if pattern in ns:
                return True
    return False


def classify_dns_evidence(
    *,
    hostname: str,
    address_results: list[dict[str, Any]],
    ns_results: list[dict[str, Any]],
    rdap: dict[str, Any] | None,
    registrar_ns_patterns: list[str],
) -> DiagnosisResult:
    ns_names = _extract_ns_names(ns_results)
    address_failures = _rcode_failures(address_results)
    any_address = _has_address_answer(address_results)
    rdap_statuses = [str(s).lower().replace(" ", "").replace("-", "") for s in ((rdap or {}).get("statuses") or [])]
    hold_statuses = sorted(set(rdap_statuses) & HOLD_STATUSES)

    evidence = {
        "hostname": hostname,
        "address_results": address_results,
        "ns_results": ns_results,
        "ns_names": ns_names,
        "rdap": rdap,
        "address_failures": address_failures,
    }

    if hold_statuses and not any_address:
        evidence["hold_statuses"] = hold_statuses
        return DiagnosisResult("registrar_hold", 0.95, evidence)

    if any_address:
        if address_failures:
            return DiagnosisResult("http_only_failure", 0.75, evidence)
        return DiagnosisResult("normal", 0.9, evidence)

    if not address_results:
        return DiagnosisResult("inconclusive", 0.2, evidence)

    failed_resolvers = len(address_failures)
    total_resolvers = len(address_results)
    mostly_failed = total_resolvers > 0 and failed_resolvers >= max(1, int(total_resolvers * 0.67))
    if mostly_failed and _matches_registrar_ns(ns_names, registrar_ns_patterns):
        return DiagnosisResult("registrar_dns_suspended", 0.85, evidence)
    if mostly_failed:
        return DiagnosisResult("dns_misconfig", 0.65, evidence)
    return DiagnosisResult("inconclusive", 0.35, evidence)


def diagnose_domain(
    domain: str,
    *,
    resolvers: list[str],
    registrar_ns_patterns: list[str],
    timeout_seconds: float = 2.0,
    rdap_enabled: bool = False,
    rdap_timeout_seconds: int = 5,
) -> DiagnosisResult:
    hostname = normalize_hostname(domain)
    if not hostname:
        return DiagnosisResult("inconclusive", 0.0, {"error": "empty hostname"})

    address_results: list[dict[str, Any]] = []
    ns_results: list[dict[str, Any]] = []
    for resolver in resolvers:
        resolver_s = str(resolver).strip()
        if not resolver_s:
            continue
        address_results.append(query_dns(resolver_s, hostname, QTYPE_A, timeout_seconds=timeout_seconds))
        address_results.append(query_dns(resolver_s, hostname, QTYPE_AAAA, timeout_seconds=timeout_seconds))
        ns_results.append(query_dns(resolver_s, hostname, QTYPE_NS, timeout_seconds=timeout_seconds))

    rdap = fetch_rdap_statuses(hostname, timeout_seconds=rdap_timeout_seconds) if rdap_enabled else None
    return classify_dns_evidence(
        hostname=hostname,
        address_results=address_results,
        ns_results=ns_results,
        rdap=rdap,
        registrar_ns_patterns=registrar_ns_patterns,
    )

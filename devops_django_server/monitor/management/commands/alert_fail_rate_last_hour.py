from __future__ import annotations

from datetime import timedelta
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from monitor.models import MonitorAlertedDomais
from monitor.management.commands.worker import _send_telegram_message

logger = logging.getLogger("monitor")


def _fmt_dt(dt) -> str:
    try:
        return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


def _chunk_lines(lines: list[str], *, header: str, max_chars: int = 3500) -> list[str]:
    chunks: list[str] = []
    cur = header.rstrip() + "\n"
    for line in lines:
        candidate = cur + line + "\n"
        if len(candidate) > max_chars:
            chunks.append(cur.rstrip())
            cur = header.rstrip() + "\n" + line + "\n"
        else:
            cur = candidate
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks


class Command(BaseCommand):
    """汇总 MonitorAlertedDomais 表中最近一段时间的告警并发送到 Telegram"""
    def add_arguments(self, parser):
        parser.add_argument("--hours", type=float, default=1.0)
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        hours = float(options["hours"])
        limit = int(options["limit"])
        dry_run = bool(options["dry_run"])

        now = timezone.now()
        cutoff = now - timedelta(seconds=int(hours * 3600))

        qs = (
            MonitorAlertedDomais.objects.filter(alert_time__gte=cutoff, alert_time__lt=now)
            .order_by("-alert_time")
            .only("id", "domain", "alert_time", "alert_type", "alert_message")
        )
        if limit > 0:
            qs = qs[:limit]

        alerts = list(qs)
        if not alerts:
            self.stdout.write(f"no alerts: window=[{_fmt_dt(cutoff)} ~ {_fmt_dt(now)}]")
            return

        header = (
            "域名告警汇总\n"
            f"时间窗口: {_fmt_dt(cutoff)} ~ {_fmt_dt(now)}\n"
            f"命中: {len(alerts)} 条\n"
            "\n"
            "域名 | 告警时间 | 类型 | 信息\n"
        )

        lines: list[str] = []
        for a in alerts:
            atype = (a.alert_type or "").strip() or "-"
            msg = (a.alert_message or "").replace("\n", " ").strip()
            if len(msg) > 500:
                msg = msg[:500] + "..."
            lines.append(f"{a.domain} | {_fmt_dt(a.alert_time)} | {atype} | {msg}")

        messages = _chunk_lines(lines, header=header, max_chars=3500)

        sent = 0
        if dry_run:
            for m in messages:
                self.stdout.write(m)
            self.stdout.write(f"dry_run=1 messages={len(messages)}")
            return

        for m in messages:
            ok = bool(_send_telegram_message(m))
            if ok:
                sent += 1
            else:
                logger.error("telegram send failed")

        self.stdout.write(
            f"done: window=[{_fmt_dt(cutoff)} ~ {_fmt_dt(now)}] alerts={len(alerts)} messages={len(messages)} sent={sent}"
        )

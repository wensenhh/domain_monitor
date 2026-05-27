import time
import hashlib
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from monitor.domain_utils import clean_domain
from monitor.models import MonitorConfig, MonitorDomainTarget, MonitorWaitingTask
import logging

logger = logging.getLogger('monitor')

def get_int(key: str, default: int) -> int:
    row = MonitorConfig.objects.filter(key=key).only("value_int", "value_str").first()
    if not row:
        return default
    if row.value_int is not None:
        return int(row.value_int)
    if row.value_str:
        s = row.value_str.strip()
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
    return default


def _target_jitter_seconds(domain: str, max_seconds: int) -> int:
    if max_seconds <= 0:
        return 0
    h = hashlib.md5(str(domain or "").encode("utf-8")).hexdigest()
    return int(h[:8], 16) % max_seconds


def is_due(target: MonitorDomainTarget, now, *, min_interval_minutes: int, jitter_seconds: int) -> bool:
    if not target.last_scheduled_at:
        return True
    minutes = max(int(target.schedule_interval_minutes or 0), int(min_interval_minutes or 0), 1)
    # 固定域名抖动用于错开大量目标的调度时间，避免 worker 同时打第三方平台导致频控或封禁。
    jitter = _target_jitter_seconds(target.domain, max(0, int(jitter_seconds or 0)))
    return now >= target.last_scheduled_at + timedelta(minutes=minutes, seconds=jitter)


def enqueue_once() -> int:
    now = timezone.now()
    enqueued = 0
    min_interval_minutes = get_int("MIN_TARGET_INTERVAL_MINUTES", 10)
    jitter_seconds = get_int("PRODUCER_SCHEDULE_JITTER_SECONDS", 60)

    with transaction.atomic():
        active_target_ids = set(
            MonitorWaitingTask.objects.filter(target_id__isnull=False)
            .filter(
                Q(status="waiting")
                | (Q(status="running") & (Q(lease_until__isnull=True) | Q(lease_until__gte=now)))
            )
            .values_list("target_id", flat=True)
        )

        targets = MonitorDomainTarget.objects.filter(enabled=True).order_by("-priority", "id")
        logger.debug(f"本轮活跃任务目标数: {len(active_target_ids)}")

        for target in targets:
            cleaned = clean_domain(target.domain)
            if not cleaned:
                logger.info(f"跳过空域名目标: id={target.id} domain={target.domain!r}")
                continue
            if not is_due(target, now, min_interval_minutes=min_interval_minutes, jitter_seconds=jitter_seconds):
                logger.debug(f"目标 {target} 未到执行时间")
                continue

            if target.id in active_target_ids:
                logger.debug(f"目标 {target} 有活跃任务, 跳过")
                continue

            if cleaned != target.domain:
                try:
                    target.domain = cleaned
                    target.save(update_fields=["domain", "updated_at"])
                except Exception as e:
                    logger.info(f"清洗 domain 保存失败: id={target.id} domain={target.domain!r} cleaned={cleaned!r} err={e}")

            task = MonitorWaitingTask.objects.create(target=target, domain=cleaned)
            logger.info(f"创建新任务 {task} 用于目标 {target}")
            target.last_scheduled_at = now
            target.save(update_fields=["last_scheduled_at"])
            logger.debug(f"更新目标 {target} 的 last_scheduled_at 为 {now}")

            enqueued += 1

    return enqueued


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        sleep_seconds = get_int("PRODUCER_SLEEP_SECONDS", 5)
        idle_sleep_seconds = get_int("PRODUCER_IDLE_SLEEP_SECONDS", 30)
        logger.info(f"输入参数列表: sleep_seconds={sleep_seconds}, idle_sleep_seconds={idle_sleep_seconds}")

        while True:
            enqueued = enqueue_once()
            logger.info(f"处理完成, 共创建 {enqueued} 个任务, 等待 {idle_sleep_seconds if enqueued == 0 else sleep_seconds} 秒后继续")

            if options["once"]:
                return

            time.sleep(idle_sleep_seconds if enqueued == 0 else sleep_seconds)

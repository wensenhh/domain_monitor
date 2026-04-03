from __future__ import annotations

from datetime import timedelta
import logging
import re

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from monitor.models import MonitorDomainResult, MonitorTask

logger = logging.getLogger("monitor")

_STATUS_RE = re.compile(r"^\d{3}$")


def _is_success_status_code(status_code) -> bool:
    if status_code is None:
        return False
    s = str(status_code).strip()
    if not _STATUS_RE.fullmatch(s):
        return False
    code = int(s)
    return 200 <= code <= 399


class Command(BaseCommand):
    """更新 MonitorTask 表中最近 1 小时的任务的失败率"""
    def add_arguments(self, parser):
        parser.add_argument("--hours", type=float, default=1.0)
        parser.add_argument("--task-batch", type=int, default=500)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        hours = float(options["hours"])
        task_batch = int(options["task_batch"])
        dry_run = bool(options["dry_run"])

        now = timezone.now()
        cutoff = now - timedelta(seconds=int(hours * 3600))

        processed = 0
        updated = 0
        last_id = 0

        while True:
            tasks = list(
                MonitorTask.objects.filter(created_at__gte=cutoff, created_at__lt=now, id__gt=last_id)
                .exclude(status="running")
                .order_by("id")
                .only("id", "failure_rate")[:task_batch]
            )
            if not tasks:
                break

            last_id = tasks[-1].id
            task_ids = [t.id for t in tasks]

            total_by_task: dict[int, int] = {tid: 0 for tid in task_ids}
            failed_by_task: dict[int, int] = {tid: 0 for tid in task_ids}

            for task_id, status_code in MonitorDomainResult.objects.filter(task_id__in=task_ids).values_list(
                "task_id", "status_code"
            ):
                total_by_task[task_id] += 1
                if not _is_success_status_code(status_code):
                    failed_by_task[task_id] += 1

            to_update: list[MonitorTask] = []
            for t in tasks:
                total = total_by_task.get(t.id, 0)
                failed = failed_by_task.get(t.id, 0)
                rate = (failed / total) if total > 0 else 1.0

                old = float(t.failure_rate or 0.0)
                if abs(old - rate) > 1e-12:
                    t.failure_rate = float(rate)
                    to_update.append(t)

            if to_update and not dry_run:
                with transaction.atomic():
                    MonitorTask.objects.bulk_update(to_update, ["failure_rate"], batch_size=task_batch)

            processed += len(tasks)
            updated += len(to_update)

            logger.info(
                f"update_failure_rate_last_hour: window=[{cutoff.isoformat()} ~ {now.isoformat()}] "
                f"processed={processed} updated={updated} dry_run={dry_run}"
            )

        self.stdout.write(
            f"window=[{cutoff.isoformat()} ~ {now.isoformat()}] processed={processed} updated={updated} dry_run={dry_run}"
        )
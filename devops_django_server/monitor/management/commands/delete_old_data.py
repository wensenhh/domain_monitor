from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from monitor.models import MonitorDomainResult, MonitorTask, MonitorWaitingTask  # 需要清理哪个表就导入哪个
import logging

logger = logging.getLogger("monitor")

class Command(BaseCommand):
    """删除 MonitorDomainResult, MonitorTask 表中 1 天前的数据"""
    def handle(self, *args, **options):
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)

        BATCH = 5000
        total_deleted = 0

        while True:
            ids = list(
                MonitorDomainResult.objects
                .filter(updated_at__lt=yesterday_start)
                .order_by("id")
                .values_list("id", flat=True)[:BATCH]
            )
            if not ids:
                break

            with transaction.atomic():
                deleted, _ = MonitorDomainResult.objects.filter(id__in=ids).delete()
            total_deleted += deleted

        # 清理 MonitorTask 表中的旧数据
        while True:
            ids = list(
                MonitorTask.objects
                .filter(created_at__lt=yesterday_start)
                .order_by("id")
                .values_list("id", flat=True)[:BATCH]
            )
            if not ids:
                break

            with transaction.atomic():
                deleted, _ = MonitorTask.objects.filter(id__in=ids).delete()
            total_deleted += deleted
            logger.info(f"deleted MonitorTask {deleted} records")
        
        self.stdout.write(f"deleted={total_deleted}")
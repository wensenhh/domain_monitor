import os
import re
import time
import json
import urllib.request
import urllib.error
from datetime import timedelta
import hashlib
from urllib.parse import urlparse

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from monitor.domain_utils import clean_domain
from monitor.models import (
    MonitorConfig,
    MonitorAlertedDomais,
    MonitorDomainResult,
    MonitorPlatform,
    MonitorTask,
    MonitorWaitingTask,
)
from monitor.monitor_platform import run_platform

import logging

logger = logging.getLogger('monitor')

def _get_config(key: str):
    row = (
        MonitorConfig.objects.filter(key=key)
        .only("value_type", "value_str", "value_int", "value_float", "value_bool", "value_json")
        .first()
    )
    if not row:
        return None

    if row.value_type == MonitorConfig.ValueType.BOOL:
        return row.value_bool
    if row.value_type == MonitorConfig.ValueType.INT:
        return row.value_int
    if row.value_type == MonitorConfig.ValueType.FLOAT:
        return row.value_float
    if row.value_type == MonitorConfig.ValueType.JSON:
        return row.value_json
    return row.value_str


def _get_bool(key: str, default: bool) -> bool:
    v = _get_config(key)
    if v is None:
        return default
    return bool(v)


def _get_int(key: str, default: int) -> int:
    v = _get_config(key)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _get_float(key: str, default: float) -> float:
    v = _get_config(key)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _get_str(key: str, default: str) -> str:
    v = _get_config(key)
    if v is None:
        return default
    s = str(v).strip()
    if s.startswith("`") and s.endswith("`") and len(s) >= 2:
        s = s[1:-1].strip()
    return s


def _is_success_status_code(status_code) -> bool:
    logger.debug(f"check status_code={status_code}")
    if status_code is None:
        return False
    s = str(status_code).strip()
    m = re.fullmatch(r"\d{3}", s)
    if not m:
        return False
    code = int(s)
    return 200 <= code <= 399


def _calc_fail_rate(task: MonitorTask):
    qs = MonitorDomainResult.objects.filter(task=task).values_list("status_code", flat=True)
    total = 0
    failed = 0
    for sc in qs:
        total += 1
        if not _is_success_status_code(sc):
            logger.debug(f"status_code={sc} 失败")
            failed += 1
        else:
            logger.debug(f"status_code={sc} 成功")
    fail_rate = (failed / total) if total > 0 else 1.0
    return total, failed, fail_rate


def _send_telegram_message(text: str):
    token = _get_str("TG_BOT_TOKEN", "")
    chat_id = _get_str("TG_CHAT_ID", "")
    if not token or not chat_id:
        logger.info("telegram 未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过告警发送")
        return False

    sender_url = (_get_str("TELEGRAM_SENDER_URL", "") or "").strip()
    port = _get_str("DJANGO_SERVER_PORT", "") or os.environ.get("DJANGO_SERVER_PORT", "") or "8001"
    if not sender_url:
        sender_url = f"http://django-server:{port}/monitor/telegram_sender"
    if "://django-server:8000/" in sender_url:
        sender_url = sender_url.replace("://django-server:8000/", f"://django-server:{port}/")
    api_key = (_get_str("TELEGRAM_SENDER_API_KEY", "") or "").strip()
    safe_token = f"{token[:6]}***{token[-4:]}" if len(token) >= 10 else "***"
    logger.info(f"telegram_sender 发送消息 url={sender_url} chat_id={chat_id} token={safe_token} text={text}")

    payload = {
        "token": token,
        "groupid": chat_id,
        "text": text,
        "max_attempts": 5,
        "timeout_seconds": 10,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    retry_delays = [0.5, 1.0, 2.0, 4.0, 8.0]
    last_err = None
    for attempt in range(len(retry_delays)):
        req = urllib.request.Request(sender_url, data=data, headers=headers, method="POST")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=15) as resp:
                body = resp.read(5000).decode("utf-8", errors="ignore")
                try:
                    obj = json.loads(body) if body else {}
                except Exception:
                    obj = {}
                ok = bool((obj or {}).get("ok"))
                logger.info(f"telegram_sender 发送结果 http_status={getattr(resp, 'status', 200)} ok={ok}")
                return ok
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            logger.error(f"telegram_sender 发送失败 http_status={e.code} body={body[:500]}")
            return False
        except Exception as e:
            last_err = e
            retryable = isinstance(e, (urllib.error.URLError, TimeoutError))
            if not retryable and isinstance(e, OSError):
                retryable = getattr(e, "errno", None) == 104
            if not retryable or attempt == len(retry_delays) - 1:
                logger.error(f"telegram_sender 发送失败 error={e}")
                return False
            delay = retry_delays[attempt]
            try:
                import random

                delay = delay + (random.random() * 0.2 * delay)
            except Exception:
                pass
            logger.warning(
                f"telegram_sender 发送失败将重试 attempt={attempt + 1}/{len(retry_delays)} delay={delay:.2f}s error={type(e).__name__}: {e}"
            )
            time.sleep(delay)
    logger.error(f"telegram_sender 发送失败 error={last_err}")
    return False


def _record_alerted_domain(domain: str, alert_type: str, alert_message: str):
    try:
        atype = (alert_type or "")[:255] or None
        msg = (alert_message or "")[:20000] or None
        obj, created = MonitorAlertedDomais.objects.get_or_create(domain=domain, defaults={"alert_type": atype, "alert_message": msg})
        MonitorAlertedDomais.objects.filter(id=obj.id).update(alert_time=timezone.now(), alert_type=atype, alert_message=msg)
        logger.info(f"已记录告警域名到 MonitorAlertedDomais: domain={domain} created={created}")
    except Exception as e:
        logger.error(f"记录告警域名失败: domain={domain} error={e}")


def _format_alert_message(
    domain: str,
    *,
    threshold: float,
    total: int,
    rate1: float,
    rate2: float,
    primary_platform: str,
    retest_platform: str,
):
    ts = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
    threshold_pct = int(round(threshold * 100))
    rate1_pct = f"{rate1 * 100:.2f}%"
    rate2_pct = f"{rate2 * 100:.2f}%"
    return (
        f"🔥🔥🔥报警实例:  {domain}\n\n"
        f"名称: 域名失败率 > {threshold_pct}% (监控节点数 {total} )\n"
        f"时间: {ts}\n"
        f"级别： Critical\n"
        f"状态: PROBLEM\n"
        f"详情: {domain} 检测失败 ,{primary_platform}:{rate1_pct} -> {retest_platform}:{rate2_pct}"
    )


def _select_retest_platform(primary: MonitorPlatform, domain: str) -> MonitorPlatform:
    platforms = list(MonitorPlatform.objects.filter(enabled=True).order_by("id"))
    if len(platforms) <= 1:
        return primary
    others = [p for p in platforms if p.id != primary.id]
    if not others:
        return primary
    h = hashlib.md5(domain.encode("utf-8")).hexdigest()
    idx = int(h[:8], 16) % len(others)
    return others[idx]


def _run_one_check(
    *,
    platform: MonitorPlatform,
    domain: str,
    proxy: str,
    headless: bool,
    screenshot_enabled: bool,
    screenshot_dir: str,
    nav_timeout_ms: int,
    action_timeout_ms: int,
):
    domain = clean_domain(domain)
    task = MonitorTask.objects.create(
        platform=platform,
        domain=domain,
        status="running",
        proxy_ip=proxy,
        headless=headless,
        count=0,
    )

    started_at = time.monotonic()
    timings_ms = {}
    try:
        t3 = time.monotonic()
        enabled_platforms = list(MonitorPlatform.objects.filter(enabled=True).order_by("id"))
        candidates = [platform] + [p for p in enabled_platforms if p.id != platform.id]
        last_err = None
        used_platform = None
        results = None
        for p in candidates:
            try:
                results = _run_platform(
                    p,
                    domain,
                    proxy=proxy,
                    headless=headless,
                    screenshot_enabled=screenshot_enabled,
                    screenshot_dir=screenshot_dir,
                    nav_timeout_ms=nav_timeout_ms,
                    action_timeout_ms=action_timeout_ms,
                )
                used_platform = p
                break
            except Exception as e:
                last_err = e
                logger.warning(f"_run_one_check platform failed: domain={domain} platform={p} error={type(e).__name__}: {e}")
        if results is None or used_platform is None:
            raise last_err or RuntimeError("all platforms failed")
        if used_platform.id != platform.id:
            task.platform = used_platform
            task.save(update_fields=["platform"])
            platform = used_platform
        timings_ms["platform_ms"] = (time.monotonic() - t3) * 1000.0
        meta = {}
        if (
            isinstance(results, tuple)
            and len(results) == 2
            and isinstance(results[0], list)
            and isinstance(results[1], dict)
        ):
            results, meta = results
        platform_timings = meta.get("timings_ms") if isinstance(meta, dict) else None
        if isinstance(platform_timings, dict):
            for k, v in platform_timings.items():
                if isinstance(k, str) and isinstance(v, (int, float)):
                    timings_ms[f"platform_{k}"] = float(v)
        real_proxy_ip = meta.get("real_proxy_ip") if isinstance(meta, dict) else None
        if real_proxy_ip:
            task.proxy_ip = str(real_proxy_ip)[:255]
            task.save(update_fields=["proxy_ip"])

        inserted = 0
        t4 = time.monotonic()
        for r in results:
            MonitorDomainResult.objects.create(task=task, domain=domain, **r)
            inserted += 1
        timings_ms["insert_results_ms"] = (time.monotonic() - t4) * 1000.0

        task.status = "success"
        task.count = inserted
        task.browser_launch_ms = timings_ms.get("platform_browser_launch_ms")
        task.collect_ms = timings_ms.get("platform_total_platform_ms") or timings_ms.get("platform_ms")
        task.insert_ms = timings_ms.get("insert_results_ms")
        task.total_ms = (time.monotonic() - started_at) * 1000.0
        task.save(update_fields=["status", "count", "browser_launch_ms", "collect_ms", "insert_ms", "total_ms"])

        total, failed, rate = _calc_fail_rate(task)
        task.failure_rate = float(rate)
        task.save(update_fields=["failure_rate"])
        return task, total, failed, rate
    except Exception as e:
        task.status = "failed"
        task.error_type = type(e).__name__
        task.error_message = str(e)[:2000]
        task.total_ms = (time.monotonic() - started_at) * 1000.0
        task.failure_rate = 1.0
        task.save(update_fields=["status", "error_type", "error_message", "total_ms", "failure_rate"])
        return task, 0, 0, 1.0


def _claim_task(worker_id: str, lease_seconds: int) -> MonitorWaitingTask | None:
    now = timezone.now()
    lease_until = now + timedelta(seconds=lease_seconds)

    logger.info(f"尝试获取任务, LEASE_SECONDS={lease_seconds}, worker_id={worker_id}, lease_until={lease_until}")

    with transaction.atomic():
        task = (
            MonitorWaitingTask.objects.select_for_update(skip_locked=True)
            .filter(status__in=["waiting", "running"])
            .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
            .order_by("created_at", "id")
            .first()
        )
        if not task:

            return None

        task.status = "running"
        task.worker_id = worker_id
        task.lease_until = lease_until
        task.attempts = (task.attempts or 0) + 1
        task.save(update_fields=["status", "worker_id", "lease_until", "attempts", "updated_at"])
        logger.info(f"成功获取任务: {task}")

        return task


def _select_platform(waiting_task: MonitorWaitingTask) -> MonitorPlatform:
    platforms = list(MonitorPlatform.objects.filter(enabled=True).order_by("id"))
    logger.info(f"当前可用域名检测平台: {platforms}")

    if not platforms:
        logger.error("没有可用的域名检测平台")
        raise RuntimeError("no enabled platform")

    domain = clean_domain(waiting_task.domain)

    def strip_www(v: str) -> str:
        s = (v or "").strip()
        if s.lower().startswith("www."):
            return s[4:]
        if "://" in s:
            try:
                u = urlparse(s)
                host = (u.hostname or "").strip()
                if host.lower().startswith("www."):
                    host = host[4:]
                    port = f":{u.port}" if u.port else ""
                    path = u.path or ""
                    query = f"?{u.query}" if u.query else ""
                    return f"{u.scheme}://{host}{port}{path}{query}"
            except Exception:
                return s
        return s

    domain_alt = strip_www(domain)
    since = timezone.now() - timedelta(days=7)
    blacklisted_17ce = MonitorTask.objects.filter(
        platform__platform="17ce",
        status="failed",
        created_at__gte=since,
        error_message__icontains="black list",
    ).filter(Q(domain=domain) | Q(domain=domain_alt)).exists()

    filtered: list[MonitorPlatform] = []
    for p in platforms:
        if (p.platform or "").strip().lower() == "17ce" and blacklisted_17ce:
            continue
        filtered.append(p)
    if not filtered:
        filtered = platforms
    idx = waiting_task.id % len(filtered)
    return filtered[idx]


def _run_platform(
    platform: MonitorPlatform,
    domain: str,
    *,
    proxy: str,
    headless: bool,
    screenshot_enabled: bool,
    screenshot_dir: str,
    nav_timeout_ms: int,
    action_timeout_ms: int,
):
    logger.info(f"开始执行平台检测: {platform}, 域名: {domain}, 配置: proxy={proxy}, headless={headless}, screenshot_enabled={screenshot_enabled}, screenshot_dir={screenshot_dir}, nav_timeout_ms={nav_timeout_ms}, action_timeout_ms={action_timeout_ms}")
    return run_platform(
        platform,
        domain,
        proxy=proxy,
        headless=headless,
        screenshot_enabled=screenshot_enabled,
        screenshot_dir=screenshot_dir,
        nav_timeout_ms=nav_timeout_ms,
        action_timeout_ms=action_timeout_ms,
    )


def _execute_task(waiting_task: MonitorWaitingTask):
    timings_ms = {}
    overall_t0 = time.monotonic()
    headless = _get_bool("HEADLESS", True)
    proxy = _get_str("DEFAULT_PROXY", "")
    screenshot_enabled = _get_bool("SCREENSHOT_ENABLED", False)
    screenshot_dir = _get_str("SCREENSHOT_DIR", "./screenshots")
    nav_timeout_ms = _get_int("PLAYWRIGHT_NAV_TIMEOUT_MS", 60000)
    action_timeout_ms = _get_int("PLAYWRIGHT_ACTION_TIMEOUT_MS", 30000)
    threshold = _get_float("ALERT_FAIL_THRESHOLD", 0.3)
    original_domain = waiting_task.domain
    domain = clean_domain(original_domain)
    if domain != original_domain:
        waiting_task.domain = domain
        waiting_task.save(update_fields=["domain", "updated_at"])
    logger.info(f"准备执行任务: {waiting_task}. 配置: headless={headless}, proxy={proxy}, screenshot_enabled={screenshot_enabled}, screenshot_dir={screenshot_dir}, nav_timeout_ms={nav_timeout_ms}, action_timeout_ms={action_timeout_ms}, alert_threshold={threshold}")

    timings_ms["load_config_ms"] = (time.monotonic() - overall_t0) * 1000.0
    t1 = time.monotonic()
    platform = _select_platform(waiting_task)
    logger.info(f"选择平台: {platform}")
    timings_ms["select_platform_ms"] = (time.monotonic() - t1) * 1000.0

    t2 = time.monotonic()
    task = MonitorTask.objects.create(
        platform=platform,
        domain=domain,
        status="running",
        proxy_ip=proxy,
        headless=headless,
        count=0,
    )
    logger.info(f"已经创建任务: {task}. 使用配置: platform={platform}, headless={headless}, proxy={proxy}, screenshot_enabled={screenshot_enabled}, screenshot_dir={screenshot_dir}, nav_timeout_ms={nav_timeout_ms}, action_timeout_ms={action_timeout_ms}")
    timings_ms["create_task_ms"] = (time.monotonic() - t2) * 1000.0

    started_at = time.monotonic()
    try:
        t3 = time.monotonic()
        logger.info(f"开始执行任务: {task}. 配置: platform={platform}, headless={headless}, proxy={proxy}, screenshot_enabled={screenshot_enabled}, screenshot_dir={screenshot_dir}, nav_timeout_ms={nav_timeout_ms}, action_timeout_ms={action_timeout_ms}")
        enabled_platforms = list(MonitorPlatform.objects.filter(enabled=True).order_by("id"))
        candidates = [platform] + [p for p in enabled_platforms if p.id != platform.id]
        last_err = None
        used_platform = None
        results = None
        for p in candidates:
            try:
                results = _run_platform(
                    p,
                    domain,
                    proxy=proxy,
                    headless=headless,
                    screenshot_enabled=screenshot_enabled,
                    screenshot_dir=screenshot_dir,
                    nav_timeout_ms=nav_timeout_ms,
                    action_timeout_ms=action_timeout_ms,
                )
                used_platform = p
                break
            except Exception as e:
                last_err = e
                logger.warning(f"平台检测失败将尝试切换平台: domain={domain} platform={p} error={type(e).__name__}: {e}")
        if results is None or used_platform is None:
            raise last_err or RuntimeError("all platforms failed")
        if used_platform.id != platform.id:
            logger.info(f"已切换平台并继续执行: domain={domain} from={platform} to={used_platform}")
            task.platform = used_platform
            task.save(update_fields=["platform"])
            platform = used_platform
        logger.info(f"{platform} 任务执行完成: {task}. 配置: platform={platform}, headless={headless}, proxy={proxy}, screenshot_enabled={screenshot_enabled}, screenshot_dir={screenshot_dir}, nav_timeout_ms={nav_timeout_ms}, action_timeout_ms={action_timeout_ms}")
        timings_ms["platform_ms"] = (time.monotonic() - t3) * 1000.0
        meta = {}
        if (
            isinstance(results, tuple)
            and len(results) == 2
            and isinstance(results[0], list)
            and isinstance(results[1], dict)
        ):
            results, meta = results
        platform_timings = meta.get("timings_ms") if isinstance(meta, dict) else None
        if isinstance(platform_timings, dict):
            for k, v in platform_timings.items():
                if isinstance(k, str) and isinstance(v, (int, float)):
                    timings_ms[f"platform_{k}"] = float(v)
        real_proxy_ip = meta.get("real_proxy_ip") if isinstance(meta, dict) else None
        if real_proxy_ip:
            task.proxy_ip = str(real_proxy_ip)[:255]
            task.save(update_fields=["proxy_ip"])
        inserted = 0
        t4 = time.monotonic()
        for r in results:
            MonitorDomainResult.objects.create(task=task, domain=domain, **r)
            inserted += 1
        logger.info(f"已经插入 {inserted} 条检测结果到数据库")
        timings_ms["insert_results_ms"] = (time.monotonic() - t4) * 1000.0

        task.status = "success"
        task.count = inserted
        task.browser_launch_ms = timings_ms.get("platform_browser_launch_ms")
        task.collect_ms = timings_ms.get("platform_total_platform_ms") or timings_ms.get("platform_ms")
        task.insert_ms = timings_ms.get("insert_results_ms")
        task.total_ms = (time.monotonic() - started_at) * 1000.0
        t5 = time.monotonic()
        task.save(update_fields=["status", "count", "browser_launch_ms", "collect_ms", "insert_ms", "total_ms"])
        logger.info(f"已经更新任务: {task} 状态为 success")
        timings_ms["update_task_ms"] = (time.monotonic() - t5) * 1000.0

        total1, failed1, rate1 = _calc_fail_rate(task)
        task.failure_rate = float(rate1)
        task.save(update_fields=["failure_rate"])
        logger.info(f"首次失败率计算: domain={domain} total={total1} failed={failed1} rate={rate1:.4f} threshold={threshold}")

        incomplete = bool(meta.get("incomplete")) if isinstance(meta, dict) else False
        if incomplete:
            logger.info(f"跳过告警: domain={domain} itdog_stats={meta.get('itdog_stats') if isinstance(meta, dict) else None}")
        elif rate1 > threshold:
            time.sleep(2)
            retest_platform = _select_retest_platform(platform, domain)
            retest_task, total2, failed2, rate2 = _run_one_check(
                platform=retest_platform,
                domain=domain,
                proxy=proxy,
                headless=headless,
                screenshot_enabled=screenshot_enabled,
                screenshot_dir=screenshot_dir,
                nav_timeout_ms=nav_timeout_ms,
                action_timeout_ms=action_timeout_ms,
            )
            if retest_task.status == "failed":
                time.sleep(2)
                retest_task, total2, failed2, rate2 = _run_one_check(
                    platform=retest_platform,
                    domain=domain,
                    proxy=proxy,
                    headless=headless,
                    screenshot_enabled=screenshot_enabled,
                    screenshot_dir=screenshot_dir,
                    nav_timeout_ms=nav_timeout_ms,
                    action_timeout_ms=action_timeout_ms,
                )
                if retest_task.status == "failed" and retest_platform.id != platform.id:
                    time.sleep(2)
                    retest_platform = platform
                    retest_task, total2, failed2, rate2 = _run_one_check(
                        platform=retest_platform,
                        domain=domain,
                        proxy=proxy,
                        headless=headless,
                        screenshot_enabled=screenshot_enabled,
                        screenshot_dir=screenshot_dir,
                        nav_timeout_ms=nav_timeout_ms,
                        action_timeout_ms=action_timeout_ms,
                    )

            logger.info(
                f"复测失败率计算: domain={domain} task_id={retest_task.id} total={total2} failed={failed2} rate={rate2:.4f} threshold={threshold}"
            )

            if rate2 > threshold:
                msg = _format_alert_message(
                    domain,
                    threshold=threshold,
                    total=total2 or total1,
                    rate1=rate1,
                    rate2=rate2,
                    primary_platform=str(platform.platform),
                    retest_platform=str(retest_platform.platform),
                )
                ok = _send_telegram_message(msg)
                logger.info(f"告警发送结果: ok={ok} domain={domain}")

                alert_info = f"{str(platform.platform)}: {rate1 * 100:.2f}% → {str(retest_platform.platform)}: {rate2 * 100:.2f}%"
                _record_alerted_domain(domain, "fail_rate", alert_info)

        waiting_task.status = "success"
        waiting_task.error_message = ""
        waiting_task.lease_until = None
        t6 = time.monotonic()
        waiting_task.save(update_fields=["status", "error_message", "lease_until", "updated_at"])
        timings_ms["update_waiting_ms"] = (time.monotonic() - t6) * 1000.0

        timings_ms["total_ms"] = (time.monotonic() - overall_t0) * 1000.0
        return timings_ms
    except Exception as e:
        task.status = "failed"
        task.error_type = type(e).__name__
        task.error_message = str(e)[:2000]
        task.total_ms = (time.monotonic() - started_at) * 1000.0
        task.save(update_fields=["status", "error_type", "error_message", "total_ms"])
        logger.info(f"已经更新任务: {task} 状态为 failed, 错误类型: {task.error_type}, 错误信息: {task.error_message}")

        waiting_task.status = "failed"
        waiting_task.error_message = str(e)[:2000]
        waiting_task.lease_until = None
        waiting_task.save(update_fields=["status", "error_message", "lease_until", "updated_at"])
        timings_ms["total_ms"] = (time.monotonic() - overall_t0) * 1000.0
        return timings_ms


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        worker_id = f"{os.uname().nodename}:{os.getpid()}"
        lease_seconds = _get_int("TASK_LEASE_SECONDS", 300)
        max_attempts = _get_int("WORKER_MAX_ATTEMPTS", 5)
        idle_sleep_seconds = _get_int("WORKER_NO_TASK_LOOP_SECONDS", 60)
        logger.info(f"输入参数列表: worker_id={worker_id}, lease_seconds={lease_seconds}, max_attempts={max_attempts}, idle_sleep_seconds={idle_sleep_seconds}")

        while True:
            waiting_task = _claim_task(worker_id=worker_id, lease_seconds=lease_seconds)
            logger.info(f"尝试获取任务: {waiting_task}")

            if not waiting_task:
                if options["once"]:
                    return
                logger.info(f"没有任务可执行, 等待 {idle_sleep_seconds} 秒后继续")
                time.sleep(idle_sleep_seconds)
                continue

            if waiting_task.attempts > max_attempts:
                waiting_task.status = "failed"
                waiting_task.error_message = "max attempts reached"
                waiting_task.lease_until = None
                waiting_task.save(update_fields=["status", "error_message", "lease_until", "updated_at"])
                logger.info(f"任务 {waiting_task} 已失败, 最大尝试次数 {max_attempts} 次, 错误信息: {waiting_task.error_message}")
                if options["once"]:
                    return
                continue

            timings = _execute_task(waiting_task=waiting_task) or {}
            if isinstance(timings, dict):
                pairs = [(k, v) for k, v in timings.items() if isinstance(v, (int, float))]
                pairs.sort(key=lambda x: x[1], reverse=True)
                top = ", ".join([f"{k}={v:.1f}ms" for k, v in pairs[:10]])
                logger.info(f"timings domain={waiting_task.domain} {top}")
            if options["once"]:
                return

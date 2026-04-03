import json
import random
import time
import urllib.error
import urllib.request

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from monitor.models import MonitorConfig

import logging

logger = logging.getLogger("monitor")



def _get_config_str(key: str) -> str | None:
    row = MonitorConfig.objects.filter(key=key).only("value_type", "value_str").first()
    if not row:
        return None
    if row.value_type != MonitorConfig.ValueType.STR:
        return None
    return row.value_str


def _safe_token(token: str) -> str:
    if not token:
        return ""
    if len(token) < 10:
        return "***"
    return f"{token[:6]}***{token[-4:]}"


def _send_telegram_message(token: str, chat_id: str, text: str, *, timeout_seconds: int, max_attempts: int):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    data = json.dumps(payload).encode("utf-8")
    logger.info(f"send_telegram_message: token={_safe_token(token)}, chat_id={chat_id}, text={text[:500]}")
    delays = [0.5, 1.0, 2.0, 4.0, 8.0]
    if max_attempts < 1:
        max_attempts = 1
    max_attempts = min(max_attempts, len(delays))
    for attempt in range(max_attempts):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=timeout_seconds) as resp:
                status = getattr(resp, "status", 200)
                ok = 200 <= status < 300
                logger.info(f"send_telegram_message: token={_safe_token(token)}, chat_id={chat_id}, text={text[:500]}, status={status}, ok={ok}")
                return {"ok": ok, "http_status": status, "attempt": attempt + 1}
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="ignore")
                logger.error(f"send_telegram_message: token={_safe_token(token)}, chat_id={chat_id}, text={text[:500]}, status={e.code}, error={body[:500]}")
            except Exception:
                body = ""
            return {"ok": False, "http_status": e.code, "attempt": attempt + 1, "error": body[:500]}
        except Exception as e:
            retryable = isinstance(e, (urllib.error.URLError, TimeoutError))
            logger.error(f"send_telegram_message: token={_safe_token(token)}, chat_id={chat_id}, text={text[:500]}, attempt={attempt + 1}, error={type(e).__name__}: {e}")
            if not retryable and isinstance(e, OSError):
                retryable = getattr(e, "errno", None) == 104
            if not retryable or attempt == max_attempts - 1:
                return {"ok": False, "attempt": attempt + 1, "error": f"{type(e).__name__}: {e}"}
            delay = delays[attempt]
            delay = delay + (random.random() * 0.2 * delay)
            time.sleep(delay)
    
    return {"ok": False, "attempt": max_attempts, "error": "unknown"}


@csrf_exempt
def telegram_sender(request):
    if request.method not in {"POST"}:
        return JsonResponse({"ok": False, "error": "method_not_allowed"}, status=405)

    api_key_cfg = (_get_config_str("TELEGRAM_SENDER_API_KEY") or "").strip()
    logger.info(f"telegram_sender: 输入参数 re")
    if api_key_cfg:
        api_key = (request.headers.get("X-Api-Key") or "").strip()
        if api_key != api_key_cfg:
            return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)

    try:
        if request.content_type and "application/json" in request.content_type.lower():
            payload = json.loads((request.body or b"{}").decode("utf-8", errors="ignore") or "{}")
        else:
            payload = request.POST.dict()
    except Exception:
        return JsonResponse({"ok": False, "error": "bad_request"}, status=400)

    token = str((payload or {}).get("token") or "").strip()
    chat_id = str((payload or {}).get("groupid") or (payload or {}).get("chat_id") or "").strip()
    text = str((payload or {}).get("text") or "").strip()
    timeout_seconds = int((payload or {}).get("timeout_seconds") or 10)
    max_attempts = int((payload or {}).get("max_attempts") or 5)
    logger.info(f"telegram_sender: 输入参数 token={_safe_token(token)}, chat_id={chat_id}, text={text[:500]}, timeout_seconds={timeout_seconds}, max_attempts={max_attempts}")
    if not token or not chat_id or not text:
        return JsonResponse({"ok": False, "error": "missing_required_fields"}, status=400)

    if timeout_seconds < 1:
        timeout_seconds = 1
    timeout_seconds = min(timeout_seconds, 60)
    if max_attempts < 1:
        max_attempts = 1
    max_attempts = min(max_attempts, 5)

    result = _send_telegram_message(token, chat_id, text, timeout_seconds=timeout_seconds, max_attempts=max_attempts)
    logger.info(f"telegram_sender: 输出参数 {result}")
    result["token"] = _safe_token(token)
    result["chat_id"] = chat_id
    return JsonResponse(result, status=200)

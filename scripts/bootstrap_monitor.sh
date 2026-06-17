#!/usr/bin/env bash
set -Eeuo pipefail

COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-vp-monitor}"
DJANGO_SERVICE="${DJANGO_SERVICE:-django-server}"
INSTALL_DIR="${INSTALL_DIR:-/opt/domain_monitor}"
TEST_DOMAINS="${TEST_DOMAINS:-example.com,www.cloudflare.com,www.google.com}"
RUN_CHECKS="${RUN_CHECKS:-1}"
WORKER_RUNS="${WORKER_RUNS:-1}"
FORCE_ALERT_TEST="${FORCE_ALERT_TEST:-1}"
BOOTSTRAP_REQUIRE_TELEGRAM="${BOOTSTRAP_REQUIRE_TELEGRAM:-1}"

HEADLESS="${HEADLESS:-true}"
DEFAULT_PROXY="${DEFAULT_PROXY:-}"
SCREENSHOT_ENABLED="${SCREENSHOT_ENABLED:-false}"
SCREENSHOT_DIR="${SCREENSHOT_DIR:-./screenshots}"
PLAYWRIGHT_NAV_TIMEOUT_MS="${PLAYWRIGHT_NAV_TIMEOUT_MS:-60000}"
PLAYWRIGHT_ACTION_TIMEOUT_MS="${PLAYWRIGHT_ACTION_TIMEOUT_MS:-30000}"
ALERT_FAIL_THRESHOLD="${ALERT_FAIL_THRESHOLD:-0.3}"
TASK_LEASE_SECONDS="${TASK_LEASE_SECONDS:-300}"
WORKER_MAX_ATTEMPTS="${WORKER_MAX_ATTEMPTS:-5}"
WORKER_NO_TASK_LOOP_SECONDS="${WORKER_NO_TASK_LOOP_SECONDS:-60}"
PRODUCER_SLEEP_SECONDS="${PRODUCER_SLEEP_SECONDS:-5}"
PRODUCER_IDLE_SLEEP_SECONDS="${PRODUCER_IDLE_SLEEP_SECONDS:-30}"
TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"
TELEGRAM_SENDER_URL="${TELEGRAM_SENDER_URL:-}"
TELEGRAM_SENDER_API_KEY="${TELEGRAM_SENDER_API_KEY:-}"
DJANGO_SERVER_PORT="${DJANGO_SERVER_PORT:-8001}"
TEST_TELEGRAM_TEXT="${TEST_TELEGRAM_TEXT:-devops-monitor bootstrap telegram test}"
TEST_ALERT_DOMAIN="${TEST_ALERT_DOMAIN:-bootstrap-alert-test.invalid}"

usage() {
  cat <<EOF
Usage:
  TG_BOT_TOKEN=<token> TG_CHAT_ID=<chat_id> bash scripts/bootstrap_monitor.sh

Writes required MonitorConfig rows, enables monitor platforms, adds test domains,
sends a Telegram test message, and runs producer/worker once for smoke testing.

Environment variables:
  INSTALL_DIR=/opt/domain_monitor
  COMPOSE_PROJECT_NAME=vp-monitor
  TEST_DOMAINS=example.com,www.cloudflare.com,www.google.com
  RUN_CHECKS=1                 # set 0 to only write config/domains
  WORKER_RUNS=1                # number of worker --once executions
  FORCE_ALERT_TEST=1           # send a simulated alert and write alert table
  BOOTSTRAP_REQUIRE_TELEGRAM=1 # fail fast when Telegram config is missing
  TG_BOT_TOKEN=<required for Telegram test>
  TG_CHAT_ID=<required for Telegram test>
  TELEGRAM_SENDER_API_KEY=<optional>
  DEFAULT_PROXY=http://user:pass@host:port
  ALERT_FAIL_THRESHOLD=0.3

Options:
  --help                       Show this help.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

log() {
  printf '[bootstrap] %s\n' "$*"
}

die() {
  printf '[bootstrap][error] %s\n' "$*" >&2
  exit 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

if ! command_exists docker; then
  die "docker command not found"
fi

if ! docker compose version >/dev/null 2>&1; then
  die "docker compose v2 not found"
fi

if [[ -d "$INSTALL_DIR" ]]; then
  cd "$INSTALL_DIR"
fi

compose() {
  docker compose -p "$COMPOSE_PROJECT_NAME" "$@"
}

require_service() {
  compose ps "$DJANGO_SERVICE" >/dev/null 2>&1 || die "compose service not found: $DJANGO_SERVICE"
}

config_value() {
  local key="$1"
  compose exec -T -e "BOOT_CONFIG_KEY=${key}" "$DJANGO_SERVICE" python devops_django_server/manage.py shell <<'PY' | tail -n 1
import os
from monitor.models import MonitorConfig

key = os.environ["BOOT_CONFIG_KEY"]
row = MonitorConfig.objects.filter(key=key).first()
if not row:
    print("")
elif row.value_type == MonitorConfig.ValueType.BOOL:
    print("true" if row.value_bool else "false")
elif row.value_type == MonitorConfig.ValueType.INT:
    print("" if row.value_int is None else row.value_int)
elif row.value_type == MonitorConfig.ValueType.FLOAT:
    print("" if row.value_float is None else row.value_float)
elif row.value_type == MonitorConfig.ValueType.JSON:
    print("" if row.value_json is None else row.value_json)
else:
    print(row.value_str or "")
PY
}

require_telegram_settings() {
  if [[ -z "$TG_BOT_TOKEN" ]]; then
    TG_BOT_TOKEN="$(config_value TG_BOT_TOKEN || true)"
  fi
  if [[ -z "$TG_CHAT_ID" ]]; then
    TG_CHAT_ID="$(config_value TG_CHAT_ID || true)"
  fi
  if [[ -z "$TELEGRAM_SENDER_API_KEY" ]]; then
    TELEGRAM_SENDER_API_KEY="$(config_value TELEGRAM_SENDER_API_KEY || true)"
  fi

  if [[ "$BOOTSTRAP_REQUIRE_TELEGRAM" == "1" && "$RUN_CHECKS" == "1" ]]; then
    if [[ -z "$TG_BOT_TOKEN" || -z "$TG_CHAT_ID" ]]; then
      die "Telegram config is missing. Re-run with TG_BOT_TOKEN='<token>' TG_CHAT_ID='<chat_id>' or add TG_BOT_TOKEN/TG_CHAT_ID in MonitorConfig."
    fi
  fi
}

seed_config_and_domains() {
  log "Writing monitor platforms, config rows, and test domains."
  compose exec -T \
    -e "BOOT_HEADLESS=${HEADLESS}" \
    -e "BOOT_DEFAULT_PROXY=${DEFAULT_PROXY}" \
    -e "BOOT_SCREENSHOT_ENABLED=${SCREENSHOT_ENABLED}" \
    -e "BOOT_SCREENSHOT_DIR=${SCREENSHOT_DIR}" \
    -e "BOOT_PLAYWRIGHT_NAV_TIMEOUT_MS=${PLAYWRIGHT_NAV_TIMEOUT_MS}" \
    -e "BOOT_PLAYWRIGHT_ACTION_TIMEOUT_MS=${PLAYWRIGHT_ACTION_TIMEOUT_MS}" \
    -e "BOOT_ALERT_FAIL_THRESHOLD=${ALERT_FAIL_THRESHOLD}" \
    -e "BOOT_TASK_LEASE_SECONDS=${TASK_LEASE_SECONDS}" \
    -e "BOOT_WORKER_MAX_ATTEMPTS=${WORKER_MAX_ATTEMPTS}" \
    -e "BOOT_WORKER_NO_TASK_LOOP_SECONDS=${WORKER_NO_TASK_LOOP_SECONDS}" \
    -e "BOOT_PRODUCER_SLEEP_SECONDS=${PRODUCER_SLEEP_SECONDS}" \
    -e "BOOT_PRODUCER_IDLE_SLEEP_SECONDS=${PRODUCER_IDLE_SLEEP_SECONDS}" \
    -e "BOOT_TG_BOT_TOKEN=${TG_BOT_TOKEN}" \
    -e "BOOT_TG_CHAT_ID=${TG_CHAT_ID}" \
    -e "BOOT_TELEGRAM_SENDER_URL=${TELEGRAM_SENDER_URL}" \
    -e "BOOT_TELEGRAM_SENDER_API_KEY=${TELEGRAM_SENDER_API_KEY}" \
    -e "BOOT_DJANGO_SERVER_PORT=${DJANGO_SERVER_PORT}" \
    -e "BOOT_TEST_DOMAINS=${TEST_DOMAINS}" \
    "$DJANGO_SERVICE" python devops_django_server/manage.py shell <<'PY'
import os
from monitor.domain_utils import clean_domain
from monitor.models import MonitorConfig, MonitorDomainTarget, MonitorPlatform


def as_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def upsert_config(key, value_type, value, description):
    defaults = {
        "value_type": value_type,
        "value_str": None,
        "value_int": None,
        "value_float": None,
        "value_bool": None,
        "value_json": None,
        "description": description,
    }
    if value_type == MonitorConfig.ValueType.BOOL:
        defaults["value_bool"] = as_bool(value)
    elif value_type == MonitorConfig.ValueType.INT:
        defaults["value_int"] = int(value)
    elif value_type == MonitorConfig.ValueType.FLOAT:
        defaults["value_float"] = float(value)
    else:
        defaults["value_str"] = str(value or "")
    MonitorConfig.objects.update_or_create(key=key, defaults=defaults)


platforms = [
    ("itdog", "https://www.itdog.cn/http/", True),
    ("17ce", "https://17ce.com/get", True),
    ("chinaz", "https://tool.chinaz.com/speedtest/", False),
]
for name, url, enabled in platforms:
    MonitorPlatform.objects.update_or_create(
        platform=name,
        defaults={"website_url": url, "enabled": enabled},
    )

config_specs = [
    ("HEADLESS", "bool", os.environ.get("BOOT_HEADLESS", "true"), "Playwright headless mode"),
    ("DEFAULT_PROXY", "str", os.environ.get("BOOT_DEFAULT_PROXY", ""), "Proxy for monitor platform access"),
    ("SCREENSHOT_ENABLED", "bool", os.environ.get("BOOT_SCREENSHOT_ENABLED", "false"), "Enable screenshots for troubleshooting"),
    ("SCREENSHOT_DIR", "str", os.environ.get("BOOT_SCREENSHOT_DIR", "./screenshots"), "Screenshot directory"),
    ("PLAYWRIGHT_NAV_TIMEOUT_MS", "int", os.environ.get("BOOT_PLAYWRIGHT_NAV_TIMEOUT_MS", "60000"), "Playwright navigation timeout"),
    ("PLAYWRIGHT_ACTION_TIMEOUT_MS", "int", os.environ.get("BOOT_PLAYWRIGHT_ACTION_TIMEOUT_MS", "30000"), "Playwright action timeout"),
    ("ALERT_FAIL_THRESHOLD", "float", os.environ.get("BOOT_ALERT_FAIL_THRESHOLD", "0.3"), "Failure rate alert threshold"),
    ("TASK_LEASE_SECONDS", "int", os.environ.get("BOOT_TASK_LEASE_SECONDS", "300"), "Worker task lease seconds"),
    ("WORKER_MAX_ATTEMPTS", "int", os.environ.get("BOOT_WORKER_MAX_ATTEMPTS", "5"), "Maximum attempts per waiting task"),
    ("WORKER_NO_TASK_LOOP_SECONDS", "int", os.environ.get("BOOT_WORKER_NO_TASK_LOOP_SECONDS", "60"), "Worker idle sleep seconds"),
    ("PRODUCER_SLEEP_SECONDS", "int", os.environ.get("BOOT_PRODUCER_SLEEP_SECONDS", "5"), "Producer active sleep seconds"),
    ("PRODUCER_IDLE_SLEEP_SECONDS", "int", os.environ.get("BOOT_PRODUCER_IDLE_SLEEP_SECONDS", "30"), "Producer idle sleep seconds"),
    ("DJANGO_SERVER_PORT", "str", os.environ.get("BOOT_DJANGO_SERVER_PORT", "8001"), "Django server port for internal sender URL"),
]
for spec in config_specs:
    upsert_config(*spec)

secret_specs = [
    ("TG_BOT_TOKEN", os.environ.get("BOOT_TG_BOT_TOKEN", ""), "Telegram bot token"),
    ("TG_CHAT_ID", os.environ.get("BOOT_TG_CHAT_ID", ""), "Telegram chat id"),
    ("TELEGRAM_SENDER_URL", os.environ.get("BOOT_TELEGRAM_SENDER_URL", ""), "Telegram sender URL"),
    ("TELEGRAM_SENDER_API_KEY", os.environ.get("BOOT_TELEGRAM_SENDER_API_KEY", ""), "Telegram sender API key"),
]
for key, value, description in secret_specs:
    if str(value or "").strip():
        upsert_config(key, "str", value, description)

raw_domains = os.environ.get("BOOT_TEST_DOMAINS", "")
created = []
updated = []
for item in raw_domains.replace("\n", ",").replace("，", ",").replace(";", ",").replace("；", ",").split(","):
    domain = clean_domain(item)
    if not domain:
        continue
    obj, was_created = MonitorDomainTarget.objects.update_or_create(
        domain=domain,
        defaults={"enabled": True, "priority": 100, "schedule_interval_minutes": 10},
    )
    if was_created:
        created.append(domain)
    else:
        updated.append(domain)

print(f"platforms={MonitorPlatform.objects.count()}")
print(f"configs={MonitorConfig.objects.count()}")
print(f"test_domains_created={created}")
print(f"test_domains_updated={updated}")
PY
}

send_telegram_test() {
  log "Sending Telegram test message through /monitor/telegram_sender."
  compose exec -T \
    -e "BOOT_TG_BOT_TOKEN=${TG_BOT_TOKEN}" \
    -e "BOOT_TG_CHAT_ID=${TG_CHAT_ID}" \
    -e "BOOT_TELEGRAM_SENDER_API_KEY=${TELEGRAM_SENDER_API_KEY}" \
    -e "BOOT_TEST_TELEGRAM_TEXT=${TEST_TELEGRAM_TEXT}" \
    "$DJANGO_SERVICE" python devops_django_server/manage.py shell <<'PY'
import json
import os
from django.test import Client

headers = {}
api_key = os.environ.get("BOOT_TELEGRAM_SENDER_API_KEY", "").strip()
if api_key:
    headers["HTTP_X_API_KEY"] = api_key

payload = {
    "token": os.environ["BOOT_TG_BOT_TOKEN"],
    "groupid": os.environ["BOOT_TG_CHAT_ID"],
    "text": os.environ.get("BOOT_TEST_TELEGRAM_TEXT", "devops-monitor telegram test"),
    "timeout_seconds": 10,
    "max_attempts": 5,
}
resp = Client().post(
    "/monitor/telegram_sender",
    data=json.dumps(payload),
    content_type="application/json",
    **headers,
)
body = resp.content.decode("utf-8", errors="ignore")
print(f"telegram_sender_status={resp.status_code}")
print(f"telegram_sender_body={body}")
if resp.status_code != 200:
    raise SystemExit(1)
try:
    obj = json.loads(body)
except Exception:
    obj = {}
if not obj.get("ok"):
    raise SystemExit(2)
PY
}

send_forced_alert_test() {
  if [[ "$FORCE_ALERT_TEST" != "1" ]]; then
    log "FORCE_ALERT_TEST=0, skip simulated alert push test."
    return
  fi

  log "Sending simulated alert through worker alert sender."
  compose exec -T \
    -e "BOOT_TEST_ALERT_DOMAIN=${TEST_ALERT_DOMAIN}" \
    "$DJANGO_SERVICE" python devops_django_server/manage.py shell <<'PY'
import os
from django.utils import timezone

from monitor.management.commands.worker import (
    _record_alerted_domain,
    _send_telegram_message,
)

domain = os.environ.get("BOOT_TEST_ALERT_DOMAIN", "bootstrap-alert-test.invalid")
ts = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
msg = (
    f"🔥🔥🔥报警实例:  {domain}\n\n"
    "名称: 告警链路测试 > 30% (监控节点数 1 )\n"
    f"时间: {ts}\n"
    "级别： Critical\n"
    "状态: PROBLEM\n"
    "详情: 这是一条 bootstrap 模拟告警，用于验证 Telegram 告警推送和告警表入库"
)
ok = bool(_send_telegram_message(msg))
print(f"forced_alert_send_ok={ok}")
_record_alerted_domain(domain, "bootstrap_alert_test", "simulated alert push test")
if not ok:
    raise SystemExit(3)
PY
}

run_monitor_smoke() {
  log "Running producer --once."
  compose exec -T "$DJANGO_SERVICE" python devops_django_server/manage.py producer --once

  local i
  for i in $(seq 1 "$WORKER_RUNS"); do
    log "Running worker --once (${i}/${WORKER_RUNS}). This can take 1-3 minutes."
    compose exec -T "$DJANGO_SERVICE" python devops_django_server/manage.py worker --once
  done

  log "Printing smoke test summary."
  compose exec -T "$DJANGO_SERVICE" python devops_django_server/manage.py shell <<'PY'
from monitor.models import MonitorDomainResult, MonitorDomainTarget, MonitorTask, MonitorWaitingTask

print(f"targets={MonitorDomainTarget.objects.count()}")
print(f"waiting_tasks={MonitorWaitingTask.objects.count()}")
print(f"tasks={MonitorTask.objects.count()}")
print(f"results={MonitorDomainResult.objects.count()}")

for task in MonitorTask.objects.order_by("-id")[:5]:
    print(
        "task",
        f"id={task.id}",
        f"domain={task.domain}",
        f"platform={task.platform}",
        f"status={task.status}",
        f"count={task.count}",
        f"failure_rate={task.failure_rate}",
        f"error={task.error_type or ''}:{(task.error_message or '')[:120]}",
    )
PY
}

main() {
  require_service
  require_telegram_settings
  seed_config_and_domains
  if [[ "$RUN_CHECKS" == "1" ]]; then
    send_telegram_test
    send_forced_alert_test
    run_monitor_smoke
  else
    log "RUN_CHECKS=0, skipped Telegram and monitor smoke tests."
  fi
  log "Bootstrap finished."
}

main "$@"

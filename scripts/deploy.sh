#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/wensenhh/domain_monitor.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/domain_monitor}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-vp-monitor}"
DJANGO_SERVER_PORT="${DJANGO_SERVER_PORT:-8001}"
POSTGRES_DB_LISTEN_PORT="${POSTGRES_DB_LISTEN_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-devops_monitor}"
POSTGRES_USER="${POSTGRES_USER:-devops_monitor}"
POSTGRES_HOST="${POSTGRES_HOST:-db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
WORKERS="${WORKERS:-2}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
DJANGO_LOG_LEVEL="${DJANGO_LOG_LEVEL:-INFO}"

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

ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
CREATE_SUPERUSER="${CREATE_SUPERUSER:-1}"

usage() {
  cat <<EOF
Usage:
  bash scripts/deploy.sh

Deploys ${REPO_URL} to INSTALL_DIR and starts the Django monitor stack.

Common environment variables:
  INSTALL_DIR=/opt/domain_monitor
  BRANCH=main
  COMPOSE_PROJECT_NAME=vp-monitor
  DJANGO_SERVER_PORT=8001
  POSTGRES_DB_LISTEN_PORT=5432
  POSTGRES_PASSWORD=<auto generated when empty>
  WORKERS=2
  TG_BOT_TOKEN=<telegram bot token>
  TG_CHAT_ID=<telegram chat id>
  DEFAULT_PROXY=http://user:pass@host:port
  ALERT_FAIL_THRESHOLD=0.3
  ADMIN_USERNAME=admin
  ADMIN_PASSWORD=<auto generated when empty>

Private GitHub repo:
  GITHUB_TOKEN=ghp_xxx bash scripts/deploy.sh

Examples:
  TG_BOT_TOKEN=123:abc TG_CHAT_ID=-100123 WORKERS=4 bash scripts/deploy.sh
  INSTALL_DIR=/data/domain_monitor DJANGO_SERVER_PORT=18001 bash scripts/deploy.sh

Options:
  --help                Show this help.
  --print-env-template Print a starter .env template and exit.
EOF
}

print_env_template() {
  cat <<EOF
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME}
POSTGRES_DB_LISTEN_PORT=${POSTGRES_DB_LISTEN_PORT}
POSTGRES_DB=${POSTGRES_DB}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=change-me
POSTGRES_HOST=${POSTGRES_HOST}
POSTGRES_PORT=${POSTGRES_PORT}
DJANGO_SERVER_PORT=${DJANGO_SERVER_PORT}
LOG_LEVEL=${LOG_LEVEL}
DJANGO_LOG_LEVEL=${DJANGO_LOG_LEVEL}
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "--print-env-template" ]]; then
  print_env_template
  exit 0
fi

log() {
  printf '[deploy] %s\n' "$*"
}

die() {
  printf '[deploy][error] %s\n' "$*" >&2
  exit 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

random_secret() {
  if command_exists openssl; then
    openssl rand -base64 32 | tr -d '\n'
  else
    tr -dc 'A-Za-z0-9_@%+=:,.^-' </dev/urandom | head -c 40
  fi
}

if [[ "$(id -u)" -ne 0 ]]; then
  if command_exists sudo; then
    log "Re-running as root with sudo."
    exec sudo -E bash "$0" "$@"
  fi
  die "Please run as root, or install sudo and retry."
fi

install_packages() {
  local packages=("$@")
  if command_exists apt-get; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y "${packages[@]}"
  elif command_exists dnf; then
    dnf install -y "${packages[@]}"
  elif command_exists yum; then
    yum install -y "${packages[@]}"
  else
    die "No supported package manager found. Install packages manually: ${packages[*]}"
  fi
}

ensure_basic_tools() {
  local missing=()
  command_exists git || missing+=("git")
  command_exists curl || missing+=("curl")
  command_exists openssl || missing+=("openssl")

  if ((${#missing[@]} > 0)); then
    log "Installing missing tools: ${missing[*]}"
    install_packages "${missing[@]}"
  fi
}

ensure_docker() {
  if ! command_exists docker; then
    log "Docker not found. Installing docker."
    if command_exists apt-get; then
      install_packages docker.io
    else
      install_packages docker
    fi
  fi

  if command_exists systemctl; then
    systemctl enable --now docker >/dev/null 2>&1 || true
  else
    service docker start >/dev/null 2>&1 || true
  fi

  docker version >/dev/null || die "Docker is installed but not running."
}

install_compose_plugin_package() {
  if command_exists apt-get; then
    apt-get update -y
    apt-get install -y docker-compose-plugin && return 0
  elif command_exists dnf; then
    dnf install -y docker-compose-plugin && return 0
  elif command_exists yum; then
    yum install -y docker-compose-plugin && return 0
  fi
  return 1
}

install_compose_plugin_binary() {
  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) arch="x86_64" ;;
    aarch64|arm64) arch="aarch64" ;;
    *) die "Unsupported CPU architecture for Docker Compose plugin: $arch" ;;
  esac

  local plugin_dir="/usr/local/lib/docker/cli-plugins"
  mkdir -p "$plugin_dir"
  log "Installing Docker Compose plugin from GitHub latest release."
  curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${arch}" \
    -o "${plugin_dir}/docker-compose"
  chmod +x "${plugin_dir}/docker-compose"
}

ensure_compose() {
  if docker compose version >/dev/null 2>&1; then
    return
  fi

  log "Docker Compose v2 not found. Installing compose plugin."
  install_compose_plugin_package || install_compose_plugin_binary

  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 install failed."
}

git_cmd() {
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    git -c "http.https://github.com/.extraheader=AUTHORIZATION: bearer ${GITHUB_TOKEN}" "$@"
  else
    git "$@"
  fi
}

sync_repo() {
  mkdir -p "$(dirname "$INSTALL_DIR")"

  if [[ -d "$INSTALL_DIR/.git" ]]; then
    log "Updating existing repository: $INSTALL_DIR"
    git_cmd -C "$INSTALL_DIR" fetch origin "$BRANCH"
    git_cmd -C "$INSTALL_DIR" checkout "$BRANCH"
    git_cmd -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
  elif [[ -e "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    die "INSTALL_DIR exists and is not an empty git repository: $INSTALL_DIR"
  else
    log "Cloning repository: $REPO_URL -> $INSTALL_DIR"
    git_cmd clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
}

env_get() {
  local key="$1"
  local file="$INSTALL_DIR/.env"
  [[ -f "$file" ]] || return 1
  grep -E "^${key}=" "$file" | tail -n 1 | cut -d= -f2-
}

env_set_if_missing() {
  local key="$1"
  local value="$2"
  local file="$INSTALL_DIR/.env"
  touch "$file"
  chmod 600 "$file"
  if ! grep -qE "^${key}=" "$file"; then
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

write_env_file() {
  local existing_password=""
  existing_password="$(env_get POSTGRES_PASSWORD || true)"
  POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$existing_password}"
  if [[ -z "$POSTGRES_PASSWORD" ]]; then
    POSTGRES_PASSWORD="$(random_secret)"
  fi

  log "Ensuring .env exists."
  env_set_if_missing COMPOSE_PROJECT_NAME "$COMPOSE_PROJECT_NAME"
  env_set_if_missing POSTGRES_DB_LISTEN_PORT "$POSTGRES_DB_LISTEN_PORT"
  env_set_if_missing POSTGRES_DB "$POSTGRES_DB"
  env_set_if_missing POSTGRES_USER "$POSTGRES_USER"
  env_set_if_missing POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
  env_set_if_missing POSTGRES_HOST "$POSTGRES_HOST"
  env_set_if_missing POSTGRES_PORT "$POSTGRES_PORT"
  env_set_if_missing DJANGO_SERVER_PORT "$DJANGO_SERVER_PORT"
  env_set_if_missing LOG_LEVEL "$LOG_LEVEL"
  env_set_if_missing DJANGO_LOG_LEVEL "$DJANGO_LOG_LEVEL"
}

compose() {
  docker compose -p "$COMPOSE_PROJECT_NAME" "$@"
}

wait_for_db() {
  log "Waiting for PostgreSQL."
  local i
  for i in $(seq 1 60); do
    if compose exec -T db pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
      return
    fi
    sleep 2
  done
  die "PostgreSQL did not become ready in time."
}

validate_workers() {
  if ! [[ "$WORKERS" =~ ^[0-9]+$ ]]; then
    die "WORKERS must be a number."
  fi
  if ((WORKERS < 1 || WORKERS > 16)); then
    die "WORKERS must be between 1 and 16 because docker-compose.yml defines monitor-worker1..16."
  fi
}

seed_database() {
  log "Seeding monitor platforms and config."
  compose run --rm -T \
    -e "SEED_HEADLESS=${HEADLESS}" \
    -e "SEED_DEFAULT_PROXY=${DEFAULT_PROXY}" \
    -e "SEED_SCREENSHOT_ENABLED=${SCREENSHOT_ENABLED}" \
    -e "SEED_SCREENSHOT_DIR=${SCREENSHOT_DIR}" \
    -e "SEED_PLAYWRIGHT_NAV_TIMEOUT_MS=${PLAYWRIGHT_NAV_TIMEOUT_MS}" \
    -e "SEED_PLAYWRIGHT_ACTION_TIMEOUT_MS=${PLAYWRIGHT_ACTION_TIMEOUT_MS}" \
    -e "SEED_ALERT_FAIL_THRESHOLD=${ALERT_FAIL_THRESHOLD}" \
    -e "SEED_TASK_LEASE_SECONDS=${TASK_LEASE_SECONDS}" \
    -e "SEED_WORKER_MAX_ATTEMPTS=${WORKER_MAX_ATTEMPTS}" \
    -e "SEED_WORKER_NO_TASK_LOOP_SECONDS=${WORKER_NO_TASK_LOOP_SECONDS}" \
    -e "SEED_PRODUCER_SLEEP_SECONDS=${PRODUCER_SLEEP_SECONDS}" \
    -e "SEED_PRODUCER_IDLE_SLEEP_SECONDS=${PRODUCER_IDLE_SLEEP_SECONDS}" \
    -e "SEED_TG_BOT_TOKEN=${TG_BOT_TOKEN}" \
    -e "SEED_TG_CHAT_ID=${TG_CHAT_ID}" \
    -e "SEED_TELEGRAM_SENDER_URL=${TELEGRAM_SENDER_URL}" \
    -e "SEED_TELEGRAM_SENDER_API_KEY=${TELEGRAM_SENDER_API_KEY}" \
    -e "SEED_DJANGO_SERVER_PORT=${DJANGO_SERVER_PORT}" \
    django-server python devops_django_server/manage.py shell <<'PY'
import os
from monitor.models import MonitorConfig, MonitorPlatform


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
    ("HEADLESS", "bool", os.environ.get("SEED_HEADLESS", "true"), "Playwright headless mode"),
    ("DEFAULT_PROXY", "str", os.environ.get("SEED_DEFAULT_PROXY", ""), "Proxy for monitor platform access"),
    ("SCREENSHOT_ENABLED", "bool", os.environ.get("SEED_SCREENSHOT_ENABLED", "false"), "Enable screenshots for troubleshooting"),
    ("SCREENSHOT_DIR", "str", os.environ.get("SEED_SCREENSHOT_DIR", "./screenshots"), "Screenshot directory"),
    ("PLAYWRIGHT_NAV_TIMEOUT_MS", "int", os.environ.get("SEED_PLAYWRIGHT_NAV_TIMEOUT_MS", "60000"), "Playwright navigation timeout"),
    ("PLAYWRIGHT_ACTION_TIMEOUT_MS", "int", os.environ.get("SEED_PLAYWRIGHT_ACTION_TIMEOUT_MS", "30000"), "Playwright action timeout"),
    ("ALERT_FAIL_THRESHOLD", "float", os.environ.get("SEED_ALERT_FAIL_THRESHOLD", "0.3"), "Failure rate threshold for alerting"),
    ("TASK_LEASE_SECONDS", "int", os.environ.get("SEED_TASK_LEASE_SECONDS", "300"), "Worker task lease seconds"),
    ("WORKER_MAX_ATTEMPTS", "int", os.environ.get("SEED_WORKER_MAX_ATTEMPTS", "5"), "Maximum attempts per waiting task"),
    ("WORKER_NO_TASK_LOOP_SECONDS", "int", os.environ.get("SEED_WORKER_NO_TASK_LOOP_SECONDS", "60"), "Worker idle sleep seconds"),
    ("PRODUCER_SLEEP_SECONDS", "int", os.environ.get("SEED_PRODUCER_SLEEP_SECONDS", "5"), "Producer active sleep seconds"),
    ("PRODUCER_IDLE_SLEEP_SECONDS", "int", os.environ.get("SEED_PRODUCER_IDLE_SLEEP_SECONDS", "30"), "Producer idle sleep seconds"),
    ("DJANGO_SERVER_PORT", "str", os.environ.get("SEED_DJANGO_SERVER_PORT", "8001"), "Django server port for internal sender URL"),
]
for spec in config_specs:
    upsert_config(*spec)

optional_secret_specs = [
    ("TG_BOT_TOKEN", os.environ.get("SEED_TG_BOT_TOKEN", ""), "Telegram bot token"),
    ("TG_CHAT_ID", os.environ.get("SEED_TG_CHAT_ID", ""), "Telegram chat id"),
    ("TELEGRAM_SENDER_URL", os.environ.get("SEED_TELEGRAM_SENDER_URL", ""), "Telegram sender URL"),
    ("TELEGRAM_SENDER_API_KEY", os.environ.get("SEED_TELEGRAM_SENDER_API_KEY", ""), "Telegram sender API key"),
]
for key, value, description in optional_secret_specs:
    if str(value or "").strip():
        upsert_config(key, "str", value, description)

print("seed complete")
PY
}

create_superuser() {
  [[ "$CREATE_SUPERUSER" == "1" ]] || return

  local credentials_file="$INSTALL_DIR/.deploy_admin_credentials"
  if [[ -z "$ADMIN_PASSWORD" && -f "$credentials_file" ]]; then
    ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' "$credentials_file" | tail -n 1 | cut -d= -f2- || true)"
  fi
  if [[ -z "$ADMIN_PASSWORD" ]]; then
    ADMIN_PASSWORD="$(random_secret)"
  fi

  log "Creating or updating Django superuser: $ADMIN_USERNAME"
  compose run --rm -T \
    -e "ADMIN_USERNAME=${ADMIN_USERNAME}" \
    -e "ADMIN_EMAIL=${ADMIN_EMAIL}" \
    -e "ADMIN_PASSWORD=${ADMIN_PASSWORD}" \
    django-server python devops_django_server/manage.py shell <<'PY'
import os
from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ["ADMIN_USERNAME"]
email = os.environ.get("ADMIN_EMAIL") or ""
password = os.environ["ADMIN_PASSWORD"]

user, _ = User.objects.get_or_create(username=username, defaults={"email": email, "is_staff": True, "is_superuser": True})
user.email = email
user.is_staff = True
user.is_superuser = True
user.set_password(password)
user.save()
print(f"superuser ready: {username}")
PY

  {
    printf 'ADMIN_URL=http://SERVER_IP:%s/admin/\n' "$DJANGO_SERVER_PORT"
    printf 'ADMIN_USERNAME=%s\n' "$ADMIN_USERNAME"
    printf 'ADMIN_PASSWORD=%s\n' "$ADMIN_PASSWORD"
  } >"$credentials_file"
  chmod 600 "$credentials_file"
}

start_services() {
  local services=(db django-server monitor-producer)
  local i
  for i in $(seq 1 "$WORKERS"); do
    services+=("monitor-worker${i}")
  done

  log "Starting services: ${services[*]}"
  compose up -d "${services[@]}"
}

main() {
  validate_workers
  ensure_basic_tools
  ensure_docker
  ensure_compose
  sync_repo

  cd "$INSTALL_DIR"
  export COMPOSE_PROJECT_NAME

  write_env_file

  log "Building images."
  compose build

  log "Starting database."
  compose up -d db
  wait_for_db

  log "Running migrations."
  compose run --rm -T django-server python devops_django_server/manage.py migrate

  seed_database
  create_superuser
  start_services

  log "Deployment finished."
  log "Admin URL: http://<server-ip>:${DJANGO_SERVER_PORT}/admin/"
  if [[ -f "$INSTALL_DIR/.deploy_admin_credentials" ]]; then
    log "Admin credentials saved at: $INSTALL_DIR/.deploy_admin_credentials"
  fi
  log "Follow logs: cd $INSTALL_DIR && docker compose -p $COMPOSE_PROJECT_NAME logs -f monitor-worker1"
}

main "$@"

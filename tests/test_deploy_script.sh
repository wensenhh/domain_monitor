#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/deploy.sh"

test -f "$SCRIPT"
bash -n "$SCRIPT"

help_output="$(bash "$SCRIPT" --help)"

grep -q "wensenhh/domain_monitor.git" <<<"$help_output"
grep -q "INSTALL_DIR" <<<"$help_output"
grep -q "TG_BOT_TOKEN" <<<"$help_output"
grep -q "COMPOSE_PROJECT_NAME" "$SCRIPT"
grep -q "docker compose" "$SCRIPT"
grep -q "ensure_buildx" "$SCRIPT"
grep -q "docker buildx version" "$SCRIPT"
grep -q "docker-buildx" "$SCRIPT"
grep -q "MonitorPlatform.objects.update_or_create" "$SCRIPT"
grep -q "MonitorConfig.objects.update_or_create" "$SCRIPT"

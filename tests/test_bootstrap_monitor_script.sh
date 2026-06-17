#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/bootstrap_monitor.sh"

test -f "$SCRIPT"
bash -n "$SCRIPT"

help_output="$(bash "$SCRIPT" --help)"

grep -q "TEST_DOMAINS" <<<"$help_output"
grep -q "TG_BOT_TOKEN" <<<"$help_output"
grep -q "MonitorDomainTarget.objects.update_or_create" "$SCRIPT"
grep -q "MonitorConfig.objects.update_or_create" "$SCRIPT"
grep -q "MonitorPlatform.objects.update_or_create" "$SCRIPT"
grep -q "telegram_sender" "$SCRIPT"
grep -q "producer --once" "$SCRIPT"
grep -q "worker --once" "$SCRIPT"

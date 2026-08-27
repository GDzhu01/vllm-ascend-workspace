#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
sed "s|__NFM_PROJECT_ROOT__|$project_root|g" deploy/npu-fleet-monitor.service > "$unit_dir/npu-fleet-monitor.service"
systemctl --user daemon-reload
systemctl --user enable --now npu-fleet-monitor.service
systemctl --user --no-pager status npu-fleet-monitor.service

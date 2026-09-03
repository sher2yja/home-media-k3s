#!/usr/bin/env bash
# Поднимает отдельную VM и запускает в ней приложение через X11.
# k3s, контейнеры и NodePort'ы остаются внутри гостя, хост не занимают.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
VM=ubuntu@192.168.122.14
SSH=(ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null)

cd "$ROOT/ansible"
ANSIBLE_LOCAL_TEMP=/tmp/home-media-ansible-local ansible-playbook k3s-test-vm.yml

"${SSH[@]}" "$VM" mkdir -p home-media-k3s
rsync -a -e "ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null" \
  "$ROOT/app" "$ROOT/deploy" "$ROOT/k8s" "$VM:home-media-k3s/"

"${SSH[@]}" -Y "$VM" \
  "cd home-media-k3s && dbus-run-session -- sh -c \
  'lxqt-policykit-agent >/tmp/home-media-policykit.log 2>&1 & agent=\$!; \
  trap \"kill \$agent 2>/dev/null || true\" EXIT; python3 app/main.py'"

#!/usr/bin/env bash
# Мягко выключает тестовую VM. Диск и установленный внутри k3s сохраняются.
set -euo pipefail
export LC_ALL=C

VM=home-media-k3s-test
state=$(virsh domstate "$VM" 2>/dev/null || true)
if [ -z "$state" ]; then
  echo "VM $VM не создана"
  exit 1
fi
if [ "$state" != "running" ]; then
  echo "VM $VM уже остановлена ($state)"
  exit 0
fi

virsh shutdown "$VM" >/dev/null
for _ in $(seq 1 60); do
  state=$(virsh domstate "$VM")
  if [ "$state" = "shut off" ]; then
    echo "VM $VM остановлена"
    exit 0
  fi
  sleep 2
done

echo "VM не выключилась за две минуты; принудительно её не останавливаю" >&2
exit 1

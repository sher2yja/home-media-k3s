#!/usr/bin/env bash
# Запуск стенда после выключения хоста: поднимает VM и ждёт готовности кластера.
# Автостарт у VM выключен намеренно — сам он не поднимется.
#
# Раньше здесь жила ещё и починка гонки istio-cni (после холодной загрузки STRICT
# mTLS молча отключался). С удалением Istio она стала не нужна — см.
# docs/runbook-phase4.md.
set -euo pipefail

VMS="k8s-cp01 k8s-worker01 k8s-nfs01"

echo "==> Стартую VM (уже запущенные пропускаю)"
for vm in $VMS; do
  state=$(virsh domstate "$vm" 2>/dev/null || echo "отсутствует")
  if [ "$state" = "running" ]; then
    echo "    $vm уже running, пропускаю"
  else
    virsh start "$vm" >/dev/null && echo "    $vm запущена"
  fi
done

echo
echo "==> Жду API-сервер"
for i in $(seq 1 60); do
  if timeout 5 kubectl get --raw /readyz >/dev/null 2>&1; then
    echo "    API готов (${i}-я попытка)"
    break
  fi
  [ "$i" = "60" ] && { echo "FAIL: API не поднялся за 5 минут"; exit 1; }
  sleep 5
done

echo
echo "==> Жду готовности подов media"
for i in $(seq 1 60); do
  total=$(kubectl get pods -n media --no-headers 2>/dev/null | wc -l)
  ready=$(kubectl get pods -n media --no-headers 2>/dev/null | awk '$2=="1/1"' | wc -l)
  echo "    готово $ready/$total"
  [ "$total" -gt 0 ] && [ "$ready" = "$total" ] && break
  [ "$i" = "60" ] && { echo "FAIL: поды media не поднялись"; exit 1; }
  sleep 5
done

echo
echo "==> Проверяю, что сервисы реально отвечают"
FAILED=0
for entry in jellyfin:30096 jellyseerr:30055 prowlarr:30696 qbittorrent:30880 radarr:30878 sonarr:30989; do
  name="${entry%%:*}"
  port="${entry##*:}"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://192.168.122.12:${port}/" 2>/dev/null)
  if [ "$code" = "000" ]; then
    echo "    FAIL $name (:$port) — не отвечает"
    FAILED=1
  else
    echo "    OK   $name (:$port) -> HTTP $code"
  fi
done
[ "$FAILED" = "1" ] && { echo "FAIL: часть сервисов недоступна"; exit 1; }

echo
echo "==> Итог"
kubectl get pods -n monitoring --no-headers 2>/dev/null | awk '{print "    "$1" "$2" "$3}'
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 "ubuntu@192.168.122.12" \
  "free -h | sed -n 2p" 2>/dev/null | awk '{print "    RAM worker01: "$7" available"}' || true

echo
echo "OK: стенд поднят. Полная проверка — 06-verify-monitoring.sh"

#!/usr/bin/env bash
# Чек-лист Phase 5 (docs/runbook-phase5.md): Prometheus + Grafana + node-exporter.
set -euo pipefail

WORKER_IP=192.168.122.12
GRAFANA_PORT=30300

echo "==> Поды monitoring — все Running, без Pending/OOMKilled"
kubectl get pods -n monitoring

echo
echo "==> PVC Prometheus привязан"
kubectl get pvc -n monitoring prometheus-data

echo
echo "==> node-exporter есть на ОБЕИХ нодах (на cp01 — только благодаря toleration)"
NODES=$(kubectl get pods -n monitoring -l app.kubernetes.io/name=node-exporter \
  -o jsonpath='{.items[*].spec.nodeName}' | tr ' ' '\n' | sort -u | wc -l)
if [ "$NODES" -eq 2 ]; then
  echo "OK: node-exporter на $NODES нодах"
else
  echo "FAIL: node-exporter только на $NODES ноде — метрик control-plane не будет"
  exit 1
fi

echo
echo "==> Все цели Prometheus в состоянии up"
# health=unknown значит "ещё не скрейпилось" — это тоже провал, цель должна успеть
kubectl exec -n monitoring deploy/prometheus -- \
  wget -qO- 'localhost:9090/api/v1/targets?state=active' \
  | python3 -c '
import json, sys
t = json.load(sys.stdin)["data"]["activeTargets"]
bad = [x for x in t if x["health"] != "up"]
for x in t:
    print("  %-20s %s" % (x["labels"]["job"], x["health"]))
if bad:
    for x in bad:
        print("  FAIL %s: %s" % (x["scrapeUrl"], x.get("lastError", "")[:100]))
    sys.exit(1)
print("OK: все %d целей up" % len(t))
'

echo
echo "==> Боевой запрос: RAM подов media видна через cAdvisor"
# Главное, ради чего фаза затевалась: RAM тут дефицит, надо видеть кто её ест
kubectl exec -n monitoring deploy/prometheus -- sh -c \
  'wget -qO- --post-data="query=sum by (pod) (container_memory_working_set_bytes{namespace=\"media\",container!=\"\"})" localhost:9090/api/v1/query' \
  | python3 -c '
import json, sys
r = json.load(sys.stdin)["data"]["result"]
for m in sorted(r, key=lambda x: -float(x["value"][1])):
    print("  %-32s %7.1f MiB" % (m["metric"]["pod"], float(m["value"][1]) / 1048576))
if len(r) < 6:
    print("FAIL: cAdvisor вернул %d подов media, ожидалось 6" % len(r))
    sys.exit(1)
print("OK: метрики по %d подам media" % len(r))
'

echo
echo "==> Свободная RAM нод через node-exporter"
kubectl exec -n monitoring deploy/prometheus -- sh -c \
  'wget -qO- --post-data="query=node_memory_MemAvailable_bytes" localhost:9090/api/v1/query' \
  | python3 -c '
import json, sys
r = json.load(sys.stdin)["data"]["result"]
for m in r:
    print("  %-22s %7.0f MiB available" % (m["metric"].get("instance", "?"), float(m["value"][1]) / 1048576))
if len(r) < 2:
    print("FAIL: node-exporter отдал метрики только с %d ноды" % len(r))
    sys.exit(1)
'

echo
echo "==> Grafana отвечает и подхватила datasource"
if curl -sf -m 10 "http://${WORKER_IP}:${GRAFANA_PORT}/api/health" >/dev/null; then
  echo "OK: /api/health отвечает"
else
  echo "FAIL: Grafana недоступна"
  exit 1
fi
curl -sf -m 10 "http://${WORKER_IP}:${GRAFANA_PORT}/api/datasources" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
if not d:
    print("FAIL: datasource не подхватился")
    sys.exit(1)
for x in d:
    print("  %s -> %s (default=%s)" % (x["name"], x["url"], x["isDefault"]))
'

echo
echo "==> Медиастек не задет мониторингом"
kubectl get pods -n media --no-headers | awk '{print "  "$1" "$2" "$3" restarts="$4}'

echo
echo "OK: Phase 5 (мониторинг) проверена."

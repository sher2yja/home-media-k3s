#!/usr/bin/env bash
# Пункты 1-3 чек-листа из плана (Шаг 6): состояние VM и уровня кластера.
# Пункты 4-10 (media namespace, доступ из браузера) — вручную, после деплоя k8s/media-stack.
set -euo pipefail

echo "==> virsh list --all"
virsh list --all

echo
echo "==> kubectl get nodes -o wide"
kubectl get nodes -o wide

echo
echo "==> kubectl get pods -A"
kubectl get pods -A

echo
echo "==> restart-лупы (RESTARTS > 0)"
kubectl get pods -A --no-headers | awk '$5+0 > 0 {print}' || true

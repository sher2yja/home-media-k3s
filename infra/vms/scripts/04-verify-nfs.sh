#!/usr/bin/env bash
# Пункты 1-6 чек-листа Phase 3 (docs/runbook-phase3.md): инфраструктурный слой NFS.
# Пункты 7-10 (hardlink-импорт, Jellyfin) — вручную, после ручной донастройки UI.
set -euo pipefail

NFS_IP=192.168.122.13
WORKER_IP=192.168.122.12

echo "==> virsh list --all"
virsh list --all

echo
echo "==> nfs-kernel-server на k8s-nfs01"
ssh -o StrictHostKeyChecking=no ubuntu@$NFS_IP "systemctl is-active nfs-kernel-server && sudo exportfs -v"

echo
echo "==> Экспорт виден с воркера (showmount, требует nfs-common)"
ssh -o StrictHostKeyChecking=no ubuntu@$WORKER_IP "showmount -e $NFS_IP"

echo
echo "==> root_squash негативный тест: root на воркере НЕ должен уметь писать в экспорт"
echo "    (ожидаемый результат — Permission denied; успешная запись = провал проверки)"
ssh -o StrictHostKeyChecking=no ubuntu@$WORKER_IP "
  sudo mkdir -p /mnt/nfs-media-test &&
  sudo mount -t nfs $NFS_IP:/srv/nfs/media /mnt/nfs-media-test &&
  sudo touch /mnt/nfs-media-test/root-write-test 2>&1;
  echo \"exit=\$?\";
  sudo umount /mnt/nfs-media-test
"

echo
echo "==> kubectl get pv,pvc -n media"
kubectl get pv,pvc -n media

echo
echo "==> Позитивный тест записи от пода (должен пройти)"
echo "    kubectl exec по умолчанию заходит как root (PID 1 linuxserver.io-образов —"
echo "    s6-overlay init), а не как процесс приложения — тестируем от имени юзера abc"
echo "    (UID 1000), под которым реально работает qbittorrent-nox, через s6-setuidgid."
kubectl exec -n media deploy/qbittorrent -- s6-setuidgid abc sh -c \
  'id; touch /data/pod-write-test && ls -la /data/pod-write-test && rm /data/pod-write-test'

echo
echo "OK: инфраструктурная часть проверена. Дальше — ручные шаги 7-10 из docs/runbook-phase3.md."

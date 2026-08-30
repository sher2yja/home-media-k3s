#!/usr/bin/env bash
# Генерирует пароль WebUI qBittorrent и кладёт его в Secret вместо того, чтобы
# держать PBKDF2-хеш в манифесте.
#
# Зачем: раньше хеш был зашит в deployment.yaml. Для одного стенда это терпимо,
# но репозиторий уезжает в публичный git и раздаётся релизами — тогда один и тот
# же пароль достаётся всем, кто скачал. Пароль должен рождаться при установке.
#
# Идемпотентен: если Secret уже есть, ничего не трогает (иначе сломались бы
# download client'ы в Sonarr/Radarr, где пароль сохранён). Смена — только явно,
# флагом --rotate.
set -euo pipefail

NS=media
SECRET=qbittorrent-webui
ROTATE="${1:-}"

if kubectl get secret -n "$NS" "$SECRET" >/dev/null 2>&1; then
  if [ "$ROTATE" != "--rotate" ]; then
    echo "==> Secret $SECRET уже существует, ничего не меняю"
    echo "    (сменить пароль: $0 --rotate — не забудь обновить download client"
    echo "     в Sonarr и Radarr, там он сохранён)"
    exit 0
  fi
  echo "==> --rotate: пересоздаю Secret $SECRET"
fi

echo "==> Генерирую пароль и PBKDF2-хеш"
# Сама криптография живёт в app/install.py и больше нигде: этот скрипт (стенд) и
# приложение-установщик (машина пользователя, k3s) обязаны давать хеш, который
# qBittorrent принимает одинаково. Две копии PBKDF2 — способ однажды тихо
# разъехаться и получить "неверный пароль" без объяснений.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
eval "$(python3 - "$REPO_ROOT" <<'PY'
import shlex, sys
sys.path.insert(0, sys.argv[1] + "/app")
import install
pwd = install.generate_password()
print("QBT_PASSWORD=%s" % shlex.quote(pwd))
print("QBT_HASH=%s"     % shlex.quote(install.qbittorrent_pbkdf2(pwd)))
PY
)"

kubectl create secret generic "$SECRET" -n "$NS" \
  --from-literal=password="$QBT_PASSWORD" \
  --from-literal=password-pbkdf2="$QBT_HASH" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "OK: Secret $SECRET создан в namespace $NS"
echo
echo "    Логин:  admin"
echo "    Пароль: $QBT_PASSWORD"
echo
echo "    Пароль сохранён в Secret, посмотреть позже:"
echo "      kubectl get secret -n $NS $SECRET -o jsonpath='{.data.password}' | base64 -d"

#!/usr/bin/env bash
# Run on EC2 as root after isolated staging has passed. No DB migration/TTL changes.
set -Eeuo pipefail
phase=preflight
cutover_started=0
on_failure() {
  failure_status=$1
  trap - ERR INT TERM HUP
  if test "$cutover_started" = 1; then
    systemctl stop hearo-mqtt-bridge-v2-final || true
    systemctl stop hearo-api-v2-final || true
    systemctl show hearo-api-v2-final hearo-mqtt-bridge-v2-final \
      -p Id -p ActiveState -p WorkingDirectory --no-pager || true
  fi
  echo "중단: 단계=$phase. 전환이 시작됐다면 writer 중단을 요청했습니다. 자동 롤백·재시도 없음. 출력과 DB 상태부터 확인하세요." >&2
  exit "$failure_status"
}
trap 'on_failure "$?"' ERR
trap 'on_failure 130' INT
trap 'on_failure 143' TERM
trap 'on_failure 129' HUP

mode=${1:---check-only}
expected_sha=${2:-}
test "$mode" = --check-only || test "$mode" = --apply
[[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]]
test "$(id -u)" = 0
r6=/opt/hearo-backend-v2-r6
r7=/opt/hearo-backend-v2-r7
test -d "$r7"
test ! -L "$r7"
test "$(sudo -u ubuntu git -C "$r7" rev-parse HEAD)" = "$expected_sha"
r7_status=$(sudo -u ubuntu git -C "$r7" status --porcelain --untracked-files=all)
test -z "$r7_status"
test "$(sudo -u ubuntu git -C "$r6" rev-parse HEAD)" = a3376800ed036049f1d2290f3dea847135a6c022
r6_status=$(sudo -u ubuntu git -C "$r6" status --porcelain --untracked-files=all)
test -z "$r6_status"

for unit in hearo-api-v2-final hearo-mqtt-bridge-v2-final
do
  test "$(systemctl show "$unit" -p ActiveState --value)" = active
  test "$(systemctl show "$unit" -p WorkingDirectory --value)" = "$r6"
  test "$(systemctl show "$unit" -p FragmentPath --value)" = "/etc/systemd/system/$unit.service"
  test -z "$(systemctl show "$unit" -p DropInPaths --value)"
  test "$(systemctl is-enabled "$unit")" = enabled
  test -f "/etc/systemd/system/$unit.service"
  test ! -L "/etc/systemd/system/$unit.service"
done
cmp /etc/systemd/system/hearo-api-v2-final.service "$r6/infra/systemd/hearo-api-v2-r6.service"
cmp /etc/systemd/system/hearo-mqtt-bridge-v2-final.service "$r6/infra/systemd/hearo-mqtt-bridge-v2-r6.service"
for env_file in /etc/hearo/hearo-api-v2.env /etc/hearo/hearo-mqtt-bridge-v2.env
do
  test -f "$env_file"
  test ! -L "$env_file"
  test "$(stat -c '%U:%G:%a' "$env_file")" = root:root:600
done
test "$(systemctl show hearo-api-v2-final -p EnvironmentFiles --value)" = '/etc/hearo/hearo-api-v2.env (ignore_errors=no)'
test "$(systemctl show hearo-mqtt-bridge-v2-final -p EnvironmentFiles --value)" = '/etc/hearo/hearo-mqtt-bridge-v2.env (ignore_errors=no)'

systemd-run --quiet --wait --pipe --collect \
  --property=User=ubuntu --property=Group=ubuntu \
  --property=EnvironmentFile=/etc/hearo/hearo-api-v2.env \
  --property=WorkingDirectory="$r7" --property=NoNewPrivileges=true \
  --property=ProtectSystem=strict --property=ProtectHome=true \
  "$r7/.venv/bin/python" -B -E -c '
from hearo_backend import __version__
from hearo_backend.config import Settings
s = Settings()
assert __version__ == "2.5.0"
assert (s.environment, s.store_backend, s.region, s.core_table, s.alerts_table, s.mqtt_enabled) == (
    "production", "dynamodb", "ap-south-1", "hearo-core-v2-final", "hearo-alerts-v2-final", True)
s.validate_for_production()
print("R7 PRODUCTION CONFIG OK / DB 호출 없음")
'

curl --connect-timeout 5 --max-time 10 -fsS http://127.0.0.1:8001/health \
  | "$r7/.venv/bin/python" -B -E -c 'import json,sys; assert json.load(sys.stdin)["version"] == "2.4.0"'
systemd-analyze verify "$r7/infra/systemd/hearo-api-v2-r7.service" "$r7/infra/systemd/hearo-mqtt-bridge-v2-r7.service"
echo "V2.5 CUTOVER READ-ONLY PREFLIGHT OK"
if test "$mode" = --check-only; then exit 0; fi
test "${3:-}" = --staging-verified
test "$(systemctl show hearo-api-v2-r7-staging -p ActiveState --value)" = inactive

phase=backup
umask 077
backup_dir=$(mktemp -d /etc/hearo/backups/before-v2.5.0.XXXXXX)
chmod 700 "$backup_dir"
install -o root -g root -m 600 \
  /etc/hearo/hearo-api-v2.env /etc/hearo/hearo-mqtt-bridge-v2.env \
  /etc/systemd/system/hearo-api-v2-final.service \
  /etc/systemd/system/hearo-mqtt-bridge-v2-final.service "$backup_dir/"
printf '%s\n' "$expected_sha" > "$backup_dir/R7_GIT_HEAD.txt"
sudo -u ubuntu git -C "$r6" archive --format=tar HEAD > "$backup_dir/R6_SOURCE.tar"
(
  cd "$backup_dir"
  sha256sum hearo-api-v2.env hearo-mqtt-bridge-v2.env \
    hearo-api-v2-final.service hearo-mqtt-bridge-v2-final.service \
    R7_GIT_HEAD.txt R6_SOURCE.tar > SHA256SUMS.txt
  sha256sum -c SHA256SUMS.txt
  tar -tf R6_SOURCE.tar >/dev/null
)
echo "백업 위치: $backup_dir"

phase=stop-old-writers
cutover_started=1
systemctl stop hearo-mqtt-bridge-v2-final
systemctl stop hearo-api-v2-final
test "$(systemctl show hearo-api-v2-final -p ActiveState --value)" = inactive
test "$(systemctl show hearo-mqtt-bridge-v2-final -p ActiveState --value)" = inactive
phase=install-new-units
install -o root -g root -m 644 "$r7/infra/systemd/hearo-api-v2-r7.service" /etc/systemd/system/hearo-api-v2-final.service
install -o root -g root -m 644 "$r7/infra/systemd/hearo-mqtt-bridge-v2-r7.service" /etc/systemd/system/hearo-mqtt-bridge-v2-final.service
systemctl daemon-reload
phase=start-new-api
systemctl start hearo-api-v2-final
healthy=false
for attempt in {1..12}
do
  if curl --connect-timeout 2 --max-time 3 -fsS http://127.0.0.1:8001/health \
    | "$r7/.venv/bin/python" -B -E -c 'import json,sys; assert json.load(sys.stdin)["version"] == "2.5.0"' 2>/dev/null
  then healthy=true; break; fi
  sleep 1
done
test "$healthy" = true
phase=start-new-bridge
systemctl start hearo-mqtt-bridge-v2-final
for unit in hearo-api-v2-final hearo-mqtt-bridge-v2-final
do
  test "$(systemctl show "$unit" -p ActiveState --value)" = active
  test "$(systemctl show "$unit" -p WorkingDirectory --value)" = "$r7"
  test "$(systemctl is-enabled "$unit")" = enabled
done
phase=external-verification
curl --connect-timeout 10 --max-time 20 -fsS https://13.233.91.248/v2/health \
  | "$r7/.venv/bin/python" -B -E -c 'import json,sys; v=json.load(sys.stdin); assert v["version"] == "2.5.0"; print(v)'
curl --connect-timeout 10 --max-time 20 -fsS https://13.233.91.248/v2/openapi.json \
  | "$r7/.venv/bin/python" -B -E -c '
import json,sys
doc=json.load(sys.stdin)
for suffix,verb in (("","get"),("/claim/preview","post"),("/claim","post")):
    assert verb in doc["paths"]["/households/{household_id}/device-kit"+suffix]
print("V2.5 EXTERNAL KIT API SURFACE OK")'
phase=done
echo "V2.5 SERVICE CUTOVER OK / DB 마이그레이션·TTL·알림 정리 서비스·실기기 설정 변경 없음"
echo "실제 키트 재고 발급과 프론트 등록 시험은 별도로 진행하세요."

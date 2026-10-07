# v2.4.0 단계별 적용 안내

이 문서는 명령 예시이며 실행 완료 기록이 아니다. GitHub PR 병합 → 별도 r6 스테이징 → 검증 → 운영 전환 → 데이터 계획 승인/마이그레이션 → TTL/정리 도구 → 로그 설정 순으로 진행한다. 명령은 한 블록씩 실행하고 오류가 나면 다음 단계로 진행하지 않는다. EC2에서만 실행하며 Mac 경로와 혼동하지 않는다.

## 0. 범위와 중단 기준

사용자 제공 로그 기준 현재 운영은 `/opt/hearo-backend-v2-r5`, 외부 `/v2/health`의 `2.3.0`이다. 이번 변경은 새 `/opt/hearo-backend-v2-r6`에 배치하고 r5를 보존한다. GitHub 반영과 EC2 배포는 별개의 승인 단계다. 실행 전 아래 health로 실제 상태를 다시 확인하고, PR 병합·CI 결과·승인된 배포 커밋을 기록한다. 키트 등록 API는 v2.4.0 미포함이며 별도 릴리스다.

다음은 공개/운영 준비에 남은 조건이다.

- 실제 문서 버전·시행일·공개 URL 확정, 프론트 동의 화면 구현. 그 전에는 문서 환경값은 빈 값, `required=false` 유지.
- 감사·보안 접속기록의 별도 1년 이상 보존. 검증 전 전역 journald 90일 설정 금지.
- owner 탈퇴 후 비활성 가구/기기에 남는 개인정보와 문서의 파기 문구 정합성 결정.
- 연동 계정의 탈퇴 도중 실패했을 때 선행 가구 연동 해제의 복구 정책·구현·검증. 후속 `409`에도 owner 가구 비활성화와 member 미연동이 이미 반영될 수 있으므로 현재 PR은 운영 전환 전 검토용이다. 실제 계정으로 실패 시험하지 않는다.
- 동의 영수증 97개 초과 계정의 관리자 처리, 일반 로그의 오래된 파일 정리와 PITR/임시 백업 보존 예외 검토.

스테이징에 운영 테이블을 연결하면 테스트의 PATCH/탈퇴/알림 생성도 **실제 운영 데이터를 변경한다**. 별도 core/alerts 테스트 테이블을 권장하며, 실제 사용자 계정을 탈퇴 테스트에 사용하지 않는다. 새로운 AWS 테이블·역할 생성은 비용과 권한이 발생하므로 별도 승인 없이 실행하지 않는다.

## 1. 운영 상태·권한 확인 (읽기 전용)

```bash
curl --connect-timeout 10 --max-time 20 -sS -w '\nHTTP %{http_code}\n' https://13.233.91.248/v2/health
```

```bash
sudo systemctl show hearo-api-v2-final hearo-mqtt-bridge-v2-final -p ActiveState -p WorkingDirectory -p ExecStart --no-pager
```

```bash
aws sts get-caller-identity --output json --no-cli-pager
```

```bash
aws dynamodb describe-table --region ap-south-1 --table-name hearo-alerts-v2-final --query 'Table.{Arn:TableArn,Status:TableStatus}' --output json --no-cli-pager
```

```bash
aws dynamodb describe-table --region ap-south-1 --table-name hearo-core-v2-final --query 'Table.{Arn:TableArn,Status:TableStatus}' --output json --no-cli-pager
```

보관정책 대상은 두 테이블의 전체 가구 알림이다. 계정 삭제 도구가 아니며 특정 `home-b0d7a380b457`만 대상으로 제한된 도구도 아니다. 적용 계획의 범위를 반드시 확인한다.

정리 도구는 두 테이블에 `DescribeTable`, `Scan`, `UpdateItem`, `DeleteItem`, `ConditionCheckItem`, 트랜잭션 구성 작업 권한이 필요하다. `Scan --limit 1` 성공만으로 전체 쓰기 권한까지 확인되지는 않는다. TTL 상태 확인·활성화는 별도 관리 권한 `DescribeTimeToLive`, `UpdateTimeToLive`가 필요하며 API 런타임 역할에 무조건 추가하지 않는다.

현재 역할에 `DescribeTable` 등이 없으면 관리 담당자가 **해당 두 테이블의 IAM 권한만 먼저** 적용한다. 마이그레이션 미리보기 전 두 `describe-table`이 성공해야 한다. 수정된 전체 CloudFormation 템플릿을 이때 그대로 적용하면 alerts TTL도 함께 활성화되므로, IAM-only 변경 세트 또는 별도 역할 정책 업데이트와 TTL 변경을 분리하여 검토한다. 이 문서가 IAM 쓰기를 자동 승인하는 것은 아니다.

## 2. r6 설치와 스테이징

GitHub 반영·병합된 **검증한 커밋**을 새 r6에 체크아웃한다. 운영 r5에서 `git pull`하여 덮어쓰지 않는다. 새 디렉터리가 이미 있으면 내용을 확인하고 덮어쓰지 않는다.

```bash
test ! -e /opt/hearo-backend-v2-r6
```

```bash
sudo install -d -o ubuntu -g ubuntu -m 755 /opt/hearo-backend-v2-r6
```

```bash
git clone https://github.com/2026-ITEC0401/backend.git /opt/hearo-backend-v2-r6
```

병합 후 승인된 **40자리 커밋 SHA**를 입력한다. 임의의 예시 SHA나 최신 main이라는 이유만으로 배포하지 않는다. 아래 확인이 실패하면 중단하며, 성공 전에는 환경 준비·서비스 전환을 실행하지 않는다.

```bash
cd /opt/hearo-backend-v2-r6
read -r -p '승인된 40자리 배포 커밋 SHA: ' HEARO_R6_APPROVED_SHA
if [[ "$HEARO_R6_APPROVED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  git checkout --detach "$HEARO_R6_APPROVED_SHA" && test "$(git rev-parse HEAD)" = "$HEARO_R6_APPROVED_SHA" && test -z "$(git status --porcelain)" && echo 'R6 APPROVED COMMIT OK'
else
  echo '유효한 승인 SHA가 아닙니다 - 중단'
fi
```

`R6 APPROVED COMMIT OK`를 확인한 뒤에만 다음 블록을 실행한다. EC2 기본 Bash에서 실행하며, 비밀번호 입력과 달리 이 SHA는 비밀값이 아니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

운영 env는 새 파일로 복사하고 권한 600을 유지한다. 출력/붙여넣기에 secret 전체를 노출하지 않는다. `/etc/hearo/hearo-api-v2-r6-staging.env`에는 `HEARO_ENV=development`, `HEARO_MQTT_ENABLED=false`, 테스트 테이블 이름을 사용한다. 기존 unread 기준시각·JWT secret·CORS를 임의 변경하지 않는다. 문서 값 미정 상태에서는 신규 5개 환경값을 비우고 `HEARO_LEGAL_CONSENT_REQUIRED=false`로 둔다.

```bash
sudo systemd-run --wait --pipe --collect --property=EnvironmentFile=/etc/hearo/hearo-api-v2-r6-staging.env --property=WorkingDirectory=/opt/hearo-backend-v2-r6 /opt/hearo-backend-v2-r6/.venv/bin/python -c 'from hearo_backend.config import Settings; s=Settings(); s.validate_for_production(); assert s.environment == "development"; assert not s.mqtt_enabled; print("R6 STAGING CONFIG OK")'
```

8002가 비어 있는지 확인하고 `infra/systemd/hearo-api-v2-r6-staging.service`를 동명으로 설치한다. 서비스 설치·기동은 상태 변경이므로 계획대로 진행할 때만 실행한다.

```bash
sudo ss -lntp | grep ':8002'
```

```bash
sudo install -o root -g root -m 644 infra/systemd/hearo-api-v2-r6-staging.service /etc/systemd/system/hearo-api-v2-r6-staging.service
```

```bash
sudo systemd-analyze verify /etc/systemd/system/hearo-api-v2-r6-staging.service
sudo systemctl daemon-reload
sudo systemctl start hearo-api-v2-r6-staging
```

```bash
curl -sS http://127.0.0.1:8002/health
curl -sS http://127.0.0.1:8002/legal/policies
sudo journalctl -u hearo-api-v2-r6-staging -n 50 --no-pager -o cat
```

정상 기준은 `2.4.0`, 공개 전 `configured=false`, 새 GET/PATCH `/me/consents`의 OpenAPI 등록이다. 버전 테스트용 문서 설정은 테스트 테이블에만 넣는다. 신규/기존/부분 동의 복구, 정확한 90일 경계, 7일 이력·미확인 유지, 탈퇴 및 기존 LED·주소·MQTT·WebSocket 회귀를 테스트한다. 패키지/실제 AWS/실제 프론트가 로컬 테스트와 같다고 가정하지 않는다.

탈퇴 실패 시험은 별도 테이블의 임시 owner/member로 진행한다. 97개 초과 동의 이력, 조건 충돌, 쓰기 장애를 주입했을 때 계정 삭제뿐 아니라 선행 가구 상태·주소·초대 코드·구성원 연결까지 확인한다. 기존 단위 테스트 성공은 이 전체 복구 경로의 운영 승인 근거가 아니다. 복구 대책이 없는 상태에서는 3절 운영 전환으로 진행하지 않는다.

## 3. 운영 설정 백업과 전환

스테이징 성공 후 승인받은 시점에만 전환한다. root-only 새 백업 디렉터리에 두 env와 두 기존 systemd 파일을 복사하고 SHA-256/커밋을 기록한다. 같은 이름의 기존 백업을 덮어쓰지 않는다. 새 파일 `hearo-api-v2-r6.service`, `hearo-mqtt-bridge-v2-r6.service`는 설치할 때 운영의 기존 **final 서비스 이름**을 대상으로 사용한다.

```bash
sudo systemd-run --wait --pipe --collect --property=EnvironmentFile=/etc/hearo/hearo-api-v2.env --property=WorkingDirectory=/opt/hearo-backend-v2-r6 /opt/hearo-backend-v2-r6/.venv/bin/python -c 'from hearo_backend.config import Settings; s=Settings(); s.validate_for_production(); print("R6 PRODUCTION CONFIG OK")'
```

```bash
sudo systemctl stop hearo-api-v2-r6-staging
sudo systemctl stop hearo-mqtt-bridge-v2-final
```

```bash
sudo install -o root -g root -m 644 infra/systemd/hearo-api-v2-r6.service /etc/systemd/system/hearo-api-v2-final.service
sudo install -o root -g root -m 644 infra/systemd/hearo-mqtt-bridge-v2-r6.service /etc/systemd/system/hearo-mqtt-bridge-v2-final.service
```

```bash
sudo systemd-analyze verify /etc/systemd/system/hearo-api-v2-final.service /etc/systemd/system/hearo-mqtt-bridge-v2-final.service
sudo systemctl daemon-reload
sudo systemctl restart hearo-api-v2-final
```

API의 내부/외부 health가 `2.4.0`을 반환하는 것을 확인한 후 Bridge를 시작한다.

```bash
curl -sS http://127.0.0.1:8001/health
curl -sS https://13.233.91.248/v2/health
```

```bash
sudo systemctl start hearo-mqtt-bridge-v2-final
sudo systemctl is-active hearo-api-v2-final hearo-mqtt-bridge-v2-final
sudo systemctl is-enabled hearo-api-v2-final hearo-mqtt-bridge-v2-final
```

신규 알림 한 건을 발생시켜 내부 alert `200`, DynamoDB의 본문/ALERTID 동일 만료값, 프론트 WebSocket, 7일 조회를 확인한다. LED OFF가 오디오 전송을 끄지 않는 기존 계약도 확인한다. 실제 앱 확인은 별도 수행해야 한다.

## 4. 기존 데이터 계획 생성 (아직 DB 수정 없음)

운영 API의 만료 조회 차단부터 먼저 배포한다. 이후 리전·테이블·ARN을 독립적으로 확인하고 계획을 생성한다. 아래 경로는 신규 파일이어야 하며 기존 파일은 덮어쓰지 않는다.

```bash
umask 077
/opt/hearo-backend-v2-r6/.venv/bin/python /opt/hearo-backend-v2-r6/scripts/alert_retention.py --mode migrate --region ap-south-1 --alerts-table hearo-alerts-v2-final --core-table hearo-core-v2-final --plan-file /home/ubuntu/hearo-retention-v240-plan.json
```

이 명령은 읽기 조회 후 0600 계획 파일만 만든다. 파일의 `plan.actions[].before`는 숫자 등 타입을 보존한 DynamoDB 형식의 변경 전 백업이다. alert 내용이 포함되므로 공개하지 않는다. 보고서에서 `expired_alerts`, `ttl_updates`, `orphan_aliases`, `action_count`, `skipped_*`, `issues`를 검토한다. 미래 시각·손상 항목·별칭 불일치가 있으면 자동으로 성공했다고 보지 말고 원인을 확인한다.

**migrate 실제 적용은 만료값만 추가하는 작업이 아니다. 이미 90일이 지난 알림과 관련 별칭을 삭제한다.** USER/TOKEN/INVITE/DEVICE/KIT는 대상이 아니다. 사용자가 승인한 정확한 계획만 적용한다. 다음 변수는 앞서 확인한 실제 ARN을 사람이 검증하여 입력한다. 빈 값/플레이스홀더 상태로 실행하지 않는다.

```bash
HEARO_CONFIRM_ALERTS_ARN='실제로 확인한 alerts TableArn'
HEARO_CONFIRM_CORE_ARN='실제로 확인한 core TableArn'
```

```bash
/opt/hearo-backend-v2-r6/.venv/bin/python /opt/hearo-backend-v2-r6/scripts/alert_retention.py --mode migrate --region ap-south-1 --alerts-table hearo-alerts-v2-final --core-table hearo-core-v2-final --plan-file /home/ubuntu/hearo-retention-v240-plan.json --apply --confirm-region ap-south-1 --confirm-alerts-table hearo-alerts-v2-final --confirm-core-table hearo-core-v2-final --confirm-alerts-table-arn "$HEARO_CONFIRM_ALERTS_ARN" --confirm-core-table-arn "$HEARO_CONFIRM_CORE_ARN"
```

각 변경은 현재 레코드의 timestamp/event_key 조건으로 보호한다. 고아 별칭은 본문이 현재도 없는지 같은 트랜잭션에서 검사한다. 단, 전체 계획은 하나의 트랜잭션이 아니므로 중간 장애 시 일부만 적용될 수 있다. 출력을 확인하고 새 미리보기를 만들어 남은 작업을 검증한다. 기존 계획 재실행의 조건부 skip은 단순 실패가 아니라 이미 처리/동시 변경일 수 있다.

## 5. TTL과 자동 정리

현재 전달받은 상태는 core TTL 활성화, alerts TTL 비활성화이다. 실제 상태를 다시 확인한다.

```bash
aws dynamodb describe-time-to-live --region ap-south-1 --table-name hearo-alerts-v2-final --output json --no-cli-pager
```

검증된 만료값 적용 후, 관리 권한과 변경 승인을 가진 담당자가 alerts TTL을 활성화한다. 기존 CloudFormation 관리 리소스라면 임의 `create-stack` 대신 해당 스택의 변경 세트를 검토하여 적용한다.

```bash
aws dynamodb update-time-to-live --region ap-south-1 --table-name hearo-alerts-v2-final --time-to-live-specification 'Enabled=true,AttributeName=expires_at_epoch' --output json --no-cli-pager
```

`ENABLING`은 아직 완료가 아니다. `ENABLED` 확인까지 기다린다. TTL 삭제는 즉시 실행되지 않는다. 실제 삭제를 보완할 시간당 도구는 먼저 수동 `--mode purge` **미리보기**로 검증한다. `--scheduled`를 붙이면 dry-run이 아니라 실제 적용을 요구하므로 미리보기에는 사용하지 않는다.

```bash
umask 077
/opt/hearo-backend-v2-r6/.venv/bin/python /opt/hearo-backend-v2-r6/scripts/alert_retention.py --mode purge --region ap-south-1 --alerts-table hearo-alerts-v2-final --core-table hearo-core-v2-final --plan-file /home/ubuntu/hearo-retention-v240-purge-preview.json
```

정기 작업은 만료 본문과 같은 이벤트의 별칭, 발생 시각 기준과 저장된 TTL이 모두 만료된 고아 별칭을 삭제한다. 고아 별칭은 본문이 현재도 존재하지 않는지 트랜잭션으로 검증한다. 최근 이벤트의 중복방지 별칭은 보존한다.

`infra/hearo-alert-retention.env.example`을 `/etc/hearo/hearo-alert-retention.env`에 root:root 600으로 배치하고 여덟 실제 리전·테이블·ARN 확인값을 채운다. 계정 비밀번호/JWT는 필요하지 않다. 비어 있는 확인값이면 서비스는 실행하지 않는다.

승인받은 dry-run 후 systemd의 retention service/timer를 설치하고 `systemd-analyze verify`를 거친다. 한 번의 수동 `systemctl start hearo-alert-retention`으로 삭제 결과를 검증한 후에만 timer를 `enable --now`한다. 실패 알림과 다음 실행 성공을 운영 담당자가 확인한다.

```bash
sudo journalctl -u hearo-alert-retention -n 50 --no-pager -o cat
systemctl list-timers --all --no-pager hearo-alert-retention.timer
```

검증용 계획·백업 파일은 운영 완료 후 즉시 정리 대상이다. 장기 보관용 새 백업을 자동 생성하지 않는다. 삭제/복구는 해당 파일의 정확한 경로와 필요성을 다시 확인하고 승인된 방식으로 진행한다.

## 6. 로그 보존은 별도 운영 검증

`infra/logrotate/hearo-nginx`는 기존 `/etc/logrotate.d/nginx`의 **교체 예시**다. 중복 stanza로 추가하지 않는다. `infra/logrotate/hearo-rsyslog-service` 사용 시 기존 `auth.log` stanza를 보존하고 일반 서비스 로그만 분리한다. 실제 로그 경로·감사 항목·설치 hook이 서버와 같은지 확인한다.

```bash
sudo logrotate --debug /etc/logrotate.conf
sudo systemd-analyze cat-config systemd/journald.conf --no-pager
systemctl list-timers --all --no-pager logrotate.timer
```

전역 journald drop-in은 감사 기록 분리 전 설치 금지다. `daily/rotate/maxage` 설정만으로 운영 보존 검증이 완료되는 것은 아니며, 비어 있는 로그의 오래된 회전본과 rsyslog 중복본, journal, 스냅샷도 점검한다. 서비스 로그의 최대 90일 삭제를 엄밀히 보장하는 별도 파일 정리 작업은 아직 운영에 설치하지 않았다.

API/Bridge에서 요청 body, 비밀번호, Authorization, credential, 전화번호·주소가 출력되지 않는지 합성 테스트 값으로 검사한다. 실제 secret이 들어 있는 로그 전체를 채팅/GitHub에 붙이지 않는다.

## 7. 롤백 주의

설정/서비스 백업과 r5 코드는 보존한다. 그러나 단순히 r5로 돌아가면 90일 조회 차단이 사라지고 새 알림 TTL이 누락된다. 공개된 90일 정책 적용 후에는 무조건적인 r5 재시작 대신 수집/조회 일시 차단, 보존 필터를 포함한 핫픽스 또는 검증된 순방향 수정이 필요하다. 되돌리는 동안도 TTL·정리 작업을 임의로 해제하지 않는다.

일괄 마이그레이션의 삭제는 API 코드 롤백으로 되돌아오지 않는다. 계획의 변경 전 이미지나 PITR 복구는 수동 검토 대상이며 만료 알림·탈퇴 계정·오래된 약관 상태를 그대로 재노출하지 않는다. TTL 활성화 후 만료된 데이터를 ‘복구용’이라는 이유로 무기한 재등록하지 않는다.

## 8. 완료 증거

운영 완료라고 보고하려면 외부 버전/새 API/CORS, 최근 정상 MQTT `200`, 기존 이력/미확인/LED/탈퇴, 본문·별칭 TTL, 신규/기존 동의 상태, 실제 프론트, 자동 정리 실행 결과, 로그·백업 보존 예외 결정의 증거를 확인한다. 로컬 테스트만으로 이를 대체하지 않는다.

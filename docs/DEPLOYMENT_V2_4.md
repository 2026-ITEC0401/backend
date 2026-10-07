# v2.4.0 단계별 적용 안내

이 문서는 명령 예시이며 실행 완료 기록이 아니다. GitHub PR 병합 → 별도 r6 스테이징 → 격리된 실제 DynamoDB 검증 → 운영 전환 → 데이터 계획 승인/마이그레이션 → TTL/정리 도구 → 로그 설정 순으로 진행한다. 명령은 한 블록씩 실행하고 오류가 나면 다음 단계로 진행하지 않는다. EC2에서만 실행하며 Mac 경로와 혼동하지 않는다.

## 0. 범위와 중단 기준

사용자 제공 로그 기준 현재 운영은 `/opt/hearo-backend-v2-r5`, 외부 `/v2/health`의 `2.3.0`이다. 이번 변경은 새 `/opt/hearo-backend-v2-r6`에 배치하고 r5를 보존한다. GitHub 반영과 EC2 배포는 별개의 승인 단계다. 실행 전 아래 health로 실제 상태를 다시 확인하고, PR 병합·CI 결과·승인된 배포 커밋을 기록한다. 키트 등록 API는 v2.4.0 미포함이며 별도 릴리스다.

다음은 공개/운영 준비에 남은 조건이다.

- 실제 문서 버전·시행일·공개 URL 확정, 프론트 동의 화면 구현. 그 전에는 문서 환경값은 빈 값, `required=false` 유지.
- 감사·보안 접속기록의 별도 1년 이상 보존. 검증 전 전역 journald 90일 설정 금지.
- owner 탈퇴 후 비활성 가구/기기에 남는 개인정보와 문서의 파기 문구 정합성 결정.
- 연결된 계정 탈퇴의 원자적 DynamoDB 트랜잭션과 `membership_version` fence는 소스에 반영됐지만 운영에는 배포하지 않았다. 기존 승인 소스의 실제 100개 성공·101개 사전 차단·조건 충돌은 검증했으며, 원본 Delete의 4MiB 한계·결과 불명 전송 실패와 새 연령 확인 변경의 실제 AWS 재검증은 아직 남아 있다.
- 한 트랜잭션에 100개를 초과하는 고유 항목이 필요하거나 멤버십 상태가 불완전해 `ACCOUNT_DELETION_REVIEW_REQUIRED`가 되는 계정의 관리자 처리, 일반 로그의 오래된 파일 정리와 PITR/임시 백업 보존 예외를 검토한다.
- 새 회원가입의 `age_over_14_agreed` 필수 계약과 프론트 체크박스를 같은 전환 창에 배포한다. 구·신 프론트와 백엔드를 섞으면 회원가입이 양방향 `422`가 되므로 동기화 전에는 가입을 열지 않는다.

스테이징에 운영 또는 다른 환경과 공유하는 테이블을 연결하면 테스트의 PATCH/탈퇴/알림 생성도 **공유 데이터를 변경한다**. 쓰기 검증은 전용 core/alerts 테스트 테이블에서만 수행하고 실제 사용자 계정을 사용하지 않는다. 메모리 스토어를 사용하는 격리 스테이징은 병행 실행해도 되지만 실제 DynamoDB 동작의 증거가 아니다. 새로운 AWS 테이블·역할 생성은 비용과 권한이 발생하므로 별도 승인 없이 실행하지 않는다.

### 사용자 제공 90일 스테이징 스모크 기록

제공된 로그에서는 합성 알림 기준 89일 데이터 노출과 91일 데이터 차단, 신규 레코드의 TTL 값, 최근 7일 이력·미확인 계산, 실제 WebSocket 알림, 탈퇴 차단 경로가 성공했다(`V2.4 REAL DYNAMODB RETENTION SMOKE TEST OK`, 12.737초). 시험 알림 본문·별칭과 임시 계정은 정리됐고 해당 비활성 합성 가구와 기기·credential 레코드는 남는다. 이는 운영 배포 완료 증거가 아니다. 정확한 90일 ±1µs 경계는 고정 시각 단위 테스트만 검증했으며, 실제 AWS 경계·TTL의 자동 물리 삭제·운영 MQTT·실제 프론트 연결·전체 기존 데이터 마이그레이션은 아직 확인하지 않았다. 이후 가입 연령 계약을 합친 새 커밋도 별도 스테이징 산출물로 다시 검증한다.

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

GitHub 반영·병합된 **검증한 커밋**을 새 r6에 체크아웃한다. 운영 r5에서 `git pull`하여 덮어쓰지 않는다. 새 디렉터리가 이미 있으면 내용을 확인하고 덮어쓰지 않는다. 90일 스모크에 사용한 기존 승인 SHA나 검증 helper를 현장에서 수정하지 말고, 연령 확인 변경을 포함한 새 병합 SHA로 새 검증 산출물을 만든다.

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

운영 env는 새 파일로 복사하고 권한 600을 유지한다. 출력/붙여넣기에 secret 전체를 노출하지 않는다. `/etc/hearo/hearo-api-v2-r6-staging.env`에는 `HEARO_ENV=development`, `HEARO_MQTT_ENABLED=false`를 사용한다. DynamoDB를 사용하는 스테이징이면 운영 또는 다른 환경과 공유하지 않는 테스트 테이블 이름만 허용한다. 기존 unread 기준시각·JWT secret·CORS를 임의 변경하지 않는다. 문서 값 미정 상태에서는 신규 5개 환경값을 비우고 `HEARO_LEGAL_CONSENT_REQUIRED=false`로 둔다.

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

정상 기준은 `2.4.0`, 공개 전 `configured=false`, 새 GET/PATCH `/me/consents`의 OpenAPI 등록이다. 연령 필드 추가만으로 서비스 버전을 바꾸지 않는다. 버전 테스트용 문서 설정은 테스트 테이블에만 넣는다. 신규/기존/부분 동의 복구, 정확한 90일 경계, 7일 이력·미확인 유지, 탈퇴 및 기존 LED·주소·MQTT·WebSocket 회귀를 테스트한다. 패키지/실제 AWS/실제 프론트가 로컬 테스트와 같다고 가정하지 않는다.

회원가입 연령 확인은 격리 스테이징에서 다음을 모두 확인한다.

- owner와 family member가 각각 실제 JSON `"age_over_14_agreed": true`로 가입되고 공개 User의 `age_over_14_agreed=true`, `age_over_14_agreed_at`이 반환된다.
- 필드 누락, `false`, `null`, 숫자 `0`·`1`, 문자열 `"true"`·`"false"`가 각각 `422 VALIDATION_ERROR`와 해당 `field_errors`를 반환하고 USER·가구·멤버십·동의 영수증을 전혀 만들지 않는다.
- 문서 설정이 비어 있는 호환 모드에서도 새 가입은 문서 버전 `null`을 유지하면서 세 확인값의 변경 불가 영수증을 만들고, PROFILE의 `consented_at`과 `age_over_14_agreed_at`이 같은 서버 UTC 시각이다. `null` 버전을 최신 문서 동의로 해석하지 않는다.
- 연령 필드가 없는 기존 사용자는 공개 User와 `GET /me/consents`에서 연령 값이 `null`이고 로그인·기존 기능이 차단되지 않으며 일괄 `true` 보정이 없다.
- `GET /me/consents`는 `age_over_14` 블록을 반환한다. `PATCH /me/consents`의 요청 모델은 기존 두 문서 동의만 받고, 성공 후에도 최초 연령 확인값·시각을 보존한다.

구 프론트는 새 백엔드의 필수 연령 필드를 보내지 않아 `422`이고, 새 프론트는 추가 필드를 금지하는 구 백엔드에서 `422`다. 전환 전에 가입 UI를 일시 중지하고, 새 병합 SHA의 백엔드와 새 프론트를 모두 전환한 뒤 위 두 가입 유형 스모크가 성공할 때만 가입을 다시 연다. 롤백도 가입을 닫은 상태에서 프론트와 백엔드를 함께 되돌린다.

연결 계정 탈퇴는 격리된 실제 DynamoDB의 합성 owner/member로 다음 항목을 모두 확인해야 한다.

- `Scan`과 `Query`가 여러 페이지를 반환하도록 만든 참조·동의 영수증을 사전 읽기에서 빠짐없이 집계한다.
- member 탈퇴와 owner 탈퇴가 각각 계정 삭제, 멤버십 변경 또는 전체 구성원 미연동, 가구 비활성화와 관련 정리를 한 트랜잭션으로 완료한다.
- 조건 충돌과 확인 가능한 트랜잭션 취소 시 `409 ACCOUNT_DELETION_CONFLICT`가 반환되고 계정·가구·멤버십이 함께 보존된다.
- 100개를 초과하는 고유 항목이 필요한 계획과 불완전한 멤버십 스냅샷은 트랜잭션 호출 전에 `409 ACCOUNT_DELETION_REVIEW_REQUIRED`가 되고 어떤 항목도 변경하지 않는다.
- 전송 결과를 알 수 없는 실패도 `409 ACCOUNT_DELETION_CONFLICT`가 될 수 있다. 이 경우 `/me`와 `/households/current`를 재조회해 실제 상태를 확인하고 같은 탈퇴 요청을 무조건 반복하지 않는다.
- 이미 미연동인 계정의 공개 탈퇴도 참조·동의·계정 식별정보를 단일 트랜잭션으로 삭제한다. 호환용 저장소 직접 호출의 묶음 정리 경로는 공개 API 검증과 별도로 계정 식별정보가 최종 단계까지 보존되는지 확인한다.

메모리 저장소와 stub 클라이언트 단위 테스트는 이 검증을 대체하지 않는다. DynamoDB 트랜잭션은 [AWS `TransactWriteItems` 명세](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html)에 따라 최대 100개의 서로 다른 항목, 전체 항목 크기 4MiB이며 한 트랜잭션에서 같은 항목을 두 작업이 대상으로 삼을 수 없다. 구현은 고유 항목 수를 사전 검사하지만 100개 이하라는 사실만으로 4MiB 충족이 증명되지는 않는다. 조건식과 실제 항목 크기를 포함한 요청이 격리된 실제 서비스에서 허용되는지 확인하고, 4MiB를 넘는 항목 계획이 일부만 반영되지 않고 원자적으로 거부되는지도 검증한다. DynamoDB가 트랜잭션을 거부하면 `ACCOUNT_DELETION_CONFLICT`로 처리한다. 위 실제 서비스 검증 증거가 없으면 3절 운영 전환으로 진행하지 않는다.

`membership_version`은 가구 멤버십 변경의 내부 fence다. 기존 가구의 누락 값은 `0`으로 읽고, 새 코드의 연동·연동 해제·연결 계정 탈퇴는 관련 변경과 같은 트랜잭션에서 이 값을 갱신한다. 이 fence를 모르는 r5 또는 수정 전 r6 API writer가 같은 DynamoDB 테이블에 동시에 쓰면 보호가 무효화될 수 있다. 메모리 저장소나 완전히 분리된 테스트 테이블의 스테이징은 병행할 수 있지만, 공유 테이블에 연결한 구버전·신버전 API의 동시 쓰기는 금지한다.

## 3. 운영 설정 백업과 전환

스테이징 성공 후 승인받은 시점에만 전환한다. root-only 새 백업 디렉터리에 두 env와 두 기존 systemd 파일을 복사하고 SHA-256/커밋을 기록한다. 같은 이름의 기존 백업을 덮어쓰지 않는다. 새 파일 `hearo-api-v2-r6.service`, `hearo-mqtt-bridge-v2-r6.service`는 설치할 때 운영의 기존 **final 서비스 이름**을 대상으로 사용한다. 가입 UI 중지와 프론트 동기화가 준비되지 않았으면 이 절을 시작하지 않는다.

```bash
sudo systemd-run --wait --pipe --collect --property=EnvironmentFile=/etc/hearo/hearo-api-v2.env --property=WorkingDirectory=/opt/hearo-backend-v2-r6 /opt/hearo-backend-v2-r6/.venv/bin/python -c 'from hearo_backend.config import Settings; s=Settings(); s.validate_for_production(); print("R6 PRODUCTION CONFIG OK")'
```

```bash
sudo systemctl stop hearo-api-v2-r6-staging
sudo systemctl stop hearo-mqtt-bridge-v2-final
sudo systemctl stop hearo-api-v2-final
```

이 시점부터 수정된 API가 시작될 때까지 운영 API 쓰기는 중단된다. 구버전 writer와 수정된 writer를 같은 테이블에 겹쳐 실행하는 무중단 전환은 허용하지 않는다.

```bash
sudo install -o root -g root -m 644 infra/systemd/hearo-api-v2-r6.service /etc/systemd/system/hearo-api-v2-final.service
sudo install -o root -g root -m 644 infra/systemd/hearo-mqtt-bridge-v2-r6.service /etc/systemd/system/hearo-mqtt-bridge-v2-final.service
```

```bash
sudo systemd-analyze verify /etc/systemd/system/hearo-api-v2-final.service /etc/systemd/system/hearo-mqtt-bridge-v2-final.service
sudo systemctl daemon-reload
sudo systemctl start hearo-api-v2-final
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

수정된 API가 `membership_version`을 사용하기 시작한 뒤에는 같은 운영 테이블을 바라보는 r5 또는 수정 전 r6 API를 writer로 다시 시작하지 않는다. 구버전은 fence를 갱신하지 않아 동시 연동·해제·탈퇴 보호를 우회할 수 있다. 문제가 생기면 API 쓰기를 중단하고 fence를 이해하는 검증된 순방향 수정으로 복구한다.

일괄 마이그레이션의 삭제는 API 코드 롤백으로 되돌아오지 않는다. 계획의 변경 전 이미지나 PITR 복구는 수동 검토 대상이며 만료 알림·탈퇴 계정·오래된 약관 상태를 그대로 재노출하지 않는다. TTL 활성화 후 만료된 데이터를 ‘복구용’이라는 이유로 무기한 재등록하지 않는다.

## 8. 완료 증거

운영 완료라고 보고하려면 외부 버전/새 API/CORS, 최근 정상 MQTT `200`, 기존 이력/미확인/LED/탈퇴, 본문·별칭 TTL, 신규/기존 동의 상태, 실제 프론트, 자동 정리 실행 결과, 로그·백업 보존 예외 결정의 증거를 확인한다. 두 가입 유형의 엄격한 연령 확인, 무효 요청 무쓰기, 기존 사용자 `null`, 버전 `null` 영수증과 프론트·백엔드 동시 전환 기록도 필요하다. 연결 계정 탈퇴의 격리된 실제 DynamoDB 경계·실패 시험과 구버전 writer가 같은 테이블에서 동시에 실행되지 않았다는 전환 기록도 필요하다. 로컬·메모리·stub 테스트만으로 이를 대체하지 않는다.

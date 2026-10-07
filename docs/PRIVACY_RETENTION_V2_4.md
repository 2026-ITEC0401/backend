# v2.4.0 개인정보 보관·동의 계약

상태: 로컬 구현 및 자동 테스트 단계. GitHub 반영, EC2 배포, 운영 데이터 마이그레이션과 문서 공개는 별도 승인 후 진행한다. 키트 등록 API는 이번 릴리스에 포함하지 않는다.

## 1. 확정된 정책과 아직 정하지 않은 값

| 항목 | 적용 기준 |
|---|---|
| 환경음 분류 결과·알림 보관 | 발생 시각부터 90 × 24시간 |
| 목록·알림 이력·미확인 계산 | 기존처럼 KST 오늘 포함 7개 날짜 |
| 알림 상세·최신 알림 | 90일 만료 데이터는 응답하지 않음 |
| 일반 서비스 이용기록 | 최대 90일 목표, 로그 설정·자동 정리 검증 필요 |
| 개인정보처리시스템 접속기록 | 제공 문서의 1년 이상 기준을 별도 관리; 일반 로그와 함께 90일로 줄이지 않음 |
| 기존 사용자의 문서 버전 | 확인되지 않으면 `null`; 동의 시각을 배포 시각으로 바꾸지 않음 |
| 문서 시행일·버전·공개 URL | 아직 미정. 2026-10-05로 소급 적용하지 않음 |

원본 제공 문서는 수정하지 않았으며, [개인정보처리방침 공개 준비본](legal/PRIVACY_POLICY_DRAFT.md)과 [서비스 이용약관 공개 준비본](legal/TERMS_OF_SERVICE_DRAFT.md)을 별도로 작성했다. 공개 준비본은 확정·공개된 법률 문서가 아니다. 실제 처리 범위, 해외 이전, 보호책임자와 보존 예외는 담당자의 최종 검토가 필요하다.

## 2. 알림 저장과 조회

신규 알림 본문과 `ALERTID#{household_id}#{event_id}` / `ALERT` 레코드에 동일한 숫자형 `expires_at_epoch`를 기록한다. 기존 PK/SK/GSI, MQTT 메시지와 프론트 알림 필드는 유지한다. 원시 오디오 저장은 추가하지 않는다.

TTL은 비동기 삭제이며 만료 직후 물리 삭제를 보장하지 않는다. 따라서 조회는 저장된 TTL의 유무와 무관하게 발생 시각으로 90일 만료를 검사한다. 만료된 알림은 상세 조회에서 찾을 수 없는 것으로 처리되고, 목록·미확인 계산·최신 조회에서도 제외된다. 이미 만료된 이벤트의 재수집은 저장·WebSocket 새 알림 전파를 하지 않는다. 서버보다 5분 초과 미래인 MQTT 알림은 `400 INVALID_TIMESTAMP`로 거부한다.

시간당 정리 도구와 TTL을 함께 사용한다. 다만 시간당 실행도 정확히 90일이 되는 순간의 삭제를 보장하지 않는다. 삭제 실패를 감시하고 물리 삭제 지연과 백업 처리 방식을 공개 문구와 일치시키는 운영 검증이 필요하다. [AWS TTL 설명](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html)

## 3. 문서 설정

아래 다섯 값은 모두 비어 있거나 모두 유효해야 한다.

```text
HEARO_TERMS_VERSION=
HEARO_PRIVACY_VERSION=
HEARO_LEGAL_EFFECTIVE_AT=
HEARO_TERMS_URL=
HEARO_PRIVACY_URL=
HEARO_LEGAL_CONSENT_REQUIRED=false
```

버전은 64자 이하 ASCII 식별자, 시행 시각은 시간대가 포함된 ISO 8601, 운영 문서 URL은 인증정보·쿼리 없는 공개 HTTPS 주소를 사용한다. `HEARO_LEGAL_CONSENT_REQUIRED`는 `true` 또는 `false`만 허용한다. 실제 값이 정해지기 전에는 위 호환 모드를 유지한다.

시행 후 `required=true`는 새 회원가입의 버전 제출을 요구하고 기존 계정에 재동의 필요 여부를 안내한다. **시행 시각 전에 `required=true`로 켜면 신규 가입은 버전 제출 여부와 무관하게 `LEGAL_POLICY_NOT_EFFECTIVE`로 차단된다.** 기존 로그인·LED·MQTT·회원 탈퇴를 서버 전체에서 일괄 차단하는 기능은 아니다. 기존 계정의 재동의 화면과 중요한 변경 시 서비스 이용 제한 여부는 프론트와 별도로 합의·구현해야 한다.

## 4. 프론트 연동 API

운영 base URL은 기존 `/v2`를 유지한다. 아래 경로는 base URL 뒤에 붙인다.

### 공개 문서 조회: `GET /legal/policies`

인증이 필요하지 않으며 `Cache-Control: no-store`로 응답한다. 현재 공개 준비 상태의 응답:

```json
{
  "configured": false,
  "effective": false,
  "required": false,
  "effective_at": null,
  "terms": {"version": null, "url": null},
  "privacy": {"version": null, "url": null}
}
```

문서의 설정·시행 여부를 확인한 뒤 서버가 반환한 URL의 해당 버전을 사용자에게 보여준다. 아직 설정되지 않은 상태에서 프론트가 버전 문자열을 임의로 만들어 보내지 않는다.

### 본인 동의 상태: `GET /me/consents`

`Authorization: Bearer {access_token}`이 필요하다. 응답에 `status`, `consent_required`, `can_consent`, `consented_at` 및 각 문서의 `agreed`, `accepted_version`, `current_version`, `is_current`가 포함된다.

| status | 의미 |
|---|---|
| `policy_not_configured` | 아직 문서 버전·공개 주소 설정 전 |
| `policy_not_effective` | 설정되었지만 시행 시각 전 |
| `legacy_unversioned` | 두 문서의 당시 동의 버전이 모두 확인되지 않음 |
| `outdated` | 다른 버전에 동의했거나 버전·동의값·시각이 부분적으로 불완전함 |
| `current` | 두 현재 버전·동의값·유효한 실제 동의 시각이 확인됨 |

### 본인 명시적 동의: `PATCH /me/consents`

사용자에게 두 문서를 보여주고 각각 동의를 받은 뒤 호출한다. 서버가 반환한 현재 버전을 그대로 사용한다.

```json
{
  "terms_service_agreed": true,
  "privacy_agreed": true,
  "terms_version": "서버에서 받은 terms.version",
  "privacy_version": "서버에서 받은 privacy.version"
}
```

위 문자열은 실제 버전이 아닌 설명용이다. 시각은 요청에 보내지 않으며 서버가 실제 처리 시각을 기록한다. 정상 응답은 `200`과 갱신된 동의 상태이다. 동일 버전의 정상적인 동의를 다시 보내면 최초 실제 동의 시각을 유지한다. 사용자와 클라이언트 IP 조합당 15분에 10회로 제한한다.

| HTTP / code | 프론트 처리 |
|---|---|
| `401` | 로그인 상태 확인 |
| `409 LEGAL_POLICY_NOT_CONFIGURED` | 공개 설정 완료 전, 동의 요청 중단 |
| `409 LEGAL_POLICY_NOT_EFFECTIVE` | 시행 전 동의 요청 중단 |
| `409 LEGAL_VERSION_MISMATCH` | 문서 정보를 재조회하고 새 버전에 다시 명시적 동의 |
| `409 LEGAL_CONSENT_CONFLICT` | 상태를 재조회한 뒤 재시도 |
| `422` | 필수 동의값 또는 요청 필드 형식 점검 |
| `429` | 요청 간격을 두고 재시도 |

### 회원가입

기존 가입 요청에 `terms_version`, `privacy_version`을 추가할 수 있다. `required=false`이면 문서의 설정·시행 여부와 관계없이 두 필드를 모두 생략할 수 있고 `null`로 저장한다. 일부만 제출하거나 실제 버전과 다른 값이면 `LEGAL_VERSION_MISMATCH`가 발생한다. 시행 후 `required=true`이면 현재 두 버전이 필요하며 누락 시 회원가입 전용 오류 `409 LEGAL_CONSENT_REQUIRED`가 발생한다. 기존 회원을 최신 버전에 일괄 동의 처리하는 마이그레이션은 제공하지 않는다.

## 5. 저장·탈퇴 무결성

PROFILE의 두 버전과 `consented_at`을 갱신할 때 최소 동의 영수증(`USER#{id}` / `CONSENT#...`)을 같은 DynamoDB 트랜잭션으로 기록한다. 영수증은 사용자 ID, 두 버전, 동의값, 시각만 포함하고 이름·번호·비밀번호·주소는 복제하지 않는다. 버전을 제출한 최초 가입은 PROFILE과 영수증을 원자적으로 생성한다. 호환 가입은 버전을 임의로 채우지 않고 영수증도 만들지 않는다.

탈퇴 시 영수증과 식별 정보를 같은 최종 트랜잭션에서 삭제한다. 실패하면 자동 성공 응답을 하지 않는다. 동의 영수증이 97개를 초과하는 드문 계정은 DynamoDB 100-action 한계 때문에 `409 ACCOUNT_DELETION_REVIEW_REQUIRED`로 중단하며 별도 관리자 처리가 필요하다. 부분 동의 이력을 먼저 삭제하는 자동 청크 정리는 하지 않는다. 토큰·개인 표시명은 필요하면 PROFILE 조건을 건 청크로 먼저 정리하므로, 후속 실패 시 일부 세션이나 표시명이 정리된 상태로 탈퇴를 재시도할 수 있다. 이때 사용자 식별 정보와 모든 동의 영수증은 아직 보존된다.

**남은 운영 차단 조건: 연동 계정의 탈퇴 전체는 하나의 트랜잭션이 아니다.** `services.delete_account()`는 가구 연동 해제를 먼저 수행하고, 그 다음 계정·영수증 삭제를 수행한다. 따라서 후속 `409` 또는 저장소 장애에도 member의 연결이 해제됐거나 owner의 가구가 비활성화되고 주소·초대 코드가 삭제되며 다른 구성원도 미연동으로 바뀔 수 있다. `ACCOUNT_DELETION_REVIEW_REQUIRED`도 연동 해제 전에 전체 요청을 중단하는 사전 검사는 아니다. 실패를 ‘DB 무변경’으로 안내하지 않는다. 프론트는 실패 뒤 `/me`와 `/households/current`를 재조회하며, owner의 가구 복구를 위한 공개 API는 현재 없다. 이 실패 경로의 복구 정책·구현·스테이징 검증을 완료하기 전 실제 owner 계정으로 시험하거나 운영 전환하지 않는다. 현재 비밀번호 불일치만 연동 해제 전 검사하므로 무변경이다.

토큰·개인 표시명 생성은 PROFILE의 내부 `reference_version` 증가와 함께 원자 처리한다. 탈퇴 스캔 도중 새 기록이 생성되면 최종 삭제 조건이 실패하여 재시도하고, PROFILE 삭제 후에는 새 관련 기록이 생성되지 않도록 한다. 인증용 `token_version`과 다른 필드이며 기존 세션을 무효화하지 않는다.

기존 owner 탈퇴 시 비활성 가구와 기기 레코드가 남는 구조는 그대로다. 문서의 ‘기기 등록정보 탈퇴 시 파기’와 실제 잔존 항목의 관계는 공개 전 별도 정리·익명화 결정이 필요하다. 이번 릴리스가 모든 개인정보 파기 요구를 완전히 충족한다고 보아서는 안 된다.

## 6. 로그·백업 운영 경계

Nginx/rsyslog 예시는 `daily`, `rotate 90`, `maxage 90`을 사용하지만 날짜별 파일 수만으로 엄밀한 최대 90일 삭제를 보장하지 않는다. `maxage`는 회전 때 확인되고, `notifempty` 등으로 오래된 파일이 남을 수 있어 실제 오래된 파일 검사와 별도 정리 절차가 필요하다. [logrotate 동작](https://man7.org/linux/man-pages/man8/logrotate.8.html)

전역 journald 90일 설정은 보안·감사 기록에도 영향을 준다. 감사 기록을 별도 저장하고 최소 1년 보존하는 방식이 검증되기 전에는 제공한 전역 drop-in을 설치하지 않는다. rsyslog의 `auth.log`도 일반 로그 정리 대상에서 제외한다.

현재 전달받은 두 테이블의 PITR 보존기간은 35일이며 알림의 일반 조회 90일과 별도다. 복구된 데이터에 90일 필터를 다시 적용하고 탈퇴 계정이 재활성화되지 않도록 해야 한다. 운영 데이터가 들어 있는 마이그레이션 계획/백업 파일은 0600으로 제한하고 검증 후 즉시 정리할 대상으로 관리한다. 복구본·스냅샷·백업의 예외와 삭제 시점은 공개 정책에 반영할지 담당자의 결정이 필요하다.

구체적인 적용 순서는 [v2.4.0 배포 안내](DEPLOYMENT_V2_4.md)를 따른다.

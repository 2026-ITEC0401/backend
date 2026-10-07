# Hearo 프론트엔드 연동용 백엔드 API 명세

- 문서 상태: v2.4.0 구현 기준·배포 전 검토용. 탈퇴 실패 복구 등 운영 차단 조건은 [`DEPLOYMENT_V2_4.md`](./DEPLOYMENT_V2_4.md)를 따릅니다.
- 최초 작성일: 2026-08-17
- 구현 반영일: 2026-10-07
- API 구현 버전: 2.4.0 (`/v2` 외부 경로 유지)
- 프론트 필드 활용 사전: [10. API 필드별 프론트 활용 가이드](#10-api-필드별-프론트-활용-가이드)

## 1. 서비스 규칙

- 로그인에는 `login_id`와 비밀번호를 사용합니다.
- 회원가입 필수 정보는 로그인 아이디, 이름, 휴대폰 번호, 비밀번호입니다.
- `신규 가구 등록` 사용자는 가구를 생성하고 `owner`가 됩니다.
- 신규 가구 owner는 주소 없이 가입할 수 있으며, 가입 후 스킵 불가 주소 온보딩을 진행합니다. 주소는 사용자 프로필이 아닌 가구에 저장합니다.
- `가족·보호자로 참여` 사용자는 먼저 미연동 계정으로 가입하며, 초대 코드는 가입 직후 또는 설정에서 입력할 수 있습니다.
- 초대 코드는 6자리 영문·숫자 조합이며, 유효기간 동안 여러 가족이 같은 코드를 사용할 수 있습니다.
- 가족 구성원의 이름 편집은 실제 회원 이름을 변경하는 기능이 아니라, 현재 로그인한 사용자의 화면에만 적용되는 개인 표시 이름(alias) 설정입니다.
- 개인 표시 이름 설정은 owner 전용 기능이 아닙니다. 가구에 연동된 사용자라면 자신이 보는 각 구성원의 표시 이름을 설정·초기화할 수 있습니다.
- 가족 연동 해제는 다른 구성원을 가구에서 내보내는 기능이 아니라, 현재 로그인한 계정이 자신의 가구 연결을 끊는 기능입니다. owner가 다른 계정의 연결을 대신 해제할 수 없습니다.
- 일반 member가 연동을 해제하면 해당 member의 연결만 끊고 가구는 유지합니다. 가구 owner가 자신의 연동을 해제하면 소유권을 이전하지 않고 가구 전체를 비활성화합니다.
- 기기 설정은 MQTT 연결 ON/OFF와 ESP32 3대의 LED 알림 ON/OFF만 제공합니다. Raspberry Pi에는 LED 설정을 제공하지 않습니다.
- 회원 탈퇴는 현재 비밀번호를 재확인한 뒤 로그인 ID·전화번호·프로필·refresh token·개인 표시 이름을 삭제합니다.
- 진동 알림과 민감도 조절은 화면과 API에서 제거합니다.
- 알림 이력은 한국시간 기준 오늘을 포함한 최근 7개 달력 날짜를 반환합니다.
- 알림 데이터는 발생 시각부터 정확히 90일 동안만 사용자 API에서 조회할 수 있습니다. 최근 7일 이력 범위와 90일 보관·조회 기한은 서로 다른 기준입니다.
- 약관·개인정보 처리방침의 실제 버전, 시행 시각과 공개 URL은 서버 정책 API를 기준으로 합니다. 기존 사용자의 확인되지 않은 버전을 최신 버전으로 임의 배정하지 않습니다.
- 녹음 파일을 생성·업로드·보관·재생하지 않습니다.

## 2. 공통 규칙

### 2.1 통신과 시간

- 운영 API는 HTTPS만 사용합니다.
- JSON 키는 `snake_case`를 사용합니다.
- 서버 저장 시각은 UTC ISO 8601 형식으로 반환합니다.
- 사용자 표시용 시각은 `Asia/Seoul` 기준 `local_time`을 함께 제공합니다.
- 가구 데이터 API는 access JWT가 필요합니다.
- 다른 가구 데이터에 접근하면 `403`을 반환합니다.
- 가구 미연동 사용자가 가구 전용 API를 호출하면 `409 HOUSEHOLD_LINK_REQUIRED`를 반환합니다.

### 2.2 인증 토큰

- access JWT 유효기간: 15분
- refresh JWT 유효기간: 30일
- refresh token은 사용할 때마다 회전하며 이전 token은 폐기합니다.
- 로그아웃 시 전달된 refresh token을 폐기합니다.

인증 헤더:

```text
Authorization: Bearer {access_token}
```

### 2.3 입력 규칙

| 필드 | 규칙 |
|---|---|
| `login_id` | 4~30자, 영문·숫자·마침표·밑줄·하이픈, 중복 불가 |
| `name` | 공백 제외 1~60자 |
| `phone_number` | 국내 번호 입력 허용, 서버에서 E.164로 정규화, 중복 불가 |
| `password` | 10자 이상, 영문자와 숫자 포함 |
| `invite_code` | 영문 대문자·숫자 6자리, 대소문자 구분 없이 처리 |
| `postal_code` | 숫자 5자리, 주소 검색 결과에서 선택 |
| `road_address` | 5~200자, 주소 검색 결과에서 선택, 줄바꿈·제어 문자 금지 |
| `detail_address` | 최대 200자, 사용자가 직접 입력, 줄바꿈·제어 문자 금지 |
| `address_provider` | 신규 입력은 `juso_go_kr` 또는 `manual`; 기존 `kakao_postcode` 레코드는 읽기 호환 |
| `verified` | 서버가 행안부 응답으로 판정하는 읽기 전용 값. 클라이언트 전송 금지 |
| `terms_version` | 최대 64자. `GET /legal/policies`가 반환한 현재 약관 버전을 그대로 사용 |
| `privacy_version` | 최대 64자. `GET /legal/policies`가 반환한 현재 개인정보 처리방침 버전을 그대로 사용 |

회원가입 화면에는 별도 로그인 아이디 입력칸이 필요합니다. 현재 와이어프레임에 없다면 프론트 화면을 추가 수정해야 합니다.

### 2.4 공통 오류 형식

```json
{
  "code": "LOGIN_ID_ALREADY_EXISTS",
  "message": "이미 사용 중인 아이디입니다.",
  "field_errors": {
    "login_id": "다른 아이디를 입력해 주세요."
  },
  "request_id": "req-01H..."
}
```

| HTTP 상태 | 의미 |
|---:|---|
| `400` | 잘못된 요청 또는 만료·형식 오류 |
| `401` | 로그인 실패 또는 만료된 인증 |
| `403` | 다른 가구 접근 또는 권한 부족 |
| `404` | 대상 데이터 없음 |
| `409` | 아이디·휴대폰 중복, 가구 미연동, 상태 충돌 |
| `422` | 필수 입력 누락 또는 입력 검증 실패 |
| `429` | 로그인·초대 코드 조회 등의 요청 횟수 초과 |
| `502` | 행안부 응답 형식 오류 |
| `503` | 행안부 주소 서비스 장애·시간 초과·승인키 설정 오류 |

### 2.5 수동 명세와 생성 OpenAPI의 역할

`/openapi.json`은 경로와 요청 모델을 확인하는 보조 자료입니다. 현재 생성 문서는 `X-Device-Credential`·`X-Internal-Token`을 보안 스키마로 표현하지 않고, 여러 성공 응답 스키마를 빈 객체로 표시하며, 런타임의 `422` 오류 형식도 충분히 반영하지 않습니다. 따라서 프론트·기기 연동에서는 이 문서에 명시한 헤더, 성공 응답, 공통 오류 형식을 계약으로 사용하고 실제 코드·테스트와 함께 변경합니다.

## 3. 화면별 API 연결표

| 화면 | API | 비고 |
|---|---|---|
| 로그인·회원가입 선택 | 없음 | 화면 이동만 수행 |
| 약관·개인정보 처리방침 표시 | `GET /legal/policies` | 공개 문서 URL·버전·시행 여부 조회 |
| 로그인 | `POST /auth/login` | `login_id`, `password` |
| 가입 유형 선택 | 없음 | 선택값을 회원가입 요청에 포함 |
| 신규 가구 회원가입 | `POST /auth/signup` | 사용자·가구·4개 기기 생성 후 주소 온보딩으로 이동 |
| 가족 참여 회원가입 | `POST /auth/signup` | 미연동 사용자 생성 |
| 초대 코드 입력 | `POST /households/link/preview` | 가구 정보를 조회만 함 |
| 조회 가구 연동 | `POST /households/link` | 확인 후 실제 연동 |
| 나중에 하기 | 없음 | 미연동 상태 유지 |
| 설정 메인 | `GET /me`, `GET /households/current` | 사용자와 연동 상태 표시 |
| 내 동의 상태·재동의 | `GET /me/consents`, `PATCH /me/consents` | 현재 버전 동의 여부 조회와 명시적 재동의 |
| 긴급 주소 조회·수정 | `GET/PATCH /households/{id}/emergency-address` | 연동 사용자 조회, owner 수정 |
| 도로명주소 검색 | `POST /households/{id}/address-search/roads` | owner 전용 행안부 프록시 |
| 동·층·호 검색 | `POST /households/{id}/address-search/details` | owner 전용 행안부 프록시 |
| 가족 표시 이름 변경 | `PATCH /households/{id}/members/{member_user_id}/display-name` | 호출자 화면에만 적용되는 별칭 |
| 가족 표시 이름 초기화 | `DELETE /households/{id}/members/{member_user_id}/display-name` | 가입 시 이름으로 복원 |
| 비밀번호 변경 | `PATCH /me/password` | 기존 비밀번호 확인 |
| 회원 탈퇴 | `DELETE /me` | 현재 비밀번호 재확인 후 계정 삭제 |
| owner 가족 설정 | `GET /households/{id}/invite-code` | 현재 공유 코드 표시 |
| 초대 코드 재발급 | `POST /households/{id}/invite-code/rotate` | 기존 코드 즉시 폐기 |
| 미연동 가족 설정 | `POST /households/link/preview`, `POST /households/link` | 가입 이후에도 연동 가능 |
| 연동 가족 목록 | `GET /households/{id}/members` | 이름·휴대폰·역할 표시 |
| 내 계정의 가구 연동 해제 | `DELETE /households/current/link` | member는 본인 연결 해제, owner는 가구 비활성화 |
| 메인 기기 목록 | `GET /households/{id}/devices` | 고정 4개 기기와 상태 표시 |
| 기기 연결 설정 | `PATCH /households/{id}/devices/{device_id}/connection` | owner만 가능 |
| LED 알림 설정 | `PATCH /households/{id}/devices/{device_id}/settings` | owner만 가능, ESP32 3대만 지원 |
| 메인 최신 알림 | `GET /households/{id}/alarms/latest` | 가장 최근 알림 1건 |
| 알림 날짜별 목록 | `GET /households/{id}/alarms/history` | 최근 7일 전체 |
| 미확인 알림 배너 | `GET /households/{id}/alarms/unread-count` | 로그인 사용자 기준 최근 7일 미확인 개수 |
| 알림 모두 확인 | `PATCH /households/{id}/alarms/seen` | 로그인 사용자의 확인 시각을 서버 현재 시각으로 갱신 |
| 알림 상세 보기 | `GET /households/{id}/alarms/{alarm_id}` | 선택한 알림 1건 |
| 보호자 연락처 | `GET/POST/PATCH/DELETE /households/{id}/contacts...` | 연락처 조회와 owner의 등록·수정·삭제 |
| 실시간 상태·알림 | `WS /ws/households/{id}` | 기기 상태와 신규 알림 |

## 4. 인증·회원가입 API

### 4.1 공개 약관 정책 조회

```text
GET /legal/policies
```

인증 없이 호출합니다. 서버에 설정된 약관·개인정보 처리방침의 버전, 공개 URL, 시행 여부와 가입 시 버전 동의 강제 여부를 반환합니다. 응답에는 `Cache-Control: no-store`가 포함됩니다.

실제 버전·시행일·URL이 아직 설정되지 않은 호환 모드 응답:

```json
{
  "configured": false,
  "effective": false,
  "required": false,
  "effective_at": null,
  "terms": {
    "version": null,
    "url": null
  },
  "privacy": {
    "version": null,
    "url": null
  }
}
```

| 필드 | 의미 |
|---|---|
| `configured` | 두 버전·시행 시각·두 공개 URL이 모두 설정됐는지 나타냅니다. 부분 설정은 애플리케이션 시작 단계에서 거부됩니다. |
| `effective` | 서버 현재 시각이 `effective_at`에 도달했는지 나타냅니다. `false`이면 해당 버전으로 아직 동의할 수 없습니다. |
| `required` | 시행된 현재 버전 없이 신규 가입할 수 있는지 결정하는 서버 설정입니다. 기존 계정의 로그인·기기·MQTT·회원 탈퇴를 일괄 차단하지 않습니다. |
| `effective_at` | 시간대가 포함된 ISO 8601 시행 시각입니다. 미설정이면 `null`입니다. |
| `terms.version`, `privacy.version` | 가입·재동의 요청에 그대로 복사할 현재 문서 버전입니다. 미설정이면 `null`입니다. |
| `terms.url`, `privacy.url` | 사용자가 실제 내용을 확인할 공개 문서 URL입니다. 미설정이면 `null`입니다. |

프론트는 URL의 문서를 사용자에게 표시한 뒤 같은 응답의 버전을 가입 또는 재동의 요청에 사용합니다. 버전을 추측하거나 앱에 하드코딩하지 않습니다.

서버 정책은 `HEARO_TERMS_VERSION`, `HEARO_PRIVACY_VERSION`, `HEARO_LEGAL_EFFECTIVE_AT`, `HEARO_TERMS_URL`, `HEARO_PRIVACY_URL` 다섯 값을 모두 비우거나 모두 유효하게 설정합니다. 실제 값이 확정되기 전에는 모두 비우고 `HEARO_LEGAL_CONSENT_REQUIRED=false`를 유지합니다. 버전은 `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, 시행 시각은 시간대 포함 ISO 8601, 운영 URL은 인증정보·쿼리·fragment가 없는 HTTPS여야 합니다. `HEARO_LEGAL_CONSENT_REQUIRED`는 `true` 또는 `false`만 허용합니다.

### 4.2 회원가입

```text
POST /auth/signup
```

신규 가구 등록 요청:

```json
{
  "login_id": "hearo_user01",
  "name": "홍길동",
  "phone_number": "010-1234-5678",
  "password": "StrongPassword123",
  "signup_type": "new_household",
  "household_name": "홍길동 가구",
  "terms_service_agreed": true,
  "privacy_agreed": true,
  "terms_version": "terms-v1",
  "privacy_version": "privacy-v1"
}
```

가족·보호자 참여 요청:

```json
{
  "login_id": "family_user01",
  "name": "김가족",
  "phone_number": "010-9876-5432",
  "password": "StrongPassword123",
  "signup_type": "family_member",
  "terms_service_agreed": true,
  "privacy_agreed": true,
  "terms_version": "terms-v1",
  "privacy_version": "privacy-v1"
}
```

위 버전 문자열은 형식 예시입니다. 실제 요청은 `GET /legal/policies`가 반환한 값을 그대로 사용합니다.

가입 동의 호환 규칙:

- `terms_service_agreed`와 `privacy_agreed`는 기존과 같이 항상 `true`여야 합니다.
- `terms_version`과 `privacy_version`은 기존 프론트 호환을 위해 스키마상 선택 필드이며 둘 다 최대 64자입니다.
- `required=false`이면 문서가 설정·시행된 뒤에도 두 버전을 모두 생략할 수 있습니다. 이 경우 두 버전은 `null`로 저장하며 서버가 최신 버전을 임의 배정하지 않습니다.
- 버전을 하나라도 제출하면 정책이 설정되고 시행된 상태여야 하며 두 값 모두 현재 버전과 정확히 일치해야 합니다.
- `required=true`이고 시행 전이면 버전 제출 여부와 관계없이 가입을 `409 LEGAL_POLICY_NOT_EFFECTIVE`로 거부합니다.
- `required=true`이고 시행 후인데 두 버전을 모두 생략하면 `409 LEGAL_CONSENT_REQUIRED`, 일부만 제출하거나 현재 버전과 다르면 `409 LEGAL_VERSION_MISMATCH`를 반환합니다.
- 정책 미설정 상태에서 버전을 임의 제출하면 `409 LEGAL_POLICY_NOT_CONFIGURED`를 반환합니다.

- `emergency_address`는 `new_household` 가입에서 선택 항목입니다. 기존 클라이언트가 주소를 함께 보내는 요청도 계속 허용하지만 서버 검증 전이므로 `verified=false`로 저장합니다.
- `family_member` 가입에는 주소를 입력하지 않고, 가구 연동 후 해당 가구 주소를 사용합니다.
- `updated_at`은 클라이언트가 전송하지 않고 서버가 저장 시 생성합니다.

응답:

```json
{
  "user": {
    "user_id": "user-a1b2",
    "login_id": "family_user01",
    "name": "김가족",
    "phone_number": "+821098765432",
    "account_type": "family_member",
    "household_id": null,
    "role": null,
    "household_link_status": "unlinked",
    "terms_version": "terms-v1",
    "privacy_version": "privacy-v1",
    "consented_at": "2026-10-07T03:00:00Z",
    "created_at": "2026-10-07T03:00:00Z"
  },
  "tokens": {
    "access_token": "...",
    "refresh_token": "...",
    "token_type": "bearer",
    "expires_in": 900
  }
}
```

신규 가구 가입 응답에는 최초 기기 설치용 credential이 한 번만 추가로 반환됩니다. 이 값은 프론트에서 지속 보관하지 않고 설치 담당자에게 안전하게 전달해야 합니다.

### 4.3 로그인

```text
POST /auth/login
```

```json
{
  "login_id": "hearo_user01",
  "password": "StrongPassword123"
}
```

성공 응답에는 사용자 정보와 access·refresh token을 반환합니다. 아이디 또는 비밀번호가 틀린 경우 어느 항목이 틀렸는지 구분하지 않고 동일한 `401 INVALID_CREDENTIALS`를 반환합니다.

### 4.4 토큰 회전과 로그아웃

```text
POST /auth/refresh
POST /auth/logout
```

```json
{
  "refresh_token": "..."
}
```

### 4.5 내 정보 조회

```text
GET /me
```

회원가입 때 입력한 실제 이름은 사용자 프로필의 `name`으로 유지합니다. 가족 설정 화면의 이름 편집은 이 값을 변경하지 않으며, 5.6절의 사용자별 표시 이름 API를 사용합니다.

`GET /me`와 회원가입·로그인 응답의 공개 사용자 객체에는 `terms_version`, `privacy_version`, `consented_at`도 포함됩니다. 기존 호환 가입처럼 버전을 확인할 수 없으면 두 버전은 `null`입니다. `consented_at`이 존재하더라도 버전이 `null`이면 현재 공개 문서에 동의했다는 뜻이 아니므로, 재동의 화면은 반드시 다음 동의 상태 API를 기준으로 분기합니다.

### 4.6 본인 동의 상태 조회와 재동의

```text
GET   /me/consents
PATCH /me/consents
Authorization: Bearer {access_token}
```

두 응답 모두 `Cache-Control: no-store`를 사용합니다. `GET`은 저장된 사용자 동의와 현재 정책을 비교하며 데이터를 변경하지 않습니다.

```json
{
  "status": "legacy_unversioned",
  "consent_required": true,
  "can_consent": true,
  "consented_at": "2026-08-22T03:00:00Z",
  "terms": {
    "agreed": true,
    "accepted_version": null,
    "current_version": "terms-v1",
    "is_current": false
  },
  "privacy": {
    "agreed": true,
    "accepted_version": null,
    "current_version": "privacy-v1",
    "is_current": false
  }
}
```

| `status` | 의미 |
|---|---|
| `policy_not_configured` | 현재 문서 버전·시행 시각·공개 URL이 아직 설정되지 않았습니다. |
| `policy_not_effective` | 정책은 설정됐지만 시행 시각 전입니다. |
| `legacy_unversioned` | 정책은 시행됐지만 사용자의 두 당시 버전이 모두 확인되지 않습니다. |
| `outdated` | 버전이 다르거나 버전·동의값·동의 시각 중 일부가 불완전합니다. |
| `current` | 두 버전이 현재 정책과 일치하고, 두 동의값이 `true`이며 시간대가 포함된 유효 동의 시각이 있습니다. |

- `consent_required`는 정책이 시행됐고 `required=true`이며 사용자가 `current`가 아닐 때 `true`입니다.
- `can_consent`는 정책이 설정·시행됐고 사용자가 아직 `current`가 아닐 때 `true`입니다.
- `terms.is_current`와 `privacy.is_current`는 각 문서의 버전·동의값·공통 동의 시각을 기준으로 계산합니다.
- 재동의 필요 상태는 안내와 화면 분기용입니다. 기존 계정의 로그인, 가구·기기·LED·MQTT API와 회원 탈퇴를 서버 전역에서 차단하지 않습니다.

재동의 요청:

```json
{
  "terms_service_agreed": true,
  "privacy_agreed": true,
  "terms_version": "terms-v1",
  "privacy_version": "privacy-v1"
}
```

- 두 동의값은 모두 `true`, 두 버전은 모두 필수이며 `GET /legal/policies`가 반환한 현재 값과 정확히 일치해야 합니다.
- 클라이언트는 동의 시각을 보내지 않습니다. 서버가 실제 처리 시각을 UTC로 기록하고 최소 동의 영수증을 저장합니다.
- 정상 응답은 `200`과 갱신된 동의 상태입니다. 같은 버전에 대한 완전하고 유효한 동의를 다시 보내면 기존 동의 시각을 유지합니다.
- 버전은 같지만 동의값이나 동의 시각이 불완전한 기존 레코드는 멱등 성공으로 간주하지 않고 새 서버 시각과 영수증으로 복구합니다.
- 요청 횟수는 클라이언트 IP와 `user_id` 조합별로 15분에 10회입니다.

| API | HTTP / code | 조건 |
|---|---|---|
| 회원가입 | `409 LEGAL_POLICY_NOT_CONFIGURED` | 정책 미설정 상태에서 버전을 하나라도 제출 |
| 회원가입 | `409 LEGAL_POLICY_NOT_EFFECTIVE` | `required=true`인 정책의 시행 전 가입, 또는 시행 전 버전 제출 |
| 회원가입 | `409 LEGAL_CONSENT_REQUIRED` | 시행된 `required=true` 정책에서 두 버전을 모두 생략 |
| 회원가입 | `409 LEGAL_VERSION_MISMATCH` | 제출 버전이 일부 누락됐거나 현재 버전과 불일치 |
| 재동의 | `409 LEGAL_POLICY_NOT_CONFIGURED` | 현재 정책 미설정 |
| 재동의 | `409 LEGAL_POLICY_NOT_EFFECTIVE` | 현재 정책 시행 전 |
| 재동의 | `409 LEGAL_VERSION_MISMATCH` | 제출 버전이 현재 버전과 불일치 |
| 재동의 | `409 LEGAL_CONSENT_CONFLICT` | 동시 요청 등으로 저장 상태가 변경되어 안전하게 기록하지 못함 |
| 재동의 | `422 VALIDATION_ERROR` | 동의값이 `true`가 아니거나 필수 필드·형식 오류 |
| 재동의 | `429 LEGAL_CONSENT_RATE_LIMITED` | 같은 IP·사용자 조합에서 15분에 10회 초과 |

### 4.7 로그인 상태의 비밀번호 변경

```text
PATCH /me/password
```

```json
{
  "current_password": "StrongPassword123",
  "new_password": "NewStrongPassword456"
}
```

변경 성공 시 기존 access·refresh token을 모두 무효화하고 재로그인을 요구합니다. 해당 사용자의 기존 WebSocket도 종료 코드 `1008`로 닫습니다.
로그인하지 못한 사용자의 비밀번호 찾기는 SMS 인증 수단이 확정되기 전까지 이번 명세에서 제외합니다.

### 4.8 회원 탈퇴

```text
DELETE /me
```

```json
{
  "current_password": "StrongPassword123"
}
```

- access token 인증과 현재 비밀번호 재확인이 모두 필요합니다.
- 성공 응답은 본문 없는 `204 No Content`입니다.
- 로그인 ID, 휴대폰 alias, 사용자 프로필, refresh token, 동의 영수증, 미확인 기준과 해당 사용자가 만든·대상인 개인 표시 이름을 삭제합니다.
- 일반 member는 본인 멤버십만 제거하며 가구는 유지합니다.
- owner는 기존 owner 연동 해제 정책과 동일하게 가구를 비활성화하고 긴급 주소를 삭제합니다. 다른 구성원의 계정은 삭제하지 않고 미연동 상태로 전환합니다.
- 성공 직후 기존 access·refresh token 및 로그인 정보는 사용할 수 없습니다.
- 비밀번호가 틀리면 `409 CURRENT_PASSWORD_MISMATCH`를 반환하며 아무 데이터도 변경하지 않습니다.
- 연결된 계정의 탈퇴는 가구 연동 해제와 최종 계정 삭제가 하나의 원자적 트랜잭션이 아닙니다. 현재 비밀번호 확인 뒤 `unlink_user`가 먼저 커밋되고 프로필·식별자·동의 영수증 삭제가 이어집니다.
- 동의 영수증이 97개를 초과하면 최종 계정 삭제를 `409 ACCOUNT_DELETION_REVIEW_REQUIRED`로 중단합니다. 그 밖의 동시 변경은 `409 ACCOUNT_DELETION_CONFLICT`, 저장소 장애는 `5xx`가 될 수 있습니다.
- 위 실패가 발생해도 앞선 연동 해제는 이미 반영됐을 수 있습니다. member는 `unlinked`, owner는 가구 `inactive`와 주소·초대 삭제 및 모든 member의 `unlinked` 전환까지 완료됐을 수 있습니다. 프론트는 `/me`와 `/households/current`를 다시 조회하고 관리자 지원을 안내해야 합니다. 비활성 owner 가구를 복구하는 사용자 API는 현재 없습니다.
- 성공 시 member 본인의 WebSocket, owner이면 해당 가구의 모든 WebSocket을 `1008`로 종료합니다. 비밀번호 불일치를 제외한 저장소 실패에도 같은 범위의 연결을 보수적으로 종료하되, 성공 상태 이벤트를 임의로 보내거나 원래 HTTP 오류를 변경하지 않습니다.
- owner 탈퇴 후 비활성 가구·기기 레코드와 보관 기한 안의 알림은 즉시 모두 삭제되지 않습니다. 이를 가구 전체의 물리 삭제 API로 해석하지 않습니다.

## 5. 가구·가족 연동 API

### 5.1 현재 가구 조회

```text
GET /households/current
```

연동된 사용자:

```json
{
  "household_link_status": "linked",
  "household": {
    "household_id": "home-a1b2c3",
    "name": "홍길동 가구",
    "member_count": 3,
    "created_at": "2026-08-17T03:00:00Z"
  },
  "membership": {
    "role": "member",
    "linked_at": "2026-08-17T04:00:00Z"
  },
  "onboarding": {
    "required": true,
    "missing_steps": ["emergency_address"],
    "next_action": "wait_for_owner",
    "can_edit_emergency_address": false
  }
}
```

- 주소가 없는 owner의 `next_action`은 `register_emergency_address`, `can_edit_emergency_address`는 `true`입니다.
- 주소가 없는 member의 `next_action`은 `wait_for_owner`, `can_edit_emergency_address`는 `false`입니다.
- 주소가 등록되면 `required=false`, `missing_steps=[]`, `next_action=null`이 됩니다.
- 온보딩 완료 여부는 별도 플래그를 저장하지 않고 가구 주소 존재 여부로 계산합니다.

미연동 사용자:

```json
{
  "household_link_status": "unlinked",
  "household": null,
  "membership": null
}
```

### 5.2 초대 코드 조회·재발급

```text
GET  /households/{household_id}/invite-code
POST /households/{household_id}/invite-code/rotate
```

- owner만 사용할 수 있습니다.
- 코드는 6자리 영문 대문자·숫자 조합입니다.
- 초대 코드는 24시간 유효합니다.
- 유효기간 안에는 여러 가족 계정이 같은 코드를 사용할 수 있습니다.
- 재발급하면 기존 코드는 즉시 사용할 수 없게 됩니다.
- 서버에는 원문이 아닌 코드 해시를 저장합니다.

```json
{
  "invite_code": "A7K2M9",
  "expires_at": "2026-08-18T03:00:00Z"
}
```

`코드 공유하기`는 프론트의 OS 공유 기능을 사용하며 별도 백엔드 API가 필요하지 않습니다.

### 5.3 초대 코드로 가구 미리보기

```text
POST /households/link/preview
```

```json
{
  "invite_code": "A7K2M9"
}
```

```json
{
  "linkable": true,
  "household": {
    "name": "홍길동 가구",
    "member_count": 3,
    "created_at": "2026-08-17T03:00:00Z"
  }
}
```

- 가구 주소와 구성원 개인정보는 미리보기에서 반환하지 않습니다.
- 없는 코드, 만료 코드, 형식 오류는 구분 가능한 오류 코드로 반환하되 모두 요청 횟수 제한을 적용합니다.

### 5.4 가구 연동

```text
POST /households/link
```

```json
{
  "invite_code": "A7K2M9"
}
```

성공 응답:

```json
{
  "household_link_status": "linked",
  "household_id": "home-a1b2c3",
  "role": "member",
  "linked_at": "2026-08-17T04:00:00Z"
}
```

이미 가구에 연동된 사용자는 다른 가구에 동시에 연동할 수 없습니다.

### 5.5 가족 구성원 목록

```text
GET /households/{household_id}/members
```

```json
{
  "members": [
    {
      "user_id": "user-owner",
      "profile_name": "홍길동",
      "display_name": "아버지",
      "display_name_is_custom": true,
      "phone_number": "+821012345678",
      "role": "owner",
      "linked_at": "2026-08-17T03:00:00Z",
      "is_me": false,
      "can_edit_display_name": true
    },
    {
      "user_id": "user-member",
      "profile_name": "김가족",
      "display_name": "김가족",
      "display_name_is_custom": false,
      "phone_number": "+821098765432",
      "role": "member",
      "linked_at": "2026-08-17T04:00:00Z",
      "is_me": true,
      "can_edit_display_name": true
    }
  ]
}
```

연동 가족 전화·문자 버튼은 이 응답의 전화번호에 `tel:` 또는 `sms:`를 붙여 프론트에서 실행합니다. 서버가 전화나 문자를 대신 발송하지 않습니다.

`display_name`은 현재 API를 호출한 사용자에게 적용된 값입니다. 별칭이 없으면 `profile_name`과 같은 값을 반환합니다. 같은 구성원을 보더라도 사용자마다 서로 다른 `display_name`을 받을 수 있습니다.

### 5.6 가족 구성원 개인 표시 이름 설정

```text
PATCH  /households/{household_id}/members/{member_user_id}/display-name
DELETE /households/{household_id}/members/{member_user_id}/display-name
```

설정 요청:

```json
{
  "display_name": "우리 엄마"
}
```

- owner와 member 모두 사용할 수 있습니다.
- 대상 구성원의 실제 `profile_name`은 변경하지 않습니다.
- 설정값은 요청한 사용자에게만 보입니다. 다른 가족 계정의 화면에는 영향을 주지 않습니다.
- `DELETE`는 개인 표시 이름을 지우고 가입 시 이름을 다시 표시합니다.
- 저장 키는 `viewer_user_id + member_user_id` 조합으로 분리하여 다른 사용자의 별칭을 덮어쓰지 않도록 합니다.

### 5.7 내 계정의 가족 연동 해제

```text
DELETE /households/current/link
```

- owner 권한을 요구하지 않으며, 현재 로그인한 사용자가 자신의 연동만 해제할 수 있습니다.
- 다른 구성원의 계정·멤버십·표시 이름을 삭제하거나 변경하지 않습니다.
- owner를 포함해 다른 사용자의 연동을 대신 해제하는 API는 제공하지 않습니다.
- 일반 member가 호출하면 해당 계정만 `unlinked` 상태로 돌아가며 가구는 계속 활성 상태를 유지합니다.
- 일반 member의 해제가 성공하면 `household.member_removed` 이벤트를 보내고 본인의 WebSocket을 `1008`로 종료합니다.
- owner가 호출하면 소유권을 다른 구성원에게 이전하지 않고 가구의 `status`를 `inactive`로 변경합니다. 가구에 연결된 모든 계정은 더 이상 해당 가구의 기기·알림 API를 사용할 수 없습니다.
- owner의 해제가 성공하면 `household.inactivated` 이벤트를 보내고 해당 가구의 모든 WebSocket을 `1008`로 종료합니다. 저장소 실패 시에도 member 본인 또는 owner 가구 전체의 연결을 보수적으로 종료하므로, 원래 HTTP 오류를 받은 프론트는 `/me`와 `/households/current`를 재조회합니다.
- owner의 가구 비활성화가 완료되면 `emergency_address`는 즉시 삭제합니다. 가구 자체를 영구 삭제하는 경우에도 별도 주소 레코드를 남기지 않습니다.
- 가구 비활성화는 사용자 계정이나 기존 알림 데이터를 즉시 삭제하는 작업이 아닙니다.
- 프론트는 owner에게 일반 연동 해제와 영향 범위가 다르다는 확인 안내를 표시해야 합니다.

### 5.8 긴급 신고 주소 조회·수정

```text
GET   /households/{household_id}/emergency-address
PATCH /households/{household_id}/emergency-address
```

- 주소는 사용자 프로필이 아닌 가구에 저장합니다.
- 가구에 연동된 owner와 member는 `GET`으로 조회할 수 있습니다.
- `PATCH`는 owner만 사용할 수 있습니다.
- 미연동 사용자와 다른 가구 사용자는 조회할 수 없습니다.
- 초대 코드 미리보기, 구성원 목록, 일반 알림, WebSocket 및 서버 로그에는 주소를 포함하지 않습니다.
- 도로명주소와 우편번호는 원칙적으로 Hearo 백엔드를 경유한 행안부 검색 결과를 사용합니다.
- 행안부에 상세주소가 없거나 서비스 장애가 발생하면 직접 입력할 수 있으며 `verified=false`로 저장합니다.

응답 및 수정 요청 본문:

```json
{
  "postal_code": "41566",
  "road_address": "대구광역시 북구 대학로 80",
  "detail_address": "101동 902호",
  "address_provider": "juso_go_kr",
  "verified": true,
  "updated_at": "2026-08-20T04:00:00Z"
}
```

`PATCH` 요청에는 서버 생성 필드인 `updated_at`과 `verified`를 제외합니다. `verified`는 서버가 행안부 결과를 확인하여 결정하며 클라이언트가 강제로 설정할 수 없습니다. 기존 가구에 주소가 등록되지 않았다면 `GET`은 `404 EMERGENCY_ADDRESS_NOT_FOUND`를 반환하고 owner가 `PATCH`로 최초 등록할 수 있습니다.

행안부 결과로 등록할 때는 도로명 검색 응답의 `provider_reference`, `detail_source`와 선택한 `juso_detail`을 함께 보냅니다. 서버는 저장 전에 도로명과 상세주소를 다시 확인합니다. 상세주소를 직접 입력하면 `detail_source=manual`, 전체 주소를 직접 입력하면 `address_provider=manual`을 사용합니다.

행안부 상세주소 선택 저장 예시:

```json
{
  "postal_code": "41566",
  "road_address": "대구광역시 북구 대학로 80",
  "detail_address": "",
  "address_provider": "juso_go_kr",
  "provider_reference": {
    "adm_cd": "2723011100",
    "road_name_code": "272303145001",
    "underground": "0",
    "building_main_no": 80,
    "building_sub_no": 0,
    "apartment": true
  },
  "detail_source": "juso",
  "juso_detail": {
    "dong_name": "101",
    "floor_name": "9",
    "ho_name": "902"
  }
}
```

`detail_source=juso`이면 서버가 `detail_address`를 행안부 결과로 다시 구성합니다. 따라서 클라이언트가 보낸 `detail_address` 값을 신뢰하지 않습니다.

### 5.9 행안부 도로명주소 검색

```text
POST /households/{household_id}/address-search/roads
```

요청 본문:

```json
{
  "keyword": "대구 북구 대학로 80",
  "page": 1,
  "page_size": 10
}
```

- owner만 호출할 수 있습니다.
- 프론트에 행안부 승인키를 제공하지 않습니다.
- 서버는 도로명주소 검색 API 승인키와 상세주소 API 승인키를 별도 환경변수로 관리합니다.
- 검색어가 Nginx 접근 로그의 URL에 남지 않도록 JSON 본문을 사용하는 `POST`로 제공합니다.
- `provider_reference`는 상세주소 조회와 최종 검증에만 사용하며 DynamoDB에는 저장하지 않습니다.
- `page_size`는 1~20, 검색 요청은 사용자·IP 기준 분당 30회로 제한합니다.

```json
{
  "page": 1,
  "page_size": 10,
  "total_count": 1,
  "items": [
    {
      "postal_code": "41566",
      "road_address": "대구광역시 북구 대학로 80",
      "building_name": "경북대학교",
      "detail_supported": true,
      "provider_reference": {
        "adm_cd": "2723011100",
        "road_name_code": "272303145001",
        "underground": "0",
        "building_main_no": 80,
        "building_sub_no": 0,
        "apartment": true
      }
    }
  ]
}
```

### 5.10 행안부 동·층·호 검색

```text
POST /households/{household_id}/address-search/details
```

동 검색은 `search_type=dong`을 사용합니다. 층·호 검색은 `search_type=floorho`와 앞 단계에서 선택한 `dong_name`을 함께 보냅니다.

```json
{
  "provider_reference": {
    "adm_cd": "2723011100",
    "road_name_code": "272303145001",
    "underground": "0",
    "building_main_no": 80,
    "building_sub_no": 0,
    "apartment": true
  },
  "search_type": "floorho",
  "dong_name": "101"
}
```

응답 예시:

```json
{
  "search_type": "floorho",
  "items": [
    {
      "dong_name": "101",
      "floor_name": "9",
      "ho_name": "902",
      "formatted_detail_address": "101동 9층 902호"
    }
  ],
  "manual_input_allowed": true
}
```

행안부 장애·시간 초과는 `503 ADDRESS_PROVIDER_UNAVAILABLE`, 각 서비스의 승인키 설정 오류는 `503 ADDRESS_PROVIDER_CONFIGURATION_ERROR`, 검색 조건 오류는 `400 INVALID_ADDRESS_SEARCH`로 반환합니다. 상세주소 결과가 없어도 정상 응답의 `items=[]`, `manual_input_allowed=true`를 반환할 수 있습니다.

## 6. 기기 API

고정 기기:

| 기기 ID | 위치 |
|---|---|
| `rpi-001` | 거실 |
| `esp32_1` | 안방 |
| `esp32_2` | 현관 |
| `esp32_3` | 화장실 |

### 6.1 기기 목록

```text
GET /households/{household_id}/devices
```

```json
{
  "devices": [
    {
      "device_id": "esp32_2",
      "location": "현관",
      "device_type": "alert_node",
      "led_alert_control_supported": true,
      "desired_mqtt_connected": true,
      "reported_mqtt_connected": true,
      "network_status": "online",
      "ui_status": "connected",
      "last_seen_at": "2026-08-17T05:00:00Z",
      "led_alert_enabled": true,
      "audio_streaming": true,
      "microphone_ok": true,
      "config_version": 7
    }
  ]
}
```

기기 상태:

| 상태 | 의미 |
|---|---|
| `connected` | MQTT 연결 정상 |
| `disabled_by_owner` | owner가 MQTT 연결을 끔 |
| `pending` | 최신 설정 반영 대기 |
| `offline` | 네트워크 offline 또는 heartbeat 45초 초과 |
| `error` | 요청 후 제한 시간 안에 최신 설정 미반영 |

### 6.2 MQTT 연결 ON/OFF

```text
PATCH /households/{household_id}/devices/{device_id}/connection
```

```json
{
  "enabled": false
}
```

- owner만 변경할 수 있습니다.
- 이 스위치는 전원이 아니라 MQTT 연결을 제어합니다.
- OFF 상태에서도 기기 전원과 Wi-Fi는 유지됩니다.
- Raspberry Pi는 MQTT OFF 중에도 AI 추론을 계속합니다.

### 6.3 LED 알림 ON/OFF

```text
PATCH /households/{household_id}/devices/{device_id}/settings
```

```json
{
  "led_alert_enabled": false
}
```

- owner만 변경할 수 있습니다.
- `esp32_1`, `esp32_2`, `esp32_3`에서만 지원합니다.
- Raspberry Pi(`rpi-001`) 요청은 `409 DEVICE_LED_CONTROL_UNSUPPORTED`를 반환합니다.
- 프론트는 `led_alert_control_supported=true`인 기기에만 LED 스위치를 표시합니다.
- 진동과 민감도 필드는 요청·응답에 포함하지 않습니다.

## 7. 알림 API

모든 사용자용 알림 조회는 알림 발생 시각부터 `90 × 24시간`이 지나기 전인 데이터만 반환합니다. 정확히 90일이 되는 순간부터 일반 목록·최근 알림·최근 7일 이력·미확인 계산·상세 조회에서 제외하며 상세 조회는 `404 ALARM_NOT_FOUND`가 됩니다. 이 조회 차단은 DynamoDB TTL의 비동기 물리 삭제 여부와 관계없이 적용됩니다.

최근 7일은 화면 이력과 미확인 계산의 범위이고, 90일은 일반 목록·최근 알림·상세 조회에도 적용되는 최대 조회·보관 기한입니다.

### 7.1 일반 알림 목록

```text
GET /households/{household_id}/alarms?limit=100
```

90일 보관 기한 안의 최신순 알림 목록과 실제 반환 개수인 `count`를 반환합니다. 현재 AlertHistoryPage는 날짜 묶음이 포함된 7.3절의 최근 7일 API를 사용하고, 이 API는 일반 목록이나 운영 확인이 필요할 때만 사용합니다.

### 7.2 최근 알림

```text
GET /households/{household_id}/alarms/latest
```

90일 보관 기한 안의 가장 최근 알림을 반환합니다. 해당 알림이 없으면 `{"alarm": null}`을 반환합니다.

### 7.3 최근 7일 날짜별 알림

```text
GET /households/{household_id}/alarms/history
```

- `Asia/Seoul` 기준 오늘을 포함한 최근 7개 달력 날짜입니다.
- 알림이 없는 날짜도 `alarms: []`로 포함합니다.
- 날짜는 최신순, 날짜 안 알림도 최신순입니다.
- 페이지네이션 없이 7일 범위를 한 번에 반환합니다.
- 7일 이전 알림은 이 날짜별 응답에 포함되지 않습니다. 다만 발생 후 90일이 되기 전에는 일반 목록이나 상세 API에서 조회할 수 있습니다.

```json
{
  "timezone": "Asia/Seoul",
  "start_date": "2026-08-11",
  "end_date": "2026-08-17",
  "total_count": 2,
  "days": [
    {
      "date": "2026-08-17",
      "display_label": "오늘",
      "alarms": [
        {
          "id": "alert-001",
          "time": "2026-08-17T00:15:00Z",
          "local_time": "2026-08-17T09:15:00+09:00",
          "location": "현관",
          "source_device_id": "esp32_2",
          "sound": "도어락소리",
          "raw_label": "도어락_개방음",
          "type": "Visitor",
          "confidence": 0.91
        }
      ]
    },
    {
      "date": "2026-08-16",
      "display_label": "어제",
      "alarms": []
    },
    {
      "date": "2026-08-15",
      "display_label": null,
      "alarms": []
    },
    {
      "date": "2026-08-14",
      "display_label": null,
      "alarms": []
    },
    {
      "date": "2026-08-13",
      "display_label": null,
      "alarms": []
    },
    {
      "date": "2026-08-12",
      "display_label": null,
      "alarms": []
    },
    {
      "date": "2026-08-11",
      "display_label": null,
      "alarms": []
    }
  ]
}
```

알림 객체에는 `audio_url`, 녹음 재생 정보, 세탁 완료 이벤트를 포함하지 않습니다. `112 전화·문자`와 가족 연락은 프론트가 `tel:`·`sms:`로 실행합니다.

`알림 오류`와 `이상 없음` 피드백 저장 기능은 요구사항이 확정되지 않아 이번 명세에서 제외합니다. 버튼을 유지하려면 별도 피드백 API 합의가 필요합니다.

### 7.4 미확인 알림 개수

```text
GET /households/{household_id}/alarms/unread-count
Authorization: Bearer {access_token}
```

- 해당 가구에 연동된 현재 로그인 사용자의 상태만 조회합니다.
- `Asia/Seoul` 기준 오늘을 포함한 최근 7개 날짜 안에서 계산합니다.
- `alarm.time > last_seen_at`인 알림만 미확인으로 계산합니다.
- 미래 시각의 알림은 계산하지 않습니다.
- owner와 member는 서로 독립된 `last_seen_at`을 사용합니다.
- 개별 알림에 `read` 필드를 추가하지 않습니다.

```json
{
  "unread_count": 3,
  "last_seen_at": "2026-08-30T03:10:00Z",
  "window_days": 7
}
```

신규 owner는 가구 생성 시각, 신규 member는 가구 연동 시각을 최초 확인 시각으로 사용합니다. `alarms_last_seen_at`이 없는 기존 구성원은 운영 환경변수 `HEARO_ALARM_UNREAD_BASELINE_AT`의 고정 배포 시각으로 최초 값을 초기화합니다.

### 7.5 알림 모두 확인

```text
PATCH /households/{household_id}/alarms/seen
Authorization: Bearer {access_token}
```

요청 본문은 없습니다. 클라이언트 시각이나 사용자 ID를 받지 않고, 인증된 사용자의 확인 시각을 서버 UTC 현재 시각으로 갱신합니다. 같은 가구의 다른 사용자 확인 상태에는 영향을 주지 않습니다.

```json
{
  "unread_count": 0,
  "last_seen_at": "2026-08-30T03:15:20Z"
}
```

프론트는 최근 7일 이력 조회가 성공한 뒤 이 API를 호출하고, 성공 응답을 받은 경우 배너 개수를 0으로 변경합니다. 이 요청의 서버 처리 시점까지 생성된 알림은 확인한 것으로 간주합니다.

### 7.6 특정 알림 상세 조회

```text
GET /households/{household_id}/alarms/{alarm_id}
```

- 해당 가구에 연동된 owner와 member만 조회할 수 있습니다.
- 다른 가구 사용자는 `403`, 존재하지 않는 알림은 `404`를 반환합니다.
- `alarm_id`는 최근 7일 목록 또는 일반 목록의 알림 객체에 포함된 `id`를 사용합니다. 과거 알림 ID를 알고 있어도 발생 후 90일이 지난 알림은 조회할 수 없습니다.
- 프론트는 응답의 `local_time`을 한국어 오전·오후와 시각으로 표시합니다.

```json
{
  "alarm": {
    "id": "alert-001",
    "date": "2026-07-08",
    "local_time": "2026-07-08T08:47:00+09:00",
    "location": "안방",
    "sound": "비상벨소리",
    "raw_label": "사이렌_삐뽀삐뽀",
    "type": "Urgent"
  }
}
```

상세 응답의 `sound`는 사용자에게 보여줄 상위 소리 이름이고, `raw_label`은 AI가 분류한 세부 레이블입니다. 과거 데이터처럼 세부 레이블이 저장되지 않은 알림도 응답 키는 유지하며 `raw_label: null`을 반환합니다. `source_device_id`, `confidence` 및 AI 진단 필드는 사용자용 상세 API에 노출하지 않습니다.

## 8. 보호자 연락처 API

```text
GET    /households/{household_id}/contacts
POST   /households/{household_id}/contacts
PATCH  /households/{household_id}/contacts/{contact_id}
DELETE /households/{household_id}/contacts/{contact_id}
```

- 가구에 연동된 owner와 member는 연락처를 조회할 수 있습니다.
- 등록·수정·삭제는 owner만 가능합니다.
- `POST`는 `201`, `DELETE`는 응답 본문 없는 `204`를 반환합니다.
- 전화번호는 서버가 E.164 형식으로 정규화하며 프론트는 `tel:` 또는 `sms:` 링크에 사용합니다.

등록·수정 요청:

```json
{
  "name": "김보호자",
  "relationship": "자녀",
  "phone_number": "010-1234-5678"
}
```

목록 응답:

```json
{
  "contacts": [
    {
      "contact_id": "contact-a1b2",
      "name": "김보호자",
      "relationship": "자녀",
      "phone_number": "+821012345678",
      "created_at": "2026-08-22T03:00:00Z"
    }
  ]
}
```

## 9. WebSocket

```text
WS /ws/households/{household_id}
```

연결 후 첫 메시지:

```json
{
  "type": "auth",
  "access_token": "..."
}
```

기기 상태 변경:

```json
{
  "type": "device.status_changed",
  "device_id": "esp32_2",
  "ui_status": "disabled_by_owner",
  "last_seen_at": "2026-08-17T05:00:03Z",
  "config_version": 8
}
```

새 알림:

```json
{
  "type": "alarm.created",
  "alarm": {
    "id": "alert-001",
    "sound": "도어락소리",
    "raw_label": "도어락_개방음",
    "type": "Visitor",
    "location": "현관",
    "time": "2026-08-17T00:15:00Z"
  }
}
```

바깥쪽 `type: "alarm.created"`는 WebSocket 이벤트 종류이고, `alarm.type`은 `Urgent`, `Visitor`, `Noise`와 같은 알림 분류입니다. `raw_label`은 알림 상세 API와 동일한 AI 세부 레이블이며 값이 없는 과거·호환 알림에서는 `null`일 수 있습니다.

새 알림 필드의 프론트 활용:

| 필드 | 프론트 활용 의도 |
|---|---|
| `type` | WebSocket 메시지가 신규 알림임을 판별합니다. 값은 항상 `alarm.created`입니다. |
| `alarm.id` | 알림 카드를 식별하고 상세 화면의 `alarm_id` 경로값으로 사용합니다. |
| `alarm.sound` | 사용자가 이해하기 쉬운 상위 소리 이름으로 카드 제목에 표시합니다. |
| `alarm.raw_label` | AI가 실제로 판단한 세부 소리 종류를 상세 문구에 사용합니다. 값이 없으면 `null`입니다. |
| `alarm.type` | `Urgent`, `Visitor`, `Noise`에 따라 배지·색상·긴급 UI를 결정합니다. |
| `alarm.location` | 소리가 감지된 방이나 위치를 표시합니다. |
| `alarm.time` | UTC 발생 시각입니다. 한국시간으로 변환해 목록 시각을 표시하거나 최신순으로 정렬합니다. |

WebSocket의 나머지 메시지와 모든 REST 필드의 활용 의도는 다음 10절을 따릅니다.

프론트는 60초보다 짧은 간격으로 `ping`을 보내고 `pong`을 받아 연결을 유지합니다. access token 갱신 후 WebSocket도 새 token으로 다시 연결합니다.

연동 해제·회원 탈퇴·비밀번호 변경 또는 인증 거부로 종료 코드 `1008`을 받으면 이전 token으로 무조건 재연결하지 않습니다. `/me`와 `/households/current`로 계정·연동 상태를 확인하고, 인증이 무효이면 재로그인한 뒤 활성 가구에 연결합니다. 서버는 연결 등록 전후에 사용자·가구 권한을 재검증합니다.

## 10. API 필드별 프론트 활용 가이드

이 절은 각 필드가 단순히 무엇을 담는지를 넘어, 프론트에서 **표시·화면 분기·다음 API 호출 중 어디에 사용하는 값인지** 설명합니다.

- `household.name`처럼 점으로 표시한 이름은 중첩 JSON 경로입니다.
- ID 필드는 보통 화면에 그대로 표시하지 않고 목록 key나 다음 API의 경로값으로 사용합니다.
- `null`이 허용된 필드는 프론트 타입에도 `null`을 포함하고 대체 문구나 분기 처리를 둡니다.
- UTC 시각은 정렬·비교에 사용하고, 사용자 화면에는 `Asia/Seoul` 기준으로 변환해 표시합니다.
- 같은 의미의 필드라도 API별 포함 여부가 다를 수 있으므로 실제 응답 예시와 필드 제공 범위를 함께 확인합니다.

### 10.1 공통 경로·오류 필드

| 필드 | 프론트 활용 의도 |
|---|---|
| `{household_id}` | `GET /households/current`에서 받은 현재 가구 ID를 가구 API 경로에 넣습니다. 사용자에게 직접 표시할 값은 아닙니다. |
| `{device_id}` | 기기 목록에서 받은 값을 연결·LED 설정 API 경로에 넣고 React/Vue 목록 key로 사용할 수 있습니다. |
| `{alarm_id}` | 알림 목록의 `id`를 상세 조회 경로에 넣습니다. |
| `{member_user_id}` | 가족 목록의 `user_id`를 개인 표시 이름 API 경로에 넣습니다. |
| `{contact_id}` | 연락처 목록의 `contact_id`를 수정·삭제 API 경로에 넣습니다. |
| `code` | 오류 종류를 판별해 중복 아이디, 권한 없음, 재로그인 같은 화면 동작을 결정합니다. |
| `message` | 알림창·토스트에 표시할 사용자용 오류 문구입니다. |
| `field_errors` | 키와 같은 이름의 입력칸 아래에 검증 오류를 표시합니다. 빈 객체일 수 있습니다. |
| `request_id` | 사용자가 오류를 제보할 때 서버 로그를 찾기 위한 식별자입니다. 일반 화면에는 숨기고 오류 상세나 복사 기능에서만 사용합니다. |

### 10.2 인증·회원 API

#### `POST /auth/signup` 요청

| 필드 | 프론트 활용 의도 |
|---|---|
| `login_id` | 사용자가 이후 로그인할 별도 아이디입니다. 휴대폰 번호와 다른 입력칸으로 받습니다. |
| `name` | 실제 회원 이름입니다. 내 정보와 가족 목록의 원래 이름으로 사용합니다. 개인 별칭 변경 대상이 아닙니다. |
| `phone_number` | 본인 연락처 입력값입니다. 서버가 E.164로 정규화하므로 응답값은 입력 형식과 달라질 수 있습니다. |
| `password` | 계정 인증에만 사용하며 화면 상태·로그·분석 도구에 남기지 않습니다. |
| `signup_type` | `new_household`이면 owner 가입, `family_member`이면 미연동 가족 가입 흐름으로 분기합니다. |
| `household_name` | 신규 가구의 화면 표시 이름입니다. `new_household`일 때만 전송합니다. |
| `emergency_address` | 기존 가입 화면 호환용 선택 필드입니다. 현재 권장 흐름은 가입 후 주소 온보딩이므로 가입 요청에서 생략할 수 있습니다. |
| `terms_service_agreed` | 서비스 이용약관 동의 체크 결과이며 `true`만 허용됩니다. |
| `privacy_agreed` | 개인정보 수집·이용 동의 체크 결과이며 `true`만 허용됩니다. |
| `terms_version` | 선택한 약관 버전입니다. `GET /legal/policies`의 `terms.version`을 그대로 사용합니다. 기존 프론트 호환 모드에서는 생략할 수 있습니다. |
| `privacy_version` | 선택한 개인정보 처리방침 버전입니다. `GET /legal/policies`의 `privacy.version`을 그대로 사용합니다. 기존 프론트 호환 모드에서는 생략할 수 있습니다. |

#### 회원가입·로그인·`GET /me`의 사용자 필드

`POST /auth/signup`과 `POST /auth/login`은 `user`와 `tokens`를 반환하고, `GET /me`는 아래 `user` 내부 필드와 같은 사용자 객체를 직접 반환합니다.

| 필드 | 프론트 활용 의도 |
|---|---|
| `user.user_id` / `user_id` | 로그인 사용자를 식별하는 내부 ID입니다. 목록 비교와 상태 저장에 쓰며 보통 화면에 표시하지 않습니다. |
| `user.login_id` / `login_id` | 설정의 계정 정보에 로그인 아이디를 표시할 때 사용합니다. |
| `user.name` / `name` | 본인의 실제 프로필 이름을 표시합니다. |
| `user.phone_number` / `phone_number` | 정규화된 본인 전화번호를 표시합니다. |
| `user.account_type` / `account_type` | `household_owner`와 `family_member`에 따라 가입 계정 유형 안내를 구분합니다. 실제 가구 제어 권한은 `role`로 판단합니다. |
| `user.household_id` / `household_id` | 연동된 가구 API 경로에 사용합니다. 미연동이면 `null`입니다. |
| `user.role` / `role` | `owner`이면 설정 버튼을 제공하고, `member`이면 조회 중심 UI를 제공합니다. 미연동이면 `null`입니다. 백엔드의 `403` 처리도 반드시 유지합니다. |
| `user.household_link_status` / `household_link_status` | `linked`이면 메인 화면, `unlinked`이면 초대 코드 입력 화면으로 이동시키는 기준입니다. |
| `user.terms_version` / `terms_version` | 사용자가 마지막으로 명시적으로 동의한 약관 버전입니다. 확인되지 않은 기존 동의는 `null`입니다. |
| `user.privacy_version` / `privacy_version` | 사용자가 마지막으로 명시적으로 동의한 개인정보 처리방침 버전입니다. 확인되지 않은 기존 동의는 `null`입니다. |
| `user.consented_at` / `consented_at` | 마지막 동의 처리 시각입니다. 현재 정책 충족 여부는 이 값만으로 판단하지 않고 `GET /me/consents`를 사용합니다. |
| `user.created_at` / `created_at` | 계정 생성일을 설정 화면 등에 표시할 때 사용합니다. |
| `tokens.access_token` | 일반 API의 `Authorization: Bearer ...` 헤더와 WebSocket 최초 인증 메시지에 사용합니다. |
| `tokens.refresh_token` | access token 갱신과 로그아웃 요청에 사용합니다. 화면에 표시하거나 로그에 남기지 않습니다. |
| `tokens.token_type` | 현재 값은 `bearer`이며 Authorization 헤더 형식을 구성할 때 사용합니다. |
| `tokens.expires_in` | access token 만료까지 남은 초 단위 시간입니다. 만료 직전 갱신 시점을 계산하는 데 사용합니다. |
| `device_credentials` | 신규 owner 가입 때 기기별 credential이 한 번만 반환됩니다. 일반 사용자 화면·브라우저 영구 저장소에는 보관하지 말고 설치 담당 전달 절차에서만 사용합니다. |

#### 로그인·토큰·비밀번호 요청

| API와 필드 | 프론트 활용 의도 |
|---|---|
| 로그인 `login_id` | 로그인 아이디 입력칸의 값입니다. |
| 로그인 `password` | 로그인 비밀번호 입력칸의 값입니다. |
| refresh `refresh_token` | access token이 만료되기 전 또는 `401` 처리 시 새 토큰 쌍을 발급받는 데 사용합니다. 응답은 `tokens`로 감싸지 않고 `access_token`, `refresh_token`, `token_type`, `expires_in`을 최상위에 반환합니다. |
| logout `refresh_token` | 서버에서 해당 refresh token을 폐기합니다. 성공 응답은 본문 없는 `204`이므로 프론트도 저장한 토큰을 삭제하고 로그인 화면으로 이동합니다. |
| 비밀번호 변경 `current_password` | 현재 비밀번호 확인용 입력값입니다. |
| 비밀번호 변경 `new_password` | 새 비밀번호 입력값입니다. 성공 응답은 `204`이며 모든 기존 토큰이 무효화되므로 재로그인합니다. |

#### 약관 정책과 본인 동의 필드

| 필드 | 프론트 활용 의도 |
|---|---|
| 정책 `configured` | `false`이면 버전 문자열을 만들어 보내지 않고 기존 호환 가입 흐름을 유지합니다. |
| 정책 `effective` | `false`이면 해당 버전의 가입 동의·재동의를 시도하지 않습니다. |
| 정책 `required` | 시행 후 신규 가입에서 현재 버전 제출이 필수인지 판단합니다. 기존 로그인이나 메인 기능 차단값으로 사용하지 않습니다. |
| 정책 `effective_at` | 시행 예정 안내에 사용할 수 있는 시간대 포함 시각입니다. 미설정이면 `null`입니다. |
| 정책 `terms.version`, `privacy.version` | 가입·재동의 요청에 그대로 복사합니다. |
| 정책 `terms.url`, `privacy.url` | 약관과 개인정보 처리방침 원문을 여는 데 사용합니다. |
| 동의 `status` | `policy_not_configured`, `policy_not_effective`, `legacy_unversioned`, `outdated`, `current` 중 하나로 화면 상태를 결정합니다. |
| 동의 `consent_required` | 현재 정책이 강제 상태이고 사용자가 아직 `current`가 아니면 재동의 안내를 표시합니다. |
| 동의 `can_consent` | 현재 서버가 재동의 요청을 받을 수 있는 상태인지 판단합니다. |
| 동의 `consented_at` | 저장된 동의 시각입니다. 버전·동의값과 함께 해석하며 단독으로 현재 동의를 뜻하지 않습니다. |
| 동의 `terms`, `privacy` | 각 문서의 `agreed`, `accepted_version`, `current_version`, `is_current`를 이용해 무엇이 최신이 아닌지 표시합니다. |

### 10.3 가구·가족 연동 API

#### `GET /households/current`

| 필드 | 프론트 활용 의도 |
|---|---|
| `household_link_status` | `linked`와 `unlinked`에 따라 가구 화면 또는 연동 안내 화면으로 분기합니다. |
| `household` | 미연동이면 `null`입니다. `null` 여부를 확인한 뒤 내부 필드에 접근합니다. |
| `household.household_id` | 이후 가구·기기·알림 API 경로에 사용합니다. |
| `household.name` | 설정과 가구 정보 화면의 가구 이름으로 표시합니다. |
| `household.member_count` | 현재 연동된 가족 수를 표시합니다. |
| `household.created_at` | 가구 등록일 표시용 UTC 시각입니다. |
| `membership` | 미연동이면 `null`이며 현재 사용자의 가구 소속 정보를 담습니다. |
| `membership.role` | 현재 가구에서의 `owner` 또는 `member` 권한 UI를 결정합니다. |
| `membership.linked_at` | 이 가구에 연동된 날짜를 표시할 때 사용합니다. |
| `onboarding.required` | `true`이면 일반 메인 기능보다 주소 온보딩 안내를 먼저 보여주는 기준입니다. |
| `onboarding.missing_steps` | 현재 누락된 단계 목록입니다. 지금은 `emergency_address`가 들어갈 수 있습니다. |
| `onboarding.next_action` | `register_emergency_address`이면 owner 주소 입력 화면, `wait_for_owner`이면 member 대기 안내로 이동합니다. 완료되면 `null`입니다. |
| `onboarding.can_edit_emergency_address` | 주소 입력·수정 버튼 제공 여부를 결정합니다. 최종 권한 검사는 서버가 다시 수행합니다. |

#### 초대·연동·가족 목록

| API와 필드 | 프론트 활용 의도 |
|---|---|
| 초대 코드 `invite_code` | owner의 6자리 공유 코드로 표시하고 OS 공유 기능에 전달합니다. |
| 초대 코드 `expires_at` | 만료 시각과 남은 시간을 표시하고 만료 후 재발급을 안내합니다. |
| 미리보기·연동 요청 `invite_code` | 사용자가 입력한 6자리 코드를 대문자로 정규화해 전송합니다. 미리보기 성공 후 같은 코드를 실제 연동 요청에 사용합니다. |
| 미리보기 `linkable` | `true`일 때만 최종 `연동하기` 버튼을 활성화합니다. |
| 미리보기 `household.name` | 사용자가 올바른 가구인지 확인하도록 표시합니다. |
| 미리보기 `household.member_count` | 연동 전 가구원 수 확인에 사용합니다. |
| 미리보기 `household.created_at` | 가구 등록일 확인에 사용합니다. |
| 연동 `household_link_status` | 성공 후 `linked`인지 확인하고 가구 화면으로 이동합니다. |
| 연동 `household_id` | 이후 가구 API 경로에 저장해 사용합니다. |
| 연동 `role` | 연동 사용자의 화면 권한을 설정합니다. 현재 가족 참여는 `member`입니다. |
| 연동 `linked_at` | 연동 완료 시각이며 필요하면 설정 화면에 표시합니다. |
| 가족 목록 `members` | 구성원 카드 배열입니다. 빈 배열이면 구성원 없음 화면을 표시합니다. |
| `members[].user_id` | 구성원 카드 key와 표시 이름 변경 API의 `member_user_id`에 사용합니다. |
| `members[].profile_name` | 가입할 때 저장된 실제 이름입니다. 별칭 초기화 시 돌아갈 이름입니다. |
| `members[].display_name` | 현재 로그인한 사용자에게 보여줄 구성원 이름입니다. 가족 카드에는 이 값을 우선 표시합니다. |
| `members[].display_name_is_custom` | 별칭 적용 여부를 표시하거나 `이름 초기화` 버튼 노출에 사용합니다. |
| `members[].phone_number` | 전화·문자 버튼의 `tel:`·`sms:` 대상입니다. 화면 표시 시 국가번호 형식을 사용자 친화적으로 변환할 수 있습니다. |
| `members[].role` | 가구 관리자 배지 표시 등에 사용합니다. |
| `members[].linked_at` | 구성원 연동일 표시용입니다. |
| `members[].is_me` | 현재 로그인한 사용자의 카드에 `나` 표시를 붙이는 기준입니다. |
| `members[].can_edit_display_name` | 해당 카드의 개인 표시 이름 편집 버튼을 제공할지 판단합니다. |

#### 표시 이름·연동 해제 응답

| API와 필드 | 프론트 활용 의도 |
|---|---|
| 표시 이름 `member_user_id` | 수정된 구성원을 로컬 목록에서 찾아 즉시 갱신합니다. |
| 표시 이름 요청 `display_name` | 현재 사용자에게만 보일 구성원 별칭 입력값입니다. 실제 회원 이름은 바뀌지 않습니다. |
| 표시 이름 `display_name` | 저장 또는 초기화 후 카드에 바로 반영할 최종 이름입니다. |
| 표시 이름 `display_name_is_custom` | `PATCH` 후 `true`, `DELETE` 후 `false`로 별칭 상태를 갱신합니다. |
| 연동 해제 `household_link_status` | 성공 후 현재 계정을 미연동 화면으로 이동시키는 기준입니다. |
| 연동 해제 `household_status` | `active`이면 member 본인만 해제된 것이고, `inactive`이면 owner 해제로 가구 전체가 비활성화된 것입니다. |

### 10.4 긴급 주소·행안부 검색 API

#### 긴급 주소 조회·수정

| 필드 | 프론트 활용 의도 |
|---|---|
| `postal_code` | 주소 카드와 119 문자 템플릿의 우편번호로 사용합니다. |
| `road_address` | 검색 결과에서 선택한 기본 도로명주소이며 주소 카드와 신고 템플릿에 사용합니다. |
| `detail_address` | 동·층·호 등 실제 출동 위치를 구체화하는 값입니다. 기본 주소 뒤에 붙여 표시합니다. |
| `address_provider` | `juso_go_kr`은 행안부 검색, `manual`은 직접 입력임을 구분합니다. 기존 데이터에서는 `kakao_postcode`도 올 수 있습니다. 일반 사용자에게 내부 값 그대로 표시할 필요는 없습니다. |
| `verified` | 서버가 행안부 결과와 일치 여부를 확인한 값입니다. `false`이면 주소 재확인 안내를 표시할 수 있으며 프론트가 임의로 전송할 수 없습니다. |
| `updated_at` | 주소가 마지막으로 변경된 시각입니다. 설정 화면의 최근 수정일이나 캐시 갱신 기준으로 사용합니다. |
| `detail_source` | 수정 요청에서 `juso`, `manual`, `none` 중 상세주소가 어디서 왔는지 서버에 알립니다. 응답 저장 필드는 아닙니다. |
| `juso_detail` | 사용자가 행안부 결과에서 선택한 동·층·호입니다. 서버 재검증용이며 최종 화면은 응답의 `detail_address`를 표시합니다. |

#### 도로명주소 검색

| 필드 | 프론트 활용 의도 |
|---|---|
| 요청 `keyword` | 사용자가 입력한 주소 검색어입니다. |
| 요청·응답 `page` | 현재 검색 결과 페이지입니다. 더보기·페이지 이동 상태에 사용합니다. |
| 요청·응답 `page_size` | 한 번에 요청·표시할 결과 개수입니다. |
| `total_count` | 전체 검색 결과 수와 다음 페이지 존재 여부를 계산하는 데 사용합니다. |
| `items` | 주소 선택 카드 배열입니다. 빈 배열이면 검색 결과 없음 화면을 표시합니다. |
| `items[].postal_code` | 선택한 주소의 우편번호로 수정 API에 다시 전달합니다. |
| `items[].road_address` | 사용자에게 표시하고 선택 시 수정 API에 다시 전달할 도로명주소입니다. |
| `items[].building_name` | 건물명 보조 문구입니다. 값이 없으면 `null`일 수 있습니다. |
| `items[].detail_supported` | `true`이면 동·층·호 검색 단계를 제공하고, `false`이면 직접 입력 또는 상세주소 없음 흐름으로 이동합니다. |
| `items[].provider_reference` | 선택 주소의 행안부 참조값입니다. 화면에 표시하지 않고 상세 검색과 최종 주소 저장 요청에 그대로 전달합니다. 영구 저장할 프론트 데이터는 아닙니다. |
| `provider_reference.adm_cd` | 행정구역 코드로, 상세 검색·검증 요청에 그대로 전달합니다. |
| `provider_reference.road_name_code` | 도로명 코드로, 상세 검색·검증 요청에 그대로 전달합니다. |
| `provider_reference.underground` | 지상·지하 구분값으로, 상세 검색·검증 요청에 그대로 전달합니다. |
| `provider_reference.building_main_no` | 건물 본번으로, 상세 검색·검증 요청에 그대로 전달합니다. |
| `provider_reference.building_sub_no` | 건물 부번으로, 상세 검색·검증 요청에 그대로 전달합니다. |
| `provider_reference.apartment` | 공동주택 여부입니다. 상세주소 단계 필요 여부를 보조 판단하는 데 사용합니다. |

#### 동·층·호 검색

| 필드 | 프론트 활용 의도 |
|---|---|
| 요청·응답 `search_type` | `dong`은 동 선택, `floorho`는 선택한 동의 층·호 검색 단계임을 뜻합니다. |
| 요청 `dong_name` | `floorho` 검색 시 앞 단계에서 선택한 동 이름입니다. |
| `items[].dong_name` | 동 선택 목록 또는 최종 상세주소 구성에 사용합니다. 없으면 `null`일 수 있습니다. |
| `items[].floor_name` | 층 선택·표시에 사용하며 없으면 `null`일 수 있습니다. |
| `items[].ho_name` | 호 선택·표시에 사용하며 없으면 `null`일 수 있습니다. |
| `items[].formatted_detail_address` | `101동 9층 902호`처럼 사용자에게 보여줄 조합 문구입니다. 최종 저장은 선택 원본인 `juso_detail`을 전송해 서버가 다시 구성합니다. |
| `manual_input_allowed` | `true`이면 결과가 없거나 원하는 상세주소가 없을 때 직접 입력 버튼을 제공합니다. |

### 10.5 기기 API

`GET /devices`와 연결·LED 설정의 성공 응답은 같은 기기 상태 구조를 사용합니다.

| 필드 | 프론트 활용 의도 |
|---|---|
| `devices` | 고정 4개 기기 카드 배열입니다. |
| `device_id` | 카드 key와 연결·설정 API 경로에 사용합니다. |
| `location` | `거실`, `안방`, `현관`, `화장실`처럼 사용자에게 기기 위치를 표시합니다. |
| `device_type` | `hub`는 Raspberry Pi, `alert_node`는 ESP32 알림 노드입니다. |
| `led_alert_control_supported` | `true`인 ESP32 카드에만 LED 알림 스위치를 표시합니다. |
| `desired_mqtt_connected` | owner가 마지막으로 요청한 MQTT 연결 상태입니다. 스위치 목표값으로 사용합니다. |
| `reported_mqtt_connected` | 기기가 실제로 보고한 MQTT 연결 상태입니다. 목표값과 다르면 아직 반영 중일 수 있습니다. |
| `network_status` | `online` 또는 `offline` 네트워크 상태 표시에 사용합니다. |
| `ui_status` | 카드의 최종 상태 배지와 스위치 처리 상태를 결정합니다. `connected`, `disabled_by_owner`, `pending`, `offline`, `error` 중 하나입니다. |
| `last_seen_at` | 마지막 heartbeat 시각입니다. `마지막 연결` 문구나 진단 화면에 사용하며 값이 없으면 `null`일 수 있습니다. |
| `led_alert_enabled` | LED 알림 스위치의 현재 목표값입니다. |
| `audio_streaming` | ESP32가 Pi로 오디오를 보내고 있는지 진단 상태에 사용합니다. |
| `microphone_ok` | 기기가 마이크 정상 상태를 보고했는지 진단 표시에 사용합니다. |
| `config_version` | 설정 변경 순서를 식별합니다. 오래된 WebSocket 이벤트가 더 최신 화면 상태를 덮어쓰지 않도록 비교할 수 있으며 일반 사용자에게 표시할 필요는 없습니다. |
| 연결 요청 `enabled` | MQTT 연결 스위치의 새 목표값입니다. 물리 전원 제어값이 아닙니다. |
| 설정 요청 `led_alert_enabled` | LED 알림 스위치의 새 목표값입니다. |

### 10.6 알림 API

#### 공통 알림 객체

| 필드 | 프론트 활용 의도 |
|---|---|
| `id` | 알림 카드 key와 상세 API의 `alarm_id`로 사용합니다. |
| `time` | UTC 기준 실제 발생 시각입니다. 정렬·비교와 WebSocket 신규 알림 시간에 사용합니다. |
| `local_time` | 한국시간이 포함된 표시용 시각입니다. 목록에서 오전·오후와 시각을 보여줄 때 우선 사용합니다. |
| `date` | 상세 화면의 한국시간 기준 날짜 표시용입니다. |
| `location` | 감지 기기의 설치 위치를 알림 카드와 상세 화면에 표시합니다. |
| `source_device_id` | 실제 소리를 수집한 기기 ID입니다. 기기 정보와 연결하거나 진단할 때 사용하며 일반 화면에는 `location`을 우선 표시합니다. |
| `sound` | 사용자 친화적인 상위 분류입니다. 가능한 값은 `비상벨소리`, `도어락소리`, `노크소리`, `아기울음소리`이며 카드 제목에 사용합니다. |
| `raw_label` | AI의 세부 분류 근거입니다. 예를 들어 `도어락_개방음`을 상세 설명에 사용합니다. 과거 알림은 `null`일 수 있습니다. |
| `type` | 알림 UI 그룹입니다. `Urgent`는 긴급, `Visitor`는 방문·출입, `Noise`는 일반 생활 알림 배지와 색상에 사용합니다. |
| `confidence` | 0~1 범위의 분류 신뢰도입니다. 필요하면 퍼센트로 표시할 수 있으나 정확성 보장값으로 해석하면 안 됩니다. 값이 없으면 `null`입니다. |

#### API별 감싸기 필드

알림 객체의 필드 제공 범위:

| API | 포함되는 알림 필드 |
|---|---|
| 일반 목록·최근 알림 | `id`, `time`, `location`, `source_device_id`, `sound`, `raw_label`, `type`, `confidence` |
| 최근 7일 이력 | 일반 목록 필드 + `local_time` |
| 미확인 개수 | 알림 객체 없이 `unread_count`, `last_seen_at`, `window_days` |
| 모두 확인 | 알림 객체 없이 `unread_count`, `last_seen_at` |
| 상세 조회 | `id`, `date`, `local_time`, `location`, `sound`, `raw_label`, `type` |
| WebSocket `alarm.created` | `id`, `sound`, `raw_label`, `type`, `location`, `time` |

| API와 필드 | 프론트 활용 의도 |
|---|---|
| 일반 목록 요청 `limit` | 한 번에 받을 최신 알림 개수이며 1~100입니다. 생략하면 100입니다. |
| 일반 목록 `alarms` | 최신순 알림 배열입니다. 현재 AlertHistoryPage는 이 값 대신 `history.days`를 사용합니다. |
| 일반 목록 `count` | 이번 응답에 실제 포함된 알림 개수입니다. 전체 누적 개수가 아닙니다. |
| 최근 알림 `alarm` | 가장 최근 알림 한 건입니다. 알림이 없으면 `null`이므로 빈 상태 UI를 표시합니다. |
| 이력 `timezone` | 날짜 묶음 기준 시간대입니다. 현재 `Asia/Seoul`입니다. |
| 이력 `start_date` | 반환된 7일 범위의 첫 날짜입니다. |
| 이력 `end_date` | 반환된 7일 범위의 마지막 날짜이며 오늘입니다. |
| 이력 `total_count` | 7일 범위에 포함된 전체 알림 개수입니다. |
| 이력 `days` | 최신 날짜부터 정렬된 7개 날짜 그룹입니다. 서버 순서를 그대로 표시할 수 있습니다. |
| `days[].date` | 해당 그룹의 `YYYY-MM-DD` 날짜입니다. |
| `days[].display_label` | 오늘은 `오늘`, 전날은 `어제`, 나머지 5개 날짜는 `null`입니다. `null`이면 프론트가 `days[].date`를 `8월 20일` 같은 문구로 변환합니다. |
| `days[].alarms` | 해당 날짜의 최신순 알림 배열입니다. 비어 있어도 날짜 행을 유지합니다. |
| 미확인 `unread_count` | 메인 상단의 `확인하지 않은 N개의 알람이 있어요`에서 N으로 표시합니다. |
| 미확인 `last_seen_at` | 현재 로그인 사용자의 서버 저장 확인 기준입니다. 프론트가 임의로 변경하지 않습니다. |
| 미확인 `window_days` | 계산 범위가 최근 7일임을 나타냅니다. 현재 값은 항상 `7`입니다. |
| 모두 확인 `unread_count` | 성공 시 `0`이며 프론트 배너 값을 즉시 초기화합니다. |
| 모두 확인 `last_seen_at` | 이 시각보다 뒤에 생성된 WebSocket 알림만 새 미확인 알림으로 다룹니다. |
| 상세 `alarm` | 선택한 알림 한 건입니다. 목록보다 화면에 필요한 날짜·세부 레이블 중심의 필드만 포함합니다. |

### 10.7 보호자 연락처 API

| 필드 | 프론트 활용 의도 |
|---|---|
| `contacts` | 보호자 연락처 카드 배열입니다. 빈 배열이면 등록 안내를 표시합니다. |
| `contact_id` | 연락처 카드 key와 수정·삭제 API 경로에 사용합니다. |
| `name` | 보호자 이름으로 카드와 전화 확인창에 표시합니다. |
| `relationship` | `자녀`, `부모`, `보호자`처럼 사용자와의 관계를 표시합니다. |
| `phone_number` | E.164 형식의 전화번호이며 `tel:`·`sms:` 링크 대상으로 사용합니다. |
| `created_at` | 연락처 등록 시각입니다. 필요하면 정렬이나 관리 화면에 사용합니다. |

### 10.8 WebSocket 메시지

| 방향·메시지와 필드 | 프론트 활용 의도 |
|---|---|
| 프론트→서버 `auth.type` | 연결 직후 `auth`로 보내 인증 메시지임을 알립니다. |
| 프론트→서버 `auth.access_token` | 현재 access token입니다. 인증 성공 전에는 다른 메시지를 보내지 않습니다. |
| 서버→프론트 `connection.ready.type` | 인증과 연결 준비가 완료됐음을 뜻합니다. |
| `connection.ready.devices` | 최초 화면 상태를 맞추기 위한 기기 스냅샷이며 10.5절의 기기 배열과 같은 필드를 사용합니다. |
| `device.status_changed.type` | 기기 상태 변경 이벤트임을 판별합니다. |
| `device.status_changed.device_id` | 갱신할 기기 카드를 찾습니다. |
| `device.status_changed.ui_status` | 해당 카드의 상태 배지와 스위치 상태를 갱신합니다. |
| `device.status_changed.last_seen_at` | 마지막 통신 시각을 갱신합니다. |
| `device.status_changed.config_version` | 현재 화면보다 오래된 이벤트인지 비교합니다. |
| `device.config_changed.type` | LED 등 기기 설정 변경 이벤트임을 판별합니다. |
| `device.config_changed.device` | 10.5절의 전체 기기 상태 객체로 해당 카드를 교체합니다. |
| `alarm.created.type` | 신규 알림 이벤트임을 판별합니다. |
| `alarm.created.alarm` | 9절 예시의 알림 객체로 토스트·최신 알림·이력 목록에 즉시 추가하고, 같은 `alarm.id`를 아직 처리하지 않았다면 미확인 개수를 1 증가시킵니다. |
| `household.inactivated.type` | owner 해제로 가구가 비활성화됐음을 뜻합니다. 가구 관련 캐시를 비우고 연동 안내 화면으로 이동합니다. |
| `household.inactivated.household_id` | 비활성화된 가구가 현재 가구와 같은지 확인합니다. |
| `household.member_removed.type` | member 연동 해제 또는 탈퇴로 구성원이 제거됐음을 뜻합니다. 가족 목록을 다시 조회합니다. |
| `household.member_removed.user_id` | 목록에서 제거되거나 재조회될 사용자 ID입니다. |
| 프론트→서버 `ping.type` | 연결 유지와 단절 감지를 위해 60초보다 짧은 간격으로 `ping`을 보냅니다. |
| 서버→프론트 `pong.type` | 연결이 살아 있음을 확인합니다. |
| `pong.time` | 서버 UTC 시각이며 연결 상태 진단에 사용할 수 있습니다. |

알 수 없는 WebSocket `type`은 앱을 중단시키지 말고 무시하거나 진단 로그에만 남기는 방식으로 처리합니다.

WebSocket 재연결, 로그인 계정 변경 또는 페이지 새로고침 시에는 로컬 숫자를 신뢰하지 않고 `GET .../alarms/unread-count`를 다시 호출합니다. `localStorage`는 화면 표시용 임시 캐시로만 사용할 수 있으며 서버 응답이 기준입니다.

### 10.9 프론트에서 직접 호출하지 않는 API

| API | 용도 |
|---|---|
| `POST /households/{household_id}/devices/{device_id}/credential/rotate` | 기기 credential 재발급용 설치·운영 API입니다. 일반 프론트 화면에서 호출하거나 반환 credential을 저장하지 않습니다. |
| `GET /device/v1/config` | Pi·ESP32가 자신의 설정과 MQTT 정보를 확인하는 기기 전용 API입니다. |
| `POST /device/v1/heartbeat` | Pi·ESP32가 실제 연결·오디오 상태를 보고하는 기기 전용 API입니다. |
| `POST /internal/mqtt/device-state` | MQTT 브리지가 기기 상태를 FastAPI에 전달하는 내부 API입니다. |
| `POST /internal/mqtt/alert` | MQTT 브리지가 분류 알림을 저장하고 WebSocket으로 전달하는 내부 API입니다. |
| `GET /health` | 배포 상태 확인용 API입니다. 서비스 상태 점검에는 사용할 수 있지만 일반 화면 데이터 API는 아닙니다. |

`GET /device/v1/config`와 `POST /device/v1/heartbeat`는 `X-Device-Credential` 헤더가 필요합니다. 두 내부 MQTT API는 `X-Internal-Token` 헤더가 필요합니다. 이 헤더 인증은 현재 OpenAPI의 security scheme에 자동 표현되지 않으므로 일반 Bearer 인증으로 대체하지 않습니다.

`POST /internal/mqtt/alert`는 사용자용 응답 필드 외에도 선택적인
`model_version`, `decision_source`, `confidence_kind`, `yamnet_family`,
`yamnet_score`, `hearo_confidence`, `applied_threshold`, `policy_version`을
수용합니다. 이 값은 어떤 모델과 판정 정책이 알림을 만들었는지 운영·성능
분석에 사용하며, 현재 사용자용 알림 상세 화면과 WebSocket에는 노출하지
않습니다. 해당 필드가 없는 기존 Pi 이벤트도 계속 정상 처리합니다.

내부 알림의 발행·수집 기기 규칙:

- `publisher_device_id`를 생략하면 `source_device_id`를 발행 기기로 사용합니다. 값을 보낼 경우 `publisher_device_id`와 `source_device_id`가 반드시 같아야 하며 다르면 `400 PUBLISHER_ID_MISMATCH`입니다.
- 발행 기기는 해당 가구에 등록된 `hub`여야 합니다. 아니면 `400 INVALID_ALERT_PUBLISHER`입니다.
- `capture_device_id`를 생략하면 발행 기기를 실제 수집 기기로 사용합니다. 값을 보낼 경우 해당 가구의 `hub` 또는 `alert_node`여야 하며 아니면 `400 INVALID_CAPTURE_DEVICE`입니다.
- `location`은 기존 발행자와의 요청 호환을 위해 본문 필수 필드로 유지하지만, 저장되는 `source_device_id`와 `location`은 서버가 실제 수집 기기 레코드의 ID·위치로 다시 작성합니다. 요청 본문의 `location`은 신뢰하지 않습니다.
- 따라서 ESP32가 수집하고 Pi가 발행한 이벤트는 `source_device_id`와 `publisher_device_id`에 Pi ID를, `capture_device_id`에 ESP32 ID를 사용합니다. `source_device_id=ESP32`, `publisher_device_id=Pi` 조합은 허용되지 않습니다.
- 서버보다 5분을 초과한 미래 시각은 `400 INVALID_TIMESTAMP`입니다. 이미 90일이 지난 이벤트와 같은 `event_id`의 중복 이벤트는 응답 자체는 `200`일 수 있지만 저장하거나 `alarm.created` WebSocket을 발행하지 않습니다.

## 11. 프론트엔드 수정 확인 목록

- 가입 화면 진입 시 `GET /legal/policies`를 조회하고, 설정·시행된 문서 URL과 서버 버전을 기준으로 동의를 받습니다.
- 로그인 후 `GET /me/consents`의 `status`, `consent_required`, `can_consent`로 재동의 안내를 표시하고, 두 문서를 실제로 보여준 뒤 `PATCH /me/consents`를 호출합니다.
- 재동의 필요 상태만으로 로그인·기기·가구 화면 전체를 임의 차단하지 않습니다. 중요한 변경 시 이용 제한은 별도 정책 합의가 필요합니다.
- 모든 회원가입 화면에 별도 로그인 아이디 입력칸을 추가합니다.
- 기기 상세에서 진동 알림과 민감도 조절을 삭제합니다.
- LED 알림 스위치는 `led_alert_control_supported=true`인 ESP32 3대에만 표시합니다.
- 회원 탈퇴 확인 화면에서 현재 비밀번호를 입력받아 `DELETE /me`를 호출하고, 성공 시 모든 로컬 token·가구 캐시를 삭제한 뒤 로그인 화면으로 이동합니다.
- 알림 상세에서 녹음 재생 UI를 삭제합니다.
- 최근 7일 이력과 90일 최대 조회 기한을 구분하며, 90일이 지나 `404 ALARM_NOT_FOUND`가 된 상세는 만료 안내로 처리합니다.
- 구성원 이름 편집은 사용자별 표시 이름 변경으로 연결하며, 실제 회원 이름을 바꾸지 않습니다.
- 가구 연동 해제 화면은 member에게는 본인 연결만 해제된다고 안내하고, owner에게는 가구 전체가 비활성화된다는 별도 확인 안내를 표시합니다.
- 신규 가구 회원가입 직후의 주소 온보딩에 도로명주소 검색, 우편번호 및 상세주소 입력 UI를 추가합니다.
- 가족·보호자 참여 회원가입에서는 주소를 입력받지 않습니다.
- 설정과 긴급 신고 화면은 전용 긴급 주소 API를 사용하며 다른 일반 응답에서 주소를 찾지 않습니다.

## 12. 배포 전 확인 사항

1. 초대 코드 유효기간은 24시간으로 구현했습니다.
2. 로그인하지 못한 사용자의 비밀번호 찾기는 SMS 본인 인증 정책 확정 전까지 제외합니다.
3. v2.4.0은 별도 r6 스테이징과 테스트 테이블에서 검증한 뒤 운영 서비스로 전환합니다. 구체적인 순서는 [`DEPLOYMENT_V2_4.md`](./DEPLOYMENT_V2_4.md)를 따릅니다.
4. 실제 약관 버전·시행 시각·공개 URL과 프론트 동의 화면이 확정되기 전에는 다섯 문서 환경값을 비우고 `HEARO_LEGAL_CONSENT_REQUIRED=false`를 유지합니다.
5. 기존 알림 테이블은 90일 조회 차단을 먼저 배포한 뒤 검토된 마이그레이션 계획, TTL과 시간당 정리 작업을 순서대로 적용합니다. 로컬 테스트만으로 운영 반영 완료를 판단하지 않습니다.

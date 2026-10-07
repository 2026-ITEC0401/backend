# 운영 인프라 예시

이 폴더는 Hearo 백엔드 v2 운영에 필요한 선언적 구성과 예시 파일을 보관합니다.

| 경로 | 역할 |
|---|---|
| `aws-v2.yaml` | DynamoDB 두 테이블과 EC2 애플리케이션 역할 예시 |
| `hearo-api.env.example` | FastAPI 운영 환경변수 형식 |
| `hearo-mqtt-bridge.env.example` | MQTT 브리지 운영 환경변수 형식 |
| `nginx/` | HTTPS `/v2/` 프록시 설정 |
| `systemd/` | API와 MQTT 브리지 자동 실행 서비스 |
| `mosquitto/` | TLS listener와 가구별 ACL 생성 예시 |
| `certbot/` | 인증서 갱신 뒤 Mosquitto 인증서를 반영하는 예시 |
| `hearo-alert-retention.env.example` | 두 실제 테이블·ARN·리전의 독립 확인값; 미입력 시 정리 중단 |
| `systemd/hearo-alert-retention.*` | 승인된 미리보기·마이그레이션 검증 후 활성화할 시간당 만료 정리 |
| `systemd/*-v2-r6*.service` | v2.4.0 별도 r6 스테이징·운영 경로 예시 |
| `logrotate/` | 일반 로그 90일 회전 예시; 기존 stanza와 병합 검토 필요 |
| `journald/` | 감사 기록 분리 전에는 설치하지 않는 전역 90일 예시 |

v2.4.0은 [새 배포 안내](../docs/DEPLOYMENT_V2_4.md)를 따른다. 시간당 정리는 수동 `--mode purge` 미리보기와 0600 계획/변경 전 백업, 실제 대상 확인, 마이그레이션 결과 검증 후에만 활성화한다. `--scheduled`는 실제 삭제 모드이며 dry-run이 아니다. scheduled 작업은 백업 파일을 남기지 않고 만료 데이터만 조건부로 삭제한다.

`systemd/hearo-api-v2-final.service`와 `hearo-mqtt-bridge-v2-final.service`는 r4 역사 예시다. v2.4.0 전환에는 r6 템플릿을 사용하고, 실제 설치 이름만 기존 final 서비스 이름으로 유지한다. 오래된 템플릿을 운영 서버에 복사하지 않는다.

`.env`, MQTT 비밀번호, JWT secret, internal token, device credential 및 개인키는 저장소에 추가하지 않습니다. 실제 값은 EC2의 `/etc/hearo/`와 Mosquitto 전용 경로에 제한된 권한으로 저장합니다.

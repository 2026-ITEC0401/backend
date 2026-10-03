import asyncio
import hmac
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__
from .config import Settings
from .domain import Alert, EmergencyAddress, Household, User, iso_utc, parse_timestamp
from .history import SEOUL
from .integrations import create_mqtt_publisher
from .juso import JusoClient, JusoError
from .realtime import ConnectionManager
from .schemas import (
    AccountDeletionRequest,
    ConnectionRequest,
    ContactRequest,
    DeviceSettingsRequest,
    DisplayNameRequest,
    EmergencyAddressRequest,
    HeartbeatRequest,
    InternalAlertRequest,
    InternalDeviceStateRequest,
    InviteCodeRequest,
    JusoDetailSearchRequest,
    JusoRoadSearchRequest,
    LoginRequest,
    PasswordChangeRequest,
    RefreshRequest,
    SignupRequest,
    WsAuthMessage,
)
from .security import SlidingWindowLimiter, TokenError, TokenManager, hash_secret, random_secret
from .services import (
    change_password,
    current_invite,
    delete_account,
    device_status,
    link_household,
    login,
    mark_all_alarms_seen,
    normalize_phone,
    preview_invite,
    recent_history,
    refresh,
    rotate_invite,
    signup,
    unread_alarm_summary,
)
from .store import (
    ConflictError,
    InvalidInviteError,
    NotFoundError,
    StoreError,
    create_repository,
)


bearer = HTTPBearer(auto_error=False)


def _error(
    status_code: int,
    code: str,
    message: str,
    field_errors: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "field_errors": field_errors or {},
        },
    )


def _client_key(request: Request, kind: str, subject: str = "") -> str:
    host = request.client.host if request.client else "unknown"
    return f"{kind}:{host}:{subject.strip().casefold()}"


def create_app(
    settings: Settings | None = None,
    repository=None,
    mqtt_publisher=None,
    juso_client=None,
) -> FastAPI:
    app_settings = settings or Settings()
    app_settings.validate_for_production()
    app = FastAPI(title="Hearo API", version=__version__)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Device-Credential",
            "X-Internal-Token",
        ],
        expose_headers=["X-Request-ID"],
    )

    app.state.settings = app_settings
    app.state.repository = repository or create_repository(app_settings)
    app.state.tokens = TokenManager(app_settings)
    app.state.limiter = SlidingWindowLimiter()
    app.state.realtime = ConnectionManager()
    app.state.mqtt = mqtt_publisher or create_mqtt_publisher(app_settings)
    app.state.juso = juso_client or JusoClient(app_settings)
    app.state.alarm_unread_baseline_at = (
        iso_utc(parse_timestamp(app_settings.alarm_unread_baseline_at))
        if app_settings.alarm_unread_baseline_at
        else iso_utc()
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = f"req-{uuid.uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error_body(request: Request, detail: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": detail["code"],
            "message": detail["message"],
            "field_errors": detail.get("field_errors", {}),
            "request_id": getattr(request.state, "request_id", f"req-{uuid.uuid4().hex}"),
        }

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            detail = exc.detail
        else:
            default_codes = {
                400: "BAD_REQUEST",
                401: "UNAUTHORIZED",
                403: "FORBIDDEN",
                404: "NOT_FOUND",
                409: "CONFLICT",
                429: "RATE_LIMITED",
            }
            detail = {
                "code": default_codes.get(exc.status_code, "HTTP_ERROR"),
                "message": str(exc.detail),
                "field_errors": {},
            }
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(request, detail),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        field_errors: dict[str, str] = {}
        for value in exc.errors():
            location = [str(item) for item in value.get("loc", ()) if item != "body"]
            field = ".".join(location) or "body"
            field_errors.setdefault(field, value.get("msg", "입력값을 확인해 주세요."))
        detail = {
            "code": "VALIDATION_ERROR",
            "message": "입력값을 확인해 주세요.",
            "field_errors": field_errors,
        }
        return JSONResponse(status_code=422, content=error_body(request, detail))

    @app.exception_handler(StoreError)
    async def store_error_handler(request: Request, exc: StoreError):
        if isinstance(exc, ConflictError):
            status_code = 409
        elif isinstance(exc, NotFoundError):
            status_code = 404
        else:
            status_code = 400
        detail = {
            "code": exc.code,
            "message": str(exc),
            "field_errors": exc.field_errors,
        }
        return JSONResponse(status_code=status_code, content=error_body(request, detail))

    def current_user(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> User:
        if not credentials or credentials.scheme.casefold() != "bearer":
            raise _error(401, "AUTHENTICATION_REQUIRED", "인증이 필요합니다.")
        try:
            payload = app.state.tokens.decode(credentials.credentials, "access")
        except TokenError as exc:
            raise _error(401, "INVALID_ACCESS_TOKEN", str(exc)) from exc
        user = app.state.repository.get_user(payload["sub"])
        if not user or user.token_version != payload.get("tv"):
            raise _error(401, "REVOKED_ACCESS_TOKEN", "폐기된 인증 정보입니다.")
        return user

    def household_user(
        household_id: str, user: Annotated[User, Depends(current_user)]
    ) -> User:
        if user.household_link_status != "linked" or not user.household_id:
            raise _error(409, "HOUSEHOLD_LINK_REQUIRED", "가구 연동이 필요합니다.")
        if user.household_id != household_id:
            raise _error(403, "HOUSEHOLD_ACCESS_DENIED", "다른 가구에는 접근할 수 없습니다.")
        household = app.state.repository.get_household(household_id)
        if not household or household.status != "active":
            raise _error(409, "HOUSEHOLD_INACTIVE", "비활성화된 가구입니다.")
        return user

    def owner_user(
        household_id: str, user: Annotated[User, Depends(household_user)]
    ) -> User:
        if user.role != "owner":
            raise _error(403, "OWNER_REQUIRED", "owner 권한이 필요합니다.")
        return user

    def onboarding_status(user: User, household: Household) -> dict[str, Any]:
        missing_address = household.emergency_address is None
        return {
            "required": missing_address,
            "missing_steps": ["emergency_address"] if missing_address else [],
            "next_action": (
                "register_emergency_address"
                if missing_address and user.role == "owner"
                else "wait_for_owner"
                if missing_address
                else None
            ),
            "can_edit_emergency_address": user.role == "owner",
        }

    def raise_juso_error(exc: JusoError) -> None:
        raise _error(exc.status_code, exc.code, str(exc)) from exc

    def device_from_credential(
        credential: Annotated[str | None, Header(alias="X-Device-Credential")] = None,
    ):
        if not credential:
            raise _error(401, "DEVICE_CREDENTIAL_REQUIRED", "기기 인증 정보가 필요합니다.")
        device = app.state.repository.get_device_by_credential(hash_secret(credential))
        if not device:
            raise _error(401, "INVALID_DEVICE_CREDENTIAL", "기기 인증 정보가 올바르지 않습니다.")
        household = app.state.repository.get_household(device.household_id)
        if not household or household.status != "active":
            raise _error(403, "HOUSEHOLD_INACTIVE", "기기가 속한 가구가 비활성화되었습니다.")
        return device

    def require_internal_token(
        value: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
    ) -> None:
        expected = app.state.settings.internal_token
        if not value or not hmac.compare_digest(value, expected):
            raise _error(401, "INVALID_INTERNAL_TOKEN", "내부 인증에 실패했습니다.")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/auth/signup", status_code=status.HTTP_201_CREATED)
    def auth_signup(payload: SignupRequest):
        try:
            return signup(app.state.repository, app.state.tokens, app.state.settings, payload)
        except ValueError as exc:
            if isinstance(exc, StoreError):
                raise
            message = str(exc)
            if "전화번호" in message:
                raise _error(
                    400,
                    "INVALID_PHONE_NUMBER",
                    message,
                    {"phone_number": message},
                ) from exc
            if "비밀번호" in message:
                raise _error(
                    400,
                    "INVALID_PASSWORD",
                    message,
                    {"password": message},
                ) from exc
            raise _error(400, "INVALID_SIGNUP_DATA", message) from exc

    @app.post("/auth/login")
    def auth_login(payload: LoginRequest, request: Request):
        key = _client_key(request, "login", payload.login_id)
        if not app.state.limiter.check(key, limit=10, window_seconds=15 * 60):
            raise _error(429, "LOGIN_RATE_LIMITED", "잠시 후 다시 시도하세요.")
        try:
            return login(
                app.state.repository,
                app.state.tokens,
                payload.login_id,
                payload.password,
            )
        except NotFoundError as exc:
            raise _error(401, exc.code, str(exc)) from exc

    @app.post("/auth/refresh")
    def auth_refresh(payload: RefreshRequest):
        try:
            return refresh(app.state.repository, app.state.tokens, payload.refresh_token)
        except (TokenError, NotFoundError) as exc:
            raise _error(401, "INVALID_REFRESH_TOKEN", str(exc)) from exc

    @app.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    def auth_logout(payload: RefreshRequest):
        try:
            app.state.tokens.decode(payload.refresh_token, "refresh")
            app.state.repository.consume_refresh_token(
                hash_secret(payload.refresh_token), datetime.now(UTC)
            )
        except (TokenError, NotFoundError):
            pass
        return None

    @app.get("/me")
    def me(user: Annotated[User, Depends(current_user)]):
        return user.public()

    @app.patch("/me/password", status_code=status.HTTP_204_NO_CONTENT)
    def update_my_password(
        payload: PasswordChangeRequest,
        user: Annotated[User, Depends(current_user)],
    ):
        change_password(
            app.state.repository,
            user,
            payload.current_password,
            payload.new_password,
        )
        return None

    @app.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_my_account(
        payload: AccountDeletionRequest,
        user: Annotated[User, Depends(current_user)],
    ):
        result = delete_account(
            app.state.repository,
            user,
            payload.current_password,
        )
        household_id = result["previous_household_id"]
        if household_id and result["household_status"] == "inactive":
            await app.state.realtime.broadcast(
                household_id,
                {"type": "household.inactivated", "household_id": household_id},
            )
            await app.state.realtime.close_household(household_id, code=1008)
        elif household_id:
            await app.state.realtime.broadcast(
                household_id,
                {"type": "household.member_removed", "user_id": user.user_id},
            )
            await app.state.realtime.close_user(user.user_id, code=1008)
        return None

    @app.get("/households/current")
    def current_household(user: Annotated[User, Depends(current_user)]):
        if user.household_link_status != "linked" or not user.household_id:
            return {
                "household_link_status": "unlinked",
                "household": None,
                "membership": None,
            }
        household = app.state.repository.get_household(user.household_id)
        if not household or household.status != "active":
            return {
                "household_link_status": "unlinked",
                "household": None,
                "membership": None,
            }
        return {
            "household_link_status": "linked",
            "household": {
                **household.public(),
                "member_count": app.state.repository.member_count(household.household_id),
            },
            "membership": {"role": user.role, "linked_at": user.linked_at},
            "onboarding": onboarding_status(user, household),
        }

    @app.get("/households/{household_id}/invite-code")
    def get_invite_code(
        household_id: str,
        _: Annotated[User, Depends(owner_user)],
    ):
        household = app.state.repository.get_household(household_id)
        if not household:
            raise NotFoundError("가구를 찾을 수 없습니다.")
        return current_invite(app.state.repository, app.state.settings, household)

    @app.post("/households/{household_id}/invite-code/rotate")
    def rotate_invite_code(
        household_id: str,
        _: Annotated[User, Depends(owner_user)],
    ):
        return rotate_invite(app.state.repository, app.state.settings, household_id)

    @app.post("/households/link/preview")
    def household_link_preview(
        payload: InviteCodeRequest,
        request: Request,
        user: Annotated[User, Depends(current_user)],
    ):
        if user.household_link_status == "linked":
            raise _error(409, "HOUSEHOLD_ALREADY_LINKED", "이미 가구에 연동되어 있습니다.")
        key = _client_key(request, "invite-preview", user.user_id)
        if not app.state.limiter.check(key, limit=10, window_seconds=15 * 60):
            raise _error(429, "INVITE_PREVIEW_RATE_LIMITED", "잠시 후 다시 시도하세요.")
        return preview_invite(app.state.repository, payload.invite_code)

    @app.post("/households/link")
    def household_link(
        payload: InviteCodeRequest,
        request: Request,
        user: Annotated[User, Depends(current_user)],
    ):
        key = _client_key(request, "household-link", user.user_id)
        if not app.state.limiter.check(key, limit=10, window_seconds=15 * 60):
            raise _error(429, "HOUSEHOLD_LINK_RATE_LIMITED", "잠시 후 다시 시도하세요.")
        return link_household(app.state.repository, user, payload.invite_code)

    @app.get("/households/{household_id}/members")
    def members(
        household_id: str,
        viewer: Annotated[User, Depends(household_user)],
    ):
        values = []
        for member in app.state.repository.list_members(household_id):
            custom_name = app.state.repository.get_display_name(
                household_id, viewer.user_id, member.user_id
            )
            values.append(
                {
                    "user_id": member.user_id,
                    "profile_name": member.name,
                    "display_name": custom_name or member.name,
                    "display_name_is_custom": custom_name is not None,
                    "phone_number": member.phone_number,
                    "role": member.role,
                    "linked_at": member.linked_at,
                    "is_me": member.user_id == viewer.user_id,
                    "can_edit_display_name": True,
                }
            )
        return {"members": values}

    @app.patch("/households/{household_id}/members/{member_user_id}/display-name")
    def set_member_display_name(
        household_id: str,
        member_user_id: str,
        payload: DisplayNameRequest,
        viewer: Annotated[User, Depends(household_user)],
    ):
        app.state.repository.set_display_name(
            household_id, viewer.user_id, member_user_id, payload.display_name
        )
        return {
            "member_user_id": member_user_id,
            "display_name": payload.display_name,
            "display_name_is_custom": True,
        }

    @app.delete("/households/{household_id}/members/{member_user_id}/display-name")
    def reset_member_display_name(
        household_id: str,
        member_user_id: str,
        viewer: Annotated[User, Depends(household_user)],
    ):
        app.state.repository.delete_display_name(
            household_id, viewer.user_id, member_user_id
        )
        member = app.state.repository.get_user(member_user_id)
        if not member:
            raise NotFoundError("가족 구성원을 찾을 수 없습니다.")
        return {
            "member_user_id": member_user_id,
            "display_name": member.name,
            "display_name_is_custom": False,
        }

    @app.delete("/households/current/link")
    async def unlink_current_household(user: Annotated[User, Depends(current_user)]):
        previous_household_id = user.household_id
        result = app.state.repository.unlink_user(user.user_id, datetime.now(UTC))
        if previous_household_id and result["household_status"] == "inactive":
            await app.state.realtime.broadcast(
                previous_household_id,
                {"type": "household.inactivated", "household_id": previous_household_id},
            )
            await app.state.realtime.close_household(previous_household_id, code=1008)
        return result

    @app.get("/households/{household_id}/emergency-address")
    def emergency_address(
        household_id: str,
        _: Annotated[User, Depends(household_user)],
    ):
        household = app.state.repository.get_household(household_id)
        if not household:
            raise NotFoundError("가구를 찾을 수 없습니다.")
        if household.emergency_address is None:
            raise NotFoundError(
                "긴급 신고 주소가 등록되지 않았습니다.", code="EMERGENCY_ADDRESS_NOT_FOUND"
            )
        return household.emergency_address.public()

    @app.patch("/households/{household_id}/emergency-address")
    def update_emergency_address(
        household_id: str,
        payload: EmergencyAddressRequest,
        request: Request,
        owner: Annotated[User, Depends(owner_user)],
    ):
        verified = False
        detail_address = payload.detail_address
        if payload.address_provider == "juso_go_kr" and payload.provider_reference:
            key = _client_key(request, "address-verify", owner.user_id)
            if not app.state.limiter.check(key, limit=30, window_seconds=60):
                raise _error(429, "ADDRESS_SEARCH_RATE_LIMITED", "잠시 후 다시 시도하세요.")
            reference = payload.provider_reference.model_dump()
            try:
                road_verified = app.state.juso.verify_road(
                    postal_code=payload.postal_code,
                    road_address=payload.road_address,
                    reference=reference,
                )
                if not road_verified:
                    raise _error(
                        422,
                        "ADDRESS_SELECTION_MISMATCH",
                        "행안부 검색 결과와 주소가 일치하지 않습니다.",
                    )
                if payload.detail_source == "juso" and payload.juso_detail:
                    detail_address = app.state.juso.verify_detail(
                        reference, payload.juso_detail.model_dump()
                    )
                    if detail_address is None:
                        raise _error(
                            422,
                            "ADDRESS_DETAIL_SELECTION_MISMATCH",
                            "행안부 상세주소 결과와 선택값이 일치하지 않습니다.",
                        )
                    verified = True
                elif payload.detail_source == "none":
                    verified = not payload.provider_reference.apartment
            except JusoError as exc:
                raise_juso_error(exc)
        address = EmergencyAddress(
            postal_code=payload.postal_code,
            road_address=payload.road_address,
            detail_address=detail_address,
            address_provider=payload.address_provider,
            verified=verified,
        )
        return app.state.repository.update_household_emergency_address(
            household_id, address
        ).public()

    @app.post("/households/{household_id}/address-search/roads")
    def search_road_addresses(
        household_id: str,
        payload: JusoRoadSearchRequest,
        request: Request,
        owner: Annotated[User, Depends(owner_user)],
    ):
        key = _client_key(request, "address-search", owner.user_id)
        if not app.state.limiter.check(key, limit=30, window_seconds=60):
            raise _error(429, "ADDRESS_SEARCH_RATE_LIMITED", "잠시 후 다시 시도하세요.")
        try:
            return app.state.juso.search_roads(
                payload.keyword, page=payload.page, page_size=payload.page_size
            )
        except JusoError as exc:
            raise_juso_error(exc)

    @app.post("/households/{household_id}/address-search/details")
    def search_address_details(
        household_id: str,
        payload: JusoDetailSearchRequest,
        request: Request,
        owner: Annotated[User, Depends(owner_user)],
    ):
        key = _client_key(request, "address-search", owner.user_id)
        if not app.state.limiter.check(key, limit=30, window_seconds=60):
            raise _error(429, "ADDRESS_SEARCH_RATE_LIMITED", "잠시 후 다시 시도하세요.")
        try:
            return app.state.juso.search_details(
                payload.provider_reference.model_dump(),
                search_type=payload.search_type,
                dong_name=payload.dong_name,
            )
        except JusoError as exc:
            raise_juso_error(exc)

    @app.get("/households/{household_id}/devices")
    def devices(
        household_id: str,
        _: Annotated[User, Depends(household_user)],
    ):
        values = app.state.repository.list_devices(household_id)
        return {"devices": [device_status(device, app.state.settings) for device in values]}

    @app.patch("/households/{household_id}/devices/{device_id}/connection")
    async def change_connection(
        household_id: str,
        device_id: str,
        payload: ConnectionRequest,
        _: Annotated[User, Depends(owner_user)],
    ):
        device = app.state.repository.update_device_desired(
            household_id, device_id, payload.enabled
        )
        command = {
            "type": "mqtt.connection.set",
            "enabled": payload.enabled,
            "config_version": device.config_version,
            "issued_at": iso_utc(),
        }
        app.state.mqtt.publish(
            f"hearo/{household_id}/devices/{device_id}/command", command
        )
        state_payload = device_status(device, app.state.settings)
        await app.state.realtime.broadcast(
            household_id,
            {
                "type": "device.status_changed",
                "device_id": device_id,
                "ui_status": state_payload["ui_status"],
                "last_seen_at": state_payload["last_seen_at"],
                "config_version": device.config_version,
            },
        )
        return state_payload

    @app.patch("/households/{household_id}/devices/{device_id}/settings")
    async def change_device_settings(
        household_id: str,
        device_id: str,
        payload: DeviceSettingsRequest,
        _: Annotated[User, Depends(owner_user)],
    ):
        device = app.state.repository.update_device_settings(
            household_id,
            device_id,
            payload.led_alert_enabled,
        )
        command = {
            "type": "device.config.set",
            "desired_mqtt_connected": device.desired_mqtt_connected,
            "led_alert_enabled": device.led_alert_enabled,
            "config_version": device.config_version,
            "issued_at": iso_utc(),
        }
        app.state.mqtt.publish(
            f"hearo/{household_id}/devices/{device_id}/command", command
        )
        value = device_status(device, app.state.settings)
        await app.state.realtime.broadcast(
            household_id, {"type": "device.config_changed", "device": value}
        )
        return value

    @app.post("/households/{household_id}/devices/{device_id}/credential/rotate")
    def rotate_credential(
        household_id: str,
        device_id: str,
        _: Annotated[User, Depends(owner_user)],
    ):
        raw = random_secret()
        app.state.repository.rotate_device_credential(
            household_id, device_id, hash_secret(raw)
        )
        return {"device_id": device_id, "device_credential": raw}

    @app.get("/device/v1/config")
    def device_config(device=Depends(device_from_credential)):
        return {
            "household_id": device.household_id,
            "device_id": device.device_id,
            "desired_mqtt_connected": device.desired_mqtt_connected,
            "config_version": device.config_version,
            "led_alert_enabled": device.led_alert_enabled,
            "poll_after_seconds": 15,
            "mqtt": {
                "host": app.state.settings.mqtt_host,
                "port": app.state.settings.mqtt_port,
                "tls": True,
                "alerts_topic": f"hearo/{device.household_id}/alerts",
                "command_topic": (
                    f"hearo/{device.household_id}/devices/{device.device_id}/command"
                ),
            },
        }

    @app.post("/device/v1/heartbeat")
    async def device_heartbeat(
        payload: HeartbeatRequest, device=Depends(device_from_credential)
    ):
        updated = app.state.repository.update_device_reported(
            device.household_id,
            device.device_id,
            payload.mqtt_connected,
            payload.config_version,
            payload.firmware_version,
            network_online=True,
            audio_streaming=payload.audio_streaming,
            microphone_ok=payload.microphone_ok,
            audio_packets_sent=payload.audio_packets_sent,
            audio_packets_dropped=payload.audio_packets_dropped,
            audio_clipped_samples=payload.audio_clipped_samples,
        )
        value = device_status(updated, app.state.settings)
        await app.state.realtime.broadcast(
            device.household_id,
            {
                "type": "device.status_changed",
                "device_id": device.device_id,
                "ui_status": value["ui_status"],
                "last_seen_at": value["last_seen_at"],
                "config_version": value["config_version"],
            },
        )
        return {
            "accepted": True,
            "server_time": iso_utc(),
            "desired_mqtt_connected": updated.desired_mqtt_connected,
            "config_version": updated.config_version,
        }

    @app.get("/households/{household_id}/alarms")
    def alarms(
        household_id: str,
        _: Annotated[User, Depends(household_user)],
        limit: int = Query(default=100, ge=1, le=100),
    ):
        alerts = app.state.repository.latest_alerts(household_id, limit)
        return {"alarms": [alert.public() for alert in alerts], "count": len(alerts)}

    @app.get("/households/{household_id}/alarms/latest")
    def latest_alarm(
        household_id: str,
        _: Annotated[User, Depends(household_user)],
    ):
        values = app.state.repository.latest_alerts(household_id, 1)
        return {"alarm": values[0].public() if values else None}

    @app.get("/households/{household_id}/alarms/history")
    def alarm_history(
        household_id: str,
        _: Annotated[User, Depends(household_user)],
    ):
        return recent_history(app.state.repository, household_id)

    @app.get("/households/{household_id}/alarms/unread-count")
    def alarm_unread_count(
        household_id: str,
        user: Annotated[User, Depends(household_user)],
    ):
        return unread_alarm_summary(
            app.state.repository,
            household_id,
            user.user_id,
            app.state.alarm_unread_baseline_at,
        )

    @app.patch("/households/{household_id}/alarms/seen")
    def mark_alarm_history_seen(
        household_id: str,
        user: Annotated[User, Depends(household_user)],
    ):
        return mark_all_alarms_seen(
            app.state.repository,
            household_id,
            user.user_id,
        )

    @app.get("/households/{household_id}/alarms/{alarm_id}")
    def alarm_detail(
        household_id: str,
        alarm_id: Annotated[str, Path(min_length=1, max_length=100)],
        _: Annotated[User, Depends(household_user)],
    ):
        alarm = app.state.repository.get_alert(household_id, alarm_id)
        if alarm is None:
            raise NotFoundError("알림을 찾을 수 없습니다.", code="ALARM_NOT_FOUND")
        local_timestamp = parse_timestamp(alarm.timestamp).astimezone(SEOUL)
        return {
            "alarm": {
                "id": alarm.event_id,
                "date": local_timestamp.date().isoformat(),
                "local_time": local_timestamp.isoformat(),
                "location": alarm.location,
                "sound": alarm.sound,
                "raw_label": alarm.raw_label,
                "type": alarm.type,
            }
        }

    @app.get("/households/{household_id}/contacts")
    def contacts(
        household_id: str,
        _: Annotated[User, Depends(household_user)],
    ):
        values = app.state.repository.list_contacts(household_id)
        keys = ("contact_id", "name", "relationship", "phone_number", "created_at")
        return {"contacts": [{key: item.get(key) for key in keys} for item in values]}

    @app.post("/households/{household_id}/contacts", status_code=status.HTTP_201_CREATED)
    def add_contact(
        household_id: str,
        payload: ContactRequest,
        _: Annotated[User, Depends(owner_user)],
    ):
        try:
            phone_number = normalize_phone(payload.phone_number)
        except ValueError as exc:
            raise _error(400, "INVALID_PHONE_NUMBER", str(exc), {"phone_number": str(exc)}) from exc
        contact = {
            "contact_id": uuid.uuid4().hex,
            "name": payload.name,
            "relationship": payload.relationship,
            "phone_number": phone_number,
            "created_at": iso_utc(),
        }
        return app.state.repository.create_contact(household_id, contact)

    @app.patch("/households/{household_id}/contacts/{contact_id}")
    def edit_contact(
        household_id: str,
        contact_id: str,
        payload: ContactRequest,
        _: Annotated[User, Depends(owner_user)],
    ):
        try:
            phone_number = normalize_phone(payload.phone_number)
        except ValueError as exc:
            raise _error(400, "INVALID_PHONE_NUMBER", str(exc), {"phone_number": str(exc)}) from exc
        return app.state.repository.update_contact(
            household_id,
            contact_id,
            {
                "name": payload.name,
                "relationship": payload.relationship,
                "phone_number": phone_number,
            },
        )

    @app.delete(
        "/households/{household_id}/contacts/{contact_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def remove_contact(
        household_id: str,
        contact_id: str,
        _: Annotated[User, Depends(owner_user)],
    ):
        app.state.repository.delete_contact(household_id, contact_id)
        return None

    @app.post(
        "/internal/mqtt/device-state", dependencies=[Depends(require_internal_token)]
    )
    async def mqtt_device_state(payload: InternalDeviceStateRequest):
        household = app.state.repository.get_household(payload.household_id)
        if not household or household.status != "active":
            raise _error(409, "HOUSEHOLD_INACTIVE", "비활성화된 가구입니다.")
        try:
            normalized_seen_at = (
                iso_utc(parse_timestamp(payload.seen_at)) if payload.seen_at else None
            )
        except ValueError as exc:
            raise _error(400, "INVALID_TIMESTAMP", str(exc)) from exc
        updated = app.state.repository.update_device_reported(
            payload.household_id,
            payload.device_id,
            payload.mqtt_connected,
            payload.config_version,
            payload.firmware_version,
            normalized_seen_at,
            payload.network_online,
        )
        value = device_status(updated, app.state.settings)
        event = {
            "type": "device.status_changed",
            "device_id": updated.device_id,
            "ui_status": value["ui_status"],
            "last_seen_at": value["last_seen_at"],
            "config_version": value["config_version"],
        }
        await app.state.realtime.broadcast(updated.household_id, event)
        return event

    @app.post("/internal/mqtt/alert", dependencies=[Depends(require_internal_token)])
    async def mqtt_alert(payload: InternalAlertRequest):
        household = app.state.repository.get_household(payload.household_id)
        if not household or household.status != "active":
            raise _error(409, "HOUSEHOLD_INACTIVE", "비활성화된 가구입니다.")
        try:
            normalized_timestamp = iso_utc(parse_timestamp(payload.timestamp))
        except ValueError as exc:
            raise _error(400, "INVALID_TIMESTAMP", str(exc)) from exc
        publisher_id = payload.publisher_device_id or payload.source_device_id
        if publisher_id != payload.source_device_id:
            raise _error(
                400,
                "PUBLISHER_ID_MISMATCH",
                "source_device_id와 publisher_device_id가 다릅니다.",
            )
        publisher = app.state.repository.get_device(payload.household_id, publisher_id)
        if not publisher or publisher.device_type != "hub":
            raise _error(400, "INVALID_ALERT_PUBLISHER", "알림은 등록된 hub 기기만 발행할 수 있습니다.")
        capture_id = payload.capture_device_id or publisher_id
        capture = app.state.repository.get_device(payload.household_id, capture_id)
        if not capture or capture.device_type not in {"hub", "alert_node"}:
            raise _error(400, "INVALID_CAPTURE_DEVICE", "등록되지 않은 수집 기기입니다.")
        alert = Alert(
            household_id=payload.household_id,
            event_id=payload.event_id,
            timestamp=normalized_timestamp,
            source_device_id=capture.device_id,
            publisher_device_id=publisher.device_id,
            location=capture.location,
            sound=payload.sound,
            type=payload.type,
            confidence=payload.confidence,
            raw_label=payload.raw_label,
            model_version=payload.model_version,
            decision_source=payload.decision_source,
            confidence_kind=payload.confidence_kind,
            yamnet_family=payload.yamnet_family,
            yamnet_score=payload.yamnet_score,
            hearo_confidence=payload.hearo_confidence,
            applied_threshold=payload.applied_threshold,
            policy_version=payload.policy_version,
        )
        created = app.state.repository.put_alert(alert)
        event = {
            "type": "alarm.created",
            "alarm": {
                "id": alert.event_id,
                "sound": alert.sound,
                "raw_label": alert.raw_label,
                "type": alert.type,
                "location": alert.location,
                "time": alert.timestamp,
            },
        }
        if created:
            await app.state.realtime.broadcast(alert.household_id, event)
        return event

    @app.websocket("/ws/households/{household_id}")
    async def household_websocket(websocket: WebSocket, household_id: str):
        await websocket.accept()
        authenticated = False
        try:
            raw = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            message = WsAuthMessage.model_validate(raw)
            token_payload = app.state.tokens.decode(message.access_token, "access")
            user = app.state.repository.get_user(token_payload["sub"])
            household = app.state.repository.get_household(household_id)
            if (
                not user
                or user.household_link_status != "linked"
                or user.household_id != household_id
                or user.token_version != token_payload.get("tv")
                or not household
                or household.status != "active"
            ):
                raise TokenError("이 WebSocket에 접근할 수 없습니다.")
            await app.state.realtime.add(household_id, user.user_id, websocket)
            authenticated = True
            snapshot = [
                device_status(item, app.state.settings)
                for item in app.state.repository.list_devices(household_id)
            ]
            await websocket.send_json({"type": "connection.ready", "devices": snapshot})
            while True:
                incoming = await websocket.receive_json()
                if incoming.get("type") == "ping":
                    await websocket.send_json({"type": "pong", "time": iso_utc()})
        except (asyncio.TimeoutError, TokenError, ValueError):
            await websocket.close(code=1008)
        except WebSocketDisconnect:
            pass
        finally:
            if authenticated:
                await app.state.realtime.remove(
                    household_id,
                    user.user_id,
                    websocket,
                )

    return app


app = create_app()

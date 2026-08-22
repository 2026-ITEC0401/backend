from __future__ import annotations

import re
import unicodedata
from typing import Any

import httpx

from .config import Settings


ROAD_SEARCH_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"
DETAIL_SEARCH_URL = "https://business.juso.go.kr/addrlink/addrDetailApi.do"


class JusoError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _text(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip())


def _integer(value: Any) -> int:
    try:
        return int(str(value or "0"))
    except ValueError as exc:
        raise JusoError(
            "주소 제공자의 응답 형식이 올바르지 않습니다.",
            code="ADDRESS_PROVIDER_INVALID_RESPONSE",
            status_code=502,
        ) from exc


def _display_component(value: str | None, suffix: str) -> str | None:
    if value is None:
        return None
    normalized = _text(value)
    if not normalized:
        return None
    if normalized.endswith(suffix):
        return normalized
    # 숫자·영문으로 된 동/층/호는 단위를 붙이고, 건물명 같은 텍스트는
    # 제공자가 준 표기를 그대로 유지합니다.
    if re.fullmatch(r"[-A-Za-z0-9]+", normalized):
        return f"{normalized}{suffix}"
    return normalized


def format_detail_address(item: dict[str, Any]) -> str:
    values = (
        _display_component(item.get("dong_name"), "동"),
        _display_component(item.get("floor_name"), "층"),
        _display_component(item.get("ho_name"), "호"),
    )
    return " ".join(value for value in values if value)


class JusoClient:
    def __init__(self, settings: Settings):
        self.confirm_key = settings.juso_confirm_key
        self.timeout_seconds = settings.juso_timeout_seconds

    def _request(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.confirm_key:
            raise JusoError(
                "주소 검색 서비스가 설정되지 않았습니다.",
                code="ADDRESS_PROVIDER_NOT_CONFIGURED",
                status_code=503,
            )
        provider_params = {
            **params,
            "confmKey": self.confirm_key,
            "resultType": "json",
        }
        try:
            response = httpx.get(
                url,
                params=provider_params,
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JusoError(
                "주소 검색 서비스를 일시적으로 사용할 수 없습니다.",
                code="ADDRESS_PROVIDER_UNAVAILABLE",
                status_code=503,
            ) from exc

        results = payload.get("results") if isinstance(payload, dict) else None
        common = results.get("common") if isinstance(results, dict) else None
        if not isinstance(common, dict):
            raise JusoError(
                "주소 제공자의 응답 형식이 올바르지 않습니다.",
                code="ADDRESS_PROVIDER_INVALID_RESPONSE",
                status_code=502,
            )
        provider_code = _text(common.get("errorCode"))
        if provider_code == "0":
            return results
        if provider_code in {"E0001", "E0014"}:
            raise JusoError(
                "주소 검색 서비스 승인키를 확인해야 합니다.",
                code="ADDRESS_PROVIDER_CONFIGURATION_ERROR",
                status_code=503,
            )
        if provider_code == "-999":
            raise JusoError(
                "주소 검색 서비스를 일시적으로 사용할 수 없습니다.",
                code="ADDRESS_PROVIDER_UNAVAILABLE",
                status_code=503,
            )
        raise JusoError(
            _text(common.get("errorMessage")) or "주소 검색 조건을 확인해 주세요.",
            code="INVALID_ADDRESS_SEARCH",
            status_code=400,
        )

    @staticmethod
    def _reference(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "adm_cd": _text(item.get("admCd")),
            "road_name_code": _text(item.get("rnMgtSn")),
            "underground": _text(item.get("udrtYn")),
            "building_main_no": _integer(item.get("buldMnnm")),
            "building_sub_no": _integer(item.get("buldSlno")),
            "apartment": _text(item.get("bdKdcd")) == "1",
        }

    def search_roads(
        self, keyword: str, *, page: int = 1, page_size: int = 10
    ) -> dict[str, Any]:
        results = self._request(
            ROAD_SEARCH_URL,
            {
                "currentPage": page,
                "countPerPage": page_size,
                "keyword": keyword,
                "hstryYn": "N",
                "firstSort": "none",
                "addInfoYn": "N",
            },
        )
        common = results["common"]
        items = []
        for raw in results.get("juso") or []:
            if not isinstance(raw, dict):
                continue
            items.append(
                {
                    "postal_code": _text(raw.get("zipNo")),
                    "road_address": _text(raw.get("roadAddrPart1"))
                    or _text(raw.get("roadAddr")),
                    "building_name": _text(raw.get("bdNm")) or None,
                    "detail_supported": _text(raw.get("bdKdcd")) == "1",
                    "provider_reference": self._reference(raw),
                }
            )
        return {
            "page": _integer(common.get("currentPage") or page),
            "page_size": _integer(common.get("countPerPage") or page_size),
            "total_count": _integer(common.get("totalCount")),
            "items": items,
        }

    def search_details(
        self,
        reference: dict[str, Any],
        *,
        search_type: str,
        dong_name: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "admCd": reference["adm_cd"],
            "rnMgtSn": reference["road_name_code"],
            "udrtYn": reference["underground"],
            "buldMnnm": reference["building_main_no"],
            "buldSlno": reference["building_sub_no"],
            "searchType": search_type,
        }
        if dong_name is not None:
            params["dongNm"] = dong_name
        results = self._request(DETAIL_SEARCH_URL, params)
        values = []
        for raw in results.get("juso") or []:
            if not isinstance(raw, dict):
                continue
            item = {
                "dong_name": _text(raw.get("dongNm")) or None,
                "floor_name": _text(raw.get("floorNm")) or None,
                "ho_name": _text(raw.get("hoNm")) or None,
            }
            item["formatted_detail_address"] = format_detail_address(item)
            values.append(item)
        return {"search_type": search_type, "items": values, "manual_input_allowed": True}

    def verify_road(
        self,
        *,
        postal_code: str,
        road_address: str,
        reference: dict[str, Any],
    ) -> bool:
        result = self.search_roads(road_address, page=1, page_size=100)
        return any(
            item["postal_code"] == postal_code
            and item["road_address"] == road_address
            and item["provider_reference"] == reference
            for item in result["items"]
        )

    def verify_detail(
        self,
        reference: dict[str, Any],
        selection: dict[str, Any],
    ) -> str | None:
        dong_name = selection.get("dong_name")
        result = self.search_details(
            reference,
            search_type="floorho",
            dong_name=dong_name,
        )
        expected = {
            "dong_name": dong_name,
            "floor_name": selection.get("floor_name"),
            "ho_name": selection.get("ho_name"),
        }
        for item in result["items"]:
            if all(item.get(key) == value for key, value in expected.items()):
                return item["formatted_detail_address"]
        return None

"""
EasyWaySeoul(쉬운길 서울) — 교통약자 전용 대중교통 MCP 서버

서울 지하철/버스를 교통약자(휠체어·시각장애·고령자·유아동반) 관점에서
'실제로 이용 가능한지'까지 판단해 안내하는 MCP 서버.

PlayMCP 제출 요건 준수:
  - Streamable HTTP transport, Stateless (no session)
  - Remote 서버 (공개 URL 배포)
  - 각 툴에 annotations 5개 값 모두 지정
  - 서버명/툴명에 "kakao" 미포함
  - description 영문 작성 + 서비스명 영문/국문 병기
  - 응답은 정제된 마크다운 텍스트 (API 원본 그대로 금지), 크기 최소화

키(.env)가 없으면 샘플 데이터로 동작 → MCP Inspector로 즉시 테스트 가능.
"""

from __future__ import annotations

import asyncio
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

load_dotenv()  # .env 로드 (배포 환경에선 주입된 env가 우선)

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

SERVICE_NAME = "EasyWaySeoul(쉬운길 서울)"

ELEVATOR_KEY = os.getenv("DATA_GO_KR_ELEVATOR_KEY", "").strip()
BUS_KEY = os.getenv("DATA_GO_KR_BUS_KEY", "").strip()

# 서울교통공사_편의시설위치정보 (data.go.kr 15143841) — 2026-07-11 스펙 실사 확정
#   GET /getFcElvtr : 엘리베이터 위치+가동현황(oprtngSitu), 매 5분 갱신, JSON 지원
#   파라미터: serviceKey, dataType, stnNm(포함검색), lineNm, numOfRows, pageNo
ELEVATOR_API_URL = "http://apis.data.go.kr/B553766/facility/getFcElvtr"
# 서울특별시_버스도착정보조회 (data.go.kr 15000314, XML 전용) — 실측 스펙 확정
#   응답: ServiceResult > msgHeader(headerCd 0=정상) > msgBody > itemList[]
#   필드: rtNm(노선), arrmsg1/2(도착메시지), busType1(1=저상), stId, stNm
BUS_ARRIVAL_API_URL = "http://ws.bus.go.kr/api/rest/arrive/getLowArrInfoByStId"
# 서울특별시_정류소정보조회 (data.go.kr 15000303, XML) — 정류소명 → stId 변환
#   getStationByName?stSrch=<정류소명> → itemList[]: stId, stNm, arsId
STATION_SEARCH_API_URL = "http://ws.bus.go.kr/api/rest/stationinfo/getStationByName"
# 서울교통공사_편의시설위치정보 — 수유실(기저귀교환대 포함), 같은 15143841 계정/키 재사용
#   GET /getFcNrsrm : dprSwchbrdCnt(기저귀교환대 개수), infntBedEn(유아용 침대), utztnHr(이용시간)
NURSING_API_URL = "http://apis.data.go.kr/B553766/facility/getFcNrsrm"
# 서울교통공사_지하철알림정보 (data.go.kr 15144070) — 열차 지연·사고·무정차 등 실시간 운행 알림
#   GET /getNtceList : lineNmLst(호선), xcseSitnBgngDt/EndDt(이례상황 시작/종료, ISO), noftTtl/Cn
ALERT_API_URL = "http://apis.data.go.kr/B553766/ntce/getNtceList"

KST = timezone(timedelta(hours=9))

HTTP_TIMEOUT = 2.5  # 초 — p99 3,000ms 요건 대비 여유 있게
CACHE_TTL = 20  # 초 — 실시간성 유지하되 반복 호출 캐싱으로 응답속도 확보
CACHE_MAX = 500  # 캐시 엔트리 상한 (메모리 무한 성장 방지)

# 가동상태 판정 — 실제 oprtngSitu 값 변형("가동중지" 등)에 견디도록 키워드 기반.
# 부정 키워드를 먼저 검사 ("가동중지"가 "가동"으로 good 판정되는 것 방지)
_BAD_KEYWORDS = ("중지", "점검", "보수", "공사", "고장", "불가", "중단")
_GOOD_KEYWORDS = ("정상", "사용가능", "가동", "운행")


def _status_class(status: str | None) -> str:
    """가동상태 문자열 → 'good' | 'bad' | 'unknown'."""
    s = (status or "").strip()
    if not s:
        return "unknown"
    if s == "N" or any(k in s for k in _BAD_KEYWORDS):
        return "bad"
    if s == "Y" or any(k in s for k in _GOOD_KEYWORDS):
        return "good"
    return "unknown"

# ---------------------------------------------------------------------------
# 간단한 인메모리 TTL 캐시 (stateless 서버 내 프로세스 캐시)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < CACHE_TTL:
        return hit[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    if len(_cache) >= CACHE_MAX:
        # 가장 오래된 절반 제거 (단순 전략으로 충분)
        for k in sorted(_cache, key=lambda k: _cache[k][0])[: CACHE_MAX // 2]:
            _cache.pop(k, None)
    _cache[key] = (time.time(), value)


# 커넥션 재사용으로 응답속도 확보 (요청마다 클라이언트 생성 금지)
_http = httpx.AsyncClient(timeout=HTTP_TIMEOUT)


async def _get_json(url: str, params: dict[str, Any]) -> Any:
    resp = await _http.get(url, params=params)
    resp.raise_for_status()
    # 공공데이터포털은 XML/JSON 혼재 → 우선 JSON 시도, 실패 시 text 반환
    try:
        return resp.json()
    except Exception:
        return {"_raw": resp.text}


async def _get_bus_xml_items(url: str, params: dict[str, Any]) -> list[ET.Element]:
    """서울시 버스 API(XML) 공통 호출 — headerCd 0(정상)/4(결과없음)만 허용."""
    resp = await _http.get(url, params=params)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    header_cd = (root.findtext(".//headerCd") or "").strip()
    if header_cd == "4":  # 정상 처리 + 결과 없음
        return []
    if header_cd not in ("0", "00"):
        raise RuntimeError(f"bus API error headerCd={header_cd}: {root.findtext('.//headerMsg')}")
    return root.findall(".//itemList")


# ---------------------------------------------------------------------------
# 데이터 접근 계층 (키 없으면 샘플로 폴백)
# ---------------------------------------------------------------------------

_SAMPLE_ELEVATORS = {
    "신촌": [
        {"exit": "1번 출구", "type": "엘리베이터", "status": "점검중", "dest": "세브란스병원 방향"},
        {"exit": "3번 출구", "type": "엘리베이터", "status": "정상", "dest": "지상"},
        {"exit": "대합실", "type": "휠체어리프트", "status": "정상", "dest": "승강장"},
    ],
    "강남": [
        {"exit": "11번 출구", "type": "엘리베이터", "status": "정상", "dest": "지상"},
        {"exit": "대합실", "type": "엘리베이터", "status": "정상", "dest": "승강장"},
    ],
    "화곡": [
        {"exit": "3번 출구", "type": "엘리베이터", "status": "정상", "dest": "지상"},
        {"exit": "대합실", "type": "엘리베이터", "status": "정상", "dest": "승강장"},
    ],
    "까치산": [
        {"exit": "2번 출구", "type": "엘리베이터", "status": "정상", "dest": "지상"},
        {"exit": "대합실", "type": "엘리베이터", "status": "정상", "dest": "승강장"},
    ],
}

_SAMPLE_BUS = {
    "서울역환승센터": [
        {"route": "742", "low_floor": True, "arrive_sec": 360, "msg": "6분 후 도착 (저상)"},
        {"route": "150", "low_floor": True, "arrive_sec": 720, "msg": "12분 후 도착 (저상)"},
    ],
}

_SAMPLE_NURSING = {
    "신촌": [
        {"exit": "4번 출구 인근", "type": "가족수유실", "diaper_table": 1, "hours": "영업시간내", "dest": "2호선"},
    ],
}

_SAMPLE_ALERTS: list[dict] = []  # 평시엔 활성 알림이 없는 경우가 많아 빈 리스트가 현실적인 샘플


def _station_variants(station: str) -> tuple[str, set[str]]:
    """역명 정규화 — 검색어(base)와 허용 정식명칭 집합을 반환.

    '서울역'처럼 정식 명칭 자체에 '역'이 포함된 역과, '신촌'처럼 접미사 없는 역을
    모두 처리한다. API는 포함검색이므로 base로 조회한 뒤 정확 매칭으로 걸러낸다
    (예: '서울' 검색 시 '서울대입구'가 섞이는 것 방지).
    """
    name = station.strip().replace(" ", "")
    base = name.removesuffix("역") or name
    return base, {base, base + "역"}


async def fetch_station_facilities(station: str) -> tuple[list[dict], bool]:
    """(설비목록, is_live) 반환. is_live=False면 샘플."""
    base, allowed = _station_variants(station)
    ckey = f"elev:{base}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    if not ELEVATOR_KEY:
        result = (_SAMPLE_ELEVATORS.get(base, []), False)
        _cache_set(ckey, result)
        return result

    try:
        data = await _get_json(
            ELEVATOR_API_URL,
            {
                "serviceKey": ELEVATOR_KEY,
                "dataType": "JSON",
                "stnNm": base,  # 포함검색 → allowed로 정확 필터
                "numOfRows": 50,
                "pageNo": 1,
            },
        )
        facilities = _parse_elevator_response(data, allowed)
        result = (facilities, True)
    except Exception:
        # 실패 시 샘플로 안전 폴백
        result = (_SAMPLE_ELEVATORS.get(base, []), False)
    _cache_set(ckey, result)
    return result


# oprtngSitu 실측 코드 매핑 (2026-07-11 전수조사: 865대 중 M=848, S=16, 빈값=1)
_OPRTNG_SITU_LABEL = {"M": "정상", "S": "운행중지"}


def _parse_elevator_response(data: Any, allowed: set[str] | None = None) -> list[dict]:
    """getFcElvtr 응답 파싱 — 실측 스키마 (2026-07-11 data.go.kr 15143841 라이브 검증).

    body.items.item[]: fcltNm(시설명), lineNm(호선), stnNm(역명),
    vcntEntrcNo(근접 출입구), dtlPstn(상세위치), bgngFlr/endFlr(운행구간),
    oprtngSitu(가동현황 코드: M=가동중, S=정지) ← 핵심 상태값
    allowed: 정식 역명 정확 매칭 집합 (포함검색 오염 방지, None이면 전체 허용)
    """
    if not isinstance(data, dict):
        return []
    header = data.get("header", data.get("response", {}).get("header", {}))
    if str(header.get("resultCode", "00")).strip() not in ("0", "00"):
        return []
    body = data.get("body", data.get("response", {}).get("body", {}))
    items = body.get("items", {})
    item = items.get("item", []) if isinstance(items, dict) else items
    if isinstance(item, dict):
        item = [item]

    out = []
    for it in item or []:
        if allowed is not None and (it.get("stnNm") or "").strip() not in allowed:
            continue
        # vcntEntrcNo 형식이 역마다 다름: "4"(순수 숫자) / "2번출구"(완성된 텍스트) / "내부" 등.
        # dtlPstn(상세위치, 예: "신설동 방면4-3")은 있을 때 정보량이 크므로 함께 조합.
        entrance = (it.get("vcntEntrcNo") or "").strip()
        dtl_pstn = (it.get("dtlPstn") or "").strip()
        parts = []
        if entrance:
            parts.append(f"{entrance}번 출구" if entrance.isdigit() else entrance)
        # dtlPstn이 출구번호를 그대로 반복하는 경우("4번 출입구")는 중복이라 생략
        redundant = entrance.isdigit() and entrance in dtl_pstn and ("출구" in dtl_pstn or "출입구" in dtl_pstn)
        if dtl_pstn and dtl_pstn not in parts and not redundant:
            parts.append(dtl_pstn)
        exit_label = " · ".join(parts) if parts else "위치미상"
        floors = ""
        if it.get("bgngFlr") and it.get("endFlr"):
            floors = f"{it['bgngFlr']}↔{it['endFlr']}층"
        # fcltNm 자유텍스트 포맷이 역마다 달라 파싱하지 않고, 키워드로만 유형 분류
        fclt_nm = (it.get("fcltNm") or "")
        if "에스컬레이터" in fclt_nm:
            short_type = "에스컬레이터"
        elif "리프트" in fclt_nm:
            short_type = "휠체어리프트"
        else:
            short_type = "엘리베이터"
        raw_status = (it.get("oprtngSitu") or "").strip()
        out.append(
            {
                "exit": exit_label,
                "type": short_type,
                "status": _OPRTNG_SITU_LABEL.get(raw_status, raw_status),
                "dest": " · ".join(x for x in [it.get("lineNm", ""), floors] if x),
            }
        )
    return out


async def fetch_nursing_facilities(station: str) -> tuple[list[dict], bool]:
    """(수유실/기저귀교환대 목록, is_live) 반환. is_live=False면 샘플."""
    base, allowed = _station_variants(station)
    ckey = f"nurs:{base}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    if not ELEVATOR_KEY:  # 같은 계정/키를 편의시설위치정보 전반에 재사용
        result = (_SAMPLE_NURSING.get(base, []), False)
        _cache_set(ckey, result)
        return result

    try:
        data = await _get_json(
            NURSING_API_URL,
            {"serviceKey": ELEVATOR_KEY, "dataType": "JSON", "stnNm": base, "numOfRows": 20, "pageNo": 1},
        )
        result = (_parse_nursing_response(data, allowed), True)
    except Exception:
        result = (_SAMPLE_NURSING.get(base, []), False)
    _cache_set(ckey, result)
    return result


def _parse_nursing_response(data: Any, allowed: set[str] | None = None) -> list[dict]:
    """getFcNrsrm 응답 파싱 — 실측 스키마 (2026-07-11 라이브 검증, 신촌역 가족수유실).

    dprSwchbrdCnt(기저귀교환대 개수), infntBedEn/infntBedCnt(유아용 침대),
    utztnHr(이용시간), dtlPstn/exitNo(위치), fcltSeNm(수유실 유형)
    allowed: 정식 역명 정확 매칭 집합 (포함검색 오염 방지, None이면 전체 허용)
    """
    if not isinstance(data, dict):
        return []
    header = data.get("header", data.get("response", {}).get("header", {}))
    if str(header.get("resultCode", "00")).strip() not in ("0", "00"):
        return []
    body = data.get("body", data.get("response", {}).get("body", {}))
    items = body.get("items", {})
    item = items.get("item", []) if isinstance(items, dict) else items
    if isinstance(item, dict):
        item = [item]

    out = []
    for it in item or []:
        if allowed is not None and (it.get("stnNm") or "").strip() not in allowed:
            continue
        exit_no = str(it.get("exitNo") or "").strip()
        exit_label = f"{exit_no}번 출구 인근" if exit_no.isdigit() else (it.get("dtlPstn") or "위치미상")
        try:
            diaper_cnt = int(it.get("dprSwchbrdCnt") or 0)
        except (TypeError, ValueError):
            diaper_cnt = 1 if (it.get("dprSwchbrdCnt") or it.get("infntBedEn") == "Y") else 0
        out.append(
            {
                "exit": exit_label,
                "type": it.get("fcltSeNm") or it.get("fcltNm") or "수유실",
                "diaper_table": diaper_cnt,
                "hours": (it.get("utztnHr") or "").strip(),
                "dest": it.get("lineNm", ""),
            }
        )
    return out


async def fetch_active_alerts() -> tuple[list[dict], bool]:
    """(현재 진행중인 이례상황 알림 목록, is_live) 반환. 정보성 안내는 제외."""
    ckey = "alerts"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    if not ELEVATOR_KEY:
        result = (_SAMPLE_ALERTS, False)
        _cache_set(ckey, result)
        return result

    try:
        data = await _get_json(
            ALERT_API_URL,
            {"serviceKey": ELEVATOR_KEY, "dataType": "JSON", "numOfRows": 50, "pageNo": 1},
        )
        result = (_parse_alert_response(data), True)
    except Exception:
        result = (_SAMPLE_ALERTS, False)
    _cache_set(ckey, result)
    return result


def _parse_alert_response(data: Any) -> list[dict]:
    """getNtceList 응답 파싱 — 실측 스키마 (2026-07-11 라이브 검증).

    xcseSitnBgngDt(이례상황 시작, ISO)가 있는 항목만 실제 운행조정으로 간주
    (테스트/공지성 메시지는 시작일시가 비어있어 자동 제외됨). 진행중(시작<=지금<=종료 또는 종료없음)인
    것만 반환.
    """
    if not isinstance(data, dict):
        return []
    header = data.get("header", data.get("response", {}).get("header", {}))
    if str(header.get("resultCode", "00")).strip() not in ("0", "00"):
        return []
    body = data.get("body", data.get("response", {}).get("body", {}))
    items = body.get("items", {})
    item = items.get("item", []) if isinstance(items, dict) else items
    if isinstance(item, dict):
        item = [item]

    now = datetime.now(timezone.utc).astimezone(KST).replace(tzinfo=None)
    out = []
    seen: set[tuple[str, datetime]] = set()  # 동일 알림 중복 제거 (제목+시작일시)
    for it in item or []:
        begin = _parse_kst_dt(it.get("xcseSitnBgngDt"))
        if begin is None or begin > now:
            continue  # 정보성 메시지이거나 아직 시작 전
        end = _parse_kst_dt(it.get("xcseSitnEndDt"))
        if end is not None and end < now:
            continue  # 이미 종료
        # 종료일시가 없는 알림은 시작 후 14일까지만 활성으로 간주
        # (수개월 지난 공지가 무기한 노출되는 것 방지 — '별도 안내 시까지'형 조정은
        #  대체로 이 기간 내 재공지되므로 실용적 절충)
        if end is None and begin < now - timedelta(days=14):
            continue
        title = (it.get("noftTtl") or "").strip()
        if (title, begin) in seen:
            continue
        seen.add((title, begin))
        lines_raw = (it.get("lineNmLst") or "").strip()
        out.append(
            {
                "title": title,
                "content": (it.get("noftCn") or "").strip().replace("\r\n", " ").replace("\n", " ")[:120],
                "lines": {x.strip() for x in lines_raw.split(",") if x.strip()},
                "since": begin,
            }
        )
    return out


def _parse_kst_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip())
    except ValueError:
        return None


async def fetch_low_floor_bus(stop_name: str) -> tuple[list[dict], bool]:
    ckey = f"bus:{stop_name}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    if not BUS_KEY:
        result = (_SAMPLE_BUS.get(stop_name, []), False)
        _cache_set(ckey, result)
        return result

    # 2단 체인: 정류소명 → stId (getStationByName) → 저상버스 도착 (getLowArrInfoByStId)
    try:
        stops = await _get_bus_xml_items(
            STATION_SEARCH_API_URL, {"serviceKey": BUS_KEY, "stSrch": stop_name}
        )
        buses: list[dict] = []
        # 같은 이름의 정류소가 방향별로 여러 개 → 앞쪽 3개까지 조회
        for stop in stops[:3]:
            st_id = (stop.findtext("stId") or "").strip()
            st_nm = (stop.findtext("stNm") or "").strip()
            ars_id = (stop.findtext("arsId") or "").strip()
            if not st_id:
                continue
            arrivals = await _get_bus_xml_items(
                BUS_ARRIVAL_API_URL, {"serviceKey": BUS_KEY, "stId": st_id}
            )
            for it in arrivals:
                msg = (it.findtext("arrmsg1") or "").strip()
                if not msg or msg == "운행종료":  # 노이즈 제거
                    continue
                buses.append(
                    {
                        "route": (it.findtext("rtNm") or it.findtext("busRouteAbrv") or "").strip(),
                        "msg": msg,
                        "stop": f"{st_nm}({ars_id})" if ars_id else st_nm,
                    }
                )
        # 중복 제거(노선+정류소 기준) 후 실제 도착예정을 앞으로 정렬
        seen: set[tuple[str, str]] = set()
        deduped = []
        for b in buses:
            key = (b["route"], b["stop"])
            if key not in seen:
                seen.add(key)
                deduped.append(b)
        deduped.sort(key=lambda b: (0 if any(c.isdigit() for c in b["msg"]) else 1))
        result = (deduped[:8], True)  # result 크기 최소화
    except Exception:
        result = (_SAMPLE_BUS.get(stop_name, []), False)
    _cache_set(ckey, result)
    return result


# ---------------------------------------------------------------------------
# MCP 서버 + 툴 정의
# ---------------------------------------------------------------------------

# DNS rebinding 보호는 localhost 개발 서버용 — 공개 읽기전용 원격 서버에선 비활성화
# (Render/Cloudflare 뒤에서 Host가 도메인으로 들어와 기본 설정이 421을 반환하는 문제 해결)
mcp = FastMCP(
    SERVICE_NAME,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _note(is_live: bool) -> str:
    return "" if is_live else "\n\n> ⚠️ *샘플 데이터입니다. 실제 API 키(.env) 설정 시 실시간 정보로 전환됩니다.*"


@mcp.tool(
    structured_output=False,  # str 반환이 content+structuredContent로 중복되는 것 방지 (result 최소화)
    annotations=ToolAnnotations(
        title="지하철역 교통약자 편의시설·엘리베이터·수유실 실시간 상태",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def check_station_facilities(station: str) -> str:
    """Checks the REAL-TIME operating status of elevators/wheelchair lifts AND the presence of
    nursing rooms (with diaper-changing tables) at a Seoul subway station, using the
    EasyWaySeoul(쉬운길 서울) transit service. Use this to warn a mobility-impaired user when
    an elevator is under inspection/broken and to suggest a working exit, or to find a nursing
    room for a parent with an infant. Input: Korean station name, with or without the '역'
    suffix (e.g. '신촌', '서울역')."""
    (facilities, elev_live), (nursing, nurs_live) = await asyncio.gather(
        fetch_station_facilities(station), fetch_nursing_facilities(station)
    )
    display = station.strip().replace(" ", "")
    if not display.endswith("역"):
        display += "역"
    if not facilities and not nursing:
        return f"**{display}** 편의시설 정보를 찾지 못했어요. 역명을 확인해 주세요 (예: '신촌', '서울역')."

    lines = [f"### {display} 교통약자 편의시설"]
    broken = [f for f in facilities if _status_class(f.get("status")) == "bad"]
    for f in facilities:
        cls = _status_class(f.get("status"))
        icon = {"good": "🟢", "bad": "🔴"}.get(cls, "⚪")
        dest = f" → {f['dest']}" if f.get("dest") else ""
        lines.append(f"- {icon} {f.get('exit','')} {f.get('type','')}{dest} : **{f.get('status','')}**")
    if broken:
        b = broken[0]
        lines.append(
            f"\n⚠️ **{b.get('exit','')} {b.get('type','')}가 현재 이용 불가**입니다. "
            f"정상 운행 중인 출구를 이용하세요."
        )

    lines.append("")
    if nursing:
        for n in nursing:
            diaper = f"기저귀교환대 {n['diaper_table']}대" if n.get("diaper_table") else "기저귀교환대 없음"
            hours = f", 이용시간 {n['hours']}" if n.get("hours") else ""
            lines.append(f"- 👶 {n.get('type','수유실')} ({n.get('exit','')}) — {diaper}{hours}")
    else:
        lines.append("- 👶 수유실: 이 역에는 정보가 없어요.")

    return "\n".join(lines) + _note(elev_live and nurs_live)


@mcp.tool(
    structured_output=False,
    annotations=ToolAnnotations(
        title="정류장 저상버스 실시간 도착정보",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_low_floor_bus_arrival(stop_name: str) -> str:
    """Returns REAL-TIME arrival info for LOW-FLOOR (wheelchair-accessible) buses only at a
    Seoul bus stop, using the EasyWaySeoul(쉬운길 서울) transit service. Filters out regular
    high-floor buses so a wheelchair user only sees boardable options. Input: Korean bus stop
    name (e.g. '서울역환승센터')."""
    buses, is_live = await fetch_low_floor_bus(stop_name)
    if not buses:
        return f"**{stop_name}** 정류장에 곧 도착하는 저상버스가 없어요.{_note(is_live)}"

    lines = [f"### {stop_name} 저상버스 도착"]
    for b in buses:
        stop_label = f" @{b['stop']}" if b.get("stop") else ""
        lines.append(f"- 🚍 **{b.get('route','')}번** — {b.get('msg','')}{stop_label}")
    return "\n".join(lines) + _note(is_live)


_MOBILITY_LABEL = {
    "wheelchair": "휠체어",
    "visual": "시각장애",
    "elderly": "고령자",
    "stroller": "유아차",
}

MAX_ROUTE_STATIONS = 10  # 응답속도(p99 3s) 보호


@mcp.tool(
    structured_output=False,
    annotations=ToolAnnotations(
        title="경로 무장애 검증 — 역별 엘리베이터+실시간 운행 알림 일괄 확인",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def verify_route_accessibility(stations: list[str], mobility_type: str = "wheelchair") -> str:
    """Verifies whether a planned Seoul subway route is ACTUALLY passable for a
    mobility-impaired user, using the EasyWaySeoul(쉬운길 서울) transit service.
    Pass the ordered station list of a route (origin, transfers, destination); it checks
    real-time elevator/lift status at every station in parallel, cross-checks any ACTIVE
    train delay/incident/schedule-adjustment alerts on the lines the route uses, reports
    which station or line blocks the route, and suggests working exits or fallbacks
    (low-floor bus, call taxi). Call this AFTER planning a route with a map/transit tool.
    mobility_type: 'wheelchair' | 'visual' | 'elderly' | 'stroller'. Station names without
    the '역' suffix (e.g. ['화곡', '신촌'])."""
    if not stations:
        return "역 목록이 비어 있어요. 경로의 역 이름을 순서대로 전달해 주세요 (예: ['화곡', '신촌'])."
    # 정규화 + 순서 보존 중복 제거 (LLM이 같은 역을 두 번 넣는 경우 방어)
    seen_st: set[str] = set()
    stations = [
        s for s in (x.strip().removesuffix("역") for x in stations)
        if s and not (s in seen_st or seen_st.add(s))
    ][:MAX_ROUTE_STATIONS]

    # 전 역 엘리베이터 + 노선 운행알림을 동시에 병렬 조회 (캐시 적용)
    facility_results, (alerts, alerts_live) = await asyncio.gather(
        asyncio.gather(*(fetch_station_facilities(s) for s in stations)),
        fetch_active_alerts(),
    )

    label = _MOBILITY_LABEL.get(mobility_type, mobility_type)
    lines = [f"### 경로 무장애 검증 ({label}) : {' → '.join(stations)}", ""]
    blocked: list[tuple[str, dict, list[dict]]] = []  # (역, 고장설비, 정상설비들)
    unknown: list[str] = []  # 정보 없는 역 — '가능' 판정 금지
    route_lines: set[str] = set()
    any_sample = False

    for st, (facilities, is_live) in zip(stations, facility_results):
        any_sample = any_sample or not is_live
        if not facilities:
            lines.append(f"- ⚪ **{st}역** — 설비 정보 없음 (역명 확인 필요)")
            unknown.append(st)
            continue
        for f in facilities:
            line_nm = (f.get("dest") or "").split(" · ")[0].strip()
            if line_nm:
                route_lines.add(line_nm)
        bad = [f for f in facilities if _status_class(f.get("status")) == "bad"]
        good = [f for f in facilities if _status_class(f.get("status")) == "good"]
        if bad:
            b = bad[0]
            lines.append(
                f"- 🔴 **{st}역** — {b.get('exit','')} {b.get('type','')} **{b.get('status','')}**"
            )
            blocked.append((st, b, good))
        else:
            lines.append(f"- 🟢 **{st}역** — 전 설비 정상")

    # 경로가 지나는 노선에 영향 주는 실시간 운행 알림(지연·사고·배차조정 등)만 필터링
    route_alerts = [
        a for a in alerts
        if a["lines"] & route_lines
        or any(st in a["content"] or st in a["title"] for st in stations)
    ]
    any_sample = any_sample or not alerts_live
    lines.append("")
    if route_alerts:
        lines.append("**🚨 실시간 운행 알림**")
        for a in route_alerts[:3]:
            lines_txt = ",".join(sorted(a["lines"])) or "노선 미상"
            since = a["since"].strftime("%m/%d %H:%M")
            lines.append(f"- {a['title']} ({lines_txt}, {since}부터) — {a['content']}")
    else:
        lines.append("🚦 현재 이 경로 노선에 영향 주는 실시간 운행 알림 없음")

    lines.append("")
    if not blocked and unknown:
        lines.append(
            f"❓ **판정 보류: {', '.join(unknown)}역의 설비 정보를 확인할 수 없습니다.** "
            "역명을 확인하거나 해당 역만 `check_station_facilities`로 재조회해 주세요."
        )
    elif not blocked:
        lines.append(f"✅ **판정: 이 경로는 {label} 이동이 가능합니다.** 전 역 승강설비 정상.")
    else:
        st, b, good = blocked[0]
        lines.append(f"⚠️ **판정: {st}역에서 경로가 막힙니다** ({b.get('exit','')} {b.get('type','')} {b.get('status','')}).")
        if good:
            alts = ", ".join(f"{g.get('exit','')} {g.get('type','')}" for g in good[:2])
            lines.append(f"- **대안 1**: {st}역 정상 설비 이용 — {alts}")
        lines.append(
            f"- **대안 2**: 해당 구간을 저상버스로 우회 — `get_low_floor_bus_arrival`로 인근 정류장 확인"
        )
        lines.append(f"- **대안 3**: 장애인 콜택시 — `find_call_taxi` 참고")

    return "\n".join(lines) + _note(not any_sample)


@mcp.tool(
    structured_output=False,
    annotations=ToolAnnotations(
        title="지하철 실시간 운행 알림(지연·사고·무정차) 조회",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_service_alerts(line: str = "") -> str:
    """Returns ACTIVE Seoul subway service alerts happening RIGHT NOW — train delays,
    incidents, skipped stops (무정차), and schedule adjustments — using the
    EasyWaySeoul(쉬운길 서울) transit service. Optionally filter by line name
    (e.g. '2호선' or '2'). Use this for questions like 'is line 2 delayed now?' or
    'any subway disruptions today?'. Data refreshes every minute from Seoul Metro."""
    alerts, is_live = await fetch_active_alerts()

    # 호선 입력 정규화: '2' → '2호선'
    line_q = line.strip().replace(" ", "")
    if line_q and not line_q.endswith("호선") and not line_q.endswith("선"):
        line_q += "호선"

    if line_q:
        matched = [a for a in alerts if line_q in a["lines"]]
        header = f"### {line_q} 실시간 운행 알림"
    else:
        matched = alerts
        header = "### 서울지하철 실시간 운행 알림"

    if not matched:
        scope = f"{line_q}에" if line_q else "서울지하철에"
        return f"🟢 현재 {scope} 접수된 지연·사고·무정차 등 이례상황이 없습니다.{_note(is_live)}"

    lines_out = [header]
    for a in matched[:5]:
        lines_txt = ",".join(sorted(a["lines"])) or "노선 미상"
        since = a["since"].strftime("%m/%d %H:%M")
        lines_out.append(f"- 🚨 **{a['title']}** ({lines_txt}, {since}부터)\n  {a['content']}")
    return "\n".join(lines_out) + _note(is_live)


@mcp.tool(
    structured_output=False,
    annotations=ToolAnnotations(
        title="장애인 콜택시(특별교통수단) 안내",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def find_call_taxi(region: str = "서울") -> str:
    """Provides contact and usage guidance for Seoul's special transport service (disabled
    call taxi) as a fallback when routes/low-floor buses are unavailable, using the
    EasyWaySeoul(쉬운길 서울) transit service. Input: region name (default '서울')."""
    # 참고: 장애등급제는 2019년 폐지 → '장애의 정도가 심한 장애인' 기준으로 표기
    return (
        f"### {region} 장애인 콜택시(특별교통수단)\n"
        "- 서울시설공단 장애인콜택시: **1588-4388**\n"
        "- 이용대상: 보행상 장애가 심한 장애인 등 교통약자 (사전 이용등록 필요)\n"
        "- 저상버스·엘리베이터 이용이 어려운 경우의 대안입니다."
    )


# ---------------------------------------------------------------------------
# 헬스체크 — Render 등 배포 플랫폼용 (GET /mcp 는 405를 반환하므로 별도 경로 필요)
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok", "service": SERVICE_NAME})


# ---------------------------------------------------------------------------
# 엔트리포인트 — Streamable HTTP
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 배포 환경(Render/Railway)은 PORT를 주입. FastMCP는 host/port를 settings로 받음.
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="streamable-http")

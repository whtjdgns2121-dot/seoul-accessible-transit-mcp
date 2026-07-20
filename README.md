# 서울대중교통(교통약자편) — 교통약자 대중교통 MCP

카카오 PlayMCP 개발 공모전(MCP Player 10, 2회차) 출품작.
서울 지하철/버스를 **교통약자(휠체어·시각장애·고령자·유아동반)** 관점에서
"실제로 이용 가능한지"까지 판단해 안내하는 Remote MCP 서버입니다.

## 핵심 가치

일반 지도앱은 최단경로만 알려주지만, 교통약자에게 진짜 문제는
**"이 경로가 나에게 실제로 가능한가"** 입니다.
이 MCP는 엘리베이터 실시간 고장·열차 지연/배차조정을 감지해 **선제 경고**하고,
저상버스·콜택시 **대안을 스스로 제시**하며, 수유실·기저귀교환대 같은 육아 편의시설까지 안내합니다.

## 툴 구성 (4개)

| 툴 | 설명 | 데이터 |
|---|---|---|
| `check_station_facilities` | 지하철역 엘리베이터/리프트 **실시간 가동상태** + **수유실·기저귀교환대** 유무 | 서울교통공사 편의시설위치정보 `getFcElvtr`+`getFcNrsrm` (data.go.kr) |
| `get_low_floor_bus_arrival` | 정류장 **저상버스** 실시간 도착 | 서울시 버스도착정보 `getLowArrInfoByStIdList` |
| `verify_route_accessibility` | **경로 무장애 검증** — 경로상 전 역 엘리베이터 병렬 확인 + 해당 노선 **실시간 운행 알림**(지연·사고·배차조정) 반영, 막힌 역·대안 판정 | 승강기 가동현황 + 서울교통공사 지하철알림정보 `getNtceList` |
| `find_call_taxi` | 장애인 콜택시 안내 (fallback) | 정적 |

> 상세 스펙: [docs/API_SPEC.md](docs/API_SPEC.md)

## 로컬 실행

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # (mac/linux: .venv/bin/pip)
cp .env.example .env                            # 키 없어도 샘플 데이터로 동작
.venv/Scripts/python server.py                  # http://0.0.0.0:8000/mcp
```

키(`.env`)가 없으면 **샘플 데이터**로 동작하므로 MCP Inspector로 즉시 테스트할 수 있습니다.

## MCP Inspector 점검

```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP / URL: http://127.0.0.1:8000/mcp
```

## PlayMCP 요건 준수

- ✅ Streamable HTTP, Stateless, Remote(배포 시 공개 URL)
- ✅ protocolVersion 2025-03-26 (요건 범위 내)
- ✅ 툴 4개, 각 툴 annotations 5개 값 모두 지정
- ✅ 서버명/툴명에 "kakao" 미포함
- ✅ description 영문 + 서비스명 영문/국문 병기
- ✅ 응답은 정제된 마크다운, 캐싱/타임아웃으로 응답속도 확보

## 배포 (Render 예시)

- Build: `pip install -r requirements.txt`
- Start: `python server.py` (PORT 자동 주입)
- 배포 후 공개 URL `https://<app>.onrender.com/mcp` 를 PlayMCP에 등록

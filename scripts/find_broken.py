"""시연 준비용 — 지금 이 순간 운행중지(S)인 엘리베이터 역 목록을 출력.

시연 직전에 실행해서 '실제로 막혀 있는 역'을 시나리오에 넣는다.
사용: .venv/Scripts/python scripts/find_broken.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import server  # noqa: E402


async def main() -> int:
    if not server.ELEVATOR_KEY:
        print("ELEVATOR_KEY가 없습니다 (.env 확인)")
        return 1

    broken: list[tuple[str, str, str]] = []  # (역, 호선, 시설)
    total = 0
    page = 1
    while True:
        data = await server._get_json(
            server.ELEVATOR_API_URL,
            {"serviceKey": server.ELEVATOR_KEY, "dataType": "JSON", "numOfRows": 500, "pageNo": page},
        )
        body = data.get("response", data).get("body", {})
        total = int(body.get("totalCount") or 0)
        items = body.get("items") or {}
        item = items.get("item", []) if isinstance(items, dict) else items
        if isinstance(item, dict):
            item = [item]
        if not item:
            break
        for it in item:
            if (it.get("oprtngSitu") or "").strip() == "S":
                broken.append((it.get("stnNm") or "?", it.get("lineNm") or "?", it.get("fcltNm") or "?"))
        if page * 500 >= total:
            break
        page += 1

    print(f"전체 엘리베이터 {total}대 중 운행중지 {len(broken)}대\n")
    for stn, line, fclt in sorted(broken):
        print(f"  🔴 {stn}역 ({line}) — {fclt}")
    if broken:
        stn = sorted(broken)[0][0]
        print(f"\n추천 시연 질문: \"휠체어 타고 {stn}역 경유해서 가려는데 엘리베이터 확인해줘\"")
    else:
        print("\n지금은 전 역 정상 — '전 구간 이용 가능' 판정 데모로 진행 (대본 Plan B 참고)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

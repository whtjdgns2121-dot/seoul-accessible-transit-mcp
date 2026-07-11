"""data.go.kr 키 활성화 대기 스크립트 (범용).

지정한 엔드포인트를 3분 간격으로 호출해 활성화되면(200) 원본 응답을
<name>_activation_result.json 에 저장하고 종료.

사용: python scripts/wait_for_key.py <name> <url> <extra_params_json>
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import server  # noqa: E402

INTERVAL = 180  # 3분
MAX_TRIES = 20  # 최대 1시간


async def try_once(name: str, url: str, params: dict, attempt: int, out: Path) -> bool:
    try:
        data = await server._get_json(url, params)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{name} try {attempt}] SUCCESS — saved to {out.name}", flush=True)
        return True
    except Exception as e:
        print(f"[{name} try {attempt}] not yet: {type(e).__name__}: {e}", flush=True)
        return False


async def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "elevator"
    url = sys.argv[2] if len(sys.argv) > 2 else server.ELEVATOR_API_URL
    extra = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    params = {"serviceKey": server.ELEVATOR_KEY, "dataType": "JSON", "numOfRows": 10, "pageNo": 1, **extra}
    out = Path(__file__).parent.parent / f"{name}_activation_result.json"

    for i in range(1, MAX_TRIES + 1):
        if await try_once(name, url, params, i, out):
            return 0
        if i < MAX_TRIES:
            time.sleep(INTERVAL)
    print(f"GAVE UP on {name} after max tries")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

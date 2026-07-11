# 실행 가이드 — 점검 → 키 발급 → 배포

계정이 필요한 단계는 회원님이, 코드 수정은 제가 도와드립니다.

---

## ① MCP Inspector로 표준 스펙 점검 (제출 필수 요건)

이미 curl로 initialize/tools/list/tools/call 핸드셰이크는 검증됐지만, PlayMCP는 Inspector 점검을 요구합니다.

```bash
# 터미널 1 — 서버 실행
.venv/Scripts/python server.py

# 터미널 2 — Inspector 실행 (Node 필요)
npx @modelcontextprotocol/inspector
```

브라우저가 열리면:
- **Transport Type**: `Streamable HTTP`
- **URL**: `http://127.0.0.1:8000/mcp`
- `Connect` → `List Tools` → 각 툴 `Run`

체크포인트: 툴 4개 표시, annotations 노출, 호출 시 마크다운 응답, 에러 없음.

---

## ② data.go.kr 서비스키 발급 (회원님 계정 필요)

두 API를 각각 신청합니다. **자동승인**이라 신청 즉시 개발키가 나옵니다.

### A. 승강기 실시간 (엘리베이터)
1. [data.go.kr](https://www.data.go.kr) 로그인 → 검색: **`서울교통공사 교통약자 이용시설 승강기 가동현황`**
2. **오픈 API** 결과 클릭 → `활용신청`
3. 활용목적 간단히 기입 → 신청 (자동승인)
4. **마이페이지 → 오픈API → 인증키**에서 `일반 인증키(Encoding/Decoding)` 확인
5. **"활용가이드" 문서 다운로드** ← 여기에 아래가 들어있습니다:
   - 요청 URL(엔드포인트)
   - 요청 변수(파라미터명: 역명/역코드 등)
   - 응답 필드(가동상태 값: 정상/점검중 등)

### B. 저상버스 도착
1. 검색: **`서울특별시 버스도착정보조회`** → 오픈 API → `활용신청`
2. 오퍼레이션 중 **`getLowArrInfoByStIdList`** (저상버스 전용) 사용
3. 정류소명 → `stId` 변환용으로 **`서울특별시 정류소정보조회`** 도 함께 신청 권장

### 발급 후 나에게 전달할 것
활용가이드에서 아래만 캡처/복사해 주시면 제가 `server.py`의 TODO를 실제 코드로 채웁니다:
- 요청 URL
- 요청 파라미터명 (특히 역명/정류소 파라미터)
- 응답 필드명 (가동상태, 도착시간, 저상 여부)

그 전까지는 **키를 `.env`에 넣으면 자동 호출을 시도**하고, 실패 시 샘플로 폴백합니다.

```
# .env
DATA_GO_KR_ELEVATOR_KEY=발급받은_디코딩키
DATA_GO_KR_BUS_KEY=발급받은_디코딩키
```

---

## ③ Render 배포 → 공개 URL 확보 (회원님 계정 필요)

Remote MCP는 공개 URL이 필수라 배포해야 제출할 수 있습니다.

1. 이 프로젝트를 GitHub 저장소로 push
2. [render.com](https://render.com) 가입 → **New → Blueprint** → 저장소 선택
   - `render.yaml`을 자동 인식합니다.
3. **Environment**에서 `DATA_GO_KR_ELEVATOR_KEY`, `DATA_GO_KR_BUS_KEY` 입력
4. 배포 완료 후 URL 확인: `https://<앱이름>.onrender.com/mcp`
5. 그 URL로 다시 MCP Inspector 점검 (원격 정상 확인)

> ⚠️ Render 무료 티어는 유휴 시 슬립 → 첫 요청이 느릴 수 있음. 공모전 시연 직전 한 번 깨워두세요.

---

## ④ PlayMCP 등록

PlayMCP → 개발자 콘솔 → 새 MCP 서버 등록 → 위 공개 URL 입력.
- 서버명/툴명에 "kakao" 금지 (준수됨)
- 개인정보 인증 불필요 → OAuth 설정 생략 가능

# AGENTS.md

치와와(Chiwawa) 저장소에서 작업하는 모든 코딩 에이전트(Claude Code, Codex, Antigravity 등)의 공통 지침.
서비스 소개와 기능 설명은 [`README.md`](README.md)에 있고, 이 문서는 **작업 방법**만 다룬다.

## 저장소 지도

| 경로 | 내용 | 런타임 |
|---|---|---|
| `frontend/` | Flutter App + Web (Riverpod, Dio, go_router) | Dart |
| `backend/` | FastAPI. `src/chiwawa_backend/{routers,schemas,services}` | Python 3.13, uv |
| `ai/image_search/` | 사진 → 장소 후보 (Cloud Vision + Gemini + Google Places) | Python 3.11(Modal) **및** 3.13(Backend in-process) |
| `ai/route_planner/` | 일자 배정·방문 순서 정확 Solver | Python 3.11(Modal) |
| `ai/free_time_recommender/` | Route Leg 삽입 후보 평가 | Python |
| `contracts/` | AI ↔ Backend 응답 JSON Schema (생성물) | — |

## 검증 명령

변경한 파트의 명령을 실제로 돌리고, 출력을 확인한 뒤에만 "완료"라고 말한다.
이미 실패하는 항목이 있으면 변경 전 결과를 먼저 기록해 두고, 변경 후와 비교해 회귀가 없음을 보인다.

```bash
# Backend (format-check + lint + type + test)
cd backend && make check

# AI 모듈 (저장소 루트에서)
PYTHONPATH=. pytest ai/image_search/tests
PYTHONPATH=. pytest ai/route_planner/tests
PYTHONPATH=. pytest ai/free_time_recommender/tests

# 별도 venv 없이 Backend 환경을 빌려 AI 테스트를 돌릴 때
PYTHONPATH=. uv run --project backend --with 'modal>=1.5,<2' --with 'fastapi[standard]>=0.115,<1' \
  --with python-dotenv python -m pytest ai/image_search/tests

# Frontend
cd frontend && flutter analyze && flutter test
```

## 반드시 지킬 불변식

1. **사진 추정과 장소 사실의 분리.** AI(Vision·Gemini·EXIF)가 낸 좌표나 이름은 가설이다.
   사용자에게 나가는 좌표·Place ID는 항상 Google Places 응답값이어야 한다. 도시·국가도 Places 값을 쓰고,
   Places 가 비워 보냈을 때만 여행 정보로 채운다(Places 값이 있는데 덮어쓰지 않는다).
2. **`ai/image_search`는 두 런타임에서 돈다.** Python 3.11 문법으로 쓰고(3.12+ 전용 문법 금지),
   Modal 이미지(`ai/image_search/modal_app.py`의 `pip_install`)에 없는 패키지를 import 하지 않는다.
   새 의존성이 필요하면 `requirements.txt`, `modal_app.py`, `backend/pyproject.toml` 세 곳을 같이 고친다.
3. **응답 계약은 생성물이다.** 원본은 `ai/image_search/domain/search_schemas.py`의 `ImageSearchResult`.
   DTO를 바꾸면 `python -m ai.image_search.scripts.export_modal_response_schema` 로
   `contracts/ai_image_search/image_search_response.schema.json` 을 다시 만들고(직접 수정 금지),
   Backend 쪽 사본 `backend/src/chiwawa_backend/schemas/image_search.py` 와
   타입 스텁 `backend/typings/ai/` 도 맞춘다. 필드는 **추가만** 하고 기존 필드의 의미를 바꾸지 않는다.
4. **Route Planner는 근사로 도망가지 않는다.** POI 12개 제한을 넘으면 휴리스틱으로 전환하지 말고 명시적으로 실패한다.
5. **테스트는 유료 API를 부르지 않는다.** Provider는 `Protocol`로 주입하므로 가짜 provider(`ai/image_search/tests/fakes.py`)나
   `httpx.MockTransport`를 쓴다. 실제 키로 호출하는 것은 `ai/image_search/scripts/run_image_search.py` 같은 수동 CLI뿐이다.
6. **외부 호출은 우아하게 저하한다.** Provider 하나가 실패해도 가능한 경로로 계속 처리하고, 결과의 `status`로 드러낸다.
7. **보안 경계를 약화하지 않는다.** `image_loader.py`의 SSRF 가드(IP 고정, 리다이렉트 금지, 크기·시간 상한)와
   Backend 업로드 검증, JWT 보호 Endpoint는 건드릴 때 테스트를 같이 유지한다.

## 코드 규칙

- 주석·문서·커밋 메시지는 한국어. 주변 코드의 주석 밀도와 이름 규칙을 따른다.
- Backend는 `ruff`(select = ALL)와 `basedpyright`(typeCheckingMode = all)를 통과해야 한다. `# type: ignore` 로 덮지 않는다.
- 새 추상화·설정값·의존성은 지금 필요한 것만. 이미 있는 헬퍼(`backend/.../services/exif.py`, `providers/errors.py` 등)를 먼저 찾아 쓴다.
- 로직을 추가하면 그 로직이 깨질 때 실패하는 테스트를 최소 하나 같이 둔다.

## Git

- 흐름: `feat/*`·`fix/*`·`refactor/*`·`docs/*` 브랜치 → `main` 대상 PR (운영 브랜치는 `main` 하나).
- 커밋 형식: `<type>: <한국어 요약>` (`feat` `fix` `refactor` `docs` `test` `chore`).
- **사용자가 요청하기 전에는 커밋·푸시·PR 생성을 하지 않는다.** 남의 미추적(untracked) 작업 파일은 건드리지 않는다.

## 여러 에이전트가 같은 작업 트리를 쓸 때

이 저장소는 한 워크스페이스에서 여러 에이전트가 동시에 열려 있는 경우가 많다. 모두 **같은 작업 트리**를 본다.

1. **쓰는 에이전트는 한 번에 하나.** 작업을 맡은(사용자가 지시한) 에이전트만 저장소 파일을 수정한다.
   나머지는 읽기 전용으로 분석·리뷰하고, 결과를 저장소 밖 파일이나 메시지로 넘긴다.
2. 파일 수정을 나눠야 하면 시작 전에 **파일 단위 소유권**을 정하고, 남의 파일은 고치지 않는다.
3. 다른 에이전트의 분석은 근거(`파일:줄`)와 함께 받고, 반영하기 전에 직접 확인한다. 추측은 추측이라고 표시한다.
4. 다른 에이전트에게 자기 세션에서 거부·차단된 작업을 대신 시키지 않는다.
5. 보고는 사실대로: 돌린 명령과 결과, 건너뛴 것, 확인하지 못한 것을 구분해 적는다.

## 외부 참조: geo-sleuth

[Oldcircle/geo-sleuth](https://github.com/Oldcircle/geo-sleuth)(MIT)는 사진 촬영 위치를 찾는 **에이전트 스킬**
(SKILL.md + 독립 스크립트)이지 import 할 수 있는 라이브러리가 아니다. 한 번 실행에 분 단위가 걸리고
(표본 8장 실측 2~9분, 저자가 공개한 어려운 사례는 32~72분) Chrome·torch에 의존하며 사진을 외부 역이미지
검색(Baidu·Yandex)에 올리므로 요청 경로(Modal 120초)에 그대로 넣지 않는다.
`ai/image_search`에는 그 **원칙과 가벼운 연산자만** 옮겨 쓴다. 옮긴 내용과 출처는
[`ai/image_search/README.md`](ai/image_search/README.md)에 적는다.

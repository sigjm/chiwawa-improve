# 증거 기반 후보 적용 — 변경 전·후 검증 기록

- 작성일: 2026-10-07
- 기준(변경 전): `develop` @ `fab6abd`
- 대상(변경 후): 이 문서가 포함된 커밋
- 변경 내용 설명: [`../README.md`](../README.md)의 "증거 기반 후보" 절

사진 장소 검색에 [Oldcircle/geo-sleuth](https://github.com/Oldcircle/geo-sleuth)의 원칙(위치 prior, 동명 후보 순위화, 읽은 글자 폴백, 위치 힌트 주변 탐색)을 옮긴 뒤, 같은 입력에서 결과가 어떻게 달라졌는지와 결함이 실제로 고쳐졌는지를 확인한 기록입니다.

## 먼저 읽을 것: 이 기록이 말하는 것과 말하지 않는 것

- **실사진·실 API로 측정한 정확도가 아닙니다.** 작업 환경에 Google API 키가 없어 Cloud Vision·Gemini·Places를 실제로 부르지 못했습니다.
- 아래 위치 비교는 **고정 시나리오 재생**입니다. Vision·Gemini 출력은 시나리오에 적어 둔 값이고, Places는 [`scripts/replay_scenarios.py`](../scripts/replay_scenarios.py)의 `SimPlaces`가 흉내 냅니다. 변경 전·후 코드에 **같은 입력**을 넣었을 때 후보 선택 로직의 차이만 보여 줍니다.
- `SimPlaces`는 두 가지 사실에 기대고 있습니다. 둘 다 Google Text Search 문서에서 확인했습니다.
  - `locationBias`·`locationRestriction`을 모두 생략하면 **호출 서버의 IP 위치**로 결과가 편향된다.
  - `locationBias`는 제한이 아니라 편향이다.
- 가정도 있습니다. 서버 IP 위치를 미국 동부로 두었고(실제 Modal 리전은 확인하지 못함), 동명 장소 일부(`가상` 표기)는 상황을 만들려고 넣었습니다. 실제 Places의 순위는 이보다 복잡합니다.

실제 정확도를 알려면 키를 넣고 `scripts/run_image_search.py`로 실사진을 돌려야 합니다.

## 1. 위치를 얼마나 잘 찾는가

오차는 "화면에 대표로 뜨는 첫 후보"와 정답 사이의 거리입니다(Flutter가 `candidates.first`를 대표 결과로 씁니다). 정답 순위는 후보 목록(최대 5개) 안에서의 위치입니다.

| # | 시나리오 | 정답 | 전: 첫 후보 | 전: 오차(km) | 후: 첫 후보 | 후: 오차(km) | 후: 정답 순위 |
|---|---|---|---|---|---|---|---|
| 1 | 유명 랜드마크 (Vision이 확신) | 센소지 | 센소지 | 0 | 센소지 | 0 | 1 |
| 2 | 랜드마크 이름이 검색되지 않아 LLM 추정으로 폴백 | 센소지 | 센소지 | 0 | 센소지 | 0 | 1 |
| 3 | 이름이 하나뿐인 가게 (LLM 추정, 위치 정보 없음) | 블루보틀 커피 교토 카페 | 블루보틀 커피 교토 카페 | 0 | 블루보틀 커피 교토 카페 | 0 | 1 |
| 4 | 랜드마크와 이름이 같은 장소가 다른 나라에 있음 | 금각사 | 금각사(가상 식당) | 9,145.2 | 금각사 | 0 | 1 |
| 5 | 체인점 + 사진에 촬영 좌표(EXIF GPS) 있음 | 스타벅스 교토 산조오하시 | 스타벅스 애슈번 | 11,099.4 | 스타벅스 교토 산조오하시 | 0 | 1 |
| 6 | 체인점 + 좌표 없음, 상위 결과에 여행 국가 지점이 섞여 있음 | 이치란 도톤보리 | 이치란 미드타운 | 11,102.3 | 이치란 시부야 | 397.2 | 2 |
| 7 | 6번과 같지만 여행 국가를 다른 표기(Japan)로 입력 | 이치란 도톤보리 | 이치란 미드타운 | 11,102.3 | 이치란 미드타운 | 11,102.3 | — |
| 8 | 체인점 + 좌표 없음, 상위 5건이 전부 서버 근처 지점 | 스타벅스 교토 산조오하시 | 스타벅스 애슈번 | 11,099.4 | 스타벅스 애슈번 | 11,099.4 | — |
| 9 | 이름 추정 없이 간판 글자만 읽힘 (같은 도시에 지점이 둘) | % 아라비카 교토 아라시야마 | 후보 없음 | — | % 아라비카 교토 히가시야마 | 9.5 | 2 |
| 10 | 간판 글자 + 촬영 좌표 | % 아라비카 교토 아라시야마 | 후보 없음 | — | % 아라비카 교토 아라시야마 | 0 | 1 |
| 11 | 이름도 글자도 없고 촬영 좌표만 있음 (음식 클로즈업) | 골목 카페(가상) | 후보 없음 | — | 골목 카페(가상) | 0 | 1 |
| 12 | 멀리서 찍은 피사체 + 촬영 좌표 (가와구치코에서 후지산) | 후지산 | 후지산 | 0 | 후지산 | 0 | 1 |
| 13 | 단서 없음 (이름·글자·좌표 모두 없음) | (없음) | 후보 없음 | — | 후보 없음 | — | — |

정답이 있는 12개 시나리오 집계:

| 지표 | 변경 전 | 변경 후 |
|---|---|---|
| 첫 후보가 정답 | 4 / 12 | 8 / 12 |
| 정답이 후보 목록 안에 있음 | 4 / 12 | 10 / 12 |
| 후보를 하나도 못 냄 ("사진에서 장소 후보를 찾지 못했어요") | 3 | 0 |
| Places 호출 수 (13개 시나리오 합계) | 19 | 24 |

읽는 법:

- **1·2·3·12번은 원래 맞던 경우**이고 변경 후에도 그대로입니다(회귀 없음). 12번은 "촬영 위치에서 가까운 것을 우선"하는 새 규칙이 멀리 있는 피사체를 밀어내지 않는지 본 것입니다.
- **4·5번**은 위치 prior(랜드마크 감지 좌표, EXIF GPS)가 있어서 정확히 맞게 된 경우입니다.
- **9·10·11번**은 전에는 후보가 아예 없던 경우입니다. 9번은 도시까지만 좁혀져 같은 도시의 다른 지점이 첫 후보가 됐고(정답은 2순위), 10번처럼 촬영 좌표가 있어야 지점까지 맞습니다.
- **6번**은 나라까지만 맞췄습니다. 여행 도시(오사카)는 순위에 쓰지 않아서 도쿄 지점이 먼저 왔고 정답은 2순위입니다.
- **7·8번은 여전히 틀립니다.** 아래 "고쳐지지 않은 것" 참고.
- 상태값이 `PARTIAL`로 나오는 시나리오가 많은 것은 시뮬레이션 세계에 주변 장소를 넣지 않아 근처 추천이 비기 때문입니다.
- Places 호출이 늘어난 것은 전에는 호출 없이 실패하던 9·10·11번이 이제 검색을 하기 때문입니다. 원래 성공하던 시나리오의 호출 수는 같습니다.

## 2. 결함이 고쳐졌는가

| # | 결함 | 변경 전 | 변경 후 | 확인 방법 |
|---|---|---|---|---|
| 1 | 요청의 위치 힌트(`latitude`·`longitude`)를 받기만 하고 검색에 쓰지 않음 (README "현재 한계"에 적혀 있던 항목) | 무시 | Places 이름 검색의 `locationBias`와 순위에 사용 | 시나리오 5: 11,099km → 0km. `TestEvidenceRanking` |
| 2 | 사진 업로드 경로가 EXIF GPS를 읽지 않음 | 힌트 없음 | 촬영 좌표를 힌트로 전달 | 새 테스트를 변경 전 소스에 돌리면 실패(`assert None == 35.0036`), 변경 후 통과 |
| 3 | Places 첫 결과를 검증 없이 채택 (`pageSize=1`) | 서버 IP 근처의 동명 장소가 그대로 정답이 됨 | 5건을 받아 위치 prior·여행 국가로 1순위를 정하고 나머지는 동명 후보로 제시 | 시나리오 4, 6 |
| 4 | Gemini가 읽은 `visible_text`를 검색에 쓰지 않음 | 이름 추정이 없으면 곧바로 `FAILED` | 글자를 여행지 범위 또는 위치 힌트 주변에서 검색 | 시나리오 9, 10 |
| 5 | 촬영 좌표가 있어도 이름을 모르면 `FAILED` | 후보 없음 | 반경 200m 안의 장소를 거리순으로 제시 (`identified = null`, `PARTIAL`) | 시나리오 11 |
| 6 | `read_exif`가 압축 폭탄 이미지에서 `DecompressionBombError`를 잡지 않음 | 예외 발생 | 빈 EXIF 반환 | 직접 호출: 전 `예외 DecompressionBombError` / 후 `PhotoExif(None, None, None)`. `test_photo_upload_survives_image_pillow_refuses_to_open` |
| 7 | `read_exif`가 방향 태그(N/S, E/W) 없는 좌표를 북반구·동반구로 가정 | 남위 33.86° 사진이 `latitude = +33.86`으로 읽힘 | 방향을 모르는 좌표는 버림 (`None`) | 직접 호출 결과. `test_photo_upload_drops_exif_coordinate_without_direction` |

6·7번 보충:

- 6번은 변경 전 업로드 검색 경로에서는 드러나지 않던 잠재 결함입니다(그 경로는 `read_exif`를 부르지 않았고, Memorial은 `validate_image`가 먼저 걸러 냄). 이번에 EXIF를 연결하면서 업로드가 500으로 죽을 수 있게 되어 함께 막았습니다.
- 7번 수정은 `read_exif`를 같이 쓰는 **Memorial에도 적용됩니다.** 방향 태그가 빠진 손상 EXIF 사진은 이제 자동 위치가 잡히지 않습니다(수동 위치 수정은 그대로 가능).

## 3. 고쳐지지 않은 것

- **좌표 prior가 없는 체인점 (시나리오 8).** Places가 서버 IP 근처 지점만 5건 돌려주면 여행 국가 우선 정렬이 할 일이 없습니다. 여행지 좌표를 넓은 `locationBias`로 넘기면 풀리지만, 여행에 좌표가 저장돼 있지 않고 요청 계약에 필드를 추가해야 해서 이번 범위에서 뺐습니다. 다음 개선 1순위입니다.
- **여행 국가 표기 불일치 (시나리오 7).** "일본"과 "Japan"을 같은 나라로 보지 못합니다. Places `addressComponents.shortText`(ISO 코드) 비교로 바꾸면 됩니다.
- **같은 나라·같은 도시 안의 지점 구분 (시나리오 6, 9).** 여행 도시는 글자 폴백의 검색 범위로만 쓰고 순위에는 쓰지 않습니다. 당일치기처럼 여행 도시 밖의 장소를 찍은 사진을 밀어내지 않기 위한 선택입니다.
- **지역만 알려 주는 글자.** 주소판이나 안내판의 관리 주체(〇〇警察署 등)를 위치 prior로 쓰지 않고, 장소 이름처럼 검색합니다.
- **화면에 근거가 보이지 않음.** 후보의 `reason`("사진 속 글자 … 로 검색한 장소", "같은 이름으로 검색된 다른 장소")은 Backend까지 전달되지만 Flutter가 읽지 않습니다(`api_photo_place_repository.dart`, `category`도 빈 문자열로 고정).
- **Gemini 프롬프트 변경은 실호출로 확인하지 못했습니다.** `visible_text`를 이름일 가능성이 높은 순으로 나열하라는 한 문장을 추가했는데, 실제 출력이 그렇게 정렬되는지는 측정하지 않았습니다.

## 4. 테스트와 정적 검사

| 검사 | 변경 전 | 변경 후 |
|---|---|---|
| `ai/image_search/tests` | 167 passed (Python 3.14) | 189 passed (Python 3.11 · 3.13 · 3.14 각각) |
| Backend `pytest` | 119 passed, 6 failed | 124 passed, 6 failed |
| Backend `ruff check` | 14 errors | 14 errors (이번에 고친 파일은 0) |
| Backend `ruff format --check` | 6 files | 5 files |
| Backend `basedpyright` | 37 errors | 29 errors |
| 응답 계약 Schema | — | 변경 없음 (`test_checked_in_json_schema_matches_response_dto` 통과) |

Backend에서 실패하는 6건은 변경 전부터 실패하던 테스트이고 전후 목록이 같습니다. 모두 저장소에 커밋되지 않은 작업 파일(`backend/tests/test_photo_places_integration.py` 5건, `test_photo_places_concurrency.py` 1건)에 있으며, 여행 생성 요청 형식이 현재 API와 맞지 않아 실패합니다. 이번 작업에서 건드리지 않았습니다.

## 5. 재현 방법

레포 루트에서 실행합니다.

```bash
# 위치 비교 (변경 전 코드를 임시 폴더에 풀어 같은 시나리오를 돌린다)
mkdir -p /tmp/before && git archive fab6abd ai backend | tar -x -C /tmp/before
PYTHONPATH=/tmp/before uv run --project backend python ai/image_search/scripts/replay_scenarios.py > before.json
PYTHONPATH=.           uv run --project backend python ai/image_search/scripts/replay_scenarios.py > after.json

# 결함 2: 새 Backend 테스트를 변경 전 소스에 돌린다 (3 failed 가 나와야 한다)
cp backend/tests/test_photo_place_upload.py /tmp/before/backend/tests/
(cd /tmp/before/backend && uv run --project "$OLDPWD/backend" pytest tests/test_photo_place_upload.py)

# 변경 후 전체 테스트
PYTHONPATH=. uv run --no-project --python 3.11 --with-requirements ai/image_search/requirements.txt \
  python -m pytest ai/image_search/tests
(cd backend && uv run pytest)
```

## 6. 협업 기록

같은 작업 트리에서 세 에이전트가 역할을 나눴습니다. 저장소 수정은 Claude만 했고 나머지는 읽기 전용이었습니다.

| 에이전트 | 한 일 | 반영 |
|---|---|---|
| Claude Code | 설계·구현·테스트·문서, 변경 전·후 검증 | — |
| Codex (사전 분석) | 캐스케이드의 결함 경로를 파일·줄 단위로 정리, 개선안 검토, 계약 영향 분석 | 순위 비교에 여행지 폴백값이 아닌 Places 실제 필드를 쓸 것, 동명 후보와 근처 추천의 자리 배분 |
| Antigravity | geo-sleuth에서 옮길 수 있는 것과 없는 것 구분, 라이선스 확인, Flutter 소비 필드 조사 | 구조화 필드를 늘리지 않고 `visible_text`를 쓰기로 함. Flutter가 `reason`·`category`를 버리는 것 발견(미수정, 3절) |
| Codex (사후 리뷰) | 미커밋 변경 리뷰: P0 없음, P1 1건, P2 4건 | 아래 |

Codex 사후 리뷰 처리:

| 지적 | 처리 |
|---|---|
| P1. 위치 prior가 있어도 Places가 먼 동명 장소를 첫 결과로 주면 그대로 1순위가 됨 | 반영. prior 반경 안의 결과를 앞으로 옮기는 `_prefer_near` 추가, 테스트 3건 |
| P2. 랜드마크 시드는 동명 후보를 싣지 않음 | 유지. 랜드마크는 감지 좌표로 앵커되고, 같은 이름의 하위 지점이 근처 추천과 겹침 |
| P2. 여행 도시를 순위에 쓰지 않고 국가는 표기가 같아야 일치 | 일부 반영. 글자 폴백 검색어에 도시와 국가를 함께 붙임. 나머지는 3절에 한계로 기록 |
| P2. 방향 태그 없는 EXIF 좌표가 반대 반구로 해석될 수 있음 | 반영. 결함 7 |
| P2. 거리순 정렬과 카테고리 필터를 함께 보내는 테스트가 없음 | 반영. 테스트에 조합 추가 |

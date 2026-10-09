# 정답 좌표를 아는 실사진 묶음을 실제 API 로 돌려 "얼마나 가까이 찾는가"를 재는 스크립트
# replay_scenarios.py 가 가짜 Places 로 로직만 비교한다면, 이쪽은 실제 정확도를 잰다.
# API 키 3종(ai/image_search/.env)이 필요하고 사진 한 장마다 Vision·Gemini·Places 비용이 든다.
#
# 사용 (레포 루트에서):
#   python -m ai.image_search.scripts.evaluate_photos --manifest 사진폴더/manifest.json
#   python -m ai.image_search.scripts.evaluate_photos --manifest ... --out result.json
#
# manifest.json 은 항목의 목록이다. image_path 는 manifest 파일이 있는 폴더 기준 상대 경로.
#   [
#     {
#       "label": "기요미즈데라",                       // 사람이 읽을 이름 (선택)
#       "image_path": "kiyomizu.jpg",                  // 또는 "image_url"
#       "truth_latitude": 34.9949, "truth_longitude": 135.7850,   // 정답 좌표 (필수)
#       "city": "교토", "country": "일본",             // 여행 맥락 (선택)
#       "latitude": 34.9951, "longitude": 135.7848,    // 촬영 좌표 힌트 (선택, Backend 가 EXIF 에서 읽어 넘기는 값)
#       "trip_latitude": 35.0116, "trip_longitude": 135.7681,     // 여행 도시 좌표 (선택, Backend 가 한 번 찾아 넘기는 값)
#       "note": "야경"                                 // 사용자 메모 (선택)
#     }
#   ]
#
# 변경 전·후 비교: replay_scenarios.py 와 같은 방법으로 PYTHONPATH 만 바꿔 두 번 돌린다.
# (같은 사진이라도 Gemini 출력은 매번 조금 다를 수 있으니 한두 장 차이는 잡음으로 본다.)
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from functools import partial
from pathlib import Path

from ai.image_search.domain.search_schemas import ImageSearchRequest
from ai.image_search.services.image_loader import load_image_bytes
from ai.image_search.services.place_recognizer import PlaceRecognizer

# 정답과 같은 장소로 보는 거리. 정답 좌표와 Places 핀이 같은 부지 안에서 어긋나는 정도를 감안한다
SAME_PLACE_KM = 0.15
_REQUEST_FIELDS = (
    "image_path",
    "image_url",
    "note",
    "latitude",
    "longitude",
    "city",
    "country",
    "trip_latitude",
    "trip_longitude",
)


# 두 좌표 (위도, 경도) 사이의 거리(km).
# 변경 전 코드 트리에서도 이 파일 하나로 돌 수 있게 다른 스크립트에서 가져오지 않는다
def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat_a, lng_a, lat_b, lng_b = map(math.radians, (*a, *b))
    h = (
        math.sin((lat_b - lat_a) / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin((lng_b - lng_a) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(h))


# Places 호출 수를 세는 얇은 대리자 (비용 비교용). 세는 것 말고는 실제 provider 에 그대로 넘긴다
class CountingPlaces:
    def __init__(self, places) -> None:
        self._places = places
        self.calls = 0

    def search_text(self, *args, **kwargs):
        self.calls += 1
        return self._places.search_text(*args, **kwargs)

    # 변경 전 코드가 부르는 진입점
    def resolve_place(self, *args, **kwargs):
        self.calls += 1
        return self._places.resolve_place(*args, **kwargs)

    def search_nearby(self, *args, **kwargs):
        self.calls += 1
        return self._places.search_nearby(*args, **kwargs)


# 사진 한 장을 돌려 지표 한 줄을 만든다
def evaluate_entry(entry: dict, landmark, vision_llm, places, base_dir: Path) -> dict:
    counting = CountingPlaces(places)
    recognizer = PlaceRecognizer(
        landmark=landmark,
        vision_llm=vision_llm,
        places=counting,
        image_loader=partial(load_image_bytes, allowed_base_dir=base_dir),
    )
    request = ImageSearchRequest(
        max_candidates=5,
        **{key: entry[key] for key in _REQUEST_FIELDS if entry.get(key) is not None},
    )
    result = recognizer.search(request)

    truth = (entry["truth_latitude"], entry["truth_longitude"])
    errors_km = [
        round(distance_km((candidate.latitude, candidate.longitude), truth), 3)
        for candidate in result.candidates
    ]
    llm = result.signals.llm
    return {
        "label": entry.get("label") or entry.get("image_path") or entry.get("image_url"),
        "status": result.status.value,
        "top_candidate": result.candidates[0].name if result.candidates else None,
        "top_source": result.candidates[0].source.value if result.candidates else None,
        "top_confidence": result.candidates[0].confidence if result.candidates else None,
        "top_reason": result.candidates[0].reason if result.candidates else None,
        # 첫 후보의 나라 (여행 국가 밖의 장소를 답했는지 보기 위함)
        "top_country": result.candidates[0].country if result.candidates else None,
        "top_error_km": errors_km[0] if errors_km else None,
        "top_is_truth": bool(errors_km) and errors_km[0] <= SAME_PLACE_KM,
        # 정답에서 SAME_PLACE_KM 안에 있는 첫 후보의 순위 (없으면 None)
        "truth_rank": next(
            (rank for rank, error in enumerate(errors_km, 1) if error <= SAME_PLACE_KM),
            None,
        ),
        "candidates": [candidate.name for candidate in result.candidates],
        "candidate_errors_km": errors_km,
        "candidate_sources": [candidate.source.value for candidate in result.candidates],
        "places_calls": counting.calls,
        # 프롬프트 튜닝용 원신호: 이름 추정과, 읽은 글자가 어떤 순서로 왔는지
        "landmark": result.signals.landmark.name if result.signals.landmark else None,
        "place_name_guess": llm.place_name_guess if llm else None,
        "category": llm.category.value if llm else None,
        "visible_text": llm.visible_text if llm else [],
    }


def summarize(rows: list[dict]) -> dict:
    errors = [row["top_error_km"] for row in rows if row["top_error_km"] is not None]
    return {
        "photos": len(rows),
        "top_is_truth": sum(row["top_is_truth"] for row in rows),
        "truth_in_candidates": sum(row["truth_rank"] is not None for row in rows),
        "no_candidates": sum(row["top_candidate"] is None for row in rows),
        "median_top_error_km": round(statistics.median(errors), 3) if errors else None,
        "places_calls": sum(row["places_calls"] for row in rows),
    }


def evaluate(manifest_path: Path, landmark, vision_llm, places) -> dict:
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = [
        evaluate_entry(entry, landmark, vision_llm, places, manifest_path.resolve().parent)
        for entry in entries
    ]
    return {"summary": summarize(rows), "photos": rows}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="정답 좌표를 아는 실사진으로 장소 검색의 위치 오차를 잽니다 (유료 API 호출)."
    )
    parser.add_argument("--manifest", required=True, type=Path, help="manifest.json 경로")
    parser.add_argument("--out", type=Path, help="결과 JSON 을 저장할 경로 (없으면 표준 출력)")
    args = parser.parse_args()

    # 실 provider 는 여기서만 만든다 (키가 없으면 어떤 키가 필요한지 알려 주고 끝낸다)
    from ai.image_search.providers.landmark_provider import LandmarkProvider
    from ai.image_search.providers.places_provider import PlacesProvider
    from ai.image_search.providers.vision_llm_provider import VisionLlmProvider

    try:
        providers = (LandmarkProvider(), VisionLlmProvider(), PlacesProvider())
    except ValueError as error:
        sys.exit(f"API 키가 없어 측정할 수 없습니다: {error}")

    report = json.dumps(
        evaluate(args.manifest, *providers), ensure_ascii=False, indent=2
    )
    if args.out is not None:
        args.out.write_text(report + "\n", encoding="utf-8")
        print(json.dumps(json.loads(report)["summary"], ensure_ascii=False))
    else:
        print(report)


if __name__ == "__main__":
    main()

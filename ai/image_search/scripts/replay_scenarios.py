# 고정된 시나리오를 PlaceRecognizer 에 재생해 "식별 위치가 정답에서 얼마나 떨어졌는지"를 재는 스크립트
# 실제 API 를 부르지 않는다. Vision·Gemini 출력은 시나리오에 고정돼 있고, Places 는 아래 SimPlaces 가 흉내 낸다.
# 그래서 이 결과는 실사진 정확도가 아니라 "같은 입력에서 후보 선택 로직이 어떻게 달라졌는가"만 보여 준다.
#
# 변경 전·후 비교 (레포 루트에서):
#   git archive <기준 커밋> ai | tar -x -C /tmp/before
#   PYTHONPATH=/tmp/before python ai/image_search/scripts/replay_scenarios.py > before.json
#   PYTHONPATH=.           python ai/image_search/scripts/replay_scenarios.py > after.json
# import 되는 ai.image_search 가 어느 쪽이든 돌도록 PlaceRecognizer 의 공개 인터페이스만 쓴다.
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from ai.image_search.domain.schemas import (
    LandmarkDetection,
    PlaceCategory,
    ResolvedPlace,
    VisionIdentification,
)
from ai.image_search.domain.search_schemas import ImageSearchRequest
from ai.image_search.services.place_recognizer import PlaceRecognizer

# 정답과 같은 장소로 보는 거리 (같은 건물·부지)
SAME_PLACE_KM = 0.1
# 위치 prior 없이 검색하면 Places 는 호출 서버의 IP 위치로 편향한다(Text Search 문서).
# 배포 리전은 확인하지 못했으므로 미국 동부로 "가정"한다. 서울로 바꿔도 체인점 결과는 여전히 틀린다.
SERVER_IP_LOCATION = (39.0438, -77.4874)


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat_a, lng_a, lat_b, lng_b = map(math.radians, (*a, *b))
    h = (
        math.sin((lat_b - lat_a) / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin((lng_b - lng_a) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class SimPlace:
    pid: str
    name: str
    key: str  # 검색어와 맞춰 볼 이름 (체인점은 지점들이 같은 key 를 가진다)
    lat: float
    lng: float
    city: str
    country: str
    popularity: int = 100

    def resolved(self) -> ResolvedPlace:
        return ResolvedPlace(
            place_id=self.pid,
            name=self.name,
            latitude=self.lat,
            longitude=self.lng,
            city=self.city,
            country=self.country,
        )


# 좌표는 실제 위치 근처의 값이다. "가상"이라고 적은 곳은 동명 장소를 만들려고 넣은 것이다.
WORLD = [
    SimPlace("sensoji", "센소지", "센소지", 35.7148, 139.7967, "도쿄", "일본", 900),
    SimPlace("kinkaku", "금각사", "금각사", 35.0394, 135.7292, "교토", "일본", 800),
    SimPlace("kinkaku-la", "금각사(가상 식당)", "금각사", 34.0620, -118.3000, "로스앤젤레스", "미국"),
    SimPlace("fuji", "후지산", "후지산", 35.3606, 138.7274, "후지노미야", "일본", 950),
    SimPlace("fuji-center", "후지산 세계유산센터", "후지산 세계유산센터", 35.2256, 138.6111, "후지노미야", "일본"),
    SimPlace("bluebottle", "블루보틀 커피 교토 카페", "블루보틀 커피 교토 카페", 35.0116, 135.7860, "교토", "일본"),
    # 스타벅스: 서버(미국 동부) 근처 5곳 + 일본 3곳 + 서울 1곳
    SimPlace("sb-ashburn", "스타벅스 애슈번", "스타벅스", 39.0440, -77.4880, "애슈번", "미국"),
    SimPlace("sb-sterling", "스타벅스 스털링", "스타벅스", 39.0062, -77.4286, "스털링", "미국"),
    SimPlace("sb-reston", "스타벅스 레스턴", "스타벅스", 38.9586, -77.3570, "레스턴", "미국"),
    SimPlace("sb-herndon", "스타벅스 헌던", "스타벅스", 38.9696, -77.3861, "헌던", "미국"),
    SimPlace("sb-leesburg", "스타벅스 리스버그", "스타벅스", 39.1157, -77.5636, "리스버그", "미국"),
    SimPlace("sb-seoul", "스타벅스 명동", "스타벅스", 37.5636, 126.9850, "서울", "대한민국"),
    SimPlace("sb-shibuya", "스타벅스 시부야 츠타야", "스타벅스", 35.6595, 139.7005, "도쿄", "일본"),
    SimPlace("sb-kyoto", "스타벅스 교토 산조오하시", "스타벅스", 35.0090, 135.7716, "교토", "일본"),
    SimPlace("sb-osaka", "스타벅스 도톤보리", "스타벅스", 34.6687, 135.5013, "오사카", "일본"),
    # 이치란: 미국 2곳 + 일본 3곳
    SimPlace("ichiran-bk", "이치란 브루클린", "이치란", 40.7074, -73.9330, "뉴욕", "미국"),
    SimPlace("ichiran-mid", "이치란 미드타운", "이치란", 40.7527, -73.9857, "뉴욕", "미국"),
    SimPlace("ichiran-shibuya", "이치란 시부야", "이치란", 35.6610, 139.7010, "도쿄", "일본"),
    SimPlace("ichiran-dotonbori", "이치란 도톤보리", "이치란", 34.6690, 135.5030, "오사카", "일본"),
    SimPlace("ichiran-fukuoka", "이치란 본점", "이치란", 33.5930, 130.4040, "후쿠오카", "일본"),
    # % ARABICA: 교토 2곳 + 두바이
    SimPlace("arabica-higashiyama", "% 아라비카 교토 히가시야마", "% ARABICA", 34.9987, 135.7810, "교토", "일본"),
    SimPlace("arabica-arashiyama", "% 아라비카 교토 아라시야마", "% ARABICA", 35.0138, 135.6778, "교토", "일본"),
    SimPlace("arabica-dubai", "% 아라비카 두바이몰", "% ARABICA", 25.1972, 55.2796, "두바이", "아랍에미리트"),
    # 나라의 골목 카페들 (이름을 알 수 없는 사진용, 가상)
    SimPlace("nara-small", "골목 카페(가상)", "골목 카페", 34.68105, 135.82990, "나라", "일본", 20),
    SimPlace("nara-popular", "유명 카페(가상)", "유명 카페", 34.68190, 135.83060, "나라", "일본", 900),
    SimPlace("nara-mid", "중간 카페(가상)", "중간 카페", 34.68060, 135.82900, "나라", "일본", 300),
]


# Google Places 를 흉내 내는 가짜. 순위 규칙:
#   이름이 검색어와 정확히 같은 것 → 검색어에 도시·국가가 들어 있으면 그 지역의 것 → 가까운 것
#   "가까운"의 기준점은 location_bias 가 있으면 그 좌표, 없으면 서버 IP 위치
class SimPlaces:
    def __init__(self) -> None:
        self.calls = 0

    def search_text(
        self,
        query,
        language_code="ko",
        region_code=None,
        max_result_count=1,
        location_bias=None,
        bias_radius_m=2000,
    ):
        self.calls += 1
        wanted = query.casefold()
        origin = location_bias or SERVER_IP_LOCATION
        matches = [place for place in WORLD if place.key.casefold() in wanted]
        matches.sort(
            key=lambda place: (
                place.key.casefold() != wanted
                and not wanted.startswith(place.key.casefold() + " "),
                place.city.casefold() not in wanted
                and place.country.casefold() not in wanted,
                distance_km(origin, (place.lat, place.lng)),
            )
        )
        return [place.resolved() for place in matches[:max_result_count]]

    # 변경 전 코드가 부르는 진입점 (실제 provider 와 같이 첫 결과만, 없으면 ValueError)
    def resolve_place(self, place_name, language_code="ko", region_code=None):
        results = self.search_text(place_name)
        if not results:
            raise ValueError(f"검색 결과 없음: {place_name}")
        return results[0]

    def search_nearby(
        self,
        latitude,
        longitude,
        category=None,
        radius_m=1500,
        max_result_count=5,
        language_code="ko",
        region_code=None,
        rank_by_distance=False,
    ):
        self.calls += 1
        center = (latitude, longitude)
        nearby = [
            place
            for place in WORLD
            if distance_km(center, (place.lat, place.lng)) * 1000 <= radius_m
        ]
        if rank_by_distance:
            nearby.sort(key=lambda place: distance_km(center, (place.lat, place.lng)))
        else:
            nearby.sort(key=lambda place: -place.popularity)
        return [place.resolved() for place in nearby[:max_result_count]]


class FixedLandmark:
    def __init__(self, detection: LandmarkDetection | None) -> None:
        self._detection = detection

    def detect(self, image_bytes=None, image_url=None):
        return self._detection


class FixedVision:
    def __init__(self, identification: VisionIdentification | None) -> None:
        self._identification = identification

    def identify(self, image_bytes, mime_type="image/jpeg", note=None):
        if self._identification is None:
            raise RuntimeError("비전 추정 없음")
        return self._identification


def vision(guess, category, texts=(), confidence=0.7) -> VisionIdentification:
    return VisionIdentification(
        place_name_guess=guess,
        category=category,
        reason="시나리오 고정 입력",
        confidence=confidence,
        visible_text=list(texts),
    )


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    truth: str  # 정답 장소의 pid (없으면 "")
    landmark: LandmarkDetection | None = None
    llm: VisionIdentification | None = None
    request: dict = field(default_factory=dict)


CAFE, FOOD, TEMPLE, NATURE = (
    PlaceCategory.CAFE,
    PlaceCategory.RESTAURANT,
    PlaceCategory.TEMPLE_SHRINE,
    PlaceCategory.NATURE,
)

SCENARIOS = [
    Scenario(
        "landmark",
        "유명 랜드마크 (Vision 이 확신)",
        "sensoji",
        landmark=LandmarkDetection(name="센소지", latitude=35.7148, longitude=139.7967, score=0.93),
        llm=vision("센소지", TEMPLE),
        request={"city": "도쿄", "country": "일본"},
    ),
    Scenario(
        "landmark-romanized",
        "랜드마크 이름이 검색되지 않아 LLM 추정으로 폴백",
        "sensoji",
        landmark=LandmarkDetection(name="Sensō-ji", latitude=35.7148, longitude=139.7967, score=0.9),
        llm=vision("센소지", TEMPLE),
        request={"city": "도쿄", "country": "일본"},
    ),
    Scenario(
        "unique-name",
        "이름이 하나뿐인 가게 (LLM 추정, 위치 정보 없음)",
        "bluebottle",
        llm=vision("블루보틀 커피 교토 카페", CAFE),
        request={"city": "교토", "country": "일본"},
    ),
    Scenario(
        "landmark-namesake",
        "랜드마크와 이름이 같은 장소가 다른 나라에 있음",
        "kinkaku",
        landmark=LandmarkDetection(name="금각사", latitude=35.0395, longitude=135.7290, score=0.88),
        llm=vision("금각사", TEMPLE),
        request={"city": "교토", "country": "일본"},
    ),
    Scenario(
        "chain-with-gps",
        "체인점 + 사진에 촬영 좌표(EXIF GPS) 있음",
        "sb-kyoto",
        llm=vision("스타벅스", CAFE),
        request={"latitude": 35.0091, "longitude": 135.7718, "city": "교토", "country": "일본"},
    ),
    Scenario(
        "chain-country",
        "체인점 + 좌표 없음, 상위 결과에 여행 국가 지점이 섞여 있음",
        "ichiran-dotonbori",
        llm=vision("이치란", FOOD),
        request={"city": "오사카", "country": "일본"},
    ),
    Scenario(
        "chain-country-other-spelling",
        "위와 같지만 여행 국가를 다른 표기(Japan)로 입력",
        "ichiran-dotonbori",
        llm=vision("이치란", FOOD),
        request={"city": "오사카", "country": "Japan"},
    ),
    Scenario(
        "chain-no-prior",
        "체인점 + 좌표 없음, 상위 5건이 전부 서버 근처 지점",
        "sb-kyoto",
        llm=vision("스타벅스", CAFE),
        request={"city": "교토", "country": "일본"},
    ),
    Scenario(
        "sign-text-only",
        "이름 추정 없이 간판 글자만 읽힘 (같은 도시에 지점이 둘)",
        "arabica-arashiyama",
        llm=vision(None, CAFE, texts=["% ARABICA", "OPEN 8:00"], confidence=0.5),
        request={"city": "교토", "country": "일본"},
    ),
    Scenario(
        "sign-text-with-gps",
        "간판 글자 + 촬영 좌표 (같은 도시의 다른 지점 근처에서 촬영)",
        "arabica-arashiyama",
        llm=vision(None, CAFE, texts=["% ARABICA"], confidence=0.5),
        request={"latitude": 35.0139, "longitude": 135.6780, "city": "교토", "country": "일본"},
    ),
    Scenario(
        "gps-only",
        "이름도 글자도 없고 촬영 좌표만 있음 (음식 클로즈업)",
        "nara-small",
        llm=vision(None, CAFE, confidence=0.4),
        request={"latitude": 34.68100, "longitude": 135.82985, "city": "나라", "country": "일본"},
    ),
    Scenario(
        "distant-subject",
        "멀리서 찍은 피사체 + 촬영 좌표 (가와구치코에서 후지산)",
        "fuji",
        llm=vision("후지산", NATURE),
        request={"latitude": 35.5000, "longitude": 138.7600, "country": "일본"},
    ),
    Scenario(
        "nothing",
        "단서 없음 (이름·글자·좌표 모두 없음)",
        "",
        llm=vision(None, PlaceCategory.ETC, confidence=0.2),
        request={"city": "교토", "country": "일본"},
    ),
]


def run(scenario: Scenario) -> dict:
    places = SimPlaces()
    recognizer = PlaceRecognizer(
        landmark=FixedLandmark(scenario.landmark),
        vision_llm=FixedVision(scenario.llm),
        places=places,
        image_loader=lambda request: b"\xff\xd8\xff scenario",
    )
    result = recognizer.search(
        ImageSearchRequest(image_base64="c2NlbmFyaW8=", max_candidates=5, **scenario.request)
    )

    truth = next((place for place in WORLD if place.pid == scenario.truth), None)
    top = result.candidates[0] if result.candidates else None
    candidate_ids = [candidate.place_id for candidate in result.candidates]
    error_km = (
        round(distance_km((top.latitude, top.longitude), (truth.lat, truth.lng)), 3)
        if top is not None and truth is not None
        else None
    )
    return {
        "key": scenario.key,
        "title": scenario.title,
        "truth": truth.name if truth else None,
        "status": result.status.value,
        "identified": result.identified.name if result.identified else None,
        # 화면이 첫 후보를 대표 결과로 쓰므로, 식별이 없어도 첫 후보를 기준으로 오차를 잰다
        "top_candidate": top.name if top else None,
        "top_error_km": error_km,
        "top_is_truth": error_km is not None and error_km <= SAME_PLACE_KM,
        "truth_rank": (
            candidate_ids.index(scenario.truth) + 1
            if scenario.truth in candidate_ids
            else None
        ),
        "candidates": [candidate.name for candidate in result.candidates],
        "places_calls": places.calls,
    }


def main() -> None:
    print(json.dumps([run(scenario) for scenario in SCENARIOS], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

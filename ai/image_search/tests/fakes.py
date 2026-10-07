# 여러 테스트가 공유하는 가짜 provider 와 빌더 (Protocol 만족, 호출 기록 포함)
# 실제 provider 대신 주입해 네트워크·API 키 없이 캐스케이드·CLI 를 검증한다.
from ai.image_search.domain.schemas import (
    LandmarkDetection,
    PlaceCategory,
    ResolvedPlace,
    VisionIdentification,
)


class FakeLandmark:
    def __init__(self, result=None, raises=False):
        self.result = result
        self.raises = raises
        self.calls = []
        self.received_bytes = None

    def detect(self, image_bytes=None, image_url=None):
        self.calls.append(image_bytes)
        self.received_bytes = image_bytes
        if self.raises:
            raise RuntimeError("Cloud Vision API 요청 실패: status=403")
        return self.result


class FakeVision:
    def __init__(self, result=None, raises=False):
        self.result = result
        self.raises = raises
        self.calls = []
        self.mime_types = []

    def identify(self, image_bytes, mime_type="image/jpeg", note=None):
        self.calls.append((image_bytes, note))
        self.mime_types.append(mime_type)
        if self.raises:
            raise RuntimeError("Gemini API 오류")
        return self.result


class FakePlaces:
    # resolve_map 이 주어지면 검색어별로 조회 (없는 검색어는 결과 없음) — 시드별 성공/실패를 구분
    # resolve_map 의 값은 장소 하나 또는 리스트(동명 후보 여러 건)
    def __init__(self, resolved=None, resolve_raises=False, nearby=None,
                 nearby_raises=False, resolve_map=None):
        self.resolved = resolved
        self.resolve_raises = resolve_raises
        self.resolve_map = resolve_map
        self.nearby = nearby if nearby is not None else []
        self.nearby_raises = nearby_raises
        self.resolve_calls = []
        self.bias_calls = []
        self.nearby_calls = []

    def search_text(self, query, language_code="ko", region_code=None,
                    max_result_count=1, location_bias=None, bias_radius_m=2000):
        self.resolve_calls.append(query)
        self.bias_calls.append(location_bias)
        if self.resolve_raises:
            raise RuntimeError("Google Places 요청 실패")
        if self.resolve_map is not None:
            found = self.resolve_map.get(query, [])
            return found if isinstance(found, list) else [found]
        return [self.resolved] if self.resolved is not None else []

    # 실제 provider 와 같이 search_text 첫 결과를 돌려준다 (CLI 진단 프로브용)
    def resolve_place(self, place_name, language_code="ko", region_code=None):
        results = self.search_text(place_name)
        if not results:
            raise ValueError(f"검색 결과 없음: {place_name}")
        return results[0]

    def search_nearby(self, latitude, longitude, category=None, radius_m=1500,
                      max_result_count=5, language_code="ko", region_code="JP",
                      rank_by_distance=False):
        self.nearby_calls.append(
            {"lat": latitude, "lng": longitude,
             "category": category, "max": max_result_count,
             "radius": radius_m, "by_distance": rank_by_distance}
        )
        if self.nearby_raises:
            raise RuntimeError("근처 검색 실패")
        return self.nearby


# --- 빌더 ---
def landmark_det(name="센소지", lat=35.70, lng=139.70, score=0.9):
    return LandmarkDetection(name=name, latitude=lat, longitude=lng, score=score)


def vision_id(guess="블루보틀", cat=PlaceCategory.CAFE, conf=0.8, texts=()):
    return VisionIdentification(
        place_name_guess=guess, category=cat, reason="추정", confidence=conf,
        visible_text=list(texts),
    )


def resolved_place(name="센소지", lat=35.7148, lng=139.7967, city="Tokyo",
                   country="Japan", pid="p1", rating=4.5):
    return ResolvedPlace(place_id=pid, name=name, latitude=lat, longitude=lng,
                         city=city, country=country, rating=rating)

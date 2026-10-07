# services/place_recognizer.py 테스트 — 캐스케이드 조립 로직
# 실제 provider 대신 가짜 provider(Protocol 만족)를 주입해 분기·상태·좌표 출처를 검증
from ai.image_search.domain.schemas import PlaceCategory
from ai.image_search.domain.search_schemas import (
    CandidateSource,
    ImageSearchRequest,
    RecognitionStatus,
)
from ai.image_search.services.place_recognizer import (
    LandmarkDetector,
    PlaceRecognizer,
    PlaceRecognizerConfig,
    PlacesResolver,
    VisionIdentifier,
)
from ai.image_search.tests.fakes import (
    FakeLandmark,
    FakePlaces,
    FakeVision,
    landmark_det,
    resolved_place,
    vision_id,
)


def make_recognizer(landmark, vision, places, **cfg):
    return PlaceRecognizer(
        landmark=landmark,
        vision_llm=vision,
        places=places,
        config=PlaceRecognizerConfig(**cfg) if cfg else None,
        image_loader=lambda req: b"fake-image-bytes",
    )


def req(**kw):
    base = dict(image_url="https://x/a.jpg", max_candidates=5)
    base.update(kw)
    return ImageSearchRequest(**base)


class TestCascade:
    # 자신 있는 랜드마크(score≥임계값) → 랜드마크 채택, 근처 추천 있음 → SUCCESS
    def test_confident_landmark_success(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[resolved_place(name="근처1", pid="n1")])
        rec = make_recognizer(FakeLandmark(landmark_det(score=0.93)), FakeVision(vision_id()), places)

        result = rec.search(req())

        assert result.status is RecognitionStatus.SUCCESS
        assert result.identified.source is CandidateSource.LANDMARK
        assert result.identified.confidence == 0.93
        assert len(result.candidates) == 2  # 식별 + 근처 1
        assert result.candidates[1].source is CandidateSource.NEARBY
        # 랜드마크 이름으로 좌표를 확정하러 갔는가
        assert places.resolve_calls == ["센소지"]

    # 약한 랜드마크(score<임계값) → Gemini 추정으로 폴백
    def test_weak_landmark_falls_back_to_llm(self):
        places = FakePlaces(resolved=resolved_place(name="블루보틀"), nearby=[])
        rec = make_recognizer(
            FakeLandmark(landmark_det(name="닮은건물", score=0.4)),
            FakeVision(vision_id(guess="블루보틀 교토점")),
            places,
        )

        result = rec.search(req())

        assert result.identified.source is CandidateSource.LLM
        assert places.resolve_calls == ["블루보틀 교토점"]  # 랜드마크가 아닌 LLM 추정으로

    # 랜드마크 감지 없음 → Gemini 경로
    def test_no_landmark_uses_llm(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(FakeLandmark(None), FakeVision(vision_id(guess="어딘가")), places)

        result = rec.search(req())

        assert result.identified.source is CandidateSource.LLM

    # 랜드마크도 없고 LLM 장소명 추정도 없음 → FAILED (빈 후보)
    def test_no_seed_returns_failed(self):
        rec = make_recognizer(
            FakeLandmark(None),
            FakeVision(vision_id(guess=None)),  # 특정 못 함
            FakePlaces(resolved=resolved_place()),
        )

        result = rec.search(req())

        assert result.status is RecognitionStatus.FAILED
        assert result.identified is None
        assert result.candidates == []

    # 식별 성공 + 근처 검색 결과 없음 → PARTIAL
    def test_nearby_empty_returns_partial(self):
        rec = make_recognizer(
            FakeLandmark(landmark_det(score=0.9)),
            FakeVision(vision_id()),
            FakePlaces(resolved=resolved_place(), nearby=[]),
        )

        result = rec.search(req())

        assert result.status is RecognitionStatus.PARTIAL
        assert len(result.candidates) == 1

    # 좌표 확정(Places resolve) 실패 → FAILED (그라운딩 불가)
    def test_resolve_failure_returns_failed(self):
        rec = make_recognizer(
            FakeLandmark(landmark_det(score=0.9)),
            FakeVision(vision_id()),
            FakePlaces(resolve_raises=True),
        )

        result = rec.search(req())

        assert result.status is RecognitionStatus.FAILED
        assert result.identified is None

    # 랜드마크 이름이 Places 에서 안 잡히면 LLM 추정으로 폴백해 재시도한다
    # (예: 랜드마크가 "Sensō-ji" 로 주는데 검색은 "센소지" 로만 잡히는 경우)
    def test_landmark_resolve_failure_falls_back_to_llm_guess(self):
        places = FakePlaces(
            resolve_map={"센소지": resolved_place(name="센소지", pid="p1")},
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(landmark_det(name="Sensō-ji", score=0.9)),
            FakeVision(vision_id(guess="센소지")),
            places,
        )

        result = rec.search(req())

        assert result.identified is not None
        assert result.identified.source is CandidateSource.LLM  # 폴백된 시드
        assert result.identified.name == "센소지"
        # 랜드마크 이름 먼저 시도 → 실패 후 LLM 추정으로 재확정
        assert places.resolve_calls == ["Sensō-ji", "센소지"]

    # 랜드마크·LLM 추정 둘 다 Places 에서 못 잡으면 FAILED
    def test_both_seeds_unresolvable_returns_failed(self):
        places = FakePlaces(resolve_map={}, nearby=[])  # 아무 이름도 못 찾음
        rec = make_recognizer(
            FakeLandmark(landmark_det(name="Sensō-ji", score=0.9)),
            FakeVision(vision_id(guess="센소지")),
            places,
        )

        result = rec.search(req())

        assert result.status is RecognitionStatus.FAILED
        assert places.resolve_calls == ["Sensō-ji", "센소지"]  # 둘 다 시도

    # 랜드마크 확정 실패 + LLM 추정 없음 → 폴백 불가로 그대로 FAILED (재시도 안 함)
    def test_landmark_resolve_failure_no_llm_guess_returns_failed(self):
        places = FakePlaces(resolve_map={}, nearby=[])
        rec = make_recognizer(
            FakeLandmark(landmark_det(name="Sensō-ji", score=0.9)),
            FakeVision(vision_id(guess=None)),
            places,
        )

        result = rec.search(req())

        assert result.status is RecognitionStatus.FAILED
        assert places.resolve_calls == ["Sensō-ji"]  # LLM 추정 없어 재시도 안 함

    # 랜드마크 provider 예외 → LLM 으로 우아하게 폴백
    def test_landmark_exception_falls_back_to_llm(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(FakeLandmark(raises=True), FakeVision(vision_id(guess="카페")), places)

        result = rec.search(req())

        assert result.identified.source is CandidateSource.LLM

    # LLM 예외 + 랜드마크 성공 → 랜드마크로 진행, 근처 검색은 카테고리 필터 없이
    def test_llm_exception_proceeds_with_landmark(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[resolved_place(name="근처", pid="n1")])
        rec = make_recognizer(FakeLandmark(landmark_det(score=0.9)), FakeVision(raises=True), places)

        result = rec.search(req())

        assert result.identified.source is CandidateSource.LANDMARK
        assert places.nearby_calls[0]["category"] is None  # LLM 없으니 카테고리 미지정

    # 좌표는 항상 Places 실제값 (랜드마크가 준 좌표를 신뢰하지 않음)
    def test_coordinates_always_from_places(self):
        places = FakePlaces(resolved=resolved_place(lat=35.7148, lng=139.7967), nearby=[])
        rec = make_recognizer(
            FakeLandmark(landmark_det(lat=35.00, lng=139.00, score=0.9)),  # 다른 좌표
            FakeVision(vision_id()),
            places,
        )

        result = rec.search(req())

        assert result.identified.latitude == 35.7148  # 랜드마크(35.0)가 아닌 Places
        assert result.identified.longitude == 139.7967

    # 감지된 이미지 MIME 타입을 Gemini 에 전달한다 (PNG 를 jpeg 로 보내지 않음)
    def test_detected_mime_type_passed_to_llm(self):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"rest-of-png"
        vision = FakeVision(vision_id())
        rec = PlaceRecognizer(
            landmark=FakeLandmark(None),
            vision_llm=vision,
            places=FakePlaces(resolved=resolved_place()),
            image_loader=lambda request: png_bytes,
        )

        rec.search(req())

        assert vision.mime_types == ["image/png"]

    # 형식을 모르는 바이트는 기본값(jpeg)으로 전달한다
    def test_unknown_bytes_default_to_jpeg(self):
        vision = FakeVision(vision_id())
        rec = PlaceRecognizer(
            landmark=FakeLandmark(None),
            vision_llm=vision,
            places=FakePlaces(resolved=resolved_place()),
            image_loader=lambda request: b"not-a-real-image",
        )

        rec.search(req())

        assert vision.mime_types == ["image/jpeg"]

    # LLM 카테고리가 식별 후보·근처 후보·근처 검색 필터로 전파된다
    def test_category_propagates_to_candidates_and_nearby_filter(self):
        places = FakePlaces(
            resolved=resolved_place(), nearby=[resolved_place(name="근처", pid="n1")]
        )
        rec = make_recognizer(
            FakeLandmark(landmark_det(score=0.9)),
            FakeVision(vision_id(cat=PlaceCategory.CAFE)),
            places,
        )

        result = rec.search(req())

        assert result.identified.category is PlaceCategory.CAFE  # 식별 후보
        assert result.candidates[1].category is PlaceCategory.CAFE  # 근처 후보
        assert places.nearby_calls[0]["category"] is PlaceCategory.CAFE  # 근처 필터

    # LLM 이 없으면(랜드마크만) 카테고리 미지정 → 후보는 ETC 로 채운다
    def test_missing_llm_category_defaults_to_etc(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(
            FakeLandmark(landmark_det(score=0.9)), FakeVision(raises=True), places
        )

        result = rec.search(req())

        assert result.identified.category is PlaceCategory.ETC

    # 원신호(landmark·llm)가 결과에 보존된다
    def test_signals_preserved(self):
        rec = make_recognizer(
            FakeLandmark(landmark_det(score=0.9)),
            FakeVision(vision_id()),
            FakePlaces(resolved=resolved_place(), nearby=[]),
        )

        result = rec.search(req())

        assert result.signals.landmark is not None
        assert result.signals.llm is not None

    # max_candidates 가 근처 검색 개수를 제한한다 (식별 1 + 근처 N-1, 자기자신 제외 대비 +1)
    def test_max_candidates_limits_nearby(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(FakeLandmark(landmark_det(score=0.9)), FakeVision(vision_id()), places)

        rec.search(req(max_candidates=3))

        assert places.nearby_calls[0]["max"] == 3  # (3 - 1) + 1(자기자신 제외 대비)

    # max_candidates=1 이면 근처 검색을 아예 하지 않고 식별만 반환
    # 요청한 1개를 온전히 채웠으므로 SUCCESS (근처 부재를 PARTIAL로 오라벨하지 않음)
    def test_max_candidates_one_skips_nearby(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[resolved_place(name="근처", pid="n1")])
        rec = make_recognizer(FakeLandmark(landmark_det(score=0.9)), FakeVision(vision_id()), places)

        result = rec.search(req(max_candidates=1))

        assert places.nearby_calls == []  # 근처 검색 호출 자체를 안 함
        assert len(result.candidates) == 1
        assert result.status is RecognitionStatus.SUCCESS

    # 근처 검색(Places) 예외 → 식별만 반환 (PARTIAL, 우아한 저하)
    def test_nearby_exception_returns_partial(self):
        rec = make_recognizer(
            FakeLandmark(landmark_det(score=0.9)),
            FakeVision(vision_id()),
            FakePlaces(resolved=resolved_place(), nearby_raises=True),
        )

        result = rec.search(req())

        assert result.status is RecognitionStatus.PARTIAL
        assert len(result.candidates) == 1
        assert result.identified is not None

    # 근처 검색 결과에 식별 장소 자신이 섞여 오면 제외한다 (place_id 기준 중복 방지)
    def test_excludes_identified_place_from_nearby(self):
        nearby = [
            resolved_place(name="센소지", pid="p1"),  # 식별 장소 자신 (같은 place_id)
            resolved_place(name="근처1", pid="n1"),
        ]
        places = FakePlaces(resolved=resolved_place(pid="p1"), nearby=nearby)
        rec = make_recognizer(FakeLandmark(landmark_det(score=0.9)), FakeVision(vision_id()), places)

        result = rec.search(req())

        ids = [c.place_id for c in result.candidates]
        assert ids.count("p1") == 1  # 식별로 한 번만 등장
        assert "n1" in ids

    # 자기 자신 제외 후에도 요청 개수를 초과하지 않게 wanted 로 자른다
    def test_truncates_nearby_to_wanted_after_filter(self):
        nearby = [resolved_place(name=f"근처{i}", pid=f"n{i}") for i in range(5)]
        places = FakePlaces(resolved=resolved_place(pid="p1"), nearby=nearby)
        rec = make_recognizer(FakeLandmark(landmark_det(score=0.9)), FakeVision(vision_id()), places)

        result = rec.search(req(max_candidates=5))

        assert len(result.candidates) == 5  # 식별 1 + 근처 4 (5개 중 1개 잘림)

    # 근처 검색 요청 개수는 Places API 상한(20)을 넘지 않는다
    def test_nearby_fetch_respects_places_api_cap(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(FakeLandmark(landmark_det(score=0.9)), FakeVision(vision_id()), places)

        rec.search(req(max_candidates=25))

        assert places.nearby_calls[0]["max"] == 20  # (25-1)+1=25 → 상한 20

    # 근처 confidence 감소가 하한(0.1) 아래로는 내려가지 않는다 (음수/0 방지)
    def test_nearby_confidence_clamped_at_floor(self):
        places = FakePlaces(
            resolved=resolved_place(), nearby=[resolved_place(name="근처", pid="n1")]
        )
        rec = make_recognizer(
            FakeLandmark(None),
            FakeVision(vision_id(guess="카페", conf=0.2)),  # 낮은 base
            places,
        )

        result = rec.search(req(max_candidates=2))

        # 0.2 - 0.15 = 0.05 → 하한 0.1 로 클램프
        assert result.candidates[1].confidence == 0.1

    # seed confidence 가 하한(0.1)보다 낮아도 근처가 식별보다 높게 정렬되지 않는다
    # (식별 장소가 항상 앵커 — 근처 confidence 는 식별 이하)
    def test_nearby_never_ranks_above_identified(self):
        places = FakePlaces(
            resolved=resolved_place(), nearby=[resolved_place(name="근처", pid="n1")]
        )
        rec = make_recognizer(
            FakeLandmark(None),
            FakeVision(vision_id(guess="카페", conf=0.05)),  # 하한보다 낮은 확신도
            places,
        )

        result = rec.search(req(max_candidates=2))

        identified_conf = result.candidates[0].confidence
        nearby_conf = result.candidates[1].confidence
        assert nearby_conf <= identified_conf  # 근처가 식별을 넘지 않음

    # 근처 추천 confidence 는 식별보다 낮게 점감한다
    def test_nearby_confidence_decays(self):
        nearby = [resolved_place(name=f"근처{i}", pid=f"n{i}") for i in range(3)]
        rec = make_recognizer(
            FakeLandmark(landmark_det(score=0.9)), FakeVision(vision_id()),
            FakePlaces(resolved=resolved_place(), nearby=nearby),
        )

        result = rec.search(req())

        confs = [c.confidence for c in result.candidates]
        assert confs[0] == 0.9  # 식별
        assert confs[1] > confs[2] > confs[3]  # 근처는 순차 감소
        assert all(0 <= c <= 1 for c in confs)


# geo-sleuth 원칙 이식: 위치 prior · 동명 후보 순위화 · 읽은 글자 폴백 · 위치 힌트 주변 탐색
class TestEvidenceRanking:
    # 랜드마크는 사진에서 나온 좌표를 앵커로 삼아 그 주변으로 이름을 검색한다
    # (좌표를 그대로 쓰는 게 아니라 검색을 편향할 뿐 — 최종 좌표는 여전히 Places)
    def test_landmark_seed_searches_near_detected_coordinates(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(
            FakeLandmark(landmark_det(lat=35.70, lng=139.70, score=0.9)),
            FakeVision(vision_id()),
            places,
        )

        result = rec.search(req(latitude=37.5, longitude=127.0))  # 요청 힌트보다 사진 앵커 우선

        assert places.bias_calls == [(35.70, 139.70)]
        assert result.identified.latitude == 35.7148  # Places 값

    # LLM 추정은 요청의 위치 힌트(EXIF GPS 등)를 prior 로 쓴다
    def test_llm_seed_searches_near_request_location_hint(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(FakeLandmark(None), FakeVision(vision_id()), places)

        rec.search(req(latitude=35.0036, longitude=135.7785))

        assert places.bias_calls == [(35.0036, 135.7785)]

    # 위도·경도가 쌍으로 오지 않으면 prior 로 쓰지 않는다
    def test_half_location_hint_is_ignored(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(FakeLandmark(None), FakeVision(vision_id()), places)

        rec.search(req(latitude=35.0036))

        assert places.bias_calls == [None]

    # 좌표 prior 가 없으면 여행 국가 안의 결과를 1순위로 올리고, 밀려난 결과는 동명 후보로 남긴다
    def test_prefers_match_in_trip_country_and_keeps_namesake(self):
        places = FakePlaces(
            resolve_map={
                "이치란": [
                    resolved_place(name="이치란 뉴욕", city="뉴욕", country="미국", pid="us"),
                    resolved_place(name="이치란 시부야", city="도쿄", country="일본", pid="jp"),
                ]
            },
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(None), FakeVision(vision_id(guess="이치란", conf=0.8)), places
        )

        result = rec.search(req(country="일본"))

        assert result.identified.place_id == "jp"
        assert [c.place_id for c in result.candidates] == ["jp", "us"]
        namesake = result.candidates[1]
        assert namesake.source is CandidateSource.LLM
        assert namesake.confidence < result.identified.confidence
        assert namesake.country == "미국"  # 여행지 폴백이 아니라 Places 실제값

    # 여행 국가와 일치하는 결과가 없으면 Places 순서를 그대로 둔다
    def test_keeps_places_order_when_no_match_in_trip_country(self):
        places = FakePlaces(
            resolve_map={
                "에펠탑": [
                    resolved_place(name="에펠탑", country="프랑스", pid="fr"),
                    resolved_place(name="에펠탑 카페", country="미국", pid="us"),
                ]
            },
            nearby=[],
        )
        rec = make_recognizer(FakeLandmark(None), FakeVision(vision_id(guess="에펠탑")), places)

        result = rec.search(req(country="일본"))

        assert result.identified.place_id == "fr"

    # 좌표 prior 가 있으면 여행지(더 약한 가설)로 재정렬하지 않는다
    def test_location_prior_outranks_trip_country(self):
        places = FakePlaces(
            resolve_map={
                "블루보틀": [
                    resolved_place(name="블루보틀 성수", lat=37.5480, lng=127.0456,
                                   country="대한민국", pid="kr"),
                    resolved_place(name="블루보틀 교토", lat=35.0116, lng=135.7860,
                                   country="일본", pid="jp"),
                ]
            },
            nearby=[],
        )
        rec = make_recognizer(FakeLandmark(None), FakeVision(vision_id()), places)

        result = rec.search(req(latitude=37.54, longitude=127.05, country="일본"))

        assert result.identified.place_id == "kr"

    # locationBias 는 편향일 뿐이라 Places 가 먼 동명 장소를 첫 결과로 줄 수 있다.
    # prior 반경 안의 결과가 있으면 그것을 1순위로 올리고, 먼 결과는 동명 후보로 남긴다.
    def test_match_inside_location_prior_outranks_far_first_result(self):
        places = FakePlaces(
            resolve_map={
                "블루보틀": [
                    resolved_place(name="블루보틀 교토", lat=35.0116, lng=135.7860, pid="far"),
                    resolved_place(name="블루보틀 성수", lat=37.5480, lng=127.0456, pid="near"),
                    resolved_place(name="블루보틀 삼청", lat=37.5800, lng=126.9810, pid="mid"),
                ]
            },
            nearby=[],
        )
        rec = make_recognizer(FakeLandmark(None), FakeVision(vision_id()), places)

        result = rec.search(req(latitude=37.54, longitude=127.05))

        # 성수(약 1km)만 반경 2km 안 → 1순위. 나머지는 Places 순서 그대로
        assert [c.place_id for c in result.candidates] == ["near", "far", "mid"]

    # 랜드마크 앵커도 같다: 감지 좌표 근처의 결과가 먼 동명 장소보다 앞선다
    def test_match_near_landmark_anchor_outranks_far_namesake(self):
        places = FakePlaces(
            resolve_map={
                "금각사": [
                    resolved_place(name="금각사(다른 도시)", lat=33.59, lng=130.40, pid="far"),
                    resolved_place(name="금각사", lat=35.0394, lng=135.7292, pid="near"),
                ]
            },
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(landmark_det(name="금각사", lat=35.0395, lng=135.7290, score=0.9)),
            FakeVision(vision_id()),
            places,
        )

        result = rec.search(req())

        assert result.identified.place_id == "near"

    # 촬영 좌표가 있으면 바로 그 앞(hint_radius_m 안)의 동명 지점이 같은 동네의 다른 지점보다 앞선다
    # (실사진 측정: 지점 20m 앞에서 찍었는데 Places 는 410m 떨어진 지점을 먼저 돌려줬다)
    def test_namesake_at_capture_point_outranks_one_elsewhere_nearby(self):
        places = FakePlaces(
            resolve_map={
                "이치란": [
                    resolved_place(
                        name="이치란 나고야 사카에점", lat=35.1672, lng=136.9072, pid="sakae"
                    ),
                    resolved_place(
                        name="이치란 나고야니시키점", lat=35.1697, lng=136.9041, pid="nishiki"
                    ),
                ]
            },
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(None), FakeVision(vision_id(guess="이치란")), places
        )

        result = rec.search(req(latitude=35.16959, longitude=136.90402))

        assert [c.place_id for c in result.candidates] == ["nishiki", "sakae"]

    # prior 반경 안에 결과가 하나도 없으면 Places 순서를 그대로 둔다 (멀리서 찍은 피사체일 수 있다)
    def test_keeps_places_order_when_nothing_inside_location_prior(self):
        places = FakePlaces(
            resolve_map={
                "후지산": [
                    resolved_place(name="후지산", lat=35.3606, lng=138.7274, pid="mt"),
                    resolved_place(name="후지산 박물관", lat=35.2000, lng=138.6000, pid="museum"),
                ]
            },
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(None), FakeVision(vision_id(guess="후지산")), places
        )

        result = rec.search(req(latitude=35.5000, longitude=138.7600))  # 가와구치코 부근

        assert result.identified.place_id == "mt"

    # 동명 후보는 상한까지만 싣고, 남는 자리는 근처 추천이 채운다 (서로 중복 없이)
    def test_namesakes_capped_and_nearby_fills_the_rest(self):
        matches = [resolved_place(name=f"스타벅스 {i}", pid=f"m{i}") for i in range(4)]
        places = FakePlaces(
            resolve_map={"스타벅스": matches},
            nearby=[
                resolved_place(name="스타벅스 1", pid="m1"),  # 동명 후보와 중복 → 제외
                resolved_place(name="근처A", pid="na"),
                resolved_place(name="근처B", pid="nb"),
                resolved_place(name="근처C", pid="nc"),
            ],
        )
        rec = make_recognizer(
            FakeLandmark(None), FakeVision(vision_id(guess="스타벅스", conf=0.8)), places
        )

        result = rec.search(req(max_candidates=5))

        assert [c.place_id for c in result.candidates] == ["m0", "m1", "m2", "na", "nb"]
        assert [c.source for c in result.candidates[3:]] == [CandidateSource.NEARBY] * 2
        confs = [c.confidence for c in result.candidates]
        assert confs == sorted(confs, reverse=True)  # 뒤로 갈수록 낮아진다
        assert places.nearby_calls[0]["max"] == 5  # 근처 2 + 제외 대비 3(식별 1 + 동명 2)

    # 랜드마크는 사진 좌표로 앵커된 유일 식별이라 동명 후보를 늘어놓지 않는다
    def test_landmark_seed_lists_no_namesakes(self):
        places = FakePlaces(
            resolve_map={
                "센소지": [
                    resolved_place(pid="p1"),
                    resolved_place(name="센소지 호조몬", pid="p2"),
                ]
            },
            nearby=[resolved_place(name="근처", pid="n1")],
        )
        rec = make_recognizer(
            FakeLandmark(landmark_det(score=0.9)), FakeVision(vision_id()), places
        )

        result = rec.search(req())

        assert [c.place_id for c in result.candidates] == ["p1", "n1"]

    # 이름 추정이 없어도 읽은 간판 글자를 여행 도시 범위로 검색한다
    def test_visible_text_fallback_scoped_to_trip_city(self):
        places = FakePlaces(
            resolve_map={"% ARABICA 교토 일본": resolved_place(name="% 아라비카 교토", pid="a1")},
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(None),
            FakeVision(vision_id(guess=None, conf=0.9, texts=["% ARABICA"])),
            places,
        )

        result = rec.search(req(city="교토", country="일본"))

        # 여행지 좌표 조회(결과 없음) → 글자 + 여행지 검색
        assert places.resolve_calls == ["교토 일본", "% ARABICA 교토 일본"]
        assert result.identified.place_id == "a1"
        assert result.identified.source is CandidateSource.LLM
        assert result.identified.confidence == 0.5  # 직접 식별이 아니므로 상한 적용
        assert "% ARABICA" in result.identified.reason

    # 위치 힌트가 있으면 글자만으로 그 주변을 검색한다
    def test_visible_text_fallback_near_location_hint(self):
        places = FakePlaces(resolve_map={"% ARABICA": resolved_place(pid="a1")}, nearby=[])
        rec = make_recognizer(
            FakeLandmark(None),
            FakeVision(vision_id(guess=None, texts=["% ARABICA"])),
            places,
        )

        rec.search(req(latitude=35.0036, longitude=135.7785, city="교토"))

        assert places.resolve_calls == ["% ARABICA"]
        assert places.bias_calls == [(35.0036, 135.7785)]

    # 글자만 있고 지역을 좁힐 근거가 없으면 검색하지 않는다 (전 세계 동명 간판과 구분 불가)
    def test_visible_text_fallback_needs_a_location_scope(self):
        places = FakePlaces(resolved=resolved_place())
        rec = make_recognizer(
            FakeLandmark(None),
            FakeVision(vision_id(guess=None, texts=["% ARABICA"])),
            places,
        )

        result = rec.search(req())

        assert places.resolve_calls == []
        assert result.status is RecognitionStatus.FAILED

    # 이름 추정이 Places 에서 안 잡히면 글자로 이어서 시도한다 (가격·한 글자 등 잡음은 건너뜀)
    def test_visible_text_tried_after_unresolvable_guess_skipping_noise(self):
        places = FakePlaces(
            resolve_map={"喫茶ソワレ 교토": resolved_place(name="소와레", pid="s1")},
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(None),
            FakeVision(
                vision_id(guess="없는 이름", texts=["¥500", "茶", " 喫茶ソワレ ", "없는 이름"])
            ),
            places,
        )

        result = rec.search(req(city="교토"))

        assert places.resolve_calls == ["교토", "없는 이름", "喫茶ソワレ 교토"]
        assert result.identified.place_id == "s1"

    # 글자 검색 횟수에는 상한이 있다 (외부 호출 예산)
    def test_visible_text_queries_are_capped(self):
        places = FakePlaces(resolve_map={}, nearby=[])
        rec = make_recognizer(
            FakeLandmark(None),
            FakeVision(vision_id(guess=None, texts=["가나", "다라", "마바", "사아"])),
            places,
        )

        result = rec.search(req(city="교토"))

        assert places.resolve_calls == ["교토", "가나 교토", "다라 교토"]
        assert result.status is RecognitionStatus.FAILED

    # 이름은 못 찾았지만 위치 힌트가 있으면 그 주변 장소를 거리순으로 제시한다
    # (식별은 아니므로 identified 는 비우고 PARTIAL)
    def test_location_hint_only_offers_places_around_it(self):
        places = FakePlaces(
            nearby=[resolved_place(name="가까운 카페", pid="n1"),
                    resolved_place(name="조금 먼 카페", pid="n2")],
        )
        rec = make_recognizer(
            FakeLandmark(None),
            FakeVision(vision_id(guess=None, cat=PlaceCategory.CAFE)),
            places,
        )

        result = rec.search(req(latitude=35.0036, longitude=135.7785, max_candidates=3))

        assert result.status is RecognitionStatus.PARTIAL
        assert result.identified is None
        assert [c.place_id for c in result.candidates] == ["n1", "n2"]
        assert all(c.source is CandidateSource.NEARBY for c in result.candidates)
        assert [c.confidence for c in result.candidates] == [0.5, 0.35]
        call = places.nearby_calls[0]
        assert (call["lat"], call["lng"]) == (35.0036, 135.7785)
        assert call["by_distance"] is True
        assert call["radius"] == 200
        assert call["category"] is PlaceCategory.CAFE
        assert call["max"] == 3

    # 위치 힌트 주변에도 아무것도 없으면 FAILED
    def test_location_hint_only_without_nearby_is_failed(self):
        rec = make_recognizer(
            FakeLandmark(None), FakeVision(vision_id(guess=None)), FakePlaces(nearby=[])
        )

        result = rec.search(req(latitude=35.0036, longitude=135.7785))

        assert result.status is RecognitionStatus.FAILED
        assert result.candidates == []


# 좌표 prior(랜드마크 앵커·촬영 좌표)가 없을 때 여행 도시를 넓은 위치 prior 로 쓴다
class TestTripAreaPrior:
    KYOTO = resolved_place(name="교토", lat=35.0116, lng=135.7681, city="교토", pid="city")

    # 여행 도시 좌표를 한 번 찾아 이름 검색을 그 주변(넓은 반경)으로 편향하고,
    # 반경 안의 결과를 1순위로 올린다 — 서버 IP 근처 지점이 정답이 되는 것을 막는다
    def test_llm_seed_without_coordinates_is_biased_to_trip_city(self):
        places = FakePlaces(
            resolve_map={
                "교토 일본": self.KYOTO,
                "스타벅스": [
                    resolved_place(name="스타벅스 애슈번", lat=39.0440, lng=-77.4880,
                                   country="미국", pid="us"),
                    resolved_place(name="스타벅스 교토 산조오하시", lat=35.0090, lng=135.7716,
                                   pid="kyoto"),
                ],
            },
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(None), FakeVision(vision_id(guess="스타벅스")), places
        )

        result = rec.search(req(city="교토", country="일본"))

        assert places.resolve_calls == ["교토 일본", "스타벅스"]
        assert places.bias_calls == [None, (35.0116, 135.7681)]
        assert places.radius_calls[1] == 20000
        assert result.identified.place_id == "kyoto"
        assert [c.place_id for c in result.candidates] == ["kyoto", "us"]

    # Backend 가 여행 도시 좌표를 실어 보내면 조회 없이 그 좌표를 쓴다 (사진마다 Places 를 다시 부르지 않는다)
    def test_supplied_trip_coordinates_skip_the_lookup(self):
        places = FakePlaces(
            resolve_map={
                "스타벅스": [
                    resolved_place(name="스타벅스 애슈번", lat=39.0440, lng=-77.4880,
                                   country="미국", pid="us"),
                    resolved_place(name="스타벅스 교토 산조오하시", lat=35.0090, lng=135.7716,
                                   pid="kyoto"),
                ],
            },
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(None), FakeVision(vision_id(guess="스타벅스")), places
        )

        result = rec.search(
            req(city="교토", country="일본", trip_latitude=35.0116, trip_longitude=135.7681)
        )

        assert places.resolve_calls == ["스타벅스"]  # "교토 일본" 조회 없음
        assert places.bias_calls == [(35.0116, 135.7681)]
        assert places.radius_calls == [20000]
        assert result.identified.place_id == "kyoto"

    # 촬영 좌표가 있으면 실려 온 여행 도시 좌표보다 그것이 우선한다
    def test_location_hint_outranks_supplied_trip_coordinates(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(FakeLandmark(None), FakeVision(vision_id()), places)

        rec.search(
            req(latitude=37.54, longitude=127.05, trip_latitude=35.0116, trip_longitude=135.7681)
        )

        assert places.bias_calls == [(37.54, 127.05)]
        assert places.radius_calls == [2000]

    # 여행 도시 좌표가 반쪽만 오면 쓰지 않고 예전처럼 직접 조회한다
    def test_half_trip_coordinates_fall_back_to_lookup(self):
        places = FakePlaces(resolve_map={"교토 일본": self.KYOTO, "블루보틀": resolved_place()},
                            nearby=[])
        rec = make_recognizer(FakeLandmark(None), FakeVision(vision_id()), places)

        rec.search(req(city="교토", country="일본", trip_latitude=35.0116))

        assert places.resolve_calls == ["교토 일본", "블루보틀"]

    # 여행 도시 prior 의 반경은 도시 규모다: 인접 도시(교토에서 43km 떨어진 오사카)의 지점이
    # Places 첫 결과로 와도 여행 도시 안의 지점을 1순위로 둔다
    def test_adjacent_city_branch_does_not_outrank_trip_city_branch(self):
        places = FakePlaces(
            resolve_map={
                "교토 일본": self.KYOTO,
                "스타벅스": [
                    resolved_place(name="스타벅스 도톤보리", lat=34.6687, lng=135.5013,
                                   city="오사카", pid="osaka"),
                    resolved_place(name="스타벅스 교토 산조오하시", lat=35.0090, lng=135.7716,
                                   city="교토", pid="kyoto"),
                ],
            },
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(None), FakeVision(vision_id(guess="스타벅스")), places
        )

        result = rec.search(req(city="교토", country="일본"))

        assert [c.place_id for c in result.candidates] == ["kyoto", "osaka"]

    # 여행 국가 표기가 Places 와 달라도(Japan ≠ 일본) 좌표로 비교하므로 영향이 없다
    def test_trip_area_prior_does_not_depend_on_country_spelling(self):
        places = FakePlaces(
            resolve_map={
                "오사카 Japan": resolved_place(name="오사카", lat=34.6937, lng=135.5023, pid="city"),
                "이치란": [
                    resolved_place(name="이치란 미드타운", lat=40.7527, lng=-73.9857,
                                   country="미국", pid="ny"),
                    resolved_place(name="이치란 시부야", lat=35.6610, lng=139.7010,
                                   country="일본", pid="tokyo"),
                    resolved_place(name="이치란 도톤보리", lat=34.6690, lng=135.5030,
                                   country="일본", pid="osaka"),
                ],
            },
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(None), FakeVision(vision_id(guess="이치란")), places
        )

        result = rec.search(req(city="오사카", country="Japan"))

        assert result.identified.place_id == "osaka"

    # 촬영 좌표가 있으면 그것이 prior 다 — 여행지 좌표는 찾지 않는다 (호출 절약)
    def test_location_hint_skips_trip_area_lookup(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(FakeLandmark(None), FakeVision(vision_id()), places)

        rec.search(req(latitude=35.0036, longitude=135.7785, city="교토", country="일본"))

        assert places.resolve_calls == ["블루보틀"]
        assert places.radius_calls == [2000]

    # 랜드마크 앵커로 확정되면 여행지 좌표는 찾지 않는다
    def test_landmark_anchor_skips_trip_area_lookup(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(
            FakeLandmark(landmark_det(score=0.9)), FakeVision(vision_id()), places
        )

        rec.search(req(city="도쿄", country="일본"))

        assert places.resolve_calls == ["센소지"]

    # 여행 도시가 없으면(국가만) 좌표 prior 를 만들지 않고 국가 우선 정렬로 남는다
    def test_country_only_trip_has_no_area_prior(self):
        places = FakePlaces(resolved=resolved_place(), nearby=[])
        rec = make_recognizer(FakeLandmark(None), FakeVision(vision_id()), places)

        rec.search(req(country="일본"))

        assert places.resolve_calls == ["블루보틀"]
        assert places.bias_calls == [None]

    # 여행지 좌표를 못 찾으면(결과 없음·오류) 국가 우선 정렬로 저하한다
    def test_unresolvable_trip_city_falls_back_to_country_preference(self):
        places = FakePlaces(
            resolve_map={
                "이치란": [
                    resolved_place(name="이치란 뉴욕", country="미국", pid="us"),
                    resolved_place(name="이치란 시부야", country="일본", pid="jp"),
                ]
            },
            nearby=[],
        )
        rec = make_recognizer(
            FakeLandmark(None), FakeVision(vision_id(guess="이치란")), places
        )

        result = rec.search(req(city="어디에도 없는 도시", country="일본"))

        assert places.bias_calls == [None, None]
        assert result.identified.place_id == "jp"

    # 단서가 하나도 없으면 여행지 좌표도 찾지 않는다 (쓸 곳이 없는 호출)
    def test_no_seed_means_no_trip_area_lookup(self):
        places = FakePlaces(resolve_map={"교토 일본": self.KYOTO})
        rec = make_recognizer(FakeLandmark(None), FakeVision(vision_id(guess=None)), places)

        result = rec.search(req(city="교토", country="일본"))

        assert places.resolve_calls == []
        assert result.status is RecognitionStatus.FAILED


# 선택적인 Places 호출(여행지 좌표 조회·글자 폴백)은 시간 예산이 남았을 때만 한다
class TestOptionalCallBudget:
    def test_slow_request_skips_optional_places_calls(self, monkeypatch):
        from types import SimpleNamespace

        from ai.image_search.services import place_recognizer as module

        ticks = iter([0.0] + [61.0] * 20)  # 시작 시각 이후로는 이미 예산을 넘긴 상태
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
        places = FakePlaces(
            resolve_map={"교토 일본": resolved_place(name="교토", pid="city")}, nearby=[]
        )
        rec = make_recognizer(
            FakeLandmark(None),
            FakeVision(vision_id(guess="없는 이름", texts=["% ARABICA"])),
            places,
        )

        result = rec.search(req(city="교토", country="일본"))

        # 필수 호출(이름 추정 검색)만 하고, 여행지 좌표 조회와 글자 폴백은 건너뛴다
        assert places.resolve_calls == ["없는 이름"]
        assert result.status is RecognitionStatus.FAILED


# 동명 후보 개수는 배포 환경변수로 조절한다 (0 이면 끔)
class TestAlternativesSetting:
    def test_env_can_turn_namesakes_off(self, monkeypatch):
        monkeypatch.setenv("IMAGE_SEARCH_MAX_ALTERNATIVES", "0")
        matches = [resolved_place(name=f"스타벅스 {i}", pid=f"m{i}") for i in range(3)]
        places = FakePlaces(
            resolve_map={"스타벅스": matches},
            nearby=[resolved_place(name="근처A", pid="na")],
        )
        rec = PlaceRecognizer(
            landmark=FakeLandmark(None),
            vision_llm=FakeVision(vision_id(guess="스타벅스")),
            places=places,
            image_loader=lambda request: b"fake-image-bytes",
        )

        result = rec.search(req())

        assert [c.place_id for c in result.candidates] == ["m0", "na"]

    def test_default_keeps_two_namesakes(self, monkeypatch):
        monkeypatch.delenv("IMAGE_SEARCH_MAX_ALTERNATIVES", raising=False)

        assert PlaceRecognizerConfig().max_alternatives == 2

    def test_negative_value_is_treated_as_off(self, monkeypatch):
        monkeypatch.setenv("IMAGE_SEARCH_MAX_ALTERNATIVES", "-3")

        assert PlaceRecognizerConfig().max_alternatives == 0


class TestProtocolConformance:
    # 가짜 provider 가 주입 Protocol 을 만족한다 (테스트가 의존하는 인터페이스 드리프트 방지)
    def test_fakes_satisfy_protocols(self):
        assert isinstance(FakeLandmark(), LandmarkDetector)
        assert isinstance(FakeVision(), VisionIdentifier)
        assert isinstance(FakePlaces(), PlacesResolver)

    # 실제 provider 도 Protocol 을 만족한다 (구현이 인터페이스에서 어긋나지 않게)
    def test_real_providers_satisfy_protocols(self):
        from ai.image_search.providers.landmark_provider import LandmarkProvider
        from ai.image_search.providers.places_provider import PlacesProvider
        from ai.image_search.providers.vision_llm_provider import VisionLlmProvider

        assert isinstance(LandmarkProvider(api_key="x"), LandmarkDetector)
        assert isinstance(PlacesProvider(api_key="x"), PlacesResolver)
        assert isinstance(
            VisionLlmProvider(api_key="x", client=object()), VisionIdentifier
        )

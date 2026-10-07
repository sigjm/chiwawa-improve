# 사진 → 장소 후보를 만드는 전체 UseCase를 조합하는 Application Service
# 세 provider(랜드마크·Gemini·Places)를 캐스케이드로 엮는다.
# 원칙: 사진 이해는 AI가, 좌표·사실은 항상 Google Places 실제값으로.
# 후보를 고르는 규칙(위치 prior·동명 후보·읽은 글자 폴백)은 geo-sleuth 의 원칙을 옮긴 것이다
# (출처와 대응 관계는 ai/image_search/README.md "증거 기반 후보" 절).
import math
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import NamedTuple, Protocol, runtime_checkable

from ai.image_search.domain.schemas import (
    LandmarkDetection,
    PlaceCategory,
    ResolvedPlace,
    VisionIdentification,
)
from ai.image_search.domain.search_schemas import (
    CandidateSource,
    ImageSearchRequest,
    ImageSearchResult,
    PlaceCandidate,
    RecognitionSignals,
    RecognitionStatus,
)
from ai.image_search.services.image_loader import (
    detect_image_mime_type,
    load_image_bytes,
)


# --- provider 인터페이스 (구체 구현에 직접 의존하지 않도록 Protocol 로 주입) ---
# runtime_checkable: 실제/가짜 provider 가 인터페이스를 만족하는지 테스트에서 isinstance 로 검증
@runtime_checkable
class LandmarkDetector(Protocol):
    def detect(
        self, image_bytes: bytes | None = None, image_url: str | None = None
    ) -> LandmarkDetection | None: ...


@runtime_checkable
class VisionIdentifier(Protocol):
    def identify(
        self, image_bytes: bytes, mime_type: str = "image/jpeg", note: str | None = None
    ) -> VisionIdentification: ...


@runtime_checkable
class PlacesResolver(Protocol):
    def search_text(
        self,
        query: str,
        language_code: str = "ko",
        region_code: str | None = None,
        max_result_count: int = 1,
        location_bias: tuple[float, float] | None = None,
        bias_radius_m: float = 2000,
        rank_by_distance: bool = False,
    ) -> list[ResolvedPlace]: ...

    def search_nearby(
        self,
        latitude: float,
        longitude: float,
        *,
        category: PlaceCategory | None = None,
        radius_m: float,
        max_result_count: int,
        language_code: str = "ko",
        region_code: str | None = None,
        rank_by_distance: bool = False,
    ) -> list[ResolvedPlace]: ...


# 동명 후보 상한의 기본값. 코드 수정 없이 배포 환경에서 조절하거나 끌 수 있게(0) 환경변수로 받는다
def _max_alternatives_from_env() -> int:
    return max(0, int(os.environ.get("IMAGE_SEARCH_MAX_ALTERNATIVES", "2")))


# 캐스케이드 동작 설정 (step 7 실호출 검증 후 튜닝 가능)
@dataclass(frozen=True)
class PlaceRecognizerConfig:
    landmark_score_threshold: float = (
        0.6  # 이 값 이상이면 랜드마크 채택, 미만이면 LLM 폴백
    )
    nearby_radius_m: float = 1500  # 근처 검색 반경
    nearby_confidence_decay: float = 0.15  # 근처 후보 confidence 순차 감소폭
    # 식별 뒤에 싣는 동명 후보 상한 (남는 자리는 근처 추천). 0 이면 동명 후보를 싣지 않는다
    max_alternatives: int = field(default_factory=_max_alternatives_from_env)
    # 위치 prior 반경: 이름 검색을 이 안으로 편향하고, 이 안의 결과를 1순위로 올린다
    prior_radius_m: float = 2000
    # 좌표 prior 가 없을 때 여행 도시를 대신 쓰는 반경. 도시 규모로 잡는다:
    # 더 넓히면 인접 도시(교토↔오사카 43km)의 동명 지점이 같은 우선 그룹에 들어온다
    trip_radius_m: float = 20000
    # 촬영 지점 "바로 앞"으로 보는 반경: 그 안의 결과가 최우선이고, 이름을 못 찾으면 이 안을 뒤진다
    hint_radius_m: float = 200
    # 이름을 직접 식별하지 못한 후보(글자 검색·위치 힌트 주변)의 confidence 상한
    weak_evidence_confidence: float = 0.5


# 두 좌표 (위도, 경도) 사이의 거리(m). 반경 안팎 판정용이라 구면 근사(haversine)로 충분하다
def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat_a, lng_a, lat_b, lng_b = map(math.radians, (*a, *b))
    h = (
        math.sin((lat_b - lat_a) / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin((lng_b - lng_a) / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


# 위치 prior: 이 좌표 주변(반경)으로 검색을 편향하고, 반경 안의 결과를 1순위로 올린다
# close_m: 중심이 촬영 지점일 때 "바로 그 앞"으로 보는 거리. 이 안의 결과는 반경 안의 다른 결과보다도 앞선다
class _Prior(NamedTuple):
    center: tuple[float, float]
    radius_m: float
    close_m: float = 0


# 이름 검색 한 번의 재료: 검색어 + 근거(confidence·출처·사유) + 위치 prior
class _Seed(NamedTuple):
    query: str
    confidence: float
    source: CandidateSource
    reason: str
    near: _Prior | None


class PlaceRecognizer:
    # Places searchNearby 의 결과 개수 상한
    _NEARBY_API_MAX = 20
    # 이름 하나로 받아 보는 Places 결과 수 (동명 후보를 가려내기 위함)
    _TEXT_SEARCH_RESULTS = 5
    # 글자 폴백 검색 횟수 상한 (visible_text 는 이름일 가능성이 높은 순으로 온다 — 프롬프트 참고)
    _MAX_TEXT_QUERIES = 2
    # 선택적인 Places 호출(여행지 좌표 조회·글자 폴백)을 시작해도 되는 경과 시간(초).
    # 외부 호출이 전부 timeout 나도 Modal 한도(120s) 안에 끝내기 위함이다: 이 시각 이후로는
    # 필수 호출(이름 검색 2회 + 근처 1회, 각 10s)만 남아 최악 100s — 이 기능을 넣기 전과 같다.
    _OPTIONAL_BUDGET_S = 60

    # provider 3종을 Protocol 로 주입 (테스트에서 가짜 provider, 실제 사용 시 구체 provider)
    # image_loader: 이미지 로딩 함수 주입용 (테스트에서 가짜, 실제 사용 시 load_image_bytes)
    def __init__(
        self,
        landmark: LandmarkDetector,
        vision_llm: VisionIdentifier,
        places: PlacesResolver,
        config: PlaceRecognizerConfig | None = None,
        image_loader: Callable[[ImageSearchRequest], bytes] | None = None,
    ) -> None:
        self.landmark = landmark
        self.vision_llm = vision_llm
        self.places = places
        self.config = config or PlaceRecognizerConfig()
        self._load = image_loader or load_image_bytes

    # 사진 요청을 받아 식별 + 근처 추천 결과를 만든다
    def search(self, request: ImageSearchRequest) -> ImageSearchResult:
        started = time.monotonic()
        image_bytes = self._load(request)

        # 두 식별기 실행 (각각 우아하게 저하 — 하나 실패해도 다른 하나로 진행)
        # 이미지 형식을 감지해 Gemini 에 올바른 MIME 타입을 넘긴다
        mime_type = detect_image_mime_type(image_bytes)
        landmark = self._safe_detect(image_bytes)
        llm = self._safe_identify(image_bytes, mime_type, request.note)
        signals = RecognitionSignals(landmark=landmark, llm=llm)

        # 카테고리는 LLM 이 제공 (근처 추천 필터에 사용) — LLM 없으면 미지정
        category = llm.category if llm is not None else None
        hint = self._location_hint(request)

        # 캐스케이드: 근거가 강한 시드부터 Places 에 물어, 처음 답이 나오는 시드에서 멈춘다
        # 좌표는 항상 Places 로 재확정 (랜드마크가 준 좌표도 신뢰하지 않음, 환각 차단)
        seed: _Seed | None = None
        matches: list[ResolvedPlace] = []
        for seed in self._seeds(landmark, llm, request, hint, started):
            matches = self._safe_search(seed)
            if matches:
                break
        if seed is None or not matches:
            return self._around_hint(hint, category, request, signals)

        # Places 순서를 그대로 믿지 않고 가진 증거로 1순위를 정한다: 위치 prior 가 있으면
        # 그 반경 안의 결과를, 없으면 여행 국가 안의 결과를 앞으로.
        matches = (
            self._prefer_near(self._with_closest(seed, matches, started), seed.near)
            if seed.near is not None
            else self._prefer_trip_country(matches, request.country)
        )
        resolved = matches[0]
        # 촬영 좌표 없이 고른 체인 지점은 어느 지점인지 가릴 근거가 없다: 확신을 낮추고 그렇게 적는다
        if hint is None and self._has_branch_elsewhere(seed, resolved, matches[1:]):
            seed = seed._replace(
                confidence=min(seed.confidence, self.config.weak_evidence_confidence),
                reason=f"{seed.reason} (사진에 촬영 위치가 없어 같은 이름의 다른 지점일 수 있음)",
            )
        identified = self._to_candidate(
            resolved, seed.confidence, seed.reason, seed.source, category, request
        )

        # 식별 1건을 뺀 자리를 동명 후보 → 근처 추천 순으로 채운다
        budget = max(0, request.max_candidates - 1)
        alternatives = self._build_alternatives(
            seed, matches[1:], budget, category, request
        )
        nearby_wanted = budget - len(alternatives)
        nearby_candidates = self._build_nearby(
            resolved,
            category,
            nearby_wanted,
            seed.confidence,
            request,
            taken=[identified, *alternatives],
        )

        candidates = [identified, *alternatives, *nearby_candidates]
        # 근처 추천을 얻었으면 SUCCESS. 못 얻었어도 애초에 근처를 요청하지 않았다면
        # (max_candidates=1) 요청을 온전히 채운 것이므로 SUCCESS.
        # 근처를 원했는데 하나도 못 얻은 경우(없거나 실패)만 PARTIAL.
        status = (
            RecognitionStatus.SUCCESS
            if nearby_candidates or nearby_wanted == 0
            else RecognitionStatus.PARTIAL
        )
        return ImageSearchResult(
            identified=identified,
            candidates=candidates,
            status=status,
            signals=signals,
        )

    # 요청의 위치 힌트(EXIF GPS 등). 위도·경도가 쌍으로 있을 때만 쓴다
    @staticmethod
    def _location_hint(request: ImageSearchRequest) -> tuple[float, float] | None:
        if request.latitude is None or request.longitude is None:
            return None
        return request.latitude, request.longitude

    # 이름 검색 시드를 근거가 강한 순으로 낸다: 랜드마크 → LLM 추정 → 읽은 글자
    # 랜드마크는 사진에서 나온 좌표를 앵커로 쓴다. 나머지는 요청의 위치 힌트를,
    # 그것도 없으면 여행 도시를 넓은 prior 로 쓴다 (_soft_prior).
    def _seeds(
        self,
        landmark: LandmarkDetection | None,
        llm: VisionIdentification | None,
        request: ImageSearchRequest,
        hint: tuple[float, float] | None,
        started: float,
    ) -> Iterator[_Seed]:
        name_seed = self._pick_seed(landmark, llm)
        if (
            name_seed is not None
            and name_seed[2] is CandidateSource.LANDMARK
            and landmark is not None
        ):
            anchor = (landmark.latitude, landmark.longitude)
            yield _Seed(*name_seed, _Prior(anchor, self.config.prior_radius_m))
            # 랜드마크 이름이 검색되지 않으면 LLM 추정으로 한 번 더 시도 (우아한 저하)
            name_seed = self._llm_fallback_seed(name_seed[2], name_seed[0], llm)

        # 여기부터는 사진에서 나온 앵커가 없는 시드다. 쓸 시드가 있을 때만 prior 를 구한다
        text_seeds = self._text_seeds(llm, request, hint)
        if name_seed is None and not text_seeds:
            return
        prior = self._soft_prior(request, hint, started)
        if name_seed is not None:
            yield _Seed(*name_seed, prior)
        for text_seed in text_seeds:
            if self._out_of_time(started):
                return
            yield _Seed(*text_seed, prior)

    # 앵커가 없는 시드가 쓸 위치 prior: 촬영 좌표(힌트)가 있으면 그것을 좁은 반경으로,
    # 없으면 여행 도시의 좌표를 도시 규모 반경으로 쓴다. 여행 도시 좌표는 요청에 실려 오면
    # 그것을, 아니면 Places 에서 한 번 찾아 쓴다.
    # prior 없이 이름만 검색하면 Places 가 호출 서버의 IP 위치로 편향하기 때문이다.
    # 국가만 있으면 만들지 않는다 (나라의 중심점 주변 20km 는 의미가 없다 → 국가 우선 정렬로 남긴다).
    # 조회한 첫 결과를 그 도시로 믿는다 (도시가 아닌 장소가 오는 경우는 걸러내지 않는다).
    def _soft_prior(
        self,
        request: ImageSearchRequest,
        hint: tuple[float, float] | None,
        started: float,
    ) -> _Prior | None:
        if hint is not None:
            return _Prior(hint, self.config.prior_radius_m, self.config.hint_radius_m)
        if request.trip_latitude is not None and request.trip_longitude is not None:
            return _Prior(
                (request.trip_latitude, request.trip_longitude),
                self.config.trip_radius_m,
            )
        if not request.city or self._out_of_time(started):
            return None
        # 호출자가 좌표를 주지 않은 경우의 폴백: 요청마다 Places 를 한 번 더 부른다.
        # (Backend 는 원격 호출일 때 도시별로 한 번만 찾아 trip_latitude/longitude 로 넘긴다.)
        try:
            found = self.places.search_text(self._scope(request), max_result_count=1)
        except Exception:
            return None
        if not found:
            return None
        return _Prior((found[0].latitude, found[0].longitude), self.config.trip_radius_m)

    # 여행지를 가리키는 검색어 조각 ("도시 국가")
    @staticmethod
    def _scope(request: ImageSearchRequest) -> str:
        return " ".join(part for part in (request.city, request.country) if part)

    # 선택적인 Places 호출을 더 시작해도 되는지 (_OPTIONAL_BUDGET_S 참고)
    def _out_of_time(self, started: float) -> bool:
        return time.monotonic() - started >= self._OPTIONAL_BUDGET_S

    # 캐스케이드 판단: (seed_name, confidence, source, reason) 또는 None
    def _pick_seed(
        self, landmark: LandmarkDetection | None, llm: VisionIdentification | None
    ) -> tuple[str, float, CandidateSource, str] | None:
        if (
            landmark is not None
            and landmark.score >= self.config.landmark_score_threshold
        ):
            reason = f"랜드마크 감지 결과와 일치 (신뢰도 {landmark.score:.2f})"
            return landmark.name, landmark.score, CandidateSource.LANDMARK, reason
        if llm is not None and llm.place_name_guess:
            return llm.place_name_guess, llm.confidence, CandidateSource.LLM, llm.reason
        return None

    # 랜드마크 시드의 Places 확정이 실패했을 때 쓸 LLM 대체 시드
    # (랜드마크 시드였고, LLM 추정이 있으며, 방금 실패한 이름과 다를 때만)
    def _llm_fallback_seed(
        self,
        source: CandidateSource,
        failed_name: str,
        llm: VisionIdentification | None,
    ) -> tuple[str, float, CandidateSource, str] | None:
        if source is not CandidateSource.LANDMARK:
            return None
        if llm is None or not llm.place_name_guess:
            return None
        if llm.place_name_guess == failed_name:
            return None
        return llm.place_name_guess, llm.confidence, CandidateSource.LLM, llm.reason

    # 읽은 간판 글자를 검색어로 쓰는 폴백 시드 (이름 추정이 없거나 검색되지 않을 때)
    # 글자만으로는 전 세계의 같은 간판과 구분되지 않으므로, 위치 힌트나 여행지로
    # 범위를 좁힐 수 있을 때만 검색한다. 가격·시간·한 글자 같은 잡음은 건너뛴다.
    # 반환: (검색어, confidence, 출처, 사유) 목록 — 위치 prior 는 _seeds 가 붙인다
    def _text_seeds(
        self,
        llm: VisionIdentification | None,
        request: ImageSearchRequest,
        hint: tuple[float, float] | None,
    ) -> list[tuple[str, float, CandidateSource, str]]:
        scope = self._scope(request)
        if llm is None or (hint is None and not scope):
            return []
        confidence = min(llm.confidence, self.config.weak_evidence_confidence)
        texts = [
            text
            for text in dict.fromkeys(raw.strip() for raw in llm.visible_text)
            if len(text) > 1
            and any(map(str.isalpha, text))
            and text != llm.place_name_guess
        ]
        return [
            (
                text if hint is not None else f"{text} {scope}",
                confidence,
                CandidateSource.LLM,
                f"사진 속 글자 '{text}'(으)로 검색한 장소",
            )
            for text in texts[: self._MAX_TEXT_QUERIES]
        ]

    # 위치 prior 반경 안의 결과를 앞으로 옮긴다 (그룹 안에서는 Places 순서 유지)
    # locationBias 는 제한이 아니라 편향이라 먼 동명 장소가 첫 결과로 올 수 있다.
    # 반경 안에 결과가 없으면(멀리서 찍은 피사체일 수 있다) 순서는 그대로.
    # 촬영 지점 바로 앞(close_m 안)의 결과는 그보다도 앞에 둔다: 실사진 측정에서 Places 는
    # 촬영 지점 20m 앞의 지점보다 같은 동네(410m)의 동명 지점을 먼저 돌려줬다.
    # 바로 앞에 첫 결과와 같은 종류의 동명 장소(체인 지점)가 여럿이면 그들끼리만 가까운 순으로
    # 자리를 바꾼다 (역 앞 편의점 3곳 중 179m 지점이 먼저 왔고, 같은 요청에도 순서가 달라졌다).
    # 다른 종류의 결과는 제 자리에 둔다 (골목 이름이 들어간 가게가 골목 자체를 밀어내지 않게).
    # 첫 결과의 종류를 모르면 같은 종류라고 말할 수 없으므로 건드리지 않는다.
    @staticmethod
    def _prefer_near(matches: list[ResolvedPlace], near: _Prior) -> list[ResolvedPlace]:
        def distance(place: ResolvedPlace) -> float:
            return _distance_m(near.center, (place.latitude, place.longitude))

        ordered = sorted(
            matches,
            key=lambda place: (distance(place) >= near.close_m, distance(place) > near.radius_m),
        )
        kind = ordered[0].primary_type if ordered else None
        if kind is None:
            return ordered
        slots = [
            index
            for index, place in enumerate(ordered)
            if place.primary_type == kind and distance(place) < near.close_m
        ]
        for slot, place in zip(slots, sorted((ordered[i] for i in slots), key=distance)):
            ordered[slot] = place
        return ordered

    # 촬영 좌표가 있는데 그 바로 앞에서 찾은 결과가 없으면 가까운 순으로 한 번 더 묻고,
    # 바로 앞에서 찾은 것만 앞에 붙인다 (없으면 처음 결과 그대로 — 멀리서 찍은 피사체).
    # 실사진 측정: 지점이 촘촘한 체인은 관련도순 5건에 바로 앞 지점이 빠지고,
    # 이름이 똑같은 먼 장소 하나만 돌아오기도 했다.
    def _with_closest(
        self, seed: _Seed, matches: list[ResolvedPlace], started: float
    ) -> list[ResolvedPlace]:
        near = seed.near
        if near is None or near.close_m <= 0 or self._out_of_time(started):
            return matches

        def is_close(place: ResolvedPlace) -> bool:
            return (
                _distance_m(near.center, (place.latitude, place.longitude)) < near.close_m
            )

        if any(is_close(place) for place in matches):
            return matches
        closest: dict[str, ResolvedPlace] = {}
        for place in self._safe_search(seed, rank_by_distance=True):
            if is_close(place):
                closest.setdefault(place.place_id, place)
        return [*closest.values(), *(p for p in matches if p.place_id not in closest)]

    # 같은 종류의 동명 장소가 식별한 곳에서 떨어져 있으면 "다른 지점"이 있는 것이다.
    # 같은 부지 안의 하위 장소(본당 등)는 지점이 아니므로 hint_radius_m 안은 세지 않는다.
    def _has_branch_elsewhere(
        self, seed: _Seed, resolved: ResolvedPlace, others: list[ResolvedPlace]
    ) -> bool:
        if seed.source is CandidateSource.LANDMARK or resolved.primary_type is None:
            return False
        here = (resolved.latitude, resolved.longitude)
        return any(
            other.primary_type == resolved.primary_type
            and _distance_m(here, (other.latitude, other.longitude))
            > self.config.hint_radius_m
            for other in others
        )

    # 좌표 prior 가 없을 때 여행 국가 안의 결과를 앞으로 옮긴다 (그룹 안에서는 Places 순서 유지)
    # 비교 대상은 요청값으로 채우기 전의 Places 실제 국가다. 일치하는 결과가 없으면 순서는 그대로.
    @staticmethod
    def _prefer_trip_country(
        matches: list[ResolvedPlace], country: str | None
    ) -> list[ResolvedPlace]:
        if not country:
            return matches
        # ponytail: 표기가 같을 때만 일치로 본다("일본" ≠ "Japan"). 표기가 섞이기 시작하면
        # Places addressComponents 의 shortText(ISO 코드)로 비교하도록 바꾼다.
        wanted = country.strip().casefold()
        return sorted(
            matches,
            key=lambda place: (place.country or "").strip().casefold() != wanted,
        )

    # 같은 이름으로 검색된 다른 장소를 후보로 남긴다 (식별보다 낮은 confidence)
    # 랜드마크는 사진 좌표로 앵커된 식별이라 동명 후보를 늘어놓지 않는다
    def _build_alternatives(
        self,
        seed: _Seed,
        others: list[ResolvedPlace],
        budget: int,
        category: PlaceCategory | None,
        request: ImageSearchRequest,
    ) -> list[PlaceCandidate]:
        if seed.source is CandidateSource.LANDMARK:
            return []
        return [
            self._to_candidate(
                place,
                self._decay(seed.confidence, index),
                "같은 이름으로 검색된 다른 장소",
                seed.source,
                category,
                request,
            )
            for index, place in enumerate(
                others[: min(budget, self.config.max_alternatives)]
            )
        ]

    # 근처 후보 리스트 생성 (confidence 순차 감소)
    # 검색 중심이 식별 장소 좌표라 이미 실은 후보(taken)가 섞여 올 수 있어,
    # 그만큼 더 요청한 뒤 place_id 로 제외하고 wanted 개수로 자른다
    def _build_nearby(
        self,
        resolved: ResolvedPlace,
        category: PlaceCategory | None,
        wanted: int,
        base_conf: float,
        request: ImageSearchRequest,
        taken: list[PlaceCandidate],
    ) -> list[PlaceCandidate]:
        if wanted <= 0:
            return []
        fetch_count = min(wanted + len(taken), self._NEARBY_API_MAX)
        nearby_places = self._safe_nearby(
            (resolved.latitude, resolved.longitude),
            category,
            fetch_count,
            self.config.nearby_radius_m,
        )
        taken_ids = {candidate.place_id for candidate in taken}
        deduped = [
            place for place in nearby_places if place.place_id not in taken_ids
        ][:wanted]
        # 동명 후보 다음 순번부터 이어서 감소시킨다 (taken 의 첫 항목은 식별)
        offset = len(taken) - 1
        return [
            self._to_candidate(
                place,
                self._decay(base_conf, offset + index),
                "근처의 비슷한 장소",
                CandidateSource.NEARBY,
                category,
                request,
            )
            for index, place in enumerate(deduped)
        ]

    # 이름은 끝내 못 찾았지만 위치 힌트가 있으면, 그 주변 장소를 가까운 순으로 제시한다
    # (인기순은 "여기서 찍었다"의 증거가 아니다). 식별은 아니므로 identified 는 비운다.
    def _around_hint(
        self,
        hint: tuple[float, float] | None,
        category: PlaceCategory | None,
        request: ImageSearchRequest,
        signals: RecognitionSignals,
    ) -> ImageSearchResult:
        if hint is None:
            return self._failed(signals)
        places = self._safe_nearby(
            hint,
            category,
            min(request.max_candidates, self._NEARBY_API_MAX),
            self.config.hint_radius_m,
            rank_by_distance=True,
        )
        if not places:
            return self._failed(signals)
        candidates = [
            self._to_candidate(
                place,
                # 첫 후보가 상한값에서 시작해 순차 감소
                self._decay(self.config.weak_evidence_confidence, index - 1),
                "사진 위치 정보 주변의 장소",
                CandidateSource.NEARBY,
                category,
                request,
            )
            for index, place in enumerate(places)
        ]
        return ImageSearchResult(
            identified=None,
            candidates=candidates,
            status=RecognitionStatus.PARTIAL,
            signals=signals,
        )

    # ResolvedPlace → PlaceCandidate 매핑 (도시/국가는 Places → 요청 힌트 순으로 채움)
    @staticmethod
    def _to_candidate(
        place: ResolvedPlace,
        confidence: float,
        reason: str,
        source: CandidateSource,
        category: PlaceCategory | None,
        request: ImageSearchRequest,
    ) -> PlaceCandidate:
        return PlaceCandidate(
            name=place.name,
            city=place.city or request.city or "",
            country=place.country or request.country or "",
            latitude=place.latitude,
            longitude=place.longitude,
            confidence=confidence,
            reason=reason,
            category=category or PlaceCategory.ETC,
            source=source,
            place_id=place.place_id,
            rating=place.rating,
        )

    # 근처 후보 confidence 를 순위 기반으로 감소 (0.1 하한, 단 식별을 넘지 않음)
    # 식별 confidence 가 하한보다 낮은 경우에도 근처가 식별 위로 올라가지 않게 클램프한다
    def _decay(self, base_conf: float, index: int) -> float:
        value = base_conf - self.config.nearby_confidence_decay * (index + 1)
        return round(min(base_conf, max(0.1, value)), 2)

    # 식별 실패 시 결과 (원신호는 보존)
    @staticmethod
    def _failed(signals: RecognitionSignals) -> ImageSearchResult:
        return ImageSearchResult(
            identified=None,
            candidates=[],
            status=RecognitionStatus.FAILED,
            signals=signals,
        )

    # --- 각 외부 호출을 우아하게 저하 (예외 → None/빈 리스트) ---
    def _safe_detect(self, image_bytes: bytes) -> LandmarkDetection | None:
        try:
            return self.landmark.detect(image_bytes=image_bytes)
        except Exception:
            return None

    def _safe_identify(
        self, image_bytes: bytes, mime_type: str, note: str | None
    ) -> VisionIdentification | None:
        try:
            return self.vision_llm.identify(
                image_bytes=image_bytes, mime_type=mime_type, note=note
            )
        except Exception:
            return None

    # 시드 하나를 Places 이름 검색으로 확정한다 (결과 없음·실패 모두 빈 리스트 → 다음 시드로)
    def _safe_search(
        self, seed: _Seed, rank_by_distance: bool = False
    ) -> list[ResolvedPlace]:
        near = seed.near
        try:
            return self.places.search_text(
                seed.query,
                max_result_count=self._TEXT_SEARCH_RESULTS,
                location_bias=near.center if near else None,
                bias_radius_m=near.radius_m if near else self.config.prior_radius_m,
                rank_by_distance=rank_by_distance,
            )
        except Exception:
            return []

    def _safe_nearby(
        self,
        center: tuple[float, float],
        category: PlaceCategory | None,
        wanted: int,
        radius_m: float,
        rank_by_distance: bool = False,
    ) -> list[ResolvedPlace]:
        try:
            return self.places.search_nearby(
                latitude=center[0],
                longitude=center[1],
                category=category,
                radius_m=radius_m,
                max_result_count=wanted,
                rank_by_distance=rank_by_distance,
            )
        except Exception:
            return []

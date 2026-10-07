# 브라우저 이미지 업로드와 AI 사진 분석 계약 통합 테스트
from __future__ import annotations

import asyncio
import base64
import io
import struct
import zlib
from http import HTTPStatus

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import ExifTags, Image

from chiwawa_backend.dependencies import get_current_user_id, get_trip_area_resolver
from chiwawa_backend.errors import UpstreamServiceError
from chiwawa_backend.main import create_app
from chiwawa_backend.schemas.image_search import (
    ImageSearchCandidate,
    ImageSearchRequest,
    ImageSearchResult,
)
from chiwawa_backend.schemas.place_search import PlaceSearchCandidateRead
from chiwawa_backend.schemas.places import PhotoPlaceSearchResponse
from chiwawa_backend.schemas.trips import TripRead
from chiwawa_backend.services import exif as exif_service
from chiwawa_backend.services.google_place_search import GooglePlaceSearchProvider
from chiwawa_backend.services.trip_area import TripAreaResolver

_FAKE_PNG = b"\x89PNG\r\n\x1a\nphoto"


class CapturingRecognizer:
    def __init__(self, result: ImageSearchResult | None = None) -> None:
        self.request: ImageSearchRequest | None = None
        self._result: ImageSearchResult = result or ImageSearchResult(
            identified=None,
            status="FAILED",
        )

    def search(self, request: ImageSearchRequest) -> ImageSearchResult:
        self.request = request
        return self._result


class CountingPlaceSearchProvider:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(
        self,
        *,
        query: str,
        city_bias: str | None,
    ) -> tuple[PlaceSearchCandidateRead, ...]:
        _ = city_bias
        self.queries.append(query)
        return (
            PlaceSearchCandidateRead(
                provider_place_id="city-kyoto",
                name="교토",
                formatted_address="일본 교토부 교토시",
                latitude=35.0116,
                longitude=135.7681,
            ),
        )


def _jpeg_with_gps(
    latitude_ref: str | None = "N",
    longitude_ref: str | None = "E",
) -> bytes:
    exif = Image.Exif()
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    if latitude_ref is not None:
        gps[ExifTags.GPS.GPSLatitudeRef] = latitude_ref
    gps[ExifTags.GPS.GPSLatitude] = (35.0, 0.0, 12.96)
    if longitude_ref is not None:
        gps[ExifTags.GPS.GPSLongitudeRef] = longitude_ref
    gps[ExifTags.GPS.GPSLongitude] = (135.0, 46.0, 42.6)
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, "JPEG", exif=exif)
    return buffer.getvalue()


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data)
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def _oversized_png() -> bytes:
    # 헤더만 20000x20000 을 선언하는 PNG (Pillow 가 압축 폭탄으로 거부하는 크기)
    header = struct.pack(">IIBBBBB", 20000, 20000, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", b"")
        + _png_chunk(b"IEND", b"")
    )


async def _upload(
    recognizer: CapturingRecognizer,
    image_bytes: bytes,
    trip_area: TripAreaResolver | None = None,
) -> int:
    app = create_app(photo_place_recognizer=recognizer)
    app.dependency_overrides[get_current_user_id] = lambda: 1
    if trip_area is not None:
        app.dependency_overrides[get_trip_area_resolver] = lambda: trip_area

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        trip_response = await client.post(
            "/api/v1/trips",
            json={
                "city": "교토",
                "country": "일본",
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
            },
        )
        trip_id = TripRead.model_validate(trip_response.json()).id
        response = await client.post(
            f"/api/v1/trips/{trip_id}/photo-places/search-upload",
            files={"file": ("photo.jpg", image_bytes, "image/jpeg")},
        )
    return response.status_code


@pytest.mark.anyio
async def test_photo_upload_passes_exif_gps_as_location_hint() -> None:
    recognizer = CapturingRecognizer()

    status_code = await _upload(recognizer, _jpeg_with_gps())

    assert status_code == HTTPStatus.CREATED
    assert recognizer.request is not None
    assert recognizer.request.latitude == pytest.approx(35.0036)
    assert recognizer.request.longitude == pytest.approx(135.7785)


@pytest.mark.anyio
async def test_photo_upload_keeps_southern_and_western_hemisphere_signs() -> None:
    recognizer = CapturingRecognizer()

    status_code = await _upload(recognizer, _jpeg_with_gps("S", "W"))

    assert status_code == HTTPStatus.CREATED
    assert recognizer.request is not None
    assert recognizer.request.latitude == pytest.approx(-35.0036)
    assert recognizer.request.longitude == pytest.approx(-135.7785)


@pytest.mark.anyio
async def test_photo_upload_drops_exif_coordinate_without_direction() -> None:
    # 방향(N/S) 태그가 없으면 반구를 알 수 없다. 북반구로 가정하면 지구 반대편이
    # 위치 힌트가 되므로 좌표를 버린다 (경도만으로는 힌트가 되지 않는다).
    recognizer = CapturingRecognizer()

    status_code = await _upload(recognizer, _jpeg_with_gps(latitude_ref=None))

    assert status_code == HTTPStatus.CREATED
    assert recognizer.request is not None
    assert recognizer.request.latitude is None
    assert recognizer.request.longitude == pytest.approx(135.7785)


@pytest.mark.anyio
async def test_photo_upload_passes_cached_trip_city_coordinates() -> None:
    # 촬영 좌표가 없는 사진은 여행 도시 좌표를 함께 넘긴다.
    # 같은 도시는 한 번만 조회하고 재사용한다 (사진마다 Places 를 다시 부르지 않는다).
    provider = CountingPlaceSearchProvider()
    trip_area = TripAreaResolver(lambda: provider)
    first, second = CapturingRecognizer(), CapturingRecognizer()

    _ = await _upload(first, b"\x89PNG\r\n\x1a\nphoto", trip_area)
    _ = await _upload(second, b"\x89PNG\r\n\x1a\nphoto", trip_area)

    assert provider.queries == ["교토 일본"]
    for recognizer in (first, second):
        assert recognizer.request is not None
        assert recognizer.request.trip_latitude == pytest.approx(35.0116)
        assert recognizer.request.trip_longitude == pytest.approx(135.7681)


@pytest.mark.anyio
async def test_photo_upload_with_exif_gps_skips_trip_city_lookup() -> None:
    provider = CountingPlaceSearchProvider()
    recognizer = CapturingRecognizer()

    _ = await _upload(
        recognizer,
        _jpeg_with_gps(),
        TripAreaResolver(lambda: provider),
    )

    assert provider.queries == []
    assert recognizer.request is not None
    assert recognizer.request.trip_latitude is None


@pytest.mark.anyio
async def test_photo_upload_does_not_look_up_trip_city_by_default() -> None:
    # 기본 구성(in-process 인식기)에서는 Backend 가 여행지 좌표를 따로 조회하지 않는다.
    recognizer = CapturingRecognizer()

    status_code = await _upload(recognizer, b"\x89PNG\r\n\x1a\nphoto")

    assert status_code == HTTPStatus.CREATED
    assert recognizer.request is not None
    assert recognizer.request.trip_latitude is None
    assert recognizer.request.trip_longitude is None


@pytest.mark.anyio
async def test_trip_area_resolver_degrades_without_caching_failures() -> None:
    class FailingProvider:
        calls: int = 0

        async def search(
            self,
            *,
            query: str,
            city_bias: str | None,
        ) -> tuple[PlaceSearchCandidateRead, ...]:
            _ = (query, city_bias)
            self.calls += 1
            message = "Google 장소 검색 요청에 실패했습니다."
            raise UpstreamServiceError(message)

    failing = FailingProvider()
    resolver = TripAreaResolver(lambda: failing)

    assert await resolver.resolve("교토", "일본") is None
    assert await resolver.resolve("교토", "일본") is None
    assert failing.calls == 2  # 일시적 실패는 기억하지 않고 다음에 다시 시도한다
    assert await resolver.resolve(None, "일본") is None  # 도시가 없으면 조회하지 않는다
    assert failing.calls == 2
    assert await TripAreaResolver(None).resolve("교토", "일본") is None


@pytest.mark.anyio
async def test_injected_recognizer_app_never_calls_the_real_place_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 개발 PC 의 .env 에 원격 URL 과 실제 Maps 키가 있어도,
    # 인식기를 주입해 만든 앱(테스트)은 여행 도시 좌표를
    # 실제 Places 로 찾지 않아야 한다.
    monkeypatch.setenv("IMAGE_SEARCH_URL", "https://example.invalid/search")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "real-looking-key")

    async def fail_if_called(
        self: GooglePlaceSearchProvider,
        *,
        query: str,
        city_bias: str | None,
    ) -> tuple[PlaceSearchCandidateRead, ...]:
        _ = (self, query, city_bias)
        message = "테스트가 실제 Google Places provider 를 호출했습니다."
        raise AssertionError(message)

    monkeypatch.setattr(GooglePlaceSearchProvider, "search", fail_if_called)
    get_trip_area_resolver.cache_clear()
    recognizer = CapturingRecognizer()
    try:
        status_code = await _upload(recognizer, b"\x89PNG\r\n\x1a\nphoto")
    finally:
        get_trip_area_resolver.cache_clear()

    assert status_code == HTTPStatus.CREATED
    assert recognizer.request is not None
    assert recognizer.request.trip_latitude is None


@pytest.mark.anyio
async def test_app_looks_up_trip_city_with_the_injected_place_provider() -> None:
    provider = CountingPlaceSearchProvider()
    recognizer = CapturingRecognizer()
    app = create_app(
        photo_place_recognizer=recognizer,
        place_search_provider=provider,
    )
    app.dependency_overrides[get_current_user_id] = lambda: 1

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        trip_response = await client.post(
            "/api/v1/trips",
            json={
                "city": "교토",
                "country": "일본",
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
            },
        )
        trip_id = TripRead.model_validate(trip_response.json()).id
        _ = await client.post(
            f"/api/v1/trips/{trip_id}/photo-places/search-upload",
            files={"file": ("photo.png", _FAKE_PNG, "image/png")},
        )

    assert provider.queries == ["교토 일본"]
    assert recognizer.request is not None
    assert recognizer.request.trip_latitude == pytest.approx(35.0116)


@pytest.mark.anyio
async def test_trip_area_resolver_looks_up_a_city_once_under_concurrency() -> None:
    # 같은 도시의 첫 요청이 동시에 여러 개 와도 유료 조회는 한 번만 한다.
    counting = CountingPlaceSearchProvider()

    class SlowProvider:
        async def search(
            self,
            *,
            query: str,
            city_bias: str | None,
        ) -> tuple[PlaceSearchCandidateRead, ...]:
            await asyncio.sleep(0.01)
            return await counting.search(query=query, city_bias=city_bias)

    provider = counting
    resolver = TripAreaResolver(SlowProvider)

    areas = await asyncio.gather(
        resolver.resolve("교토", "일본"),
        resolver.resolve("교토", "일본"),
        resolver.resolve("교토", "일본"),
    )

    assert provider.queries == ["교토 일본"]
    assert areas == [(35.0116, 135.7681)] * 3


@pytest.mark.anyio
async def test_photo_upload_requires_authentication() -> None:
    recognizer = CapturingRecognizer()
    app = create_app(photo_place_recognizer=recognizer)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trips/any-trip/photo-places/search-upload",
            files={"file": ("photo.png", b"\x89PNG\r\n\x1a\nphoto", "image/png")},
        )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert recognizer.request is None


@pytest.mark.anyio
async def test_photo_upload_returns_candidate_category() -> None:
    candidate = ImageSearchCandidate(
        name="% 아라비카 교토 히가시야마",
        city="교토",
        country="일본",
        latitude=34.9987,
        longitude=135.7810,
        confidence=0.8,
        reason="간판의 상호가 뚜렷함",
        category="CAFE",
        source="LLM",
        place_id="place-1",
    )
    recognizer = CapturingRecognizer(
        ImageSearchResult(
            identified=candidate,
            candidates=[candidate],
            status="SUCCESS",
        ),
    )
    app = create_app(photo_place_recognizer=recognizer)
    app.dependency_overrides[get_current_user_id] = lambda: 1

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        trip_response = await client.post(
            "/api/v1/trips",
            json={
                "city": "교토",
                "country": "일본",
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
            },
        )
        trip_id = TripRead.model_validate(trip_response.json()).id
        response = await client.post(
            f"/api/v1/trips/{trip_id}/photo-places/search-upload",
            files={"file": ("cafe.png", b"\x89PNG\r\n\x1a\nphoto", "image/png")},
        )

    search = PhotoPlaceSearchResponse.model_validate(response.json())
    assert search.candidates[0].category == "CAFE"
    assert search.candidates[0].reason == "간판의 상호가 뚜렷함"


@pytest.mark.anyio
async def test_large_photo_is_shrunk_for_recognition_but_keeps_gps_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 원본(EXIF 포함)에서 촬영 좌표를 읽은 뒤, 인식 서비스로는 줄인 JPEG 를 보낸다.
    monkeypatch.setattr(exif_service, "SHRINK_ABOVE_BYTES", 512)
    monkeypatch.setattr(exif_service, "SHRINK_MAX_SIDE", 64)
    exif = Image.Exif()
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[ExifTags.GPS.GPSLatitudeRef] = "N"
    gps[ExifTags.GPS.GPSLatitude] = (35.0, 0.0, 12.96)
    gps[ExifTags.GPS.GPSLongitudeRef] = "E"
    gps[ExifTags.GPS.GPSLongitude] = (135.0, 46.0, 42.6)
    buffer = io.BytesIO()
    Image.effect_noise((400, 200), 80).convert("RGB").save(
        buffer,
        "JPEG",
        quality=95,
        exif=exif,
    )
    original = buffer.getvalue()
    recognizer = CapturingRecognizer()

    status_code = await _upload(recognizer, original)

    assert status_code == HTTPStatus.CREATED
    assert recognizer.request is not None
    sent = base64.b64decode(recognizer.request.image_base64 or "")
    assert len(sent) < len(original)
    assert recognizer.request.image_mime_type == "image/jpeg"
    with Image.open(io.BytesIO(sent)) as shrunk:
        assert max(shrunk.size) == 64
    assert recognizer.request.latitude == pytest.approx(35.0036)


def test_shrink_applies_exif_orientation_to_the_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 다시 인코딩하면 EXIF 가 사라지므로,
    # 눕혀 저장된 사진은 픽셀을 세워서 내보내야 한다.
    monkeypatch.setattr(exif_service, "SHRINK_ABOVE_BYTES", 512)
    monkeypatch.setattr(exif_service, "SHRINK_MAX_SIDE", 64)
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6  # 시계 방향 90도 회전해서 봐야 하는 사진
    buffer = io.BytesIO()
    Image.effect_noise((400, 200), 80).convert("RGB").save(
        buffer,
        "JPEG",
        quality=95,
        exif=exif,
    )

    shrunk = exif_service.shrink_large_image(buffer.getvalue())

    assert shrunk is not None
    with Image.open(io.BytesIO(shrunk)) as image:
        assert image.size == (32, 64)


def test_shrink_skips_huge_non_jpeg_images_instead_of_decoding_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # JPEG 가 아닌 형식은 전체 픽셀을 풀어야 줄일 수 있다.
    # 픽셀 수가 상한을 넘으면 메모리를 쓰지 않고 원본을 그대로 보내게 한다.
    monkeypatch.setattr(exif_service, "SHRINK_ABOVE_BYTES", 512)
    monkeypatch.setattr(exif_service, "SHRINK_MAX_DECODED_PIXELS", 100 * 100)
    buffer = io.BytesIO()
    Image.effect_noise((200, 200), 80).save(buffer, "PNG")
    assert len(buffer.getvalue()) > 512

    assert exif_service.shrink_large_image(buffer.getvalue()) is None


@pytest.mark.anyio
async def test_photo_upload_without_exif_sends_no_location_hint() -> None:
    recognizer = CapturingRecognizer()
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(buffer, "PNG")

    status_code = await _upload(recognizer, buffer.getvalue())

    assert status_code == HTTPStatus.CREATED
    assert recognizer.request is not None
    assert recognizer.request.latitude is None
    assert recognizer.request.longitude is None


@pytest.mark.anyio
async def test_photo_upload_survives_image_pillow_refuses_to_open() -> None:
    recognizer = CapturingRecognizer()

    status_code = await _upload(recognizer, _oversized_png())

    assert status_code == HTTPStatus.CREATED
    assert recognizer.request is not None
    assert recognizer.request.latitude is None


@pytest.mark.anyio
async def test_photo_upload_reaches_recognizer_as_base64() -> None:
    recognizer = CapturingRecognizer()
    app = create_app(photo_place_recognizer=recognizer)
    app.dependency_overrides[get_current_user_id] = lambda: 1

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        trip_response = await client.post(
            "/api/v1/trips",
            json={
                "city": "Paris",
                "country": "France",
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
            },
        )
        trip_id = TripRead.model_validate(trip_response.json()).id
        image_bytes = b"\x89PNG\r\n\x1a\nphoto"

        response = await client.post(
            f"/api/v1/trips/{trip_id}/photo-places/search-upload",
            files={"file": ("eiffel.png", image_bytes, "image/png")},
        )

    assert response.status_code == HTTPStatus.CREATED
    assert recognizer.request is not None
    assert recognizer.request.image_mime_type == "image/png"
    assert base64.b64decode(recognizer.request.image_base64 or "") == image_bytes


@pytest.mark.anyio
async def test_photo_upload_omits_optional_empty_city_from_ai_contract() -> None:
    recognizer = CapturingRecognizer()
    app = create_app(photo_place_recognizer=recognizer)
    app.dependency_overrides[get_current_user_id] = lambda: 1

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        trip_response = await client.post(
            "/api/v1/trips",
            json={
                "country": "France",
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
            },
        )
        trip_id = TripRead.model_validate(trip_response.json()).id
        response = await client.post(
            f"/api/v1/trips/{trip_id}/photo-places/search-upload",
            files={"file": ("eiffel.png", b"\x89PNG\r\n\x1a\nphoto", "image/png")},
        )

    assert response.status_code == HTTPStatus.CREATED
    assert recognizer.request is not None
    assert recognizer.request.city is None
    assert recognizer.request.country == "France"


@pytest.mark.anyio
async def test_photo_upload_rejects_non_image_file() -> None:
    recognizer = CapturingRecognizer()
    app = create_app(photo_place_recognizer=recognizer)
    app.dependency_overrides[get_current_user_id] = lambda: 1

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        trip_response = await client.post(
            "/api/v1/trips",
            json={
                "city": "Paris",
                "country": "France",
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
            },
        )
        trip_id = TripRead.model_validate(trip_response.json()).id
        response = await client.post(
            f"/api/v1/trips/{trip_id}/photo-places/search-upload",
            files={"file": ("note.txt", b"not-image", "text/plain")},
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert recognizer.request is None

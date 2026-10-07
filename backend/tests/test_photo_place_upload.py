# 브라우저 이미지 업로드와 AI 사진 분석 계약 통합 테스트
from __future__ import annotations

import base64
import io
import struct
import zlib
from http import HTTPStatus

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import ExifTags, Image

from chiwawa_backend.dependencies import get_current_user_id
from chiwawa_backend.main import create_app
from chiwawa_backend.schemas.image_search import ImageSearchRequest, ImageSearchResult
from chiwawa_backend.schemas.trips import TripRead


class CapturingRecognizer:
    def __init__(self) -> None:
        self.request: ImageSearchRequest | None = None

    def search(self, request: ImageSearchRequest) -> ImageSearchResult:
        self.request = request
        return ImageSearchResult(identified=None, status="FAILED")


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


async def _upload(recognizer: CapturingRecognizer, image_bytes: bytes) -> int:
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

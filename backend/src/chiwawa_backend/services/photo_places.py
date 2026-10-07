from __future__ import annotations

import base64
import inspect
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from ai.image_search.services.image_loader import ImageLoadError
from fastapi.concurrency import run_in_threadpool

from chiwawa_backend.errors import DomainValidationError, NotFoundError
from chiwawa_backend.schemas.base import PlaceSource
from chiwawa_backend.schemas.image_search import ImageSearchRequest, ImageSearchResult
from chiwawa_backend.schemas.places import (
    ConfirmedPhotoPlaceRead,
    PhotoPlaceCandidateRead,
    PhotoPlaceConfirmRequest,
    PhotoPlaceSearchRequest,
    PhotoPlaceSearchResponse,
    WantedPlaceCreateRequest,
)
from chiwawa_backend.services.common import require_photo_search, require_trip
from chiwawa_backend.services.exif import read_exif, shrink_large_image
from chiwawa_backend.services.wanted_places import create_wanted_place
from chiwawa_backend.state import AppState, synchronized

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from chiwawa_backend.schemas.trips import TripRead

# ponytail: 앱 로깅 설정이 없어 uvicorn 의 로거를 빌린다.
# (기본 설정에서도 콘솔에 찍힌다.)
# 앱 로깅을 구성하게 되면 __name__ 로거로 바꾼다.
_log = logging.getLogger("uvicorn.error")

PHOTO_SEARCH_MAX_CANDIDATES = 5
PHOTO_UPLOAD_MAX_BYTES = 20 * 1024 * 1024
INVALID_UPLOAD_TYPE_MESSAGE = "이미지 파일만 업로드할 수 있습니다."
EMPTY_UPLOAD_MESSAGE = "선택한 이미지 파일이 비어 있습니다."
UPLOAD_TOO_LARGE_MESSAGE = "이미지는 20MB 이하만 업로드할 수 있습니다."


class PhotoPlaceRecognizer(Protocol):
    def search(
        self,
        request: ImageSearchRequest,
    ) -> ImageSearchResult | Awaitable[ImageSearchResult]: ...


class TripAreaLookup(Protocol):
    async def resolve(
        self,
        city: str | None,
        country: str | None,
    ) -> tuple[float, float] | None: ...


@dataclass(frozen=True, slots=True)
class PhotoPlaceSearchContext:
    trip_id: str
    payload: PhotoPlaceSearchRequest
    recognizer: PhotoPlaceRecognizer
    trip_area: TripAreaLookup | None = None


@dataclass(frozen=True, slots=True)
class PhotoPlaceUploadContext:
    trip_id: str
    image_bytes: bytes
    content_type: str
    recognizer: PhotoPlaceRecognizer
    trip_area: TripAreaLookup | None = None


async def search_uploaded_photo_places(
    state: AppState,
    context: PhotoPlaceUploadContext,
) -> PhotoPlaceSearchResponse:
    if not context.content_type.startswith("image/"):
        raise DomainValidationError(INVALID_UPLOAD_TYPE_MESSAGE)
    if not context.image_bytes:
        raise DomainValidationError(EMPTY_UPLOAD_MESSAGE)
    if len(context.image_bytes) > PHOTO_UPLOAD_MAX_BYTES:
        raise DomainValidationError(UPLOAD_TOO_LARGE_MESSAGE)

    with state.lock:
        trip = require_trip(state, context.trip_id)
    # 촬영 위치(EXIF GPS)가 남아 있으면 장소 검색의 위치 힌트로 넘긴다.
    # SNS 저장본·스크린샷은 대개 지워져 있어 None 이 흔하다.
    exif = read_exif(context.image_bytes)
    has_capture_location = exif.latitude is not None and exif.longitude is not None
    # 같은 도시 안의 지점 구분은 촬영 좌표에 달려 있다. 기기·브라우저가 좌표를
    # 지우고 올리는지 서버에서 바로 볼 수 있게 남긴다 (좌표 값은 남기지 않는다).
    _log.info(
        "사진 업로드: 촬영 좌표 %s (%s, %dKB)",
        "있음" if has_capture_location else "없음",
        context.content_type,
        len(context.image_bytes) // 1024,
    )
    trip_latitude, trip_longitude = await _trip_coordinates(
        None if has_capture_location else context.trip_area,
        trip,
    )
    # 좌표는 원본에서 읽었으니, 인식 서비스로는 큰 사진을 줄여 보낸다.
    shrunk = await run_in_threadpool(shrink_large_image, context.image_bytes)
    image_bytes = context.image_bytes if shrunk is None else shrunk
    request = ImageSearchRequest(
        image_base64=base64.b64encode(image_bytes).decode("ascii"),
        image_mime_type=context.content_type if shrunk is None else "image/jpeg",
        latitude=exif.latitude,
        longitude=exif.longitude,
        trip_latitude=trip_latitude,
        trip_longitude=trip_longitude,
        city=trip.city or None,
        country=trip.country or None,
        max_candidates=PHOTO_SEARCH_MAX_CANDIDATES,
    )
    result = await _search_recognizer(context.recognizer, request)
    return _store_photo_search_result(state, context.trip_id, result)


async def search_photo_places(
    state: AppState,
    context: PhotoPlaceSearchContext,
) -> PhotoPlaceSearchResponse:
    with state.lock:
        trip = require_trip(state, context.trip_id)

    has_capture_location = (
        context.payload.latitude is not None and context.payload.longitude is not None
    )
    trip_latitude, trip_longitude = await _trip_coordinates(
        None if has_capture_location else context.trip_area,
        trip,
    )
    request = ImageSearchRequest(
        image_url=context.payload.image_url,
        note=context.payload.note,
        latitude=context.payload.latitude,
        longitude=context.payload.longitude,
        trip_latitude=trip_latitude,
        trip_longitude=trip_longitude,
        city=trip.city or None,
        country=trip.country or None,
        max_candidates=PHOTO_SEARCH_MAX_CANDIDATES,
    )
    try:
        result = await _search_recognizer(context.recognizer, request)
    except ImageLoadError as error:
        raise DomainValidationError(str(error)) from error

    return _store_photo_search_result(state, context.trip_id, result)


# 여행 도시의 좌표 (촬영 좌표가 없는 사진의 위치 prior).
# 조회기가 없거나 못 찾으면 둘 다 None.
async def _trip_coordinates(
    trip_area: TripAreaLookup | None,
    trip: TripRead,
) -> tuple[float | None, float | None]:
    if trip_area is None:
        return None, None
    area = await trip_area.resolve(trip.city, trip.country)
    return area if area is not None else (None, None)


def _store_photo_search_result(
    state: AppState,
    trip_id: str,
    result: ImageSearchResult,
) -> PhotoPlaceSearchResponse:
    with state.lock:
        trip = require_trip(state, trip_id)
        candidates = [
            PhotoPlaceCandidateRead(
                id=state.next_id("candidate"),
                provider_place_id=candidate.place_id,
                name=candidate.name,
                city=candidate.city or trip.city,
                country=candidate.country or trip.country,
                latitude=candidate.latitude,
                longitude=candidate.longitude,
                confidence=candidate.confidence,
                reason=candidate.reason,
                category=candidate.category,
            )
            for candidate in result.candidates
        ]
        search = PhotoPlaceSearchResponse(
            id=state.next_id("photo_search"),
            trip_id=trip_id,
            candidates=candidates,
        )
        state.photo_searches[search.id] = search
    return search


async def _search_recognizer(
    recognizer: PhotoPlaceRecognizer,
    request: ImageSearchRequest,
) -> ImageSearchResult:
    search = recognizer.search
    if inspect.iscoroutinefunction(search):
        async_search = cast(
            "Callable[[ImageSearchRequest], Awaitable[ImageSearchResult]]",
            search,
        )
        return await async_search(request)
    sync_search = cast(
        "Callable[[ImageSearchRequest], ImageSearchResult]",
        search,
    )
    return await run_in_threadpool(sync_search, request)


@synchronized
def confirm_photo_place(
    state: AppState,
    trip_id: str,
    search_id: str,
    payload: PhotoPlaceConfirmRequest,
) -> ConfirmedPhotoPlaceRead:
    search = require_photo_search(state, trip_id, search_id)
    candidate = next(
        (item for item in search.candidates if item.id == payload.candidate_id),
        None,
    )
    if candidate is None:
        raise NotFoundError(entity="photo_candidate", entity_id=payload.candidate_id)
    existing = state.confirmed_photo_places.get(candidate.id)
    if existing is not None:
        current_place = state.wanted_places.get(existing.wanted_place.id)
        # wanted place가 삭제됐다면 아래에서 새로 만들고,
        # 남아 있다면 확정 당시 스냅샷 대신 현재 상태로 응답을 재구성해
        # PATCH 이후에도 GET /wanted-places와 일치하도록 한다.
        if current_place is not None:
            return ConfirmedPhotoPlaceRead(
                search_id=existing.search_id,
                candidate=existing.candidate,
                wanted_place=current_place,
            )
    trip = require_trip(state, trip_id)
    wanted_place = create_wanted_place(
        state,
        trip_id,
        WantedPlaceCreateRequest(
            provider_place_id=candidate.provider_place_id,
            name=candidate.name,
            city=candidate.city or trip.city,
            country=candidate.country or trip.country,
            latitude=candidate.latitude,
            longitude=candidate.longitude,
            priority=5,
            notes=candidate.reason,
        ),
        source=PlaceSource.PHOTO,
    )
    confirmation = ConfirmedPhotoPlaceRead(
        search_id=search_id,
        candidate=candidate,
        wanted_place=wanted_place,
    )
    state.confirmed_photo_places[candidate.id] = confirmation
    return confirmation

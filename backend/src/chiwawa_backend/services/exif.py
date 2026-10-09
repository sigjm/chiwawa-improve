"""사진 파일의 EXIF에서 촬영 시각과 GPS 좌표를 추출한다."""

from __future__ import annotations

import datetime as dt
import io
import math
import re
from dataclasses import dataclass
from typing import SupportsFloat, cast

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError
from pillow_heif import (  # pyright: ignore[reportMissingTypeStubs]
    register_heif_opener,  # pyright: ignore[reportUnknownVariableType]
)

register_heif_opener()


@dataclass(frozen=True, slots=True)
class PhotoExif:
    taken_at: dt.datetime | None
    latitude: float | None
    longitude: float | None


EMPTY_EXIF = PhotoExif(taken_at=None, latitude=None, longitude=None)

# GPS 좌표는 (도, 분, 초) 세 부분으로 구성된다.
_DMS_PART_COUNT = 3


# 폼 입력 좌표와 동일한 유효 범위. 손상된 EXIF(예: GPSLatitude 500/1)가
# 검증을 우회하지 못하도록 범위를 벗어난 좌표는 버린다.
_MAX_ABS_LATITUDE = 90.0
_MAX_ABS_LONGITUDE = 180.0

# EXIF 표준 날짜 형식("YYYY:MM:DD ...")인지 판별하는 패턴.
_EXIF_COLON_DATE = re.compile(r"^\d{4}:\d{2}:\d{2}")


# 인식 서비스로 보내기 전에 줄일 크기 기준.
# Cloud Vision 은 JSON 요청이 10MB 를 넘으면 받지 않는데,
# base64 로 실으면 7.5MB 사진이 그 한도다.
SHRINK_ABOVE_BYTES = 4 * 1024 * 1024
SHRINK_MAX_SIDE = 2048
# JPEG 가 아닌 형식은 전체 픽셀을 메모리에 풀어야 줄일 수 있다.
# 압축된 크기는 작아도 풀면 수백 MB 가 될 수 있어, 이 픽셀 수를 넘으면 줄이지 않는다.
SHRINK_MAX_DECODED_PIXELS = 24_000_000
_SHRINK_JPEG_QUALITY = 85


class InvalidImageError(ValueError):
    pass


def validate_image(data: bytes) -> None:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as error:
        raise InvalidImageError from error


def shrink_large_image(data: bytes) -> bytes | None:
    """큰 사진을 줄인 JPEG로 다시 만든다. 줄이지 않을 때는 None.

    None 인 경우: 충분히 작다, 열 수 없다, 줄이려면 메모리를 너무 많이 써야 한다.
    다시 인코딩하면 EXIF가 사라지므로, 방향은 픽셀에 반영하고
    촬영 좌표는 호출자가 원본에서 미리 읽어 둬야 한다.
    """
    if len(data) <= SHRINK_ABOVE_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if image.format != "JPEG" and width * height > SHRINK_MAX_DECODED_PIXELS:
                return None
            # 먼저 줄인다: JPEG 는 디코더가 줄여서 읽으므로(draft)
            # 원본 전체를 풀지 않는다.
            # 방향 반영은 작아진 그림에 한다.
            image.thumbnail((SHRINK_MAX_SIDE, SHRINK_MAX_SIDE))
            upright = ImageOps.exif_transpose(image)
            buffer = io.BytesIO()
            upright.convert("RGB").save(buffer, "JPEG", quality=_SHRINK_JPEG_QUALITY)
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ):
        return None
    return buffer.getvalue()


def read_exif(data: bytes) -> PhotoExif:
    try:
        with Image.open(io.BytesIO(data)) as image:
            exif = image.getexif()
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ):
        return EMPTY_EXIF
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    return PhotoExif(
        taken_at=_taken_at(exif),
        latitude=_coordinate(
            gps,
            value_tag=ExifTags.GPS.GPSLatitude,
            ref_tag=ExifTags.GPS.GPSLatitudeRef,
            refs=("N", "S"),
            max_abs_degrees=_MAX_ABS_LATITUDE,
        ),
        longitude=_coordinate(
            gps,
            value_tag=ExifTags.GPS.GPSLongitude,
            ref_tag=ExifTags.GPS.GPSLongitudeRef,
            refs=("E", "W"),
            max_abs_degrees=_MAX_ABS_LONGITUDE,
        ),
    )


def _taken_at(exif: Image.Exif) -> dt.datetime | None:
    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
    raw: object = exif_ifd.get(ExifTags.Base.DateTimeOriginal) or exif.get(
        ExifTags.Base.DateTime
    )
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    # EXIF 표준 형식 "YYYY:MM:DD HH:MM:SS"만 ISO 8601로 바꾼다.
    # 일부 편집기는 이미 ISO dash 형식("2023-05-01T10:20:30")을 쓰는데,
    # 그 경우 콜론을 치환하면 시간 구분자가 망가져 파싱에 실패한다.
    if _EXIF_COLON_DATE.match(text):
        text = text.replace(":", "-", 2)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    offset: object = exif_ifd.get(ExifTags.Base.OffsetTimeOriginal)
    if parsed.tzinfo is None and isinstance(offset, str):
        try:
            return dt.datetime.fromisoformat(text + offset.strip())
        except ValueError:
            return parsed
    return parsed


def _coordinate(
    gps: dict[int, object],
    *,
    value_tag: int,
    ref_tag: int,
    refs: tuple[str, str],
    max_abs_degrees: float,
) -> float | None:
    raw: object = gps.get(value_tag)
    if not isinstance(raw, tuple):
        return None
    values = cast("tuple[object, ...]", raw)
    if len(values) != _DMS_PART_COUNT:
        return None
    parts = [_to_float(part) for part in values]
    if any(part is None for part in parts):
        return None
    degrees, minutes, seconds = (part for part in parts if part is not None)
    decimal = degrees + minutes / 60 + seconds / 3600
    if abs(decimal) > max_abs_degrees:
        return None
    # 방향 태그가 없거나 알 수 없는 값이면 반구를 정할 수 없다.
    # 양수로 가정하면 남반구·서반구 사진이 지구 반대편 좌표가 되므로 버린다.
    # refs 는 (양수 방향, 음수 방향) 쌍이다.
    ref: object = gps.get(ref_tag)
    direction = ref.strip().upper() if isinstance(ref, str) else ""
    if direction not in refs:
        return None
    return -decimal if direction == refs[1] else decimal


def _to_float(value: object) -> float | None:
    if not isinstance(value, SupportsFloat):
        return None
    try:
        result = float(value)
    except (ValueError, ZeroDivisionError):
        return None
    # Pillow의 IFDRational(x, 0)은 예외 대신 NaN을 반환하므로 여기서 걸러낸다.
    return result if math.isfinite(result) else None

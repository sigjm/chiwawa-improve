# 여행 도시의 좌표를 한 번만 찾아 재사용하는 조회기 (사진 장소 검색의 위치 prior 용)
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from chiwawa_backend.errors import ConfigurationError, UpstreamServiceError

if TYPE_CHECKING:
    from collections.abc import Callable

    from chiwawa_backend.services.place_search import PlaceSearchProvider

# 기억해 둘 도시 수 상한. 넘으면 비우고 다시 채운다.
# (사용자가 입력한 문자열이 키라서 그대로 두면 무한정 늘 수 있다.)
MAX_KNOWN_AREAS = 1024


class TripAreaResolver:
    """여행의 "도시 국가"를 좌표로 바꾼다.

    같은 도시는 프로세스 수명 동안 한 번만 조회한다.
    """

    def __init__(
        self,
        provider_factory: Callable[[], PlaceSearchProvider] | None,
    ) -> None:
        # provider_factory 가 None 이면 조회하지 않는다 (좌표 없이 진행).
        self._provider_factory: Callable[[], PlaceSearchProvider] | None = (
            provider_factory
        )
        self._known: dict[str, tuple[float, float] | None] = {}
        # ponytail: 조회 전체를 한 줄로 세운다(도시가 달라도 순서대로).
        # 도시별 첫 조회에서만 걸리므로 충분하다.
        # 막히기 시작하면 도시별 잠금으로 바꾼다.
        self._lookup_lock: asyncio.Lock = asyncio.Lock()

    async def resolve(
        self,
        city: str | None,
        country: str | None,
    ) -> tuple[float, float] | None:
        if self._provider_factory is None or not city:
            return None
        scope = " ".join(part for part in (city, country) if part)
        key = scope.casefold()
        if key in self._known:
            return self._known[key]
        async with self._lookup_lock:
            # 기다리는 동안 다른 요청이 같은 도시를 찾아 뒀을 수 있다.
            if key in self._known:
                return self._known[key]
            try:
                candidates = await self._provider_factory().search(
                    query=scope,
                    city_bias=None,
                )
            except (ConfigurationError, UpstreamServiceError):
                # 키 미설정·일시적 실패는 기억하지 않는다.
                # → 다음 요청에서 다시 시도한다.
                return None
            area = (
                (candidates[0].latitude, candidates[0].longitude)
                if candidates
                else None
            )
            if len(self._known) >= MAX_KNOWN_AREAS:
                self._known.clear()
            self._known[key] = area
            return area

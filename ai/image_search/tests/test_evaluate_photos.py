# scripts/evaluate_photos.py 테스트 — 실사진 측정 스크립트의 지표 계산
# 실제 provider 대신 가짜 provider 를 넣어, 키·네트워크 없이 오차·순위·호출 수 집계를 검증한다
import json

from ai.image_search.scripts.evaluate_photos import SAME_PLACE_KM, evaluate
from ai.image_search.tests.fakes import (
    FakeLandmark,
    FakePlaces,
    FakeVision,
    landmark_det,
    resolved_place,
    vision_id,
)

# 1x1 JPEG 가 아니어도 된다 — 가짜 provider 는 바이트를 해석하지 않는다
PHOTO_BYTES = b"\xff\xd8\xff photo"


def write_manifest(tmp_path, entries):
    (tmp_path / "photo.jpg").write_bytes(PHOTO_BYTES)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return manifest


class TestEvaluate:
    # 첫 후보가 정답 근처면 정답으로 세고, 오차·호출 수를 함께 남긴다
    def test_scores_first_candidate_against_truth(self, tmp_path):
        manifest = write_manifest(
            tmp_path,
            [
                {
                    "label": "센소지",
                    "image_path": "photo.jpg",
                    "truth_latitude": 35.7148,
                    "truth_longitude": 139.7967,
                }
            ],
        )
        places = FakePlaces(resolved=resolved_place(), nearby=[])

        report = evaluate(
            manifest, FakeLandmark(landmark_det(score=0.9)), FakeVision(vision_id()), places
        )

        row = report["photos"][0]
        assert row["label"] == "센소지"
        assert row["top_candidate"] == "센소지"
        assert row["top_error_km"] == 0
        assert row["top_is_truth"] is True
        assert row["truth_rank"] == 1
        assert row["places_calls"] == 2  # 이름 검색 1 + 근처 검색 1
        assert row["landmark"] == "센소지"
        assert report["summary"] == {
            "photos": 1,
            "top_is_truth": 1,
            "truth_in_candidates": 1,
            "no_candidates": 0,
            "median_top_error_km": 0,
            "places_calls": 2,
        }

    # 첫 후보가 틀려도 정답이 뒤 후보에 있으면 그 순위를 남긴다 (거리로 판정)
    def test_reports_rank_when_truth_is_a_later_candidate(self, tmp_path):
        manifest = write_manifest(
            tmp_path,
            [
                {
                    "image_path": "photo.jpg",
                    "truth_latitude": 34.6690,
                    "truth_longitude": 135.5030,
                }
            ],
        )
        places = FakePlaces(
            resolve_map={
                "이치란": [
                    resolved_place(name="이치란 시부야", lat=35.6610, lng=139.7010, pid="tokyo"),
                    resolved_place(name="이치란 도톤보리", lat=34.6690, lng=135.5030, pid="osaka"),
                ]
            },
            nearby=[],
        )

        report = evaluate(
            manifest, FakeLandmark(None), FakeVision(vision_id(guess="이치란")), places
        )

        row = report["photos"][0]
        assert row["label"] == "photo.jpg"
        assert row["top_is_truth"] is False
        assert row["top_error_km"] > SAME_PLACE_KM
        assert row["truth_rank"] == 2
        assert row["place_name_guess"] == "이치란"
        assert report["summary"]["top_is_truth"] == 0
        assert report["summary"]["truth_in_candidates"] == 1

    # 후보가 없으면 오차 없이 "후보 없음"으로 센다. 읽은 글자는 튜닝용으로 남긴다
    def test_counts_photos_without_candidates(self, tmp_path):
        manifest = write_manifest(
            tmp_path,
            [
                {
                    "image_path": "photo.jpg",
                    "truth_latitude": 35.0,
                    "truth_longitude": 135.0,
                }
            ],
        )

        report = evaluate(
            manifest,
            FakeLandmark(None),
            FakeVision(vision_id(guess=None, texts=["OPEN"])),
            FakePlaces(),
        )

        row = report["photos"][0]
        assert row["top_candidate"] is None
        assert row["top_error_km"] is None
        assert row["truth_rank"] is None
        assert row["visible_text"] == ["OPEN"]
        assert report["summary"]["no_candidates"] == 1
        assert report["summary"]["median_top_error_km"] is None

    # manifest 의 여행 맥락과 촬영 좌표 힌트가 요청으로 전달된다
    def test_passes_trip_context_and_location_hint(self, tmp_path):
        manifest = write_manifest(
            tmp_path,
            [
                {
                    "image_path": "photo.jpg",
                    "truth_latitude": 35.0090,
                    "truth_longitude": 135.7716,
                    "latitude": 35.0091,
                    "longitude": 135.7718,
                    "city": "교토",
                    "country": "일본",
                }
            ],
        )
        places = FakePlaces(resolved=resolved_place(lat=35.0090, lng=135.7716), nearby=[])

        evaluate(manifest, FakeLandmark(None), FakeVision(vision_id()), places)

        assert places.bias_calls == [(35.0091, 135.7718)]

"""Fallback localization must recover failed families without changing legacy successes."""

import json

import numpy as np
import pytest

from buspipe.config import PipelineSettings
from buspipe.core import PosterExtractor
from buspipe.layout import detect_route_blocks
from buspipe.recovery import (
    classify_non_route,
    recover_route_layout,
    locate_recovered_stop_band,
)
from buspipe.review import ReviewEngine
from buspipe.batch import BatchConfig, run_batch
from tests.synth import SyntheticOcrBackend, make_poster


def _page(width=2042, height=4678):
    return np.full((height, width, 3), 255, dtype=np.uint8)


def test_frame_in_route_window_does_not_hide_independent_number():
    rgb = _page()
    rgb[:, :430] = (220, 0, 0)
    rgb[:, -100:] = (220, 0, 0)
    rgb[300:450, 550:850] = (220, 0, 0)
    settings = PipelineSettings(extract_mode="endpoints")
    with pytest.raises(RuntimeError, match="No route-number"):
        detect_route_blocks(rgb, settings.layout)
    recovered = recover_route_layout(rgb, settings, "测试-北-01")
    assert recovered is not None
    assert recovered.blocks == [(300, 449)]
    assert recovered.red_mask[350, 600]
    assert not recovered.red_mask[350, 320]


@pytest.mark.parametrize(
    "width,height,box",
    [
        (2042, 4678, (300, 395, 300, 650)),
        (2954, 7796, (200, 740, 250, 1750)),
    ],
)
def test_family_specific_font_sizes_recover_real_route_band(width, height, box):
    rgb = _page(width, height)
    y0, y1, x0, x1 = box
    rgb[y0:y1, x0:x1] = (220, 0, 0)
    settings = PipelineSettings(extract_mode="endpoints")
    with pytest.raises(RuntimeError, match="No route-number"):
        detect_route_blocks(rgb, settings.layout)
    recovered = recover_route_layout(rgb, settings, "测试-北-01")
    assert recovered.blocks == [(y0, y1 - 1)]


def test_unknown_family_does_not_get_permissive_recovery():
    assert (
        recover_route_layout(
            _page(701, 1403), PipelineSettings(extract_mode="endpoints"), "x"
        )
        is None
    )


def test_legacy_success_never_calls_recovery(tmp_path, monkeypatch):
    path = make_poster(tmp_path / "测试-北-01.jpg", routes=(6,))

    def unexpected(*args, **kwargs):
        pytest.fail("legacy success must not enter recovery or classification")

    monkeypatch.setattr("buspipe.core.recover_route_layout", unexpected)
    monkeypatch.setattr("buspipe.core.classify_non_route", unexpected)
    result = PosterExtractor(
        SyntheticOcrBackend(), PipelineSettings(extract_mode="endpoints")
    ).extract(path)
    assert result["entries"]
    assert "recovery" not in result


class CaptionBackend:
    model_version = "caption-test"

    def __init__(self, text):
        self.text = text

    def recognize(self, images, params):
        return [
            [{"text": self.text, "score": 0.99, "x0": 0, "x1": 10, "y0": 0, "y1": 10}]
            for _ in images
        ]


def test_blank_and_explicit_inactive_are_separate_from_failed_routes():
    settings = PipelineSettings(extract_mode="endpoints")
    rgb = _page()
    rgb[:, :100] = rgb[:, -100:] = (220, 0, 0)
    blank = classify_non_route(rgb, "空白-北-01", CaptionBackend(""), settings)
    assert blank["board_type"] == "blank"
    rgb[500:700, 400:1700] = (220, 0, 0)
    inactive = classify_non_route(
        rgb, "测试-北-01", CaptionBackend("本站未启用"), settings
    )
    assert inactive["board_type"] == "inactive"
    assert (
        classify_non_route(rgb, "测试-北-01", CaptionBackend("不确定文字"), settings)
        is None
    )


def test_jpeg_colored_frame_rule_does_not_make_blank_board_unknown():
    rgb = _page()
    rgb[170:172, :] = (119, 20, 20)
    decision = classify_non_route(
        rgb,
        "空白-北-01",
        CaptionBackend(""),
        PipelineSettings(extract_mode="endpoints"),
    )
    assert decision["board_type"] == "blank"


def test_real_route_ink_is_not_skipped_by_an_inactive_caption(tmp_path):
    rgb = _page()
    rgb[300:395, 300:650] = (220, 0, 0)
    rgb[450:700, 300:1700:30] = (0, 0, 0)
    assert (
        classify_non_route(
            rgb,
            "测试-北-01",
            CaptionBackend("本站未启用"),
            PipelineSettings(extract_mode="endpoints"),
        )
        is None
    )


def test_inactive_caption_confidence_does_not_include_non_title_noise():
    class NoisyCaption(CaptionBackend):
        def recognize(self, images, params):
            rows = super().recognize(images, params)
            for row in rows:
                row.append(
                    {
                        "text": "_1一",
                        "score": 0.1,
                        "x0": 12,
                        "x1": 14,
                        "y0": 20,
                        "y1": 22,
                    }
                )
            return rows

    rgb = _page()
    rgb[500:700, 400:1700] = (220, 0, 0)
    decision = classify_non_route(
        rgb,
        "测试-北-01",
        NoisyCaption("本站未启用"),
        PipelineSettings(extract_mode="endpoints"),
    )
    assert decision["board_type"] == "inactive" and decision["confidence"] == 0.99


def test_skipped_board_is_terminal_and_not_reported_as_done_or_incomplete(tmp_path):
    source = tmp_path / "source"
    make_poster(source / "测试-北-01.jpg")
    calls = []

    def extract(path, settings, event):
        calls.append(path)
        return {
            "poster": path.stem,
            "path": str(path),
            "extract_mode": "endpoints",
            "entries": [],
            "skipped": True,
            "board_type": "inactive",
            "skip_reason": "本站未启用",
        }

    config = BatchConfig(
        out_dir=tmp_path / "out", pipeline=PipelineSettings(extract_mode="endpoints")
    )
    result = run_batch(source, config, extractor=extract)
    assert result.counts["skipped"] == 1
    report = json.loads(
        (config.out_dir / "batch_report.json").read_text(encoding="utf-8")
    )
    assert (
        report["counts"]["done"]
        == report["counts"]["failed"]
        == report["counts"]["incomplete"]
        == 0
    )
    assert len(report["skipped"]) == 1
    assert (
        json.loads((config.out_dir / "station_routes.json").read_text(encoding="utf-8"))
        == []
    )
    resumed = run_batch(source, config, extractor=extract)
    assert len(calls) == 1
    assert resumed.poster_files == {}


def test_recovered_white_on_blue_columns_are_selected_before_footer_qr():
    rgb = _page()
    rgb[300:395, 300:650] = (220, 0, 0)
    rgb[530:820, 100:1940] = (20, 20, 160)
    for x in (180, 550, 950, 1350, 1800):
        rgb[550:750, x : x + 30] = 255
    rgb[1100:1500, 1400:1800] = 0
    recovered = recover_route_layout(
        rgb, PipelineSettings(extract_mode="endpoints"), "x"
    )
    y0, y1, columns, debug = locate_recovered_stop_band(
        rgb, 400, None, recovered.settings
    )
    assert 540 <= y0 < 600 and 700 < y1 < 820
    assert len(columns) == 5
    assert columns[0]["x0"] == 180 and columns[-1]["x0"] == 1800


def test_red_current_station_at_colored_row_edge_is_not_replaced_by_next_stop():
    rgb = _page()
    rgb[300:395, 300:650] = (220, 0, 0)
    rgb[530:820, 100:1940] = (20, 20, 160)
    for x in (180, 550, 950, 1350, 1800):
        rgb[550:750, x : x + 30] = 255
    rgb[550:750, 180:210] = (220, 0, 0)
    recovered = recover_route_layout(
        rgb, PipelineSettings(extract_mode="endpoints"), "x"
    )
    _, _, columns, _ = locate_recovered_stop_band(rgb, 400, None, recovered.settings)
    assert columns[0]["x0"] == 180


def test_footer_squares_without_station_columns_do_not_become_endpoints():
    rgb = _page()
    rgb[300:395, 300:650] = (220, 0, 0)
    rgb[900:1300, 300:700] = 0
    rgb[900:1300, 1400:1800] = 0
    recovered = recover_route_layout(
        rgb, PipelineSettings(extract_mode="endpoints"), "x"
    )
    with pytest.raises(RuntimeError, match="no verified vertical endpoint row"):
        locate_recovered_stop_band(rgb, 400, None, recovered.settings)


def test_garbled_endpoint_is_reviewed_without_rewriting_ocr():
    entry = {
        "route": "159",
        "route_ocr": "159",
        "direction": "东",
        "extract_mode": "endpoints",
        "endpoints": [
            {
                "name": "么M仁德立",
                "display_name": "么M仁德立",
                "role": "first",
                "seq": 1,
                "score": 0.9,
            },
            {
                "name": "之キ之急占",
                "display_name": "之キ之急占",
                "role": "last",
                "seq": 2,
                "score": 0.9,
            },
        ],
    }
    items = ReviewEngine(PipelineSettings(extract_mode="endpoints")).collect(
        {"entries": [entry]}, "hash"
    )
    assert {r["seq"] for r in items if r["reason"] == "suspicious_text"} == {1, 2}
    assert entry["endpoints"][0]["name"] == "么M仁德立"


def test_stacked_faces_keep_source_coordinates_and_reject_other_dimensions(tmp_path):
    from PIL import Image
    from tests.test_landscape import board
    from buspipe.landscape import locate_stacked_landscape
    from buspipe.config import LandscapeSettings

    front = Image.open(board(tmp_path / "front.png", side="front", background="white"))
    back = Image.open(board(tmp_path / "back.png", side="back", background="white"))
    split = round(3912 * 0.52)
    image = Image.new("RGB", (2961, 3912), "white")
    image.paste(front.resize((2961, split)), (0, 0))
    image.paste(back.resize((2961, 3912 - split)), (0, split))
    layouts = locate_stacked_landscape(np.asarray(image), LandscapeSettings())
    assert len(layouts) == 2 and layouts[1].route_box[1] >= split
    assert all(c["y0"] >= split for group in layouts[1].groups for c in group)
    assert layouts[1].arrow == "right"
    with pytest.raises(RuntimeError):
        locate_stacked_landscape(
            np.zeros((1000, 1600, 3), dtype=np.uint8), LandscapeSettings()
        )


def test_large_title_family_keeps_detached_first_station_character():
    rgb = _page(2954, 7796)
    rgb[200:740, 250:1750] = (220, 0, 0)
    rgb[880:1230, 100:2900] = (20, 20, 160)
    for x in (180, 650, 1100, 1650, 2200, 2800):
        rgb[900:950, x : x + 25] = 255
        rgb[980:1120, x : x + 25] = 255
        rgb[1162:1212, x : x + 25] = 255
    recovered = recover_route_layout(
        rgb, PipelineSettings(extract_mode="endpoints"), "x"
    )
    y0, y1, columns, _ = locate_recovered_stop_band(rgb, 740, None, recovered.settings)
    assert y0 == 900 and y1 == 1211


def test_4726_frame_family_also_contains_smaller_route_glyphs():
    rgb = _page(4726, 7678)
    rgb[:, :600] = (220, 0, 0)
    rgb[:, -150:] = (220, 0, 0)
    rgb[300:410, 800:1150] = (220, 0, 0)
    recovered = recover_route_layout(
        rgb, PipelineSettings(extract_mode="endpoints"), "x"
    )
    assert recovered.blocks == [(300, 409)]

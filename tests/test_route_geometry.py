"""Route-number geometry regressions found on the 1-10 station folder."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from buspipe.batch import BatchConfig, run_batch
from buspipe.config import LayoutSettings, PipelineSettings
from buspipe.core import PosterExtractor, UnsupportedTemplateError
from buspipe.layout import detect_route_blocks, locate_stop_band, make_red_mask, route_number_bounds
from buspipe.parsing import parse_header
from buspipe.store import ManifestStore
from tests.synth import SyntheticOcrBackend, make_poster


def _canvas(width: int = 1400, height: int = 2000) -> np.ndarray:
    return np.full((height, width, 3), 255, dtype=np.uint8)


def test_route_number_bounds_cover_digits_past_projection_window() -> None:
    rgb = _canvas()
    rgb[300:460, 520:900] = (220, 0, 0)
    settings = LayoutSettings()

    blocks = detect_route_blocks(rgb, settings)
    assert blocks == [(300, 459)]
    assert route_number_bounds(make_red_mask(rgb), blocks[0], settings) == (520, 899)


def test_wrapped_route_title_is_merged_into_one_block() -> None:
    rgb = _canvas()
    rgb[300:420, 400:1000] = (220, 0, 0)
    rgb[450:570, 400:1000] = (220, 0, 0)

    assert detect_route_blocks(rgb, LayoutSettings()) == [(300, 569)]


@pytest.mark.parametrize("heights", [(160, 80), (68, 73)])
def test_wrapped_route_title_keeps_smaller_lines(heights) -> None:
    rgb = _canvas()
    first, second = heights
    second_y = 300 + first + 30
    rgb[300:300 + first, 400:1000] = (220, 0, 0)
    rgb[second_y:second_y + second, 400:1000] = (220, 0, 0)

    assert detect_route_blocks(rgb, LayoutSettings()) == [(300, second_y + second - 1)]


def test_wrapped_title_merge_excludes_page_frame_and_stop_symbols() -> None:
    rgb = _canvas()
    rgb[250:270, :] = (220, 0, 0)
    rgb[300:460, 400:800] = (220, 0, 0)
    # Thin red outlines above the stop row are not another title line.
    rgb[490:530, 400:800:40] = (220, 0, 0)
    rgb[490:492, 400:800] = (220, 0, 0)
    rgb[528:530, 400:800] = (220, 0, 0)
    rgb[550:594, 500:580] = (220, 0, 0)

    assert detect_route_blocks(rgb, LayoutSettings()) == [(300, 459)]


def test_title_merge_keeps_nearby_narrow_current_stop_glyph_separate() -> None:
    rgb = _canvas()
    rgb[300:560, 400:750] = (220, 0, 0)
    rgb[610:664, 540:571] = (220, 0, 0)

    assert detect_route_blocks(rgb, LayoutSettings()) == [(300, 559)]


def test_stacked_current_stop_characters_do_not_become_route_blocks() -> None:
    rgb = _canvas()
    rgb[300:560, 400:750] = (220, 0, 0)
    for y in (700, 780, 860):
        rgb[y:y + 65, 540:605] = (220, 0, 0)

    assert detect_route_blocks(rgb, LayoutSettings()) == [(300, 559)]


def test_landscape_next_stop_board_is_rejected(tmp_path) -> None:
    path = tmp_path / "单线路-北-01.jpg"
    Image.new("RGB", (800, 500), "white").save(path)

    with pytest.raises(RuntimeError, match="unsupported landscape next-stop board"):
        PosterExtractor(None, PipelineSettings()).extract(path)  # type: ignore[arg-type]


@pytest.mark.parametrize("style", ["wide_vertical", "horizontal_name", "empty_band"])
@pytest.mark.parametrize("extract_mode", ["full", "endpoints"])
def test_landscape_next_stop_column_is_not_exported(tmp_path, style, extract_mode) -> None:
    source = tmp_path / "source"
    source.mkdir()
    rgb = _canvas(width=1600, height=1000)
    rgb[150:301, 320:660] = (220, 0, 0)
    if style == "wide_vertical":
        rgb[340:540, 760:960] = (0, 0, 0)
    elif style == "horizontal_name":
        for x in range(760, 1300, 80):
            rgb[340:470, x:x + 60] = (0, 0, 0)
    Image.fromarray(rgb).save(source / "单线路-北-01.jpg", quality=95)

    class NoOcr:
        model_version = "unused"

        def recognize(self, images, params):
            raise AssertionError("unsupported templates must be rejected before OCR")

    result = run_batch(source, BatchConfig(out_dir=tmp_path / "out", pipeline=PipelineSettings(extract_mode=extract_mode)), ocr_backend=NoOcr())

    assert result.counts["failed"] == 1
    poster = ManifestStore(result.manifest_path).posters()[0]
    assert "unsupported landscape next-stop board" in poster["error"]
    exported = json.loads((result.out_dir / "all_raw.json").read_text(encoding="utf-8"))
    assert exported["posters"] == []


def _number_item(text, x0=400, y0=300, score=0.98):
    return {"text": text, "score": score, "x0": x0, "x1": x0 + 100, "y0": y0, "y1": y0 + 150}


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (["K1"], "K1"),
        (["k1"], "K1"),
        (["通学专线13"], "通学专线13"),
        (["通学专线2", "（福州十中东山校区）"], "通学专线2"),
        (["琅岐公交旅游专线"], "琅岐公交旅游专线"),
        (["102"], "102"),
        (["I02"], "102"),
        (["8O路"], "80"),
    ],
)
def test_header_preserves_route_identifiers(texts, expected) -> None:
    items = [_number_item(text, y0=300 + i * 180) for i, text in enumerate(texts)]
    header = parse_header([], items, [], [], {}, "新站点-北-01", 0, 900)

    assert header["route"] == expected
    assert header["route_ocr"] == expected


def test_header_joins_split_route_identifier_in_reading_order() -> None:
    # OCR may return the digit before a slightly lower prefix on the same line.
    items = [_number_item("1", x0=520, y0=300), _number_item("K", x0=400, y0=305)]
    header = parse_header([], items, [], [], {}, "新站点-北-01", 0, 900)

    assert header["route"] == "K1"


@pytest.mark.parametrize("route", ["K1", "通学专线13"])
def test_route_identifier_survives_extraction_and_batch_export(tmp_path, route) -> None:
    source = tmp_path / "source"
    make_poster(source / "新站点-北-01.jpg", stops=6)

    class NamedRouteBackend(SyntheticOcrBackend):
        def _one(self, image):
            items = super()._one(image)
            return [{**item, "text": route} if item["text"].isdigit() else item for item in items]

    result = run_batch(
        source,
        BatchConfig(out_dir=tmp_path / "out", route_hints={}),
        ocr_backend=NamedRouteBackend(),
    )

    assert result.counts["done"] == 1
    exported = json.loads((result.out_dir / "all_raw.json").read_text(encoding="utf-8"))
    entry = exported["posters"][0]["entries"][0]
    assert entry["route"] == route
    assert entry["route_ocr"] == route
    assert entry["stop_count"] == 6


@pytest.mark.slow
def test_existing_sample_templates_are_classified_before_ocr() -> None:
    if os.environ.get("RUN_SAMPLE_LAYOUT") != "1":
        pytest.skip("set RUN_SAMPLE_LAYOUT=1 to validate the existing sample directory")
    root = Path(os.environ.get("BUS_OCR_SAMPLE_ROOT", r"E:/BaiduNetdiskDownload/底板/1-10"))
    paths = sorted(root.rglob("*.jpg"))
    assert paths, f"no sample JPGs found in {root}"

    class NoOcr:
        model_version = "unused"

        def recognize(self, images, params):
            raise AssertionError("template validation must not call OCR")

    settings = PipelineSettings()
    portrait = landscape = 0
    for path in paths:
        with Image.open(path) as image:
            if image.width > image.height:
                with pytest.raises(UnsupportedTemplateError, match="unsupported landscape"):
                    PosterExtractor(NoOcr(), settings).extract(path)
                landscape += 1
                continue
            rgb = np.asarray(image.convert("RGB"))
        blocks = detect_route_blocks(rgb, settings.layout)
        for i, (_, end) in enumerate(blocks):
            next_start = blocks[i + 1][0] if i + 1 < len(blocks) else None
            _, _, columns, _ = locate_stop_band(rgb, end, next_start, settings.layout)
            assert columns, (path.name, i)
        portrait += 1
    print(f"sample layouts: {portrait} portrait posters, {landscape} rejected landscape boards")

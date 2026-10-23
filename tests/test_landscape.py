"""Horizontal boards keep ordered terminals and share the endpoint batch contract."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from buspipe.batch import BatchConfig, run_batch
from buspipe.cache import BlockCache, FileCache
from buspipe.config import PipelineSettings
from buspipe.core import PosterExtractor, UnsupportedTemplateError
from buspipe.landscape import _vertical_ocr_image, canonical_route, route_from_filename
from buspipe.parsing import normalize_for_compare
from buspipe.review import ReviewEngine
from tests.conftest import FIXTURES


SETTINGS = PipelineSettings(extract_mode="endpoints", route_hints={})


def board(path: Path, *, side="front", background="green", arrow="right", note="", scale=1):
    colors = {"green": (0, 255, 0), "purple": (128, 0, 160), "white": (255, 255, 255)}
    bg = colors[background]
    ink = (0, 0, 0) if background == "white" else (255, 255, 255)
    im = Image.new("RGB", (1600, 1000), "white")
    draw = ImageDraw.Draw(im)
    draw.rectangle((100, 90, 500, 350), fill="red")
    if side == "front":
        draw.rectangle((0, 700, 1599, 999), fill=bg)
        draw.rectangle((500, 780, 600, 900), fill="red")
        for x in [100, 210, 320, 650, 760, 870]:
            draw.rectangle((x, 795, x + 80, 895), fill=ink)
        # This unrelated next-stop text must not enter the terminal OCR crops.
        draw.rectangle((850, 550, 1150, 630), fill="red")
        # A separated operator logo must not become part of the last station.
        draw.rectangle((1300, 790, 1500, 910), fill=ink)
    else:
        draw.rectangle((0, 500, 1599, 999), fill=bg)
        draw.rectangle((0, 490, 1599, 496), fill="red")
        if arrow == "right":
            draw.polygon(((1550, 460), (1599, 490), (1550, 490)), fill="red")
        elif arrow == "left":
            draw.polygon(((50, 460), (0, 490), (50, 490)), fill="red")
        xs = [100, 300, 500, 700, 900, 1100, 1400]
        if note == "first":
            xs.insert(1, 150)
        if note == "last":
            xs.append(1450)
        for x in xs:
            width = 36 if (note == "first" and x == 150) or (note == "last" and x == 1450) else 40
            for y in [600, 700, 800]:
                draw.rectangle((x, y, x + width - 1, y + 30), fill=ink)
        draw.ellipse((1380, 525, 1450, 560), outline=ink, width=3)
        if background == "white":
            draw.rectangle((0, 860, 1599, 999), fill="yellow")
            draw.rectangle((100, 890, 1500, 920), fill="black")
    if scale != 1:
        im = im.resize((round(1600 * scale), round(1000 * scale)))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    return path


class RegionBackend:
    model_version = "landscape-test"

    def __init__(self, names=("甲站", "乙站"), route="141"):
        self.names, self.route = names, route
        self.calls = []

    def recognize(self, images, params):
        self.calls.append(images)
        is_route = len(images) == 1
        texts = [self.route] if is_route else self.names
        assert len(texts) == len(images)
        return [[{"text": text, "score": 0.99, "x0": 0, "y0": 0,
                  "x1": im.shape[1] - 1, "y1": im.shape[0] - 1}] if text else []
                for text, im in zip(texts, images)]


@pytest.mark.parametrize("side", ["front", "back"])
@pytest.mark.parametrize("background", ["green", "purple", "white"])
@pytest.mark.parametrize("scale", [1, 0.67])
def test_supported_templates_extract_only_terminals(tmp_path, side, background, scale):
    path = board(tmp_path / "测试站-西-141-01.png", side=side, background=background, scale=scale)
    backend = RegionBackend()
    result = PosterExtractor(backend, SETTINGS).extract(path)
    entry = result["entries"][0]
    assert "error" not in entry
    assert entry["template"] == f"landscape_{side}"
    assert result["station_key"] == entry["station_key"] == "测试站-西"
    assert entry["route"] == entry["route_ocr"] == "141"
    assert entry["route_direction"] == "甲站→乙站"
    assert entry["stops"] == [] and "stop_count" not in entry
    assert [len(call) for call in backend.calls] == [1, 2]
    # No full row, time table, next-stop text or direction separator is OCR'd.
    for crop in backend.calls[-1]:
        assert np.array_equal(crop[:, :, 0], crop[:, :, 1])
        assert crop.shape[1] < 1600 * scale * 0.65


@pytest.mark.parametrize("arrow,expected", [("right", "甲站→乙站"), ("left", "乙站→甲站")])
def test_arrow_orders_terminals_independently_of_compass(tmp_path, arrow, expected):
    path = board(tmp_path / "测试站-东-141-02.png", side="back", arrow=arrow)
    entry = PosterExtractor(RegionBackend(), SETTINGS).extract(path)["entries"][0]
    assert entry["route_direction"] == expected
    assert entry["direction"] == "东"
    assert [e["role"] for e in entry["endpoints"]] == ["first", "last"]


def test_missing_arrow_is_reviewed(tmp_path):
    path = board(tmp_path / "测试站-北-141-02.png", side="back", arrow="none")
    result = PosterExtractor(RegionBackend(), SETTINGS).extract(path)
    assert {i["seq"] for i in ReviewEngine(SETTINGS).collect(result, "hash")
            if i["reason"] == "direction_uncertain"} == {1, 2}


def test_similar_tall_multi_character_runs_are_not_reordered():
    # Equal run heights do not imply one glyph per run. This is the shape of
    # the compressed 旗山 / 万佛寺 column which previously became 山旗万佛寺.
    mask = np.zeros((350, 50), dtype=bool)
    mask[10:160, 10:40] = True
    mask[190:340, 10:40] = True
    _, vertical = _vertical_ocr_image(mask, SETTINGS.landscape.glyph_height)
    assert vertical is True


@pytest.mark.parametrize("note,names,expected", [
    ("first", ("仓山科技园", "（浦下村委会）", "淮安公交总站"), "仓山科技园（浦下村委会）→淮安公交总站"),
    ("last", ("淮安公交总站", "仓山科技园", "（浦下村委会）"), "淮安公交总站→仓山科技园（浦下村委会）"),
])
def test_attached_terminal_note_is_kept(tmp_path, note, names, expected):
    path = board(tmp_path / "测试站-南-141-02.png", side="back", note=note)
    entry = PosterExtractor(RegionBackend(names), SETTINGS).extract(path)["entries"][0]
    assert entry["route_direction"] == expected


def test_unreadable_terminal_is_not_replaced_by_middle(tmp_path):
    path = board(tmp_path / "测试站-北-141-02.png", side="back")
    result = PosterExtractor(RegionBackend(("甲站", "")), SETTINGS).extract(path)
    entry = result["entries"][0]
    assert entry["last_stop"] == entry["route_direction"] == ""
    assert any(i["reason"] == "empty_ocr" for i in ReviewEngine(SETTINGS).collect(result, "hash"))


@pytest.mark.parametrize("names", [("仓山科技园（浦下村委会）", "乙站"), ("甲站", "仓山科技园(浦下村委会)")])
def test_inline_station_qualifier_keeps_closing_parenthesis(tmp_path, names):
    path = board(tmp_path / "测试站-东-141-01.png")
    entry = PosterExtractor(RegionBackend(names), SETTINGS).extract(path)["entries"][0]
    assert normalize_for_compare(entry["first_stop"]) == normalize_for_compare(names[0])
    assert normalize_for_compare(entry["last_stop"]) == normalize_for_compare(names[1])


@pytest.mark.parametrize("text,canonical", [("K1", "K1"), ("通学专线13", "通学专线13"),
                                           ("地铁接驳6号专线", "接驳6"), ("142", "142")])
def test_prefixed_routes_and_filename_hints(text, canonical):
    assert canonical_route(text) == canonical
    assert route_from_filename(f"站名-北-{canonical}-02") == canonical
    assert route_from_filename("站名-北-01") == ""


def test_filename_route_disagreement_requires_review(tmp_path):
    path = board(tmp_path / "测试站-北-141-01.png")
    result = PosterExtractor(RegionBackend(route="142"), SETTINGS).extract(path)
    assert result["entries"][0]["route_ocr"] == "142"
    assert any(i["reason"] == "route_number_uncertain" for i in ReviewEngine(SETTINGS).collect(result, "hash"))


def test_landscape_checkpoint_and_only_blocks(tmp_path):
    path = board(tmp_path / "测试站-东-141-01.png")
    backend = RegionBackend()
    cache = BlockCache(FileCache(tmp_path / "cache", "blocks"), "hash", SETTINGS.fingerprint())
    extractor = PosterExtractor(backend, SETTINGS, checkpoint=cache)
    assert extractor.extract(path, only_blocks={1})["entries"] == []
    assert not backend.calls
    extractor.extract(path)
    assert extractor.extract(path)["entries"][0]["cache_hit"]
    assert len(backend.calls) == 2


def test_batch_export_and_resume_use_landscape_branch(tmp_path):
    source = tmp_path / "src"
    # Scanner accepts JPG files; the shape/pixels remain deterministic.
    path = board(source / "测试站-东-141-01.jpg")
    config = BatchConfig(out_dir=tmp_path / "out", pipeline=SETTINGS)
    backend = RegionBackend()
    result = run_batch(source, config, ocr_backend=backend)
    assert result.counts["done"] == 1 and not result.errors
    rows = json.loads(Path(result.report_files["station_routes_json"]).read_text(encoding="utf-8"))
    assert rows[0]["station_key"] == "测试站-东" and rows[0]["route_direction"] == "甲站→乙站"
    before = len(backend.calls)
    run_batch(source, config, ocr_backend=backend)
    assert len(backend.calls) == before


def test_unrelated_landscape_and_full_mode_still_rejected(tmp_path):
    empty = tmp_path / "地图-北-01.png"
    Image.new("RGB", (1600, 1000), "white").save(empty)
    with pytest.raises(UnsupportedTemplateError):
        PosterExtractor(None, SETTINGS).extract(empty)
    path = board(tmp_path / "测试站-东-141-01.png")
    with pytest.raises(UnsupportedTemplateError):
        PosterExtractor(None, PipelineSettings()).extract(path)


@pytest.mark.slow
def test_real_landscape_endpoints():
    if os.environ.get("RUN_REAL_LANDSCAPE_OCR") != "1":
        pytest.skip("set RUN_REAL_LANDSCAPE_OCR=1 to recognize horizontal sample boards")
    from buspipe.ocr import LocalRapidOcrBackend

    root = Path(os.environ.get("BUS_OCR_SAMPLE_ROOT", "E:/BaiduNetdiskDownload/底板/1-10"))
    expected = json.loads((FIXTURES / "landscape_expected.json").read_text(encoding="utf-8"))
    backend = LocalRapidOcrBackend(threads=4)
    for relative, row in expected.items():
        path = root / relative
        if not path.exists():
            pytest.skip(f"missing local image: {path}")
        entry = PosterExtractor(backend, SETTINGS).extract(path)["entries"][0]
        assert "error" not in entry, relative
        assert entry["route"] == row["route"], relative
        assert normalize_for_compare(entry["first_stop"]) == normalize_for_compare(row["first_stop"]), relative
        assert normalize_for_compare(entry["last_stop"]) == normalize_for_compare(row["last_stop"]), relative

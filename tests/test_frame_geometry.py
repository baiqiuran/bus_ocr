"""A solid page frame must never become a route terminal OCR crop."""

from dataclasses import replace
import os
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from buspipe.config import LayoutSettings, PipelineSettings
from buspipe.endpoints import recognize_endpoints
from buspipe.layout import detect_route_blocks, detect_stop_columns, detect_stop_row, locate_stop_band


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("split_wide", [False, True])
@pytest.mark.parametrize("solid_terminal", [False, True])
def test_page_frame_is_excluded_but_red_terminal_is_kept(monkeypatch, side, split_wide, solid_terminal):
    rgb = np.full((700, 1600, 3), 255, dtype=np.uint8)
    row = (260, 499)
    expected_x = [400, 650, 900]
    for index, x in enumerate(expected_x):
        color = (210, 15, 15) if index in (0, 2) else (0, 0, 0)
        if solid_terminal:
            # Even a solid red terminal stops at the text band's boundaries.
            rgb[260:500, x:x + 32] = color
        else:
            for y in range(260, 500, 40):
                rgb[y:y + 30, x:x + 32] = color
    frame_x = 40 if side == "left" else 1280
    rgb[:, frame_x:frame_x + 65] = (185, 20, 20)
    # Sparse light pixels model JPEG edge noise without changing the frame.
    rgb[::80, frame_x:frame_x + 65:5] = (255, 255, 255)
    settings = replace(LayoutSettings(), stop_x0=0, stop_x1=1500)

    columns = detect_stop_columns(rgb, *row, settings, split_wide=split_wide)
    assert [c["x0"] for c in columns] == expected_x

    selected = []

    def replay(image, crops, *args):
        selected.extend(c["x0"] for c in crops)
        names = {400: "首站", 900: "末站"}
        return [dict(c, text=names[c["x0"]], score=1.0) for c in crops]

    monkeypatch.setattr("buspipe.endpoints.recognize_columns", replay)
    endpoints, _ = recognize_endpoints(
        rgb, columns, *row, None, PipelineSettings(extract_mode="endpoints", layout=settings)
    )
    assert selected == [400, 900]
    assert [e["name"] for e in endpoints] == ["首站", "末站"]


@pytest.mark.parametrize("advert", [False, True])
def test_text_crossing_initial_search_limit_keeps_its_full_height(advert):
    rgb = np.full((1300, 1600, 3), 255, dtype=np.uint8)
    for x in (400, 650, 900):
        rgb[480:900, x:x + 32] = (0, 0, 0)
    if advert:
        rgb[950:1250, 350:1200] = (0, 0, 0)
    y0, y1, columns, debug = locate_stop_band(
        rgb, 100, 1050, LayoutSettings(), adaptive=True,
    )
    assert (y0, y1) == (480, 899)
    assert all(c["y1"] == 899 for c in columns)
    assert debug["search"][1] <= 1050 - 60


def test_station_row_does_not_merge_distant_header_text():
    rgb = np.full((1000, 1600, 3), 255, dtype=np.uint8)
    rgb[200:210, 700:820] = (0, 0, 0)
    for x in (400, 650, 900):
        rgb[260:450, x:x + 32] = (0, 0, 0)
    y0, y1, _ = detect_stop_row(rgb, 100, None, LayoutSettings(), adaptive=True)
    assert (y0, y1) == (260, 449)


@pytest.mark.parametrize("footer", [False, True])
def test_search_does_not_expand_for_text_ending_at_limit_or_red_footer(footer):
    rgb = np.full((1600, 1600, 3), 255, dtype=np.uint8)
    text_end = 450 if footer else 600
    for x in (400, 650, 900):
        rgb[260:text_end, x:x + 32] = (0, 0, 0)
    if footer:
        rgb[580:650] = (190, 0, 0)
    # Unrelated text outside the first search must not cause an expansion.
    rgb[1100:1350, 700:1000] = (0, 0, 0)
    _, _, debug = detect_stop_row(rgb, 100, None, LayoutSettings(), adaptive=True)
    assert debug["search"] == [115, 600]


def test_solid_red_footer_does_not_become_the_station_row():
    rgb = np.full((1200, 1600, 3), 255, dtype=np.uint8)
    for x in (400, 650, 900):
        rgb[260:450, x:x + 32] = (0, 0, 0)
    rgb[580:700] = (190, 0, 0)
    y0, y1, columns, _ = locate_stop_band(rgb, 100, None, LayoutSettings(), adaptive=True)
    assert (y0, y1) == (260, 449)
    assert [c["x0"] for c in columns] == [400, 650, 900]


@pytest.mark.slow
def test_real_aofeng_frame_crops_are_excluded():
    if os.environ.get("RUN_FRAME_SAMPLE") != "1":
        pytest.skip("set RUN_FRAME_SAMPLE=1 to check the 35 real route blocks")
    root = Path(os.environ.get("BUS_OCR_SAMPLE_ROOT", "E:/BaiduNetdiskDownload/底板/1-10"))
    samples = {
        "鳌峰公园/鳌峰公园-北-01.jpg": 6,
        "鳌峰公园/鳌峰公园-北-02.jpg": 7,
        "鳌峰大桥/鳌峰大桥-北-01.jpg": 10,
        "鳌峰大桥/鳌峰大桥-北-02.jpg": 12,
    }
    settings = LayoutSettings()
    checked = 0
    for relative, count in samples.items():
        path = root / relative
        assert path.is_file(), path
        with Image.open(path) as image:
            rgb = np.asarray(image.convert("RGB"))
        blocks = detect_route_blocks(rgb, settings)
        assert len(blocks) >= count
        for index, (_, end) in enumerate(blocks[:count]):
            next_start = blocks[index + 1][0] if index + 1 < len(blocks) else None
            _, _, columns, _ = locate_stop_band(rgb, end, next_start, settings, adaptive=True)
            assert columns, (relative, index)
            for column in (columns[0], columns[-1]):
                crop = rgb[column["y0"]:column["y1"] + 1, column["x0"]:column["x1"] + 1]
                assert (crop < 120).all(axis=2).mean() > 0.01, (relative, index, column)
            checked += 1
    assert checked == 35

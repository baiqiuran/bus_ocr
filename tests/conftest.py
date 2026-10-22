from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_ROOT = Path(os.environ.get("BUS_OCR_SAMPLE_ROOT", r"E:/BaiduNetdiskDownload/底板"))
ANPING_DIR = SAMPLE_ROOT / "安平小区"
ANPING_EAST = ANPING_DIR / "安平小区-东-01.jpg"


@pytest.fixture(scope="session")
def golden_east() -> dict:
    return json.loads((FIXTURES / "anping_east01_raw.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def anping_east_path() -> Path:
    if not ANPING_EAST.exists():
        pytest.skip("安平小区 fixture image is not available on this machine")
    return ANPING_EAST


@pytest.fixture(scope="session")
def anping_sample_dir() -> Path:
    if not ANPING_DIR.is_dir():
        pytest.skip("安平小区 fixture images are not available on this machine")
    return ANPING_DIR

"""Layout-first bus poster OCR pipeline.

The package is deliberately dependency-light: numpy/Pillow for layout, RapidOCR
only when the local backend is instantiated.  The batch entry point is
``buspipe.batch.run_batch``.
"""

from buspipe.config import (
    DEFAULT_ROUTE_HINTS,
    CanvasSettings,
    LayoutSettings,
    OcrSettings,
    PipelineSettings,
)

__all__ = [
    "DEFAULT_ROUTE_HINTS",
    "CanvasSettings",
    "LayoutSettings",
    "OcrSettings",
    "PipelineSettings",
]

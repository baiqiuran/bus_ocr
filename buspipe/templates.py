"""Template-family profiles for layout thresholds.

The default is intentionally legacy-compatible: no profile means the existing
``PipelineSettings`` are used unchanged.  A profile is opt-in, matched by
width/height/aspect/orientation/template key/stem, and may only replace layout
or landscape settings for that family.  This is the seam for adding new
resolution-specific thresholds without changing old template branches.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Sequence

from buspipe.config import LandscapeSettings, LayoutSettings, PipelineSettings

Transform = Callable[[LayoutSettings, int, int], LayoutSettings]
LandscapeTransform = Callable[[LandscapeSettings, int, int], LandscapeSettings]


def template_key(width: int | None, height: int | None) -> str:
    if not width or not height:
        return ""
    orientation = "V" if height >= width else "L"
    return f"{orientation}_{width}x{height}"


@dataclass(frozen=True)
class TemplateProfile:
    """One opt-in family of posters.

    Matching is intentionally explicit.  A profile without a matcher would be
    dangerous: it could silently capture old templates.
    """

    name: str
    version: str = "1"
    min_width: int | None = None
    max_width: int | None = None
    min_height: int | None = None
    max_height: int | None = None
    min_aspect: float | None = None
    max_aspect: float | None = None
    orientation: str | None = None  # "V" | "L"
    template_keys: tuple[str, ...] = ()
    stem_contains: tuple[str, ...] = ()
    layout_transform: Transform | None = None
    landscape_transform: LandscapeTransform | None = None

    @property
    def cache_key(self) -> str:
        return f"{self.name}:{self.version}"

    def matches(
        self,
        width: int | None,
        height: int | None,
        stem: str,
    ) -> bool:
        if width is None or height is None or width <= 0 or height <= 0:
            return False
        aspect = width / height
        if self.min_width is not None and width < self.min_width:
            return False
        if self.max_width is not None and width > self.max_width:
            return False
        if self.min_height is not None and height < self.min_height:
            return False
        if self.max_height is not None and height > self.max_height:
            return False
        if self.min_aspect is not None and aspect < self.min_aspect:
            return False
        if self.max_aspect is not None and aspect > self.max_aspect:
            return False
        key = template_key(width, height)
        if self.orientation and not key.startswith(self.orientation + "_"):
            return False
        if self.template_keys and key not in self.template_keys:
            return False
        if self.stem_contains and not any(token in stem for token in self.stem_contains):
            return False
        return True

    def apply(
        self,
        settings: PipelineSettings,
        width: int,
        height: int,
    ) -> PipelineSettings:
        layout = (
            self.layout_transform(settings.layout, width, height)
            if self.layout_transform
            else settings.layout
        )
        landscape = (
            self.landscape_transform(settings.landscape, width, height)
            if self.landscape_transform
            else settings.landscape
        )
        if layout is settings.layout and landscape is settings.landscape:
            return settings
        return replace(settings, layout=layout, landscape=landscape)


# Add a TemplateProfile here only after a new family has a fixed baseline and a
# regression fixture.  Keeping this tuple empty preserves every existing
# template branch exactly.
PROFILES: tuple[TemplateProfile, ...] = ()


def resolve_template_profile(
    width: int | None,
    height: int | None,
    stem: str,
    profiles: Sequence[TemplateProfile] | None = None,
) -> TemplateProfile | None:
    for profile in (PROFILES if profiles is None else profiles):
        if profile.matches(width, height, stem):
            return profile
    return None


def apply_template_profile(
    settings: PipelineSettings,
    width: int | None,
    height: int | None,
    stem: str,
    profiles: Sequence[TemplateProfile] | None = None,
) -> tuple[PipelineSettings, str]:
    """Return the effective settings and profile cache key (``legacy`` by default)."""
    profile = resolve_template_profile(width, height, stem, profiles=profiles)
    if profile is None:
        return settings, "legacy"
    return profile.apply(settings, int(width), int(height)), profile.cache_key


def template_profile_key(
    width: int | None,
    height: int | None,
    stem: str,
    profiles: Sequence[TemplateProfile] | None = None,
) -> str:
    """Cache-key component for a profile; empty means the legacy cache key."""
    profile = resolve_template_profile(width, height, stem, profiles=profiles)
    return profile.cache_key if profile is not None else ""

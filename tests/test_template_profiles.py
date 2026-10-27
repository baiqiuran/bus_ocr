from dataclasses import replace

from buspipe.config import PipelineSettings
from buspipe.templates import (
    TemplateProfile,
    apply_template_profile,
    resolve_template_profile,
    template_key,
    template_profile_key,
)


def test_no_profile_is_legacy_and_does_not_change_settings():
    settings = PipelineSettings()
    effective, key = apply_template_profile(settings, 4253, 9745, "legacy-poster")
    assert key == "legacy"
    assert effective is settings
    assert effective.layout == settings.layout
    assert effective.fingerprint() == settings.fingerprint()
    assert template_profile_key(4253, 9745, "legacy-poster") == ""


def test_profile_matches_dimensions_and_changes_only_the_profile_family():
    profile = TemplateProfile(
        name="wide_vertical",
        version="1",
        min_width=8000,
        orientation="V",
        layout_transform=lambda layout, width, height: replace(layout, stop_x1=8000),
    )
    settings = PipelineSettings()
    effective, key = apply_template_profile(
        settings, 8505, 19489, "wide-poster", profiles=(profile,)
    )
    assert key == "wide_vertical:1"
    assert effective.layout.stop_x1 == 8000
    assert settings.layout.stop_x1 != 8000
    assert effective.fingerprint() != settings.fingerprint()

    old, old_key = apply_template_profile(
        settings, 4253, 9745, "wide-poster", profiles=(profile,)
    )
    assert old_key == "legacy"
    assert old is settings


def test_builtin_8505_and_4726_profiles_are_isolated_from_legacy():
    settings = PipelineSettings()

    wide, wide_key = apply_template_profile(settings, 8505, 19489, "wide")
    assert wide_key == "vertical_8505_ratio:1"
    assert wide.layout.route_scan_x1 == 2381
    assert wide.layout.stop_x1 == 8080
    assert wide.layout.row_project_x1 == 7995
    assert wide.layout.route_max_height == 680

    wide4, wide4_key = apply_template_profile(settings, 8504, 19489, "wide4")
    assert wide4_key == "vertical_8505_ratio:1"
    assert wide4.layout.stop_x1 == 8079

    medium, medium_key = apply_template_profile(settings, 4726, 7678, "medium")
    assert medium_key == "vertical_4726_right:1"
    assert medium.layout.stop_x1 == settings.layout.stop_x1
    assert medium.layout.row_project_x1 == 4253
    assert medium.layout.route_x1 == settings.layout.route_x1

    for width, height in ((4253, 9745), (2836, 6497), (2042, 4678)):
        effective, key = apply_template_profile(settings, width, height, "legacy")
        assert key == "legacy"
        assert effective is settings


def test_profile_requires_explicit_key_or_aspect_match():
    profile = TemplateProfile(
        name="wide_landscape",
        template_keys=("L_5000x1000",),
        min_aspect=2.0,
        landscape_transform=lambda landscape, width, height: replace(landscape, glyph_height=80),
    )
    assert resolve_template_profile(5000, 1000, "x", profiles=(profile,)) is profile
    assert resolve_template_profile(5001, 1000, "x", profiles=(profile,)) is None
    assert template_key(8505, 19489) == "V_8505x19489"
    assert template_key(5000, 1000) == "L_5000x1000"

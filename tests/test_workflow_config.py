from app.core.config import settings
from app.modules.payflow.services.workflow_suggestion import config


def test_every_catalog_band_has_a_definition():
    for field, labels in config.SEGMENT_FIELDS.items():
        assert labels[0].startswith("All"), field
        assert set(labels[1:]) == set(config.BAND_DEFINITIONS[field]), field


def test_numeric_ranges_have_no_gaps_or_overlaps():
    for ranges in (
        config.DELINQUENCY_RANGES,
        config.BALANCE_RANGES,
        config.AGE_RANGES,
        config.TENURE_RANGES,
    ):
        bounds = sorted(ranges.values(), key=lambda r: r[0])
        for (_, high), (next_low, _) in zip(bounds, bounds[1:]):
            assert high == next_low
        assert bounds[-1][1] is None


def test_limits_are_sane():
    assert 0 < config.MIN_STEPS <= config.MAX_STEPS
    assert 0 < config.MAX_COMMUNICATIONS <= config.MAX_STEPS
    assert config.MIN_SEGMENT_SIZE > 0 and config.MAX_SEGMENTS > 0


def test_groq_settings_have_defaults():
    assert settings.GROQ_MODEL
    assert settings.GROQ_TIMEOUT_SECONDS > 0

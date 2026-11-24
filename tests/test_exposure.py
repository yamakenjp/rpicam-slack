import pytest
from PIL import Image

from rpicam_slack.config import BudgetConfig, ExposureConfig
from rpicam_slack.exposure import (
    ImageStats,
    command_timeout_s,
    effective_target,
    is_converged,
    label,
    max_shutter_for,
    measure_image,
    next_total,
    plan_exposure,
    shot_cost_s,
)

CFG = ExposureConfig()


def stats(luma: float, high: float = 0.0, low: float = 0.0) -> ImageStats:
    return ImageStats(luma=luma, clipped_high=high, clipped_low=low)


@pytest.mark.parametrize(
    ("total", "shutter_s", "gain", "limited"),
    [
        (0.001, 0.001, 1.0, None),  # bright: unity gain, short shutter
        (0.0002, 0.0005, 1.0, "bright"),  # cannot go darker than the minimum shutter
        (0.05, 0.05, 1.0, None),  # still unity gain at the comfortable shutter
        (0.08, 0.05, 1.6, None),  # then gain rises first ...
        (1.0, 0.5, 2.0, None),  # ... then the shutter lengthens at the preferred gain
        (24.0, 12.0, 2.0, None),
        (100.0, 12.0, 8.3333, None),  # ... then gain rises again
        (1000.0, 12.0, 16.0, "dark"),  # hardware limit
    ],
)
def test_plan_exposure_prefers_low_noise(total, shutter_s, gain, limited):
    planned = plan_exposure(total, CFG, max_shutter_s=CFG.max_shutter_s)
    assert planned.shutter_s == pytest.approx(shutter_s, rel=1e-3)
    assert planned.gain == pytest.approx(gain, rel=1e-3)
    assert planned.limited == limited


def test_plan_exposure_honours_a_lower_shutter_cap():
    planned = plan_exposure(8.0, CFG, max_shutter_s=0.5)
    assert (planned.shutter_s, planned.gain, planned.limited) == (0.5, 16.0, None)
    assert plan_exposure(20.0, CFG, max_shutter_s=0.5).limited == "dark"


def test_plan_exposure_keeps_the_requested_brightness():
    for total in (0.003, 0.07, 0.9, 7.0, 20.0):
        planned = plan_exposure(total, CFG, max_shutter_s=12.0)
        assert planned.total == pytest.approx(total, rel=1e-3)


def test_next_total_is_a_fixed_point_at_the_target():
    assert next_total(2.0, stats(CFG.target_luma), CFG) == pytest.approx(2.0)


def test_next_total_scales_with_the_gamma_model():
    # half the luma needs about 2 ** gamma times more exposure
    assert next_total(1.0, stats(CFG.target_luma / 2), CFG) == pytest.approx(4.0)
    assert next_total(1.0, stats(CFG.target_luma * 2), CFG) == pytest.approx(0.25)


def test_next_total_limits_a_single_step():
    assert next_total(1.0, stats(0.0), CFG) == CFG.step_limit  # a black frame says nothing
    tight = ExposureConfig(step_limit=2.0)
    assert next_total(1.0, stats(1.0), tight) == pytest.approx(2.0)
    assert next_total(1.0, stats(255.0), tight) == pytest.approx(0.5)


def test_blown_highlights_are_never_brightened():
    assert next_total(1.0, stats(60.0, high=0.5), CFG) == 1.0
    assert next_total(1.0, stats(200.0, high=0.5), CFG) < 1.0


def test_ev_bias_moves_the_target_and_the_convergence_point():
    biased = ExposureConfig(ev=1.0)
    assert effective_target(biased) > effective_target(CFG)
    luma = effective_target(biased)
    assert is_converged(stats(luma), biased)
    assert next_total(1.0, stats(luma), biased) == pytest.approx(1.0)


def test_is_converged_uses_the_tolerance():
    assert is_converged(stats(CFG.target_luma + CFG.tolerance), CFG)
    assert not is_converged(stats(CFG.target_luma + CFG.tolerance + 1), CFG)


def test_label_thresholds():
    assert label(0.001, CFG) == "day"
    assert label(0.1, CFG) == "twilight"
    assert label(5.0, CFG) == "night"


def test_measure_image_reads_luma_and_clipping(tmp_path):
    grey = tmp_path / "grey.jpg"
    Image.new("RGB", (80, 45), (128, 128, 128)).save(grey, quality=95)
    result = measure_image(grey)
    assert result.luma == pytest.approx(128, abs=2)
    assert result.clipped_high == 0

    white = tmp_path / "white.png"
    Image.new("L", (40, 40), 255).save(white)
    assert measure_image(white).clipped_high == 1.0


def test_cost_model_covers_a_long_exposure():
    """Regression: a 12 s final shot took ~53 s in the field, far beyond 'timeout + shutter + 10'."""
    budget = BudgetConfig()
    cost = shot_cost_s(12.0, 2000, budget)
    assert cost > 53
    assert command_timeout_s(cost, budget) > cost


def test_max_shutter_for_inverts_the_cost_model():
    budget = BudgetConfig()
    shutter = max_shutter_for(30.0, 2000, budget)
    assert shot_cost_s(shutter, 2000, budget) == pytest.approx(30.0)
    assert max_shutter_for(1.0, 2000, budget) == 0.0

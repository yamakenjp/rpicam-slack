"""The shipped example files must stay loadable: they are what people copy first."""

from pathlib import Path

from rpicam_slack.config import ExposureConfig, load_config, read_slack_options

ROOT = Path(__file__).resolve().parent.parent


def test_the_example_configuration_loads_and_matches_the_defaults():
    cfg = load_config([ROOT / "config" / "camera.example.toml"])
    assert cfg.exposure == ExposureConfig()  # the example documents the defaults, nothing else
    assert cfg.camera.width == 2304 and cfg.budget.max_total_s == 110.0


def test_the_per_camera_example_layers_on_top():
    cfg = load_config(
        [ROOT / "config" / "camera.example.toml", ROOT / "config" / "camera.local.example.toml"]
    )
    assert cfg.name == "cam2" and cfg.camera.lens_position == 4.5
    assert cfg.camera.width == 2304  # still from the shared file


def test_the_slack_option_example_is_valid():
    options = read_slack_options(ROOT / "config" / "slack_option.example")
    assert options.channel == "C0123456789"
    assert "{profile}" in options.comment_template

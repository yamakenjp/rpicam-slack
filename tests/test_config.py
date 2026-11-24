import pytest

from rpicam_slack.config import (
    ConfigError,
    config_from_dict,
    default_config_files,
    insecure_permissions,
    load_config,
    read_slack_options,
)


def test_defaults_are_valid():
    cfg = config_from_dict({})
    assert cfg.camera.lens_position is None
    assert cfg.exposure.max_tests == 4
    assert cfg.camera_name  # the hostname


def test_layers_merge_with_later_files_winning(tmp_path):
    base = tmp_path / "camera.toml"
    base.write_text('name = "base"\n[camera]\nwidth = 1920\nheight = 1080\n', encoding="utf-8")
    local = tmp_path / "camera.local.toml"
    local.write_text('name = "cam2"\n[camera]\nlens_position = 4.5\n', encoding="utf-8")
    cfg = load_config([base, local])
    assert cfg.name == "cam2"
    assert (cfg.camera.width, cfg.camera.height) == (1920, 1080)  # kept from the base file
    assert cfg.camera.lens_position == 4.5


def test_unknown_keys_are_rejected_with_the_valid_names():
    with pytest.raises(ConfigError, match=r"\[exposure\] unknown key\(s\): lux_twilight_min"):
        config_from_dict({"exposure": {"lux_twilight_min": 0.5}})
    with pytest.raises(ConfigError, match="unknown top-level"):
        config_from_dict({"expsure": {}})


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"camera": {"width": "wide"}}, r"\[camera\] width must be an integer"),
        ({"camera": {"quality": 0}}, "quality must be 1..100"),
        ({"camera": {"hdr": "maybe"}}, "hdr must be one of"),
        ({"camera": {"hdr": "multi-exp"}}, "hdr must be one of"),
        ({"camera": {"lens_position": "near"}}, "lens_position must be a number"),
        ({"exposure": {"max_tests": 0}}, "max_tests must be 1..10"),
        ({"exposure": {"max_gain": 1.5, "preferred_gain": 2.0}}, "min_gain <= preferred_gain <= max_gain"),
        ({"exposure": {"max_shutter_s": 0.01}}, "min_shutter_us <= comfortable_shutter_s"),
        ({"budget": {"max_total_s": 20, "upload_reserve_s": 30}}, "upload_reserve_s too large"),
        ({"slack": {"retries": 0}}, "retries must be 1..10"),
        ({"output": {"embed_exif": "yes"}}, "embed_exif must be true or false"),
    ],
)
def test_invalid_values_are_rejected(data, message):
    with pytest.raises(ConfigError, match=message):
        config_from_dict(data)


def test_invalid_toml_reports_the_file(tmp_path):
    bad = tmp_path / "camera.toml"
    bad.write_text("[camera\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="camera.toml: invalid TOML"):
        load_config([bad])


def test_integers_are_accepted_for_float_fields():
    cfg = config_from_dict({"camera": {"lens_position": 4}, "exposure": {"target_luma": 120}})
    assert cfg.camera.lens_position == 4.0
    assert cfg.exposure.target_luma == 120.0


def test_environment_variable_selects_the_files(monkeypatch, tmp_path):
    one, two = tmp_path / "a.toml", tmp_path / "b.toml"
    monkeypatch.setenv("RPICAM_SLACK_CONFIG", f"{one}:{two}")
    assert default_config_files() == [one, two]


# --- the legacy Slack option file ------------------------------------------------------------


def write_options(tmp_path, text):
    path = tmp_path / "slack_option"
    path.write_text(text, encoding="utf-8")
    return path


def test_legacy_slack_option_file_is_read_unchanged(tmp_path):
    path = write_options(
        tmp_path,
        "# Slack bot token\nSLACK_TOKEN=xoxb-secret\n\nCHANNEL=C0123456789\n"
        "COMMENT_TEMPLATE=Photo taken at {timestamp}! profile={profile}\n",
    )
    options = read_slack_options(path)
    assert (options.token, options.channel) == ("xoxb-secret", "C0123456789")
    assert options.comment_template == "Photo taken at {timestamp}! profile={profile}"


def test_alternative_names_export_and_quotes(tmp_path):
    path = write_options(
        tmp_path, "export SLACK_BOT_TOKEN='xoxb-q'\nSLACK_CHANNEL_ID=\"C9\"\nSLACK_TOKEN=ignored\n"
    )
    options = read_slack_options(path)
    assert options.token == "xoxb-q"  # SLACK_BOT_TOKEN wins, as before
    assert options.channel == "C9"


def test_comment_template_defaults_and_is_validated(tmp_path):
    path = write_options(tmp_path, "SLACK_TOKEN=t\nCHANNEL=C1\n")
    assert "{timestamp}" in read_slack_options(path).comment_template
    path = write_options(tmp_path, "SLACK_TOKEN=t\nCHANNEL=C1\nCOMMENT_TEMPLATE={bogus}\n")
    with pytest.raises(ConfigError, match="unsupported field"):
        read_slack_options(path)


def test_missing_values_and_files_are_reported(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        read_slack_options(tmp_path / "nope")
    with pytest.raises(ConfigError, match="SLACK_BOT_TOKEN or SLACK_TOKEN"):
        read_slack_options(write_options(tmp_path, "CHANNEL=C1\n"))


def test_the_token_never_appears_in_repr(tmp_path):
    options = read_slack_options(write_options(tmp_path, "SLACK_TOKEN=xoxb-secret\nCHANNEL=C1\n"))
    assert "xoxb-secret" not in repr(options)


def test_permission_warning(tmp_path):
    path = write_options(tmp_path, "SLACK_TOKEN=t\nCHANNEL=C1\n")
    path.chmod(0o664)
    assert "chmod 600" in insecure_permissions(path)
    path.chmod(0o600)
    assert insecure_permissions(path) is None

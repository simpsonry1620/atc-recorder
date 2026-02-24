from pathlib import Path

from atc_recorder.config import load_config


def test_load_config_uses_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    cfg = load_config()

    assert cfg.output_dir == Path("./recordings")
    assert cfg.segment_duration == 1800
    assert cfg.request_delay == 1.0


def test_load_config_uses_cwd_config_yaml_when_present(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "output_dir: ./custom-output\nsegment_duration: 120\nrequest_delay: 2.5\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    cfg = load_config()

    assert cfg.output_dir == Path("./custom-output")
    assert cfg.segment_duration == 120
    assert cfg.request_delay == 2.5


def test_load_config_explicit_path_overrides_cwd_default(tmp_path, monkeypatch):
    cwd_cfg = tmp_path / "config.yaml"
    explicit_cfg = tmp_path / "custom.yaml"
    cwd_cfg.write_text("segment_duration: 60\n", encoding="utf-8")
    explicit_cfg.write_text("segment_duration: 999\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cfg = load_config(explicit_cfg)

    assert cfg.segment_duration == 999

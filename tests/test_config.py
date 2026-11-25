import os
from pathlib import Path

import pytest

from bunny_ai.config import ensure_data_dir, load_config
from bunny_ai.errors import ConfigError


def _isolate_platform_env(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.delenv("BUNNY_CONFIG", raising=False)
    monkeypatch.delenv("BUNNY_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(root / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(root / "localappdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "xdg-data"))
    monkeypatch.setenv("HOME", str(root / "home"))


def test_defaults_when_no_config_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_platform_env(monkeypatch, tmp_path)
    config = load_config()
    assert config.config_path is None
    assert config.storage.busy_timeout_ms == 5_000
    assert config.history.enabled is False
    assert config.history.retention_days == 30
    assert config.history.max_context_turns == 10
    assert config.onboarding.ask_intro_question is True


def test_toml_loading_and_relative_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_platform_env(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "bunny.toml"
    cfg_file.write_text(
        """
[storage]
data_dir = "state/profile-1"
busy_timeout_ms = 2500

[history]
enabled = true
retention_days = 14
max_context_turns = 5

[onboarding]
ask_intro_question = false
""".strip(),
        encoding="utf-8",
    )
    config = load_config(cli_config_path=cfg_file)
    assert config.config_path == cfg_file.resolve()
    assert config.data_dir == (cfg_dir / "state" / "profile-1").resolve()
    assert config.storage.busy_timeout_ms == 2500
    assert config.history.enabled is True
    assert config.history.retention_days == 14
    assert config.history.max_context_turns == 5
    assert config.onboarding.ask_intro_question is False


def test_explicit_data_dir_overrides_toml_and_ignores_host_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_platform_env(monkeypatch, tmp_path)
    # Create invalid host-default config to prove explicit --data-dir ignores it.
    for host_cfg in (
        tmp_path / "appdata" / "bunny-ai",
        tmp_path / "xdg-config" / "bunny-ai",
        tmp_path / "home" / "Library" / "Application Support" / "bunny-ai",
    ):
        host_cfg.mkdir(parents=True, exist_ok=True)
        (host_cfg / "config.toml").write_text("not = valid = toml", encoding="utf-8")

    explicit_dir = tmp_path / "isolated-data"
    config = load_config(cli_data_dir=explicit_dir)
    assert config.data_dir == explicit_dir.resolve()
    assert config.config_path is None
    assert config.history.enabled is False


def test_explicit_data_dir_reads_local_config_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_platform_env(monkeypatch, tmp_path)
    data_dir = tmp_path / "profile"
    data_dir.mkdir()
    local_cfg = data_dir / "config.toml"
    local_cfg.write_text("[history]\nenabled = true\n", encoding="utf-8")

    config = load_config(cli_data_dir=data_dir)
    assert config.config_path == local_cfg.resolve()
    assert config.history.enabled is True


@pytest.mark.parametrize(
    "toml_body",
    [
        "[unknown]\nvalue = 1\n",
        "[storage]\nunknown_key = 1\n",
        '[storage]\napi_key = "sk-example"\n',
        '[history]\ntoken = "nope"\n',
        "[storage]\nbusy_timeout_ms = true\n",
        "[storage]\nbusy_timeout_ms = 50\n",
        "[history]\nenabled = 1\n",
        "[history]\nretention_days = 0\n",
        "[history]\nmax_context_turns = 999\n",
        "[onboarding]\nask_intro_question = 'yes'\n",
        "not = valid = toml",
    ],
)
def test_invalid_toml_rejected(
    toml_body: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_platform_env(monkeypatch, tmp_path)
    cfg_file = tmp_path / "bad.toml"
    cfg_file.write_text(toml_body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(cli_config_path=cfg_file)


def test_missing_explicit_config_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_platform_env(monkeypatch, tmp_path)
    with pytest.raises(ConfigError):
        load_config(cli_config_path=tmp_path / "missing.toml")


def test_non_utf8_config_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_platform_env(monkeypatch, tmp_path)
    cfg_file = tmp_path / "bad-encoding.toml"
    cfg_file.write_bytes(b"\xff\xfe\x00\x00")
    with pytest.raises(ConfigError):
        load_config(cli_config_path=cfg_file)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory mode semantics")
def test_ensure_data_dir_posix_permissions_new_and_existing(tmp_path: Path) -> None:
    existing_parent = tmp_path / "shared-parent"
    existing_parent.mkdir(mode=0o755)
    os.chmod(existing_parent, 0o755)

    new_dir = existing_parent / "nested" / "bunny-data"
    ensure_data_dir(new_dir)
    assert (existing_parent.stat().st_mode & 0o777) == 0o755
    assert ((existing_parent / "nested").stat().st_mode & 0o777) == 0o700
    assert (new_dir.stat().st_mode & 0o777) == 0o700

    existing_dir = tmp_path / "existing-user-dir"
    existing_dir.mkdir(mode=0o755)
    os.chmod(existing_dir, 0o755)
    ensure_data_dir(existing_dir)
    assert (existing_dir.stat().st_mode & 0o777) == 0o755


def test_ensure_data_dir_posix_mode_behavior_mocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chmod_calls: list[tuple[Path, int]] = []

    def fake_chmod(path: Path, mode: int) -> None:
        chmod_calls.append((path, mode))

    monkeypatch.setattr("bunny_ai.config.os.name", "posix")
    monkeypatch.setattr("bunny_ai.config.os.chmod", fake_chmod)

    existing_parent = tmp_path / "existing-parent"
    existing_parent.mkdir()
    new_dir = existing_parent / "child" / "data"
    ensure_data_dir(new_dir)
    assert chmod_calls == [
        (existing_parent / "child", 0o700),
        (new_dir, 0o700),
    ]

    chmod_calls.clear()
    ensure_data_dir(existing_parent)
    assert chmod_calls == []

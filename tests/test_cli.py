from importlib.metadata import version
from pathlib import Path

import pytest

from bunny_ai.cli import main


@pytest.mark.parametrize("args", [[], ["--help"]])
def test_help(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main(args)
    assert result.value.code == 0
    assert "demo" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == f"Bunny.ai {version('bunny-ai')}"


def test_demo_is_simulated_and_leaves_no_files(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "Hello Bunny"]) == 0
    output = capsys.readouterr()
    assert "simulated" in output.out
    assert "11 characters" in output.out
    assert output.err == ""
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("args", [["demo"], ["demo", "   "], ["chat"]])
def test_invalid_input_fails(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as result:
        main(args)
    assert result.value.code == 2
    assert "error:" in capsys.readouterr().err

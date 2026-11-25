import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]

SYNTHETIC_FILES: dict[str, str] = {
    ".env": "BUNNY_PLACEHOLDER=example-value\n",
    ".env.local": "BUNNY_PLACEHOLDER=local-example\n",
    "memory.sqlite": "placeholder-sqlite",
    "memory.sqlite3": "placeholder-sqlite3",
    "memory.sqlite3-wal": "placeholder-wal",
    "state.db": "placeholder-db",
    "state.db-shm": "placeholder-shm",
    "nested/.env": "BUNNY_PLACEHOLDER=nested-example\n",
    "nested/cache.sqlite3": "placeholder-nested-sqlite3",
}


def _copy_package_tree(destination: Path) -> None:
    for relative_path in (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "src/bunny_ai/__init__.py",
        "src/bunny_ai/cli.py",
    ):
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative_path, target)


def _run_uv_build(
    uv_executable: str,
    project_dir: Path,
    out_dir: Path,
    *,
    wheel_only: bool = False,
) -> None:
    command = [
        uv_executable,
        "build",
        "--offline",
        "--no-cache",
        "--python",
        sys.executable,
        "--out-dir",
        str(out_dir),
    ]
    if wheel_only:
        command.append("--wheel")
    command.append(str(project_dir))
    subprocess.run(command, check=True, capture_output=True, text=True)


def test_archives_exclude_env_and_sqlite_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    _copy_package_tree(project_dir)

    package_dir = project_dir / "src" / "bunny_ai"
    for relative_path, content in SYNTHETIC_FILES.items():
        target = package_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    uv_executable = shutil.which("uv")
    assert uv_executable is not None

    dist_dir = tmp_path / "dist"
    direct_wheel_dir = tmp_path / "dist-direct-wheel"
    _run_uv_build(uv_executable, project_dir, dist_dir)
    _run_uv_build(uv_executable, project_dir, direct_wheel_dir, wheel_only=True)

    sdists = list(dist_dir.glob("*.tar.gz"))
    wheels = list(dist_dir.glob("*.whl"))
    direct_wheels = list(direct_wheel_dir.glob("*.whl"))
    assert len(sdists) == 1
    assert len(wheels) == 1
    assert len(direct_wheels) == 1

    synthetic_names = {PurePosixPath(path).name for path in SYNTHETIC_FILES}

    with tarfile.open(sdists[0], "r:gz") as sdist_archive:
        sdist_members = sdist_archive.getnames()

    leaked_sdist = [
        member
        for member in sdist_members
        if PurePosixPath(member).name in synthetic_names
    ]
    assert leaked_sdist == [], f"Sdist leaked synthetic files: {leaked_sdist}"

    sdist_relative = {
        "/".join(PurePosixPath(member).parts[1:])
        for member in sdist_members
        if len(PurePosixPath(member).parts) > 1
    }
    assert {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "src/bunny_ai/__init__.py",
        "src/bunny_ai/cli.py",
    }.issubset(sdist_relative)

    for label, wheel_path in (
        ("wheel", wheels[0]),
        ("direct wheel", direct_wheels[0]),
    ):
        with zipfile.ZipFile(wheel_path) as wheel_archive:
            wheel_members = wheel_archive.namelist()

        leaked_wheel = [
            member
            for member in wheel_members
            if PurePosixPath(member).name in synthetic_names
        ]
        assert leaked_wheel == [], f"{label} leaked synthetic files: {leaked_wheel}"

        assert {"bunny_ai/__init__.py", "bunny_ai/cli.py"}.issubset(wheel_members)
        assert any(
            member.endswith(".dist-info/licenses/LICENSE") for member in wheel_members
        )

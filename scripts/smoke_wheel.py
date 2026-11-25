"""Install one wheel without an index and exercise its console script."""

import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

NETWORK_GUARD = """\
import socket


def denied(*args, **kwargs):
    raise RuntimeError("Bunny smoke test blocked the network")


socket.socket = denied
socket.getaddrinfo = denied
"""

IDENTITY_RE = re.compile(r"^Identity:\s+([0-9a-f-]+)$", re.MULTILINE)


def _extract_identity(output: str) -> str:
    match = IDENTITY_RE.search(output)
    if match is None:
        raise RuntimeError(f"Could not find Identity in output:\n{output}")
    return match.group(1)


def smoke_test(wheel: Path) -> None:
    with TemporaryDirectory(prefix="bunny-wheel-") as directory:
        root = Path(directory)
        environment = root / "venv"
        bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        bunny = bin_dir / ("bunny.exe" if os.name == "nt" else "bunny")

        subprocess.run(
            ["uv", "venv", "--offline", "--python", sys.executable, str(environment)],
            check=True,
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--offline",
                "--no-deps",
                "--python",
                str(python),
                str(wheel),
            ],
            check=True,
        )

        guard_dir = root / "guard"
        guard_dir.mkdir()
        (guard_dir / "sitecustomize.py").write_text(NETWORK_GUARD, encoding="utf-8")
        run_env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}
        }
        run_env.update(PYTHONPATH=str(guard_dir), PYTHONNOUSERSITE="1")
        probe = subprocess.run(
            [str(python), "-c", "import socket; socket.socket()"],
            cwd=root,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if (
            probe.returncode == 0
            or "Bunny smoke test blocked the network" not in probe.stderr
        ):
            raise RuntimeError("Network guard did not load in the wheel environment")

        for args, expected in (
            (["--version"], "Bunny.ai "),
            (["--help"], "Run one simulated offline turn"),
            (
                ["demo", "Hello Bunny"],
                "[Offline demo, simulated] Received 11 characters.",
            ),
        ):
            result = subprocess.run(
                [str(bunny), *args],
                cwd=root,
                env=run_env,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            if expected not in result.stdout or result.stderr:
                raise RuntimeError(f"Unexpected output for bunny {args[0]}")
            print(result.stdout.rstrip())

        smoke_data = root / "smoke-data"
        pre_status = subprocess.run(
            [str(bunny), "status", "--data-dir", str(smoke_data)],
            cwd=root,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if "State: not initialized" not in pre_status.stdout or smoke_data.exists():
            raise RuntimeError("Status before awakening altered state unexpectedly")

        first = subprocess.run(
            [
                str(bunny),
                "chat",
                "--data-dir",
                str(smoke_data),
                "--skip-intro",
                "-m",
                "Hello",
            ],
            cwd=root,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if "First launch" not in first.stdout or first.stderr:
            raise RuntimeError(f"Unexpected first-launch output:\n{first.stdout}")
        first_id = _extract_identity(first.stdout)

        restart = subprocess.run(
            [str(bunny), "chat", "--data-dir", str(smoke_data), "-m", "Back again"],
            cwd=root,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if (
            "Returning" not in restart.stdout
            or "Welcome back!" not in restart.stdout
            or restart.stderr
        ):
            raise RuntimeError(f"Unexpected restart output:\n{restart.stdout}")
        if _extract_identity(restart.stdout) != first_id:
            raise RuntimeError("Restart did not preserve Bunny identity")

        post_status = subprocess.run(
            [str(bunny), "status", "--data-dir", str(smoke_data)],
            cwd=root,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if (
            first_id not in post_status.stdout
            or "Saved turns: 0" not in post_status.stdout
        ):
            raise RuntimeError(f"Unexpected post-status output:\n{post_status.stdout}")
        print(f"Wheel smoke verified identity continuity: {first_id}")


if __name__ == "__main__":
    wheels = sorted(Path("dist").resolve().glob("bunny_ai-*.whl"))
    if len(wheels) != 1:
        sys.exit("Expected exactly one Bunny.ai wheel in dist/.")
    smoke_test(wheels[0])

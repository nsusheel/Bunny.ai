"""Install one wheel without an index and exercise its console script."""

import os
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


if __name__ == "__main__":
    wheels = sorted(Path("dist").resolve().glob("bunny_ai-*.whl"))
    if len(wheels) != 1:
        sys.exit("Expected exactly one Bunny.ai wheel in dist/.")
    smoke_test(wheels[0])

"""Drive an installed ``bunny chat`` process interactively for CLI tests."""

from __future__ import annotations

import queue
import re
import subprocess
import threading
from pathlib import Path
from types import TracebackType
from typing import IO

from test_cli_integration import _installed_bunny

PROMPTS = ("You> ", "You (optional)> ", "[y/N] ")
SHORT_ID_RE = re.compile(r"\[([0-9a-f]{8})\]")


class ChatDriver:
    """A live chat subprocess that returns output up to each prompt."""

    def __init__(self, args: list[str], env: dict[str, str], cwd: Path) -> None:
        child_env = {**env, "PYTHONUNBUFFERED": "1"}
        self._proc = subprocess.Popen(
            [str(_installed_bunny()), "chat", *args],
            cwd=cwd,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        self._chars: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.opening = self.read()

    def _pump(self) -> None:
        stdout: IO[str] | None = self._proc.stdout
        assert stdout is not None
        while True:
            char = stdout.read(1)
            if char == "":
                self._chars.put(None)
                return
            self._chars.put(char)

    def read(self, timeout: float = 15.0) -> str:
        buffer = ""
        while not buffer.endswith(PROMPTS):
            try:
                char = self._chars.get(timeout=timeout)
            except queue.Empty:
                raise AssertionError(f"Timed out waiting for a prompt; got:\n{buffer}")
            if char is None:
                break
            buffer += char
        return buffer

    def send(self, line: str) -> str:
        stdin = self._proc.stdin
        assert stdin is not None
        stdin.write(line + "\n")
        stdin.flush()
        return self.read()

    def finish(self) -> int:
        stdin = self._proc.stdin
        assert stdin is not None
        try:
            stdin.write("/exit\n")
            stdin.flush()
            stdin.close()
        except OSError:
            pass
        return self._proc.wait(timeout=15)

    def __enter__(self) -> ChatDriver:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait(timeout=5)


def short_ids(output: str) -> list[str]:
    """Return the 8-character fact ids printed as ``[abcd1234]``."""
    return SHORT_ID_RE.findall(output)

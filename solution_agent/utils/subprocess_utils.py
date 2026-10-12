from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, Union


Command = Union[Sequence[str], str]


@dataclass(frozen=True)
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str
    command: Command


def decode_output(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not data:
        return ""
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def subprocess_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def hidden_subprocess_kwargs(*, new_process_group: bool = False, detached: bool = False) -> dict[str, object]:
    if os.name != "nt":
        return {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if new_process_group:
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if detached:
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
    startupinfo.wShowWindow = 0
    return {"creationflags": creationflags, "startupinfo": startupinfo}


def run_subprocess(
    cmd: Command,
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    check: bool = False,
    shell: bool = False,
    env: Mapping[str, str] | None = None,
    stdin: int | None = subprocess.DEVNULL,
) -> SubprocessResult:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        timeout=timeout,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=shell,
        env=subprocess_env(env),
        **hidden_subprocess_kwargs(),
    )
    stdout = decode_output(completed.stdout)
    stderr = decode_output(completed.stderr)
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, cmd, output=stdout, stderr=stderr)
    return SubprocessResult(completed.returncode, stdout, stderr, cmd)

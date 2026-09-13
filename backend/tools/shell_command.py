from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import Enum


class ShellKind(str, Enum):
    POSIX = "posix"
    POWERSHELL = "powershell"
    CMD = "cmd"


@dataclass(frozen=True)
class ResolvedShell:
    executable: str
    kind: ShellKind

    def derive_argv(self, command: str, *, login: bool) -> list[str]:
        if self.kind is ShellKind.POWERSHELL:
            return [self.executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        if self.kind is ShellKind.CMD:
            return [self.executable, "/D", "/S", "/C", command]
        return [self.executable, "-lc" if login else "-c", command]


def resolve_shell(requested: str | None) -> ResolvedShell:
    if requested:
        name = requested.strip()
        lowered = os.path.basename(name).lower()
        if lowered in {"pwsh", "pwsh.exe", "powershell", "powershell.exe"}:
            executable = shutil.which(name) or name
            return ResolvedShell(executable, ShellKind.POWERSHELL)
        if lowered in {"cmd", "cmd.exe"}:
            executable = shutil.which(name) or name
            return ResolvedShell(executable, ShellKind.CMD)
        executable = shutil.which(name) or name
        if os.name == "nt" and lowered not in {"bash", "sh", "zsh", "fish"}:
            raise ValueError(f"不支持的 shell：{requested}")
        return ResolvedShell(executable, ShellKind.POSIX)

    if os.name == "nt":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if executable:
            return ResolvedShell(executable, ShellKind.POWERSHELL)
        return ResolvedShell("cmd.exe", ShellKind.CMD)
    executable = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    return ResolvedShell(executable, ShellKind.POSIX)

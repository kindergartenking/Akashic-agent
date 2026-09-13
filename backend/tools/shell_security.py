from __future__ import annotations

import ipaddress
import os
import shlex
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse

_IS_WINDOWS = os.name == "nt"
_BANNED = frozenset({"nc", "netcat", "telnet", "curlie", "axel", "aria2c", "lynx", "w3m", "links", "chrome", "firefox", "safari"})
_NETWORK_CMDS = frozenset({"curl", "wget", "http", "httpie", "xh"})
_NET_WRITE_FLAGS = frozenset({"-o", "-O", "-T", "-F", "--output", "--remote-name", "--upload-file", "--form", "--form-string", "--output-document", "--post-file", "@"})
_RESTRICTED_META_CHARS = ("|", ";", "&", ">", "<", "`", "$(")
_RESTRICTED_RUNNERS = frozenset({"sh", "bash", "zsh", "fish", "python", "python3", "node", "perl", "ruby", "php", "lua", "powershell", "pwsh", "cmd"})


def validate_command(command: str, *, allow_network: bool, restricted_dir: Path | None, cwd: Path | None = None) -> str | None:
    try:
        tokens = _split_command(command)
    except ValueError:
        return "命令解析失败，请检查引号是否匹配"
    if not tokens:
        return None
    name = _command_name(tokens[0])
    if name in _BANNED:
        return f"命令 '{name}' 不被允许（安全限制）"
    if not allow_network and name in _NETWORK_CMDS:
        return "当前 shell 配置禁止网络访问"
    if restricted_dir is not None:
        error = _validate_restricted_cwd(cwd, restricted_dir)
        if error:
            return error
        if any(marker in command for marker in _RESTRICTED_META_CHARS):
            return "受限 shell 禁止管道、重定向或串联命令"
        if name in _RESTRICTED_RUNNERS:
            return f"受限 shell 禁止启动解释器或二级 shell：{name}"
        for token in tokens[1:]:
            if token.startswith("-") or token == "--":
                continue
            if _looks_like_path(token) and _path_escapes(token, restricted_dir):
                return f"受限 shell 禁止访问任务目录外路径：{token}"
    return validate_network_command(command, tokens=tokens)


def validate_network_command(command: str, *, tokens: list[str] | None = None) -> str | None:
    if tokens is None:
        try:
            tokens = _split_command(command)
        except ValueError:
            return "命令解析失败，请检查引号是否匹配"
    if not tokens or _command_name(tokens[0]) not in _NETWORK_CMDS:
        return None
    for token in tokens[1:]:
        lowered = token.lower()
        if lowered in _NET_WRITE_FLAGS or any(lowered.startswith(flag + "=") for flag in _NET_WRITE_FLAGS if flag != "@") or token.startswith("@") or "=@" in token:
            return f"网络命令参数 '{token}' 不被允许（禁止上传/写文件）"
    urls = [token for token in tokens[1:] if token.startswith(("http://", "https://"))]
    if not urls:
        return "网络命令必须显式提供 http:// 或 https:// URL"
    for url in urls:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            return "仅允许 http:// 或 https:// URL"
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return f"禁止访问内网/本地地址：{host}"
        except ValueError:
            if host.endswith((".local", ".localhost")):
                return f"禁止访问本地域名：{host}"
    return None


def _split_command(command: str) -> list[str]:
    return [token.strip("\"'") for token in shlex.split(command, posix=not _IS_WINDOWS)]


def _command_name(token: str) -> str:
    return (PureWindowsPath(token).name if _IS_WINDOWS else Path(token).name).lower().removesuffix(".exe")


def _validate_restricted_cwd(cwd: Path | None, root: Path) -> str | None:
    if cwd is None:
        return None
    try:
        resolved = cwd.expanduser().resolve()
        allowed = root.expanduser().resolve()
    except OSError:
        resolved, allowed = cwd, root
    if resolved != allowed and allowed not in resolved.parents:
        return f"受限 shell 禁止使用任务目录外工作目录：{cwd}"
    return None


def _looks_like_path(token: str) -> bool:
    return token in {".", ".."} or "/" in token or "\\" in token or token.startswith((".", "~"))


def _path_escapes(token: str, root: Path) -> bool:
    token = token.strip("\"'")
    if token.startswith("~") or ".." in Path(token).parts:
        return True
    candidate = Path(token)
    if not candidate.is_absolute():
        return False
    try:
        resolved = candidate.resolve()
        allowed = root.resolve()
    except OSError:
        return True
    return resolved != allowed and allowed not in resolved.parents


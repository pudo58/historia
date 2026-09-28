"""Framed, read-only shell probes for terminal-only SSH gateways.

The script is encoded before sending so terminal echo cannot look like results.
Each command carries its real exit code and base64-encoded merged output.
"""
import base64
import re
import textwrap

from ghm.executors.base import CommandResult


class PreflightTransportError(RuntimeError):
    pass


class PTYRequiredError(PreflightTransportError):
    """The gateway rejected an exec request, even if its channel exit code is zero."""


def requires_pty(result: CommandResult) -> bool:
    text = (result.stdout + result.stderr).lower()
    return "doesn't support pty" in text or "does not support pty" in text or "pty is required" in text


def probe_script(commands: dict[str, str], nonce: str) -> str:
    lines = []
    for name, command in commands.items():
        if not re.fullmatch(r"[a-z_]+", name):
            raise ValueError("Invalid probe identifier")
        lines += [f"output=$( ( {command}\n) 2>&1)", "rc=$?",
                  f"printf '\\n{nonce}:{name}:%s:' \"$rc\"",
                  "printf '%s' \"$output\" | base64 | tr -d '\\r\\n'", "printf '\\n'"]
    encoded = base64.b64encode("\n".join(lines).encode()).decode()
    delimiter = "GHM_INPUT_" + nonce
    return f"base64 -d <<'{delimiter}' | sh\n" + "\n".join(textwrap.wrap(encoded, 76)) + f"\n{delimiter}\n"


def parse_frames(output: str, nonce: str, names: set[str]) -> dict[str, CommandResult]:
    # CSI/OSC terminal controls may surround prompts or carriage returns.
    clean = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", output)
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", clean).replace("\r", "")
    results = {}
    for match in re.finditer(r"(?m)^" + re.escape(nonce) + r":([a-z_]+):(\d+):([A-Za-z0-9+/=]*)\n", clean):
        name, rc, encoded = match.groups()
        if name not in names or name in results:
            raise PreflightTransportError("SSH trả kết quả kiểm tra trùng hoặc không hợp lệ.")
        try:
            value = base64.b64decode(encoded, validate=True).decode("utf-8", errors="replace")
        except ValueError as exc:
            raise PreflightTransportError("SSH trả kết quả kiểm tra bị hỏng.") from exc
        results[name] = CommandResult(int(rc), value, "")
    return results

"""Turn a pytest `--tb=short` failure body into the few lines that matter."""

from __future__ import annotations

import re
from dataclasses import dataclass

# "path\to\file.py:12: in func" - the optional drive letter keeps "C:\x\y.py" intact.
_FRAME = re.compile(r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+?):(?P<line>\d+): in (?P<func>.+)$")
_FINAL = re.compile(r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+?):(?P<line>\d+): (?P<exc>\S.*)$")
_NOISE_PATH = re.compile(r"site-packages|/_pytest/|/pluggy/|/lib/python|/Lib/|<frozen|node_modules/|node:")
_LOC = re.compile(r"^(?P<file>(?:[A-Za-z]:)?[^\s:][^:]*?\.\w+):(?P<line>\d+): ?(?P<rest>.*)$")
_CARET_ONLY = re.compile(r"^\s*[\^~]+\s*$")
_SECTION = re.compile(r"^-{3,} Captured (?P<what>[A-Za-z ]+?) -{3,}$")

MAX_FRAMES = 5
MAX_E_LINES = 12
MAX_TEXT = 1500


@dataclass
class Frame:
    file: str
    line: int
    func: str
    code: str

    def as_dict(self) -> dict[str, object]:
        return {"file": self.file, "line": self.line, "in": self.func, "code": self.code}


def norm_path(p: str) -> str:
    return p.replace("\\", "/")


def parse_traceback(text: str) -> tuple[list[Frame], list[str]]:
    """Return (all frames outermost-first, the 'E ' lines with the prefix removed)."""
    frames: list[Frame] = []
    e_lines: list[str] = []
    cur: Frame | None = None
    for raw in text.splitlines():
        if _CARET_ONLY.match(raw):
            continue
        if raw.startswith("E ") or raw == "E":
            e_lines.append(raw[1:].strip() if raw.startswith("E ") else "")
            cur = None
            continue
        m = _FRAME.match(raw)
        if m:
            cur = Frame(norm_path(m["file"]), int(m["line"]), m["func"], "")
            frames.append(cur)
            continue
        if cur is not None and raw.startswith("    ") and not cur.code:
            cur.code = raw.strip()
    if not frames:
        # --tb=long prints "path:line: ExcType" after each frame's source instead.
        for raw in text.splitlines():
            m2 = _LOC.match(raw)
            if m2:
                frames.append(Frame(norm_path(m2["file"]), int(m2["line"]), "", ""))
    return frames, e_lines


def relevant_frames(frames: list[Frame]) -> tuple[list[Frame], int]:
    """Drop library frames, then keep the first and the last few. Returns (kept, dropped)."""
    own = [f for f in frames if not _NOISE_PATH.search(f.file)]
    if not own:
        own = frames[-1:]
    dropped = len(frames) - len(own)
    if len(own) > MAX_FRAMES:
        dropped += len(own) - MAX_FRAMES
        own = own[:1] + own[-(MAX_FRAMES - 1):]
    return own, dropped


def clip(text: str, limit: int = MAX_TEXT) -> str:
    """Keep the END of long text (the interesting part of output is the tail)."""
    text = text.strip("\n")
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    return f"[... {cut} chars trimmed ...]\n" + text[-limit:]


def clip_middle(text: str, limit: int) -> str:
    text = text.strip("\n")
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - limit} chars trimmed ...]\n{text[-half:]}"


def split_captured(text: str) -> tuple[str, str]:
    """Split a failure body into (traceback, captured output) at the first
    '--- Captured stdout call ---' style heading."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if _SECTION.match(ln.strip()):
            return "\n".join(lines[:i]), "\n".join(lines[i:])
    return text, ""


def strip_banners(body: str) -> str:
    """junit_logging=all wraps captured text in 'Captured Out/Err/Log' banners
    that are empty most of the time. Return just the text."""
    kept = [
        ln for ln in body.splitlines()
        if not re.match(r"^-{3,} Captured (Out|Err|Log) -{3,}$", ln.strip())
    ]
    return "\n".join(kept).strip()

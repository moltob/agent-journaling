#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Generic mechanics of the agentic-journaling skill.

Commands:
  init   create the machine file entry and a config note in a journal folder
  check  validate configuration and preview open and stuck requests (read-only)
  begin  start sweeps: log sweep-start, render docs, discover and claim requests
  report write one request's result (done or error) back to the journal
  end    log sweep-end, with counts, for sweeps started by begin
  rename rename a file in the open Obsidian vault and verify its backlinks survive
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

# Fixed constants.
TAG = "#agent"
ID_PREFIX = "aj-block-"
RUN_PREFIX = "aj-run-"
SWEEP_PREFIX = "aj-sweep-"
TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
OPEN, IN_PROGRESS, DONE, ERROR, CANCELLED = " ", "/", "x", "!", "-"
STATUS_NAMES = {OPEN: "open", IN_PROGRESS: "in-progress", DONE: "done", ERROR: "error", CANCELLED: "cancelled"}
CAS_RETRIES = 3
CAS_BACKOFF_S = 0.5
VERIFY_DELAY_S = 3.0
RECENT_S = 30
SCHEMA_VERSION = 1
CONFIG_NOTE_NAME = "Agentic Journaling.md"
DOCS_START = "%% agentic-journal:docs start (maintained by the agent, edits here are replaced) %%"
DOCS_END = "%% agentic-journal:docs end %%"
LOG_NAME = "agentic-journal"
DEFAULT_RETENTION_DAYS = 30

SCRIPT = Path(__file__).resolve()
DOCS_TEMPLATE = SCRIPT.parent.parent / "templates" / "docs.md"

TAG_RE = re.compile(r"(?<!\S)#agent(?![\w/-])")
ITEM_RE = re.compile(r"^([ \t]*)([-*+]|\d+[.)])([ \t]+)(.*)$")
TASK_RE = re.compile(r"^\[(.)\](?=[ \t]|$)")
STATUS_RE = re.compile(r"^([ \t]*(?:[-*+]|\d+[.)])[ \t]+\[).(\])")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
AGENT_ID_RE = re.compile(r"[ \t]+\^(aj-block-[a-z0-9]{4})[ \t]*$")
RUN_ID_RE = re.compile(r"^aj-run-[a-z0-9]{4}$")
BLOCK_ARG_RE = re.compile(r"^aj-block-[a-z0-9]{4}$")
BLOCK_ID_RE = re.compile(r"[ \t]+\^([A-Za-z0-9-]+)[ \t]*$")
FEEDBACK_RE = re.compile(r"\bRun `?aj-run-[a-z0-9]{4}`?\.[ \t]*$")
LOGFMT_RE = re.compile(r'([A-Za-z_][\w-]*)=("(?:[^"\\]|\\.)*"|\S*)')
ALIASES_RE = re.compile(r"^aliases:[ \t]*(.*)$", re.IGNORECASE)
URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


class AJError(Exception):
    pass


class Gone(AJError):
    """The request can no longer be located or is no longer in the expected state."""


# ---------------------------------------------------------------- logging


def _fmt(value: object) -> str:
    s = str(value)
    if s and not re.search(r'[\s"=\\]', s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


class LogfmtFormatter(logging.Formatter):
    """An ISO 8601 timestamp, then logfmt pairs: sweep, ev, then the record's fields."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = dt.datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")
        fields: dict = dict(getattr(record, "fields", {}))
        parts = [stamp, f"sweep={_fmt(fields.pop('sweep', '-'))}", f"ev={_fmt(record.getMessage())}"]
        parts += [f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None]
        return " ".join(parts)


_logger = logging.getLogger(LOG_NAME)


def log(ev: str, **fields: object) -> None:
    """One event: a single logging call reaches the log file and stdout."""
    _logger.log(logging.ERROR if ev == "error" else logging.INFO, ev, extra={"fields": fields})


def rotate_log(path: Path, retention_days: int) -> None:
    """Daily rotation and retention, done once at process start.

    Several short-lived processes share the file, so a handler that renames it mid-run would break
    (on Windows the rename fails while another process holds the file). A failed rename leaves the
    file in place; the next process retries.
    """
    today = dt.date.today()
    try:
        last = dt.date.fromtimestamp(path.stat().st_mtime)
        rotated = path.with_name(f"{path.name}.{last.isoformat()}")
        if last < today and not rotated.exists():
            os.rename(path, rotated)
    except OSError:
        pass
    cutoff = today - dt.timedelta(days=retention_days)
    for old in path.parent.glob(f"{path.name}.*"):
        try:
            if dt.date.fromisoformat(old.name[len(path.name) + 1 :]) < cutoff:
                old.unlink()
        except (ValueError, OSError):
            pass


def setup_logging(machine: Machine) -> None:
    machine.log_file.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(machine.log_file, machine.retention_days)
    formatter = LogfmtFormatter()
    for handler in (logging.FileHandler(machine.log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        handler.setFormatter(formatter)
        _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def sweep_lines(machine: Machine, sweep: str) -> list[tuple[str, dict[str, str]]]:
    """(timestamp, fields) of every log line of one sweep, from the current and the newest rotated file."""
    path = machine.log_file
    rotated = sorted(path.parent.glob(f"{path.name}.*"))
    needle = re.compile(rf"(?:^| )sweep={re.escape(sweep)}(?: |$)")
    out = []
    for f in [*rotated[-1:], path]:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if needle.search(line):
                stamp, _, rest = line.partition(" ")
                out.append((stamp, {k: (json.loads(v) if v.startswith('"') else v) for k, v in LOGFMT_RE.findall(rest)}))
    return out


def end_sweep(machine: Machine, sweep: str, journal: str | None = None) -> None:
    lines = sweep_lines(machine, sweep)
    evs = [f.get("ev") for _, f in lines]
    states = [f.get("status") for _, f in lines if f.get("ev") == "state"]
    start = next((s for s, f in lines if f.get("ev") == "sweep-start"), None)
    journal = journal or next((f.get("journal") for _, f in lines if f.get("ev") == "sweep-start"), None)
    duration = None
    if start:
        try:
            duration = f"{(dt.datetime.now().astimezone() - dt.datetime.fromisoformat(start)).total_seconds():.0f}s"
        except ValueError:
            pass
    log("sweep-end", sweep=sweep, journal=journal, duration=duration,
        found=evs.count("discover") + evs.count("skipped"), completed=states.count(STATUS_NAMES[DONE]),
        failed=states.count(STATUS_NAMES[ERROR]), skipped=evs.count("skipped"))


# ---------------------------------------------------------------- configuration


def _home_dir(windows_var: str, mac: str, xdg_var: str, xdg_default: str) -> Path:
    if os.name == "nt":
        return Path(os.environ[windows_var])
    if sys.platform == "darwin":
        return Path.home() / mac
    return Path(os.environ.get(xdg_var) or xdg_default).expanduser()


def machine_path() -> Path:
    base = _home_dir("APPDATA", "Library/Application Support", "XDG_CONFIG_HOME", "~/.config")
    return base / LOG_NAME / "config.yaml"


def default_log_file() -> Path:
    base = _home_dir("LOCALAPPDATA", "Library/Logs", "XDG_STATE_HOME", "~/.local/state")
    return base / LOG_NAME / f"{LOG_NAME}.log"


@dataclass
class Machine:
    journals: dict[str, Path]
    log_file: Path
    retention_days: int


def load_machine() -> Machine:
    path = machine_path()
    if not path.exists():
        raise AJError(f"machine file {path} not found; create it with: uv run {SCRIPT} init --name <name> <journal folder>")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise AJError(f"{path}: expected a mapping")
    unknown = sorted(set(data) - {"version", "log", "journals"})
    if unknown:
        raise AJError(f"{path}: unknown keys {', '.join(unknown)}")
    if data.get("version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise AJError(f"{path}: unsupported version {data.get('version')!r}, expected {SCHEMA_VERSION}")
    log_cfg = data.get("log") or {}
    if not isinstance(log_cfg, dict) or set(log_cfg) - {"file", "retention_days"}:
        raise AJError(f"{path}: log takes only file and retention_days")
    log_file = Path(log_cfg["file"]).expanduser() if log_cfg.get("file") else default_log_file()
    retention = log_cfg.get("retention_days", DEFAULT_RETENTION_DAYS)
    if not isinstance(retention, int) or isinstance(retention, bool) or retention < 1:
        raise AJError(f"{path}: log.retention_days must be a whole number of days, at least 1")
    journals = data.get("journals")
    if not isinstance(journals, dict) or not journals:
        raise AJError(f"{path}: journals needs at least one <name>: <config note path> entry")
    out: dict[str, Path] = {}
    for name, note in journals.items():
        if not isinstance(note, str) or not note.strip():
            raise AJError(f"{path}: journals.{name} must be a path to a config note")
        out[str(name)] = Path(note).expanduser()
    return Machine(out, log_file, retention)


@dataclass
class Target:
    name: str
    aliases: list[str]
    line: int
    empty: bool


@dataclass
class NoteInfo:
    targets: list[Target]
    has_targets_heading: bool
    docs_span: tuple[int, int] | None
    h1_line: int | None
    errors: list[str]
    warnings: list[str]


@dataclass
class Journal:
    name: str
    note: Path
    vault: Path
    folder: Path
    info: NoteInfo
    indent_unit: str

    def rel(self, path: Path) -> str:
        return path.relative_to(self.vault).as_posix()


def find_vault(start: Path) -> Path | None:
    for parent in [start, *start.parents]:
        if (parent / ".obsidian").is_dir():
            return parent
    return None


def load_journal(name: str, note: Path) -> Journal:
    """Load a journal; raises AJError on problems that make a run impossible."""
    note = note.resolve()
    if not note.is_file():
        raise AJError(f"config note {note} not found")
    vault = find_vault(note.parent)
    if vault is None:
        raise AJError(f"no vault: no parent folder of {note} contains .obsidian/")
    settings_file = vault / ".obsidian" / "daily-notes.json"
    folder_setting = ""
    if settings_file.exists():
        folder_setting = (json.loads(settings_file.read_text(encoding="utf-8")) or {}).get("folder", "") or ""
    folder = (vault / folder_setting.strip("/")).resolve()
    if not folder.is_dir():
        raise AJError(f"Daily Notes folder {folder} does not exist")
    info = parse_note(read_text(note))
    if not settings_file.exists():
        info.warnings.append(f"{settings_file} missing: scanning the whole vault as the Daily Notes folder")
    if info.errors:
        raise AJError(f"{note}: " + "; ".join(info.errors))
    app = {}
    app_file = vault / ".obsidian" / "app.json"
    if app_file.exists():
        app = json.loads(app_file.read_text(encoding="utf-8")) or {}
    unit = "\t" if app.get("useTab", True) else " " * int(app.get("tabSize", 4))
    return Journal(name, note, vault, folder, info, unit)


# ---------------------------------------------------------------- markdown model


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def indent_width(ws: str) -> int:
    width = 0
    for ch in ws:
        width = (width // 4 + 1) * 4 if ch == "\t" else width + 1
    return width


@dataclass
class Line:
    raw: str
    kind: str  # meta, blank, comment, code, heading, item, text
    indent: int
    masked: str = ""
    level: int = 0  # heading level
    status: str | None = None  # task checkbox character
    is_request: bool = False


def _mask(s: str, in_comment: bool) -> tuple[str, bool]:
    """Blank out inline code spans and %% comments, keeping column positions."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if in_comment:
            j = s.find("%%", i)
            if j < 0:
                out.append(" " * (n - i))
                break
            out.append(" " * (j + 2 - i))
            i, in_comment = j + 2, False
            continue
        if s[i] == "`":
            j = i
            while j < n and s[j] == "`":
                j += 1
            run, k, close = j - i, j, -1
            while k < n:
                if s[k] == "`":
                    m = k
                    while m < n and s[m] == "`":
                        m += 1
                    if m - k == run:
                        close = m
                        break
                    k = m
                else:
                    k += 1
            if close < 0:
                out.append(s[i:j])
                i = j
            else:
                out.append(" " * (close - i))
                i = close
            continue
        if s.startswith("%%", i):
            out.append("  ")
            i, in_comment = i + 2, True
            continue
        out.append(s[i])
        i += 1
    return "".join(out), in_comment


def parse(text: str) -> list[Line]:
    raws = text.split("\n")
    lines: list[Line] = []
    start = 0
    if raws and raws[0].strip() == "---":
        for j in range(1, len(raws)):
            if raws[j].strip() in ("---", "..."):
                lines += [Line(r, "meta", 0) for r in raws[: j + 1]]
                start = j + 1
                break
    fence: str | None = None
    in_comment = False
    for raw in raws[start:]:
        ws = raw[: len(raw) - len(raw.lstrip(" \t"))]
        ind = indent_width(ws)
        if fence:
            lines.append(Line(raw, "code", ind))
            if re.match(rf"^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", raw):
                fence = None
            continue
        if not in_comment and (m := FENCE_RE.match(raw)):
            fence = m.group(1)
            lines.append(Line(raw, "code", ind))
            continue
        started_in_comment = in_comment
        masked, in_comment = _mask(raw, in_comment)
        if not raw.strip():
            lines.append(Line(raw, "blank", ind))
        elif started_in_comment or (not masked.strip() and "%%" in raw):
            lines.append(Line(raw, "comment", ind, masked))
        elif hm := HEADING_RE.match(masked):
            lines.append(Line(raw, "heading", ind, masked, level=len(hm.group(1))))
        elif im := ITEM_RE.match(masked):
            body = masked[im.start(4):]
            line = Line(raw, "item", ind, masked)
            if tm := TASK_RE.match(body):
                line.status = tm.group(1)
                line.is_request = bool(TAG_RE.search(body[3:]))
            lines.append(line)
        else:
            lines.append(Line(raw, "text", ind, masked))
    return lines


def heading_text(line: Line) -> str:
    m = HEADING_RE.match(line.masked)
    return (m.group(2) or "").strip() if m else ""


def agent_id(line: Line) -> str | None:
    m = AGENT_ID_RE.search(line.masked)
    return m.group(1) if m else None


def instruction_of(line: Line) -> str:
    im = ITEM_RE.match(line.raw)
    body = im.group(4)[3:] if im else line.raw
    body = BLOCK_ID_RE.sub("", body)
    body = TAG_RE.sub("", body)
    return " ".join(body.split())


def children_end(lines: list[Line], idx: int) -> int:
    """Index after the last child line of the item at idx (trailing blank lines excluded)."""
    end, j = idx + 1, idx + 1
    while j < len(lines):
        ln = lines[j]
        if ln.kind == "blank":
            j += 1
            continue
        if ln.kind == "meta" or ln.indent <= lines[idx].indent:
            break
        j += 1
        end = j
    return end


# ---------------------------------------------------------------- config note


def parse_note(text: str) -> NoteInfo:
    lines = parse(text)
    info = NoteInfo([], False, None, None, [], [])
    meta = [ln.raw for ln in lines if ln.kind == "meta"]
    front = None
    if meta:
        try:
            front = yaml.safe_load("\n".join(meta[1:-1]))
        except yaml.YAMLError:
            front = None
    if front != {"agentic-journal": SCHEMA_VERSION}:
        info.errors.append(f"frontmatter must hold exactly one property, agentic-journal: {SCHEMA_VERSION}")
    first = next((i for i, ln in enumerate(lines) if ln.kind not in ("meta", "blank")), None)
    if first is not None and lines[first].kind == "heading" and lines[first].level == 1:
        info.h1_line = first
    else:
        info.warnings.append("the note does not start with an H1 heading")
    starts = [i for i, ln in enumerate(lines) if ln.raw.strip() == DOCS_START]
    ends = [i for i, ln in enumerate(lines) if ln.raw.strip() == DOCS_END]
    if len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]:
        info.docs_span = (starts[0], ends[0])
    elif starts or ends:
        info.errors.append("docs block markers are broken (need exactly one start and one end marker, in order)")
    else:
        info.warnings.append("docs block missing; the next run inserts it after the H1")

    def in_docs(i: int) -> bool:
        return info.docs_span is not None and info.docs_span[0] <= i <= info.docs_span[1]

    headings = [(i, ln) for i, ln in enumerate(lines) if ln.kind == "heading" and not in_docs(i)]
    t_idx = next((i for i, ln in headings if ln.level == 2 and heading_text(ln) == "Targets"), None)
    if t_idx is None:
        info.errors.append("missing ## Targets section")
        return info
    info.has_targets_heading = True
    after = [(i, ln) for i, ln in headings if i > t_idx]
    section_end = next((i for i, ln in after if ln.level <= 2), len(lines))
    starts3 = [(i, ln) for i, ln in after if i < section_end and ln.level == 3]
    for k, (i, ln) in enumerate(starts3):
        end = starts3[k + 1][0] if k + 1 < len(starts3) else section_end
        aliases: list[str] = []
        substantive = False
        for body in lines[i + 1 : end]:
            stripped = body.raw.strip()
            if not stripped:
                continue
            if body.kind == "text" and (am := ALIASES_RE.match(stripped)):
                aliases += [a.strip() for a in am.group(1).split(",") if a.strip()]
            else:
                substantive = True
        name = heading_text(ln)
        info.targets.append(Target(name, aliases, i + 1, not substantive))
    if not info.targets:
        info.warnings.append("## Targets has no ### target sections")
    seen: dict[str, str] = {}
    for t in info.targets:
        if t.empty:
            info.warnings.append(f"target {t.name!r} has an empty section")
        for key in [t.name, *t.aliases]:
            k = key.casefold()
            if k in seen and seen[k] != t.name:
                info.warnings.append(f"{key!r} names both target {seen[k]!r} and target {t.name!r}")
            seen.setdefault(k, t.name)
    return info


def render_docs() -> str:
    body = DOCS_TEMPLATE.read_text(encoding="utf-8").strip("\n")
    return f"{DOCS_START}\n{body}\n{DOCS_END}"


def apply_docs(text: str) -> str:
    info = parse_note(text)
    block = render_docs()
    raws = text.split("\n")
    if info.docs_span:
        s, e = info.docs_span
        if "\n".join(raws[s : e + 1]) == block:
            return text
        return "\n".join(raws[:s] + block.split("\n") + raws[e + 1 :])
    if info.h1_line is not None:
        at = info.h1_line + 1
    else:
        at = sum(1 for ln in parse(text) if ln.kind == "meta")
    return "\n".join(raws[:at] + ["", *block.split("\n"), ""] + raws[at:])


# ---------------------------------------------------------------- writes


def _signature(path: Path) -> tuple[bytes, tuple[int, int, str]]:
    data = path.read_bytes()
    st = path.stat()
    return data, (st.st_mtime_ns, st.st_size, hashlib.sha256(data).hexdigest())


def cas_write(path: Path, mutate, verify) -> bool:
    """Compare-and-swap write. mutate(text) -> new text (may raise Gone); verify(text) -> bool.

    Returns False when mutate made no change (nothing written).
    """
    delay = CAS_BACKOFF_S
    for attempt in range(CAS_RETRIES + 1):
        data, sig = _signature(path)
        text = data.decode("utf-8")
        crlf = "\r\n" in text
        new = mutate(text.replace("\r\n", "\n"))
        if new == text.replace("\r\n", "\n"):
            return False
        if crlf:
            new = new.replace("\n", "\r\n")
        tmp = path.with_name(f".{path.name}.agent-tmp")
        tmp.write_bytes(new.encode("utf-8"))
        shutil.copymode(path, tmp)
        if _signature(path)[1] == sig:
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                pass  # Windows: another process holds the file; retry like a concurrent change.
        tmp.unlink(missing_ok=True)
        if attempt == CAS_RETRIES:
            raise AJError(f"{path} kept changing or stayed locked during {CAS_RETRIES} retries")
        time.sleep(delay)
        delay *= 2
    time.sleep(VERIFY_DELAY_S)
    if not verify(read_text(path)):
        raise AJError(f"{path}: the change was not found when re-read {VERIFY_DELAY_S:g} s after writing")
    return True


def find_by_id(lines: list[Line], rid: str) -> int | None:
    hits = [i for i, ln in enumerate(lines) if ln.is_request and agent_id(ln) == rid]
    if len(hits) > 1:
        raise Gone(f"^{rid} appears on {len(hits)} request lines")
    return hits[0] if hits else None


def set_request(text: str, idx: int, status: str, rid: str, feedback: str | None, unit: str) -> str:
    lines = parse(text)
    raws = text.split("\n")
    line = raws[idx]
    line = STATUS_RE.sub(lambda m: f"{m.group(1)}{status}{m.group(2)}", line, count=1)
    if not AGENT_ID_RE.search(line):
        line = line.rstrip() + f" ^{rid}"
    raws[idx] = line
    if feedback is None:
        return "\n".join(raws)
    end = children_end(lines, idx)
    keep = [
        raws[j]
        for j in range(idx + 1, end)
        if not (lines[j].kind == "item" and FEEDBACK_RE.search(lines[j].masked))
    ]
    prefix = line[: len(line) - len(line.lstrip(" \t"))]
    sub = f"{prefix}{file_unit(lines) or unit}- {feedback}"
    return "\n".join(raws[: idx + 1] + [sub] + keep + raws[end:])


def file_unit(lines: list[Line]) -> str | None:
    """Indentation unit used for nested list items in this file, if any."""
    for ln in lines:
        if ln.kind == "item" and ln.indent > 0:
            ws = ln.raw[: len(ln.raw) - len(ln.raw.lstrip(" \t"))]
            return "\t" if ws.startswith("\t") else " " * len(ws) if len(ws) in (2, 3, 4) else None
    return None


def feedback_ok(run: str, link: str, summary: str, title: str | None, change: str | None) -> str:
    if URL_RE.match(link):
        target = link
        title = title or link
    else:
        target = link.replace(" ", "%20")
        title = title or Path(link).stem
    title = title.replace("[", "(").replace("]", ")")
    verb = {"created": "Created", "updated": "Updated"}.get(change or "", "Done")
    summary = " ".join(summary.split()).rstrip(".")
    return f"{verb} [{title}]({target}): {summary}. Run `{run}`."


def feedback_error(run: str, message: str) -> str:
    return f"Failed: {' '.join(message.split()).rstrip('.')}. Run `{run}`."


def finalize(journal: Journal, path: Path, rid: str, status: str, feedback: str) -> None:
    def mutate(text: str) -> str:
        lines = parse(text)
        idx = find_by_id(lines, rid)
        if idx is None:
            raise Gone(f"^{rid} not found in {journal.rel(path)}")
        if lines[idx].status != IN_PROGRESS:
            raise Gone(f"^{rid} is no longer in progress (status [{lines[idx].status}])")
        return set_request(text, idx, status, rid, feedback, journal.indent_unit)

    def verify(text: str) -> bool:
        lines = parse(text)
        idx = find_by_id(lines, rid)
        return idx is not None and lines[idx].status == status

    cas_write(path, mutate, verify)


# ---------------------------------------------------------------- discovery


@dataclass
class Found:
    path: Path
    idx: int
    raw: str
    status: str
    rid: str | None
    instruction: str


def rg_files(pattern: str, folder: Path, fixed: bool = True) -> list[Path]:
    if shutil.which("rg") is None:
        raise AJError("rg (ripgrep) not found on PATH")
    cmd = ["rg", "--files-with-matches", "--no-ignore", "--glob", "*.md"]
    cmd += ["--fixed-strings"] if fixed else []
    cmd += ["--", pattern, str(folder)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        raise AJError(f"rg failed: {proc.stderr.strip()}")
    return sorted(Path(p) for p in proc.stdout.splitlines() if p)


def discover(journal: Journal) -> tuple[list[Found], list[Found], list[Path]]:
    """Return open requests, stuck in-progress requests and files skipped as recently modified."""
    open_, stuck, recent = [], [], []
    now = time.time()
    for path in rg_files(TAG, journal.folder):
        if path.resolve() == journal.note:
            continue
        lines = parse(read_text(path))
        if not any(ln.is_request and ln.status in (OPEN, IN_PROGRESS) for ln in lines):
            continue
        if now - path.stat().st_mtime < RECENT_S:
            recent.append(path)
            continue
        for i, ln in enumerate(lines):
            if not ln.is_request or ln.status not in (OPEN, IN_PROGRESS):
                continue
            found = Found(path, i, ln.raw, ln.status, agent_id(ln), instruction_of(ln))
            (open_ if ln.status == OPEN else stuck).append(found)
    return open_, stuck, recent


def existing_ids(journal: Journal) -> set[str]:
    ids: set[str] = set()
    for path in rg_files(r"\^aj-block-[a-z0-9]{4}", journal.folder, fixed=False):
        ids.update(re.findall(r"\^(aj-block-[a-z0-9]{4})", read_text(path)))
    return ids


def new_token(n: int) -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(n))


def claim(journal: Journal, req: Found, rid: str) -> None:
    def mutate(text: str) -> str:
        lines = parse(text)
        idx = find_by_id(lines, rid) if req.rid else None
        if idx is None:
            if req.idx < len(lines) and lines[req.idx].raw == req.raw:
                idx = req.idx
            else:
                hits = [i for i, ln in enumerate(lines) if ln.raw == req.raw]
                if len(hits) != 1:
                    raise Gone("request line changed or moved")
                idx = hits[0]
        if not lines[idx].is_request or lines[idx].status != OPEN:
            raise Gone("request is no longer open")
        return set_request(text, idx, IN_PROGRESS, rid, None, journal.indent_unit)

    def verify(text: str) -> bool:
        lines = parse(text)
        idx = find_by_id(lines, rid)
        return idx is not None and lines[idx].status == IN_PROGRESS

    cas_write(req.path, mutate, verify)


# ---------------------------------------------------------------- commands


def journals_from(machine: Machine, only: str | None) -> list[str]:
    if only is None:
        return list(machine.journals)
    if only not in machine.journals:
        raise AJError(f"journal {only!r} is not in {machine_path()} (known: {', '.join(machine.journals)})")
    return [only]


def cmd_begin(args: argparse.Namespace) -> int:
    machine = load_machine()
    setup_logging(machine)
    results = []
    for name in journals_from(machine, args.journal):
        sweep = SWEEP_PREFIX + new_token(4)
        entry: dict = {"journal": name, "sweep": sweep, "ended": False, "requests": []}
        results.append(entry)
        log("sweep-start", sweep=sweep, journal=name)
        try:
            journal = load_journal(name, machine.journals[name])
        except AJError as exc:
            log("error", sweep=sweep, message=str(exc))
            end_sweep(machine, sweep, name)
            entry.update(ended=True, error=str(exc))
            continue
        entry.update(
            vault=str(journal.vault),
            config_note=str(journal.note),
            targets=[{"name": t.name, "aliases": t.aliases} for t in journal.info.targets],
        )
        try:
            cas_write(journal.note, apply_docs, lambda text: apply_docs(text) == text)
        except (AJError, OSError) as exc:
            log("error", sweep=sweep, message=f"docs block not rendered: {exc}")
        try:
            open_, stuck, _recent = discover(journal)
            ids = existing_ids(journal)
        except (AJError, OSError) as exc:
            log("error", sweep=sweep, message=str(exc))
            end_sweep(machine, sweep, name)
            entry.update(ended=True, error=str(exc))
            continue
        for s in stuck:
            log("error", sweep=sweep, block=s.rid, file=journal.rel(s.path), line=s.idx + 1,
                message="in progress, not claimed by this sweep")
        claimed = []
        # Bottom-up per file, so feedback lines inserted below a request never shift the ones above it.
        for req in sorted(open_, key=lambda f: (str(f.path), -f.idx)):
            block = req.rid
            while block is None or (block in ids and block != req.rid):
                block = ID_PREFIX + new_token(4)
            ids.add(block)
            run = RUN_PREFIX + new_token(4)
            rel = journal.rel(req.path)
            try:
                claim(journal, req, block)
            except (AJError, OSError) as exc:
                log("skipped", sweep=sweep, block=req.rid, file=rel, line=req.idx + 1, message=str(exc))
                continue
            log("discover", sweep=sweep, run=run, block=block, file=rel, text=req.instruction)
            claimed.append({
                "run": run,
                "block": block,
                "file": rel,
                "line": req.idx + 1,
                "instruction": req.instruction,
            })
        entry["requests"] = sorted(claimed, key=lambda r: (r["file"], r["line"]))
        if not claimed:
            end_sweep(machine, sweep, name)
            entry["ended"] = True
    print("== begin result ==")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    if not RUN_ID_RE.fullmatch(args.run):
        raise AJError(f"--run must look like aj-run-xxxx, got {args.run!r}")
    if not BLOCK_ARG_RE.fullmatch(args.block):
        raise AJError(f"--block must look like aj-block-xxxx, got {args.block!r}")
    block = args.block
    if args.error is None and (args.link is None or args.summary is None):
        raise AJError("report needs --link and --summary, or --error")
    machine = load_machine()
    name = journals_from(machine, args.journal)[0]
    journal = load_journal(name, machine.journals[name])
    setup_logging(machine)
    ids = {"sweep": args.sweep, "run": args.run, "block": block}
    log("action", **ids, target=args.target, link=args.link, summary=args.summary, error=args.error)
    if args.error is not None:
        status, text = ERROR, feedback_error(args.run, args.error)
    else:
        status, text = DONE, feedback_ok(args.run, args.link, args.summary, args.title, args.change)
    try:
        files = [p for p in rg_files(f"^{block}", journal.folder) if p.resolve() != journal.note]
        if len(files) != 1:
            raise Gone(f"^{block} found in {len(files)} files under {journal.rel(journal.folder) or '.'}")
        finalize(journal, files[0], block, status, text)
    except (AJError, OSError) as exc:
        log("error", **ids, message=str(exc))
        return 1
    log("state", **ids, status=STATUS_NAMES[status])
    return 0


def cmd_end(args: argparse.Namespace) -> int:
    machine = load_machine()
    setup_logging(machine)
    for sweep in args.sweep:
        end_sweep(machine, sweep)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    errors = 0
    print(f"machine file: {machine_path()}")
    try:
        machine = load_machine()
        names = journals_from(machine, args.journal)
    except AJError as exc:
        print(f"  ERROR {exc}")
        return 1
    print(f"log file: {machine.log_file} (kept {machine.retention_days} days)")
    for name in names:
        print(f"\njournal {name}: {machine.journals[name]}")
        try:
            journal = load_journal(name, machine.journals[name])
        except AJError as exc:
            print(f"  ERROR {exc}")
            errors += 1
            continue
        print(f"  vault root:     {journal.vault}")
        print(f"  journal folder: {journal.folder}")
        for w in journal.info.warnings:
            print(f"  WARNING {w}")
        print(f"  targets ({len(journal.info.targets)}):")
        for t in journal.info.targets:
            aliases = f" aliases: {', '.join(t.aliases)}" if t.aliases else ""
            print(f"    - {t.name} (line {t.line}){aliases}")
        text = read_text(journal.note)
        print(f"  docs block: {'up to date' if apply_docs(text) == text else 'a run would re-render it'}")
        try:
            open_, stuck, recent = discover(journal)
        except AJError as exc:
            print(f"  ERROR {exc}")
            errors += 1
            continue
        for p in recent:
            print(f"  NOTE {journal.rel(p)} changed in the last {RECENT_S} s; a run now would skip it")
        print(f"  open requests ({len(open_)}):")
        for f in open_:
            print(f"    {journal.rel(f.path)}:{f.idx + 1} {f.rid or '(new id)'} {f.instruction!r}")
        print(f"  stuck in progress [/] ({len(stuck)}):")
        for f in stuck:
            print(f"    {journal.rel(f.path)}:{f.idx + 1} {f.rid or '(no id)'} {f.instruction!r} -> set back to [ ] to retry")
    return 1 if errors else 0


def cmd_init(args: argparse.Namespace) -> int:
    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        raise AJError(f"{folder} is not a folder")
    vault = find_vault(folder)
    if vault is None:
        raise AJError(f"no parent folder of {folder} contains .obsidian/")
    note = folder / CONFIG_NOTE_NAME
    mpath = machine_path()
    data = {"version": SCHEMA_VERSION, "journals": {}}
    if mpath.exists():
        data = yaml.safe_load(mpath.read_text(encoding="utf-8")) or data
        data.setdefault("journals", {})
    home = str(Path.home())
    shown = "~" + str(note)[len(home):] if str(note).startswith(home + os.sep) else str(note)
    current = data["journals"].get(args.name)
    if current is not None and Path(current).expanduser().resolve() != note:
        raise AJError(f"journal {args.name!r} already points to {current}")
    if note.exists():
        print(f"config note exists, left unchanged: {note}")
    else:
        body = (
            f"---\nagentic-journal: {SCHEMA_VERSION}\n---\n# Agentic Journaling\n\n"
            f"{render_docs()}\n\n## Targets\n\n"
        )
        with note.open("x", encoding="utf-8") as fh:
            fh.write(body)
        print(f"created config note: {note}")
    if current is None:
        data["journals"][args.name] = shown
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(f"registered journal {args.name!r} in {mpath}")
    settings = vault / ".obsidian" / "daily-notes.json"
    daily = (json.loads(settings.read_text(encoding="utf-8")) or {}).get("folder", "") if settings.exists() else ""
    if (vault / daily.strip("/")).resolve() != folder:
        print(f"note: the Daily Notes folder is {vault / daily.strip('/')}; only that folder is scanned")
    return 0


def obsidian(*args: str) -> str:
    try:
        proc = subprocess.run(["obsidian", *args], capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        raise AJError("the obsidian CLI is not on PATH")
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or out.startswith("Error:"):
        raise AJError(f"obsidian {' '.join(args)} failed: {out or proc.stderr.strip()}")
    return out


def backlink_sources(path: str) -> set[str]:
    out = obsidian("backlinks", f"path={path}", "format=json")
    if not out or out.startswith("No backlinks"):
        return set()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {ln.split("\t")[0] for ln in out.splitlines() if ln.strip()}
    return {str(e.get("file") or e.get("path")) if isinstance(e, dict) else str(e) for e in data}


def cmd_rename(args: argparse.Namespace) -> int:
    info = {k: v for k, _, v in (ln.partition("\t") for ln in obsidian("vault").splitlines())}
    if "path" not in info:
        raise AJError("obsidian vault returned no path")
    root = Path(info["path"]).resolve()
    src = Path(args.file).expanduser().resolve()
    try:
        rel = src.relative_to(root).as_posix()
    except ValueError:
        raise AJError(f"{src} is not inside the open vault {root}")
    if not src.is_file():
        raise AJError(f"{src} does not exist")
    if "/" in args.name or "\\" in args.name:
        raise AJError("the new name must be a file name, not a path; use move to change folders")
    new_name = args.name if Path(args.name).suffix else args.name + src.suffix
    new_rel = (Path(rel).parent / new_name).as_posix()
    if (root / new_rel).exists():
        raise AJError(f"{new_rel} already exists")
    before = backlink_sources(rel)
    obsidian("rename", f"path={rel}", f"name={new_name}")
    if not (root / new_rel).is_file():
        raise AJError(f"rename did not produce {new_rel}")
    after = backlink_sources(new_rel)
    # A renamed file that links to itself shows up under its own path.
    before.discard(rel)
    after.discard(new_rel)
    lost = sorted(before - after)
    print(f"renamed {rel} -> {new_rel}")
    print(f"backlinks: {len(before)} before, {len(after)} after")
    for s in lost:
        print(f"  lost: {s}")
    return 1 if lost else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="aj.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create the machine file entry and a config note")
    p.add_argument("--name", required=True, help="journal name used in the machine file and logs")
    p.add_argument("folder", help="journal folder that gets 'Agentic Journaling.md'")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("check", help="validate configuration and preview requests (read-only)")
    p.add_argument("--journal")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("begin", help="start sweeps and claim open requests")
    p.add_argument("--journal")
    p.set_defaults(fn=cmd_begin)

    p = sub.add_parser("report", help="write one request's result")
    p.add_argument("--journal", required=True)
    p.add_argument("--sweep", required=True, help="aj-sweep-xxxx, from the begin result")
    p.add_argument("--run", required=True, help="aj-run-xxxx, from the begin result")
    p.add_argument("--block", required=True, help="aj-block-xxxx, from the begin result")
    p.add_argument("--target", help="matched target name")
    p.add_argument("--link", help="result: vault-relative path or URL")
    p.add_argument("--title", help="link text; default: file name or URL")
    p.add_argument("--change", choices=["created", "updated"], help="how the target changed")
    p.add_argument("--summary", help="one sentence on what was done")
    p.add_argument("--error", help="one sentence on why it failed")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("end", help="log sweep-end with counts")
    p.add_argument("--sweep", required=True, action="append")
    p.set_defaults(fn=cmd_end)

    p = sub.add_parser("rename", help="rename a vault file via the obsidian CLI and verify backlinks")
    p.add_argument("file", help="path of the file to rename; must be inside the open vault")
    p.add_argument("name", help="new file name (extension kept if omitted)")
    p.set_defaults(fn=cmd_rename)

    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page.
    args = parser.parse_args()
    try:
        return args.fn(args)
    except AJError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

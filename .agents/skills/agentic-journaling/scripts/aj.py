#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Generic mechanics of the agentic-journaling skill.

Commands:
  init   create the machine file entry and a config note in a journal folder
  check  validate configuration and preview open and stuck requests (read-only)
  begin  start runs: log run-start, render docs, discover and claim requests
  report write one request's result (done or error) back to the journal
  end    log run-end for runs started by begin
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Constants fixed by the specification.
TAG = "#agent"
ID_PREFIX = "agent-"
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
LOG_IDENTIFIER = "agentic-journal"
JOURNALD_SOCKET = "/run/systemd/journal/socket"

SCRIPT = Path(__file__).resolve()
DOCS_TEMPLATE = SCRIPT.parent.parent / "templates" / "docs.md"

TAG_RE = re.compile(r"(?<!\S)#agent(?![\w/-])")
ITEM_RE = re.compile(r"^([ \t]*)([-*+]|\d+[.)])([ \t]+)(.*)$")
TASK_RE = re.compile(r"^\[(.)\](?=[ \t]|$)")
STATUS_RE = re.compile(r"^([ \t]*(?:[-*+]|\d+[.)])[ \t]+\[).(\])")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
AGENT_ID_RE = re.compile(r"[ \t]+\^(agent-[a-z0-9]{4})[ \t]*$")
BLOCK_ID_RE = re.compile(r"[ \t]+\^([A-Za-z0-9-]+)[ \t]*$")
FEEDBACK_RE = re.compile(r"\bRun r[a-z0-9]{3}\.[ \t]*$")
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


class Log:
    """One logfmt line per event on stdout, mirrored to journald when its socket exists."""

    def __init__(self) -> None:
        self.sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.connect(JOURNALD_SOCKET)
            self.sock = sock
        except OSError:
            self.sock = None

    def emit(self, run: str, ev: str, **fields: object) -> None:
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        parts = [stamp, f"run={run}", f"ev={ev}"]
        parts += [f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None]
        line = " ".join(parts)
        print(line, flush=True)
        if self.sock is None:
            return
        priority = 3 if ev == "error" else 6
        payload = f"MESSAGE={line}\nSYSLOG_IDENTIFIER={LOG_IDENTIFIER}\nPRIORITY={priority}\n"
        try:
            self.sock.send(payload.encode())
        except OSError:
            self.sock = None


# ---------------------------------------------------------------- configuration


def machine_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "agentic-journal" / "config.yaml"


def load_machine() -> dict[str, Path]:
    path = machine_path()
    if not path.exists():
        raise AJError(f"machine file {path} not found; create it with: uv run {SCRIPT} init --name <name> <journal folder>")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise AJError(f"{path}: expected a mapping")
    unknown = sorted(set(data) - {"version", "journals"})
    if unknown:
        raise AJError(f"{path}: unknown keys {', '.join(unknown)}")
    if data.get("version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise AJError(f"{path}: unsupported version {data.get('version')!r}, expected {SCHEMA_VERSION}")
    journals = data.get("journals")
    if not isinstance(journals, dict) or not journals:
        raise AJError(f"{path}: journals needs at least one <name>: <config note path> entry")
    out: dict[str, Path] = {}
    for name, note in journals.items():
        if not isinstance(note, str) or not note.strip():
            raise AJError(f"{path}: journals.{name} must be a path to a config note")
        out[str(name)] = Path(note).expanduser()
    return out


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


def dedent(raw: str, width: int) -> str:
    col, i = 0, 0
    while i < len(raw) and raw[i] in " \t" and col < width:
        col = (col // 4 + 1) * 4 if raw[i] == "\t" else col + 1
        i += 1
    return raw[i:]


@dataclass
class Scope:
    content: str = ""
    parent: str | None = None
    heading: str | None = None
    error: str | None = None


def compute_scope(lines: list[Line], r: int) -> Scope:
    level = lines[r].indent
    top: int | None = None
    i = r - 1
    while i >= 0:
        ln = lines[i]
        if ln.kind in ("blank", "comment"):
            i -= 1
            continue
        if ln.kind in ("heading", "meta"):
            break
        if ln.indent > level:
            i -= 1
            continue
        if ln.kind == "item" and ln.indent == level:
            if ln.is_request:
                break
            top = i
            i -= 1
            continue
        break
    scope = Scope()
    for j in range(r - 1, -1, -1):
        ln = lines[j]
        if scope.parent is None and ln.kind == "item" and ln.indent < level:
            scope.parent = ln.raw.strip()
        if ln.kind == "heading":
            scope.heading = ln.raw.strip()
            break
    if top is None:
        scope.error = "the request has no items above it at the same list level"
        return scope
    body = [dedent(ln.raw, level) for ln in lines[top:r]]
    while body and not body[-1].strip():
        body.pop()
    scope.content = "\n".join(body)
    return scope


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


def render_docs(targets: list[Target]) -> str:
    if targets:
        listing = "\n".join(
            f"- [[#{t.name}]]" + (f" (aliases: {', '.join(t.aliases)})" if t.aliases else "") for t in targets
        )
    else:
        listing = "No targets yet. Add one under [[#Targets]]."
    body = DOCS_TEMPLATE.read_text(encoding="utf-8").replace("{{targets}}", listing).strip("\n")
    return f"{DOCS_START}\n{body}\n{DOCS_END}"


def apply_docs(text: str) -> str:
    info = parse_note(text)
    block = render_docs(info.targets)
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
            os.replace(tmp, path)
            break
        tmp.unlink(missing_ok=True)
        if attempt == CAS_RETRIES:
            raise AJError(f"{path} kept changing during {CAS_RETRIES} retries")
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
    return f"{verb} [{title}]({target}): {summary}. Run {run}."


def feedback_error(run: str, message: str) -> str:
    return f"Failed: {' '.join(message.split()).rstrip('.')}. Run {run}."


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
    scope: Scope


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
            found = Found(path, i, ln.raw, ln.status, agent_id(ln), instruction_of(ln), compute_scope(lines, i))
            (open_ if ln.status == OPEN else stuck).append(found)
    return open_, stuck, recent


def existing_ids(journal: Journal) -> set[str]:
    ids: set[str] = set()
    for path in rg_files(r"\^agent-[a-z0-9]{4}", journal.folder, fixed=False):
        ids.update(re.findall(r"\^(agent-[a-z0-9]{4})", read_text(path)))
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


def journals_from(machine: dict[str, Path], only: str | None) -> list[str]:
    if only is None:
        return list(machine)
    if only not in machine:
        raise AJError(f"journal {only!r} is not in {machine_path()} (known: {', '.join(machine)})")
    return [only]


def cmd_begin(args: argparse.Namespace) -> int:
    log = Log()
    machine = load_machine()
    results = []
    for name in journals_from(machine, args.journal):
        run = "r" + new_token(3)
        entry: dict = {"journal": name, "run": run, "ended": False, "requests": []}
        results.append(entry)
        try:
            journal = load_journal(name, machine[name])
        except AJError as exc:
            log.emit(run, "run-start", journal=name)
            log.emit(run, "error", message=str(exc))
            log.emit(run, "run-end")
            entry.update(ended=True, error=str(exc))
            continue
        log.emit(run, "run-start", journal=name)
        entry.update(
            vault=str(journal.vault),
            config_note=str(journal.note),
            targets=[{"name": t.name, "aliases": t.aliases} for t in journal.info.targets],
        )
        try:
            cas_write(journal.note, apply_docs, lambda text: apply_docs(text) == text)
        except (AJError, OSError) as exc:
            log.emit(run, "error", message=f"docs block not rendered: {exc}")
        try:
            open_, stuck, _recent = discover(journal)
            ids = existing_ids(journal)
        except (AJError, OSError) as exc:
            log.emit(run, "error", message=str(exc))
            log.emit(run, "run-end")
            entry.update(ended=True, error=str(exc))
            continue
        for s in stuck:
            log.emit(run, "error", id=s.rid, file=journal.rel(s.path), line=s.idx + 1,
                     message="in progress, not claimed by this run")
        claimed = []
        # Bottom-up per file, so feedback lines inserted below a request never shift the ones above it.
        for req in sorted(open_, key=lambda f: (str(f.path), -f.idx)):
            rid = req.rid
            while rid is None or (rid in ids and rid != req.rid):
                rid = ID_PREFIX + new_token(4)
            ids.add(rid)
            rel = journal.rel(req.path)
            try:
                claim(journal, req, rid)
            except (AJError, OSError) as exc:
                log.emit(run, "skipped", file=rel, line=req.idx + 1, message=str(exc))
                continue
            log.emit(run, "discover", id=rid, file=rel, text=req.instruction)
            if req.scope.error:
                log.emit(run, "error", id=rid, message=req.scope.error)
                try:
                    finalize(journal, req.path, rid, ERROR, feedback_error(run, req.scope.error))
                    log.emit(run, "state", id=rid, status=STATUS_NAMES[ERROR])
                except (AJError, OSError) as exc:
                    log.emit(run, "error", id=rid, message=f"could not write the scope error: {exc}")
                continue
            claimed.append({
                "id": rid,
                "file": rel,
                "line": req.idx + 1,
                "instruction": req.instruction,
                "heading": req.scope.heading,
                "parent": req.scope.parent,
                "content": req.scope.content,
            })
        entry["requests"] = sorted(claimed, key=lambda r: (r["file"], r["line"]))
        if not claimed:
            log.emit(run, "run-end")
            entry["ended"] = True
    print("== begin result ==")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    log = Log()
    rid = args.id.lstrip("^")
    if not re.fullmatch(r"agent-[a-z0-9]{4}", rid):
        raise AJError(f"--id must look like agent-xxxx, got {args.id!r}")
    machine = load_machine()
    journal = load_journal(args.journal, machine[journals_from(machine, args.journal)[0]])
    if args.error is None and (args.link is None or args.summary is None):
        raise AJError("report needs --link and --summary, or --error")
    log.emit(args.run, "action", id=rid, target=args.target, link=args.link,
             summary=args.summary, error=args.error)
    if args.error is not None:
        status, text = ERROR, feedback_error(args.run, args.error)
    else:
        status, text = DONE, feedback_ok(args.run, args.link, args.summary, args.title, args.change)
    try:
        files = [p for p in rg_files(f"^{rid}", journal.folder) if p.resolve() != journal.note]
        if len(files) != 1:
            raise Gone(f"^{rid} found in {len(files)} files under {journal.rel(journal.folder) or '.'}")
        finalize(journal, files[0], rid, status, text)
    except (AJError, OSError) as exc:
        log.emit(args.run, "error", id=rid, message=str(exc))
        return 1
    log.emit(args.run, "state", id=rid, status=STATUS_NAMES[status])
    return 0


def cmd_end(args: argparse.Namespace) -> int:
    log = Log()
    for run in args.run:
        log.emit(run, "run-end")
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
    for name in names:
        print(f"\njournal {name}: {machine[name]}")
        try:
            journal = load_journal(name, machine[name])
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
            if f.scope.heading:
                print(f"      heading: {f.scope.heading}")
            if f.scope.parent:
                print(f"      parent:  {f.scope.parent}")
            if f.scope.error:
                print(f"      SCOPE ERROR: {f.scope.error} (a run sets [!])")
            else:
                for c in f.scope.content.split("\n"):
                    print(f"      | {c}")
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
            f"{render_docs([])}\n\n## Targets\n\n"
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

    p = sub.add_parser("begin", help="start runs and claim open requests")
    p.add_argument("--journal")
    p.set_defaults(fn=cmd_begin)

    p = sub.add_parser("report", help="write one request's result")
    p.add_argument("--journal", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--id", required=True, help="agent-xxxx")
    p.add_argument("--target", help="matched target name")
    p.add_argument("--link", help="result: vault-relative path or URL")
    p.add_argument("--title", help="link text; default: file name or URL")
    p.add_argument("--change", choices=["created", "updated"], help="how the target changed")
    p.add_argument("--summary", help="one sentence on what was done")
    p.add_argument("--error", help="one sentence on why it failed")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("end", help="log run-end")
    p.add_argument("--run", required=True, action="append")
    p.set_defaults(fn=cmd_end)

    args = parser.parse_args()
    try:
        return args.fn(args)
    except AJError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

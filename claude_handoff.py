#!/usr/bin/env python3
"""
claude-handoff: move Claude Code sessions between machines, folders and accounts.

A Claude Code session is a JSONL transcript stored at

    <config-dir>/projects/<encoded-project-path>/<session-id>.jsonl

with an optional folder of the same name next to it (subagent transcripts,
large tool outputs). `claude --resume` only looks in the folder that matches
the directory you run it from, and the transcript itself is full of absolute
paths from the machine it was recorded on.

This tool copies a session to a new place and rewrites those paths so the
session resumes cleanly there. It works across operating systems (Windows,
macOS, Linux, WSL), across project paths, and across accounts that use
separate CLAUDE_CONFIG_DIR folders.

Standard library only. Python 3.8+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

if sys.version_info < (3, 8):  # pragma: no cover
    sys.exit("claude-handoff needs Python 3.8 or newer")

__version__ = "0.1.0"

# Claude Code turns the project path into a folder name by replacing every
# character that is not [A-Za-z0-9] with "-" (per UTF-16 code unit). Names
# longer than 200 characters are cut and given a hash suffix.
MAX_SLUG_LEN = 200

BUNDLE_FORMAT = 1
BUNDLE_SUFFIX = ".claude-session.tar.gz"

# A transcript touched more recently than this is probably still open.
ACTIVE_WINDOW_SECONDS = 120

# Claude Code deletes transcripts idle longer than this unless
# cleanupPeriodDays in settings.json says otherwise.
DEFAULT_CLEANUP_DAYS = 30

# Content blocks covered by a cryptographic signature. Editing their text can
# make the API reject the conversation, so they are never rewritten.
FROZEN_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})

# Keys whose values are identifiers or signatures, never filesystem paths.
OPAQUE_KEYS = frozenset({
    "signature", "uuid", "parentUuid", "logicalParentUuid", "leafUuid",
    "sessionId", "requestId", "id", "tool_use_id", "messageId", "promptId",
})

SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
CWD_FIELD_RE = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
# A Windows absolute path as it appears in raw JSON text, e.g. C:\\Users
RAW_WINDOWS_PATH_RE = re.compile(rb'(?<![A-Za-z0-9])[A-Za-z]:(?:\\\\[^"\\\s]+)+')


class HandoffError(Exception):
    """An error that should be shown to the user without a traceback."""


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def info(msg: str = "") -> None:
    print(msg)


def warn(msg: str) -> None:
    sys.stdout.flush()  # keep warnings in order with normal output
    print(f"warning: {msg}", file=sys.stderr)


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # pragma: no cover


def shorten(text: str, width: int) -> str:
    text = " ".join(text.split())
    if width <= 1:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def expand_path(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).absolute()


def default_config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return expand_path(env) if env else Path.home() / ".claude"


def is_windows_style(p: str) -> bool:
    """True for C:\\foo, C:/foo and \\\\server\\share paths."""
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\])", p))


def host_is_windows() -> bool:
    return os.name == "nt"


def normalize_root(p: str) -> str:
    """Clean up a project root given on the command line or found in a transcript."""
    p = unicodedata.normalize("NFC", p.strip())
    if not p:
        raise HandoffError("empty path")
    if is_windows_style(p):
        unc = p.startswith("\\\\")
        body = re.sub(r"[\\/]+", r"\\", p.lstrip("\\/") if unc else p)
        p = ("\\\\" + body) if unc else body
        p = p.rstrip("\\")
        if re.fullmatch(r"[A-Za-z]:", p):
            raise HandoffError(f"refusing to map a whole drive ({p}\\); pass the project folder")
        if not unc:
            p = p[0].upper() + p[1:]
    else:
        p = re.sub(r"/+", "/", p).rstrip("/")
        if not p:
            raise HandoffError("refusing to map the filesystem root; pass the project folder")
        if not p.startswith("/"):
            raise HandoffError(f"not an absolute path: {p}")
    return p


def local_canonical(p: str) -> str:
    """Resolve a path on this machine the way Claude Code does (realpath + NFC).

    A path in the other OS's style (preparing a session for another machine)
    is only cleaned up, since there is nothing here to resolve it against.
    """
    expanded = os.path.expandvars(os.path.expanduser(p))
    if is_windows_style(expanded) != host_is_windows() and (
            is_windows_style(expanded) or expanded.startswith("/")):
        return normalize_root(expanded)
    resolved = os.path.realpath(os.path.abspath(expanded))
    return normalize_root(unicodedata.normalize("NFC", resolved))


def home_of(path: str) -> Optional[str]:
    """The home folder a project path sits under, e.g. /Users/me or C:\\Users\\me."""
    m = (re.match(r"^([A-Za-z]:\\Users\\[^\\]+)", path) if is_windows_style(path)
         else re.match(r"^(/(?:Users|home)/[^/]+|/root)(?:/|$)", path))
    return m.group(1) if m else None


def slugify(path: str) -> str:
    """Folder name Claude Code uses for a project path (before length capping)."""
    out = []
    for ch in path:
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        else:
            # JavaScript replaces per UTF-16 code unit, so astral characters
            # (emoji etc.) become two dashes.
            out.append("--" if ord(ch) > 0xFFFF else "-")
    return "".join(out)


def session_cwds(path: Path, limit_bytes: Optional[int] = None) -> List[str]:
    """Distinct cwd values in a transcript, in first-seen order."""
    seen: Dict[str, None] = {}
    read = 0
    with open(path, "rb") as f:
        for raw in f:
            read += len(raw)
            for m in CWD_FIELD_RE.finditer(raw):
                try:
                    value = json.loads(b'"' + m.group(1) + b'"')
                except ValueError:
                    continue
                if isinstance(value, str) and value:
                    seen.setdefault(value, None)
            if limit_bytes is not None and seen and read > limit_bytes:
                break
    return list(seen)


def resolve_project_dir(config: Path, cwd: str, override: Optional[str] = None) -> Path:
    """Find (or name) the projects/<slug> folder for cwd inside a config dir."""
    projects = config / "projects"
    if override:
        return expand_path(override)
    slug = slugify(cwd)
    if len(slug) <= MAX_SLUG_LEN:
        return projects / slug
    # Long paths get a hash suffix whose algorithm depends on the Claude Code
    # build. Rather than guess it, reuse a folder Claude Code already made.
    prefix = slug[:MAX_SLUG_LEN] + "-"
    if projects.is_dir():
        for candidate in sorted(projects.iterdir()):
            if not (candidate.is_dir() and candidate.name.startswith(prefix)):
                continue
            for transcript in candidate.glob("*.jsonl"):
                cwds = session_cwds(transcript, limit_bytes=1 << 20)
                if cwds and normalize_root(cwds[0]) == cwd:
                    return candidate
    raise HandoffError(
        "this project path is longer than 200 characters, so Claude Code names its\n"
        "session folder with a hash this tool does not reproduce. Run `claude` once in\n"
        f"{cwd}\nwith this account (it creates the folder), exit, and run this again.\n"
        "Or pass --project-dir with the folder to use."
    )


# --------------------------------------------------------------------------
# Reading transcripts
# --------------------------------------------------------------------------

def iter_raw_lines(path: Path) -> Iterator[bytes]:
    """Lines without their line ending, with a UTF-8 BOM on line 1 removed."""
    with open(path, "rb") as f:
        first = True
        for raw in f:
            line = raw.rstrip(b"\r\n")
            if first:
                if line.startswith(b"\xef\xbb\xbf"):
                    line = line[3:]
                first = False
            yield line


def parse_line(line: bytes):
    if not line.strip():
        return None
    try:
        return json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


_NOISE_PREFIXES = (
    "<command-", "<local-command", "<bash-", "<system-reminder", "Caveat:",
    "[Request interrupted",
)


def prompt_text(rec) -> Optional[str]:
    """The text of a real user prompt, or None for tool results and meta lines."""
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return None
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isSidechain"):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, list):
        # Real prompts are often preceded by injected <system-reminder> blocks.
        texts = [b.get("text") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        texts = [t.strip() for t in texts if isinstance(t, str)]
        content = " ".join(t for t in texts if t and not t.startswith(_NOISE_PREFIXES))
    if not isinstance(content, str):
        return None
    content = content.strip()
    if not content or content.startswith(_NOISE_PREFIXES):
        return None
    return " ".join(content.split())


class SessionInfo:
    def __init__(self, path: Path):
        self.path = path
        self.session_id = path.stem
        stat = path.stat()
        self.size = stat.st_size
        self.mtime = stat.st_mtime
        self.cwds: List[str] = []
        self.title: Optional[str] = None
        self.first_prompt: Optional[str] = None

    @property
    def cwd(self) -> Optional[str]:
        return self.cwds[0] if self.cwds else None

    @property
    def label(self) -> str:
        return self.title or self.first_prompt or "(no prompt yet)"


def scan_session(path: Path) -> SessionInfo:
    """Collect cwds, title and first prompt, parsing as few lines as possible."""
    s = SessionInfo(path)
    custom = ai = summary = None
    cwds: Dict[str, None] = {}
    for line in iter_raw_lines(path):
        for m in CWD_FIELD_RE.finditer(line):
            try:
                value = json.loads(b'"' + m.group(1) + b'"')
            except ValueError:
                continue
            if isinstance(value, str) and value:
                cwds.setdefault(value, None)
        wants_title = (b'"customTitle"' in line or b'"aiTitle"' in line
                       or b'"type":"summary"' in line or b'"type": "summary"' in line)
        wants_prompt = s.first_prompt is None and b'"user"' in line
        if not (wants_title or wants_prompt):
            continue
        rec = parse_line(line)
        if not isinstance(rec, dict):
            continue
        if isinstance(rec.get("customTitle"), str):
            custom = rec["customTitle"]
        if isinstance(rec.get("aiTitle"), str):
            ai = rec["aiTitle"]
        if rec.get("type") == "summary" and isinstance(rec.get("summary"), str):
            summary = rec["summary"]
        if wants_prompt:
            s.first_prompt = prompt_text(rec)
    s.cwds = list(cwds)
    s.title = custom or ai or summary
    return s


def find_session(config: Path, ref: str) -> Path:
    """Locate a transcript by full session id or a unique prefix of one."""
    projects = config / "projects"
    if not projects.is_dir():
        raise HandoffError(f"no projects folder in {config}")
    ref = ref.strip().lower()
    if ref.endswith(".jsonl"):
        ref = ref[: -len(".jsonl")]
    if len(ref) < 4:
        raise HandoffError("give at least the first 4 characters of the session id")
    matches = [p for p in projects.glob("*/*.jsonl") if p.stem.lower().startswith(ref)]
    exact = [p for p in matches if p.stem.lower() == ref]
    if exact:
        matches = exact
    if not matches:
        raise HandoffError(f"no session matching '{ref}' in {projects}")
    if len(matches) > 1:
        listing = "\n".join(f"  {p.parent.name}/{p.name}" for p in sorted(matches))
        raise HandoffError(f"'{ref}' matches more than one session:\n{listing}\n"
                           "Give more of the id, or pass the .jsonl path directly.")
    return matches[0]


def sidecar_dir(transcript: Path) -> Optional[Path]:
    d = transcript.with_suffix("")
    return d if d.is_dir() and not d.is_symlink() else None


def cleanup_days(config: Path) -> int:
    try:
        with open(config / "settings.json", encoding="utf-8") as f:
            value = json.load(f).get("cleanupPeriodDays")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
    except (OSError, ValueError, AttributeError):
        pass
    return DEFAULT_CLEANUP_DAYS


def check_not_active(path: Path) -> None:
    age = time.time() - path.stat().st_mtime
    if age < ACTIVE_WINDOW_SECONDS:
        warn(f"{path.name} was written {int(age)}s ago. If that session is still open,\n"
             "         exit it first, or the copy will miss its newest turns.")


# --------------------------------------------------------------------------
# Path rewriting
# --------------------------------------------------------------------------

# Characters allowed inside one path segment after the project root. Spaces
# are deliberately excluded: they end a path in free text far more often than
# they appear inside a repository's own folder names.
_SEGMENT = r"[^\\/\s\"'`<>|*?:\x00-\x1f]+"
_BEFORE = r"(?<![\w.~\-])"
_TRAILING_PUNCT = "`'\">),.;:!?]"
_AFTER = r"(?![\w.\-])"


MAC_ROOTS = ("/Users/", "/Volumes/", "/private/")


def looks_like_macos(p: str) -> bool:
    return p.startswith(MAC_ROOTS) or p in ("/Users", "/Volumes")


class PathMapper:
    """Rewrites every occurrence of one or more source roots in a string.

    Source forms matched, besides the root as given:
      Windows   C:/x/app, c:\\X\\APP (case-insensitive), /c/x/app (Git Bash),
                /mnt/c/x/app (WSL), /cygdrive/c/x/app
      macOS     any letter case (APFS and HFS+ are case-insensitive by default),
                /var <-> /private/var aliases, decomposed (NFD) Unicode
      Linux     exact, case-sensitive

    Output form:
      to Linux/macOS  always forward slashes
      to Windows      backslashes when the whole value is the path or the match
                      used backslashes; otherwise D:/x/app, which works in Git
                      Bash, PowerShell and cmd alike (a backslash path would be
                      mangled by Git Bash, which Claude Code uses on Windows)
    """

    def __init__(self, pairs: Sequence[Tuple[str, str]]):
        rules = []  # (priority, pattern, case_insensitive, tail_sep, dst)
        for src, dst in pairs:
            for pattern, ci, tail_sep in self._variants(src):
                rules.append((len(src), pattern, ci, tail_sep, dst))
        rules.sort(key=lambda r: -r[0])
        self._targets: List[str] = []
        parts = []
        for i, (_, pattern, ci, tail_sep, dst) in enumerate(rules):
            body = f"(?i:{pattern})" if ci else pattern
            tail = f"(?P<t{i}>(?:{tail_sep}+{_SEGMENT})*(?:{tail_sep}+)?)"
            parts.append(f"(?P<r{i}>{_BEFORE}{body}{_AFTER}{tail})")
            self._targets.append(dst)
        self.regex = re.compile("|".join(parts)) if parts else None
        self.count = 0
        self._len = 0

    @staticmethod
    def _variants(src: str):
        if is_windows_style(src):
            if src.startswith("\\\\"):
                segs = [s for s in src.split("\\") if s]
                yield (r"[\\/]{2}" + r"[\\/]+".join(re.escape(s) for s in segs), True, r"[\\/]")
                return
            drive, rest = src[0], [s for s in src[2:].split("\\") if s]
            native = re.escape(drive) + ":" + "".join(r"[\\/]+" + re.escape(s) for s in rest)
            yield native, True, r"[\\/]"
            posixish = (r"/(?:mnt/|cygdrive/)?" + re.escape(drive)
                        + "".join("/" + re.escape(s) for s in rest))
            yield posixish, True, "/"
            return
        mac = looks_like_macos(src)
        forms = [src]
        m = re.match(r"^/private(/(?:var|tmp|etc)(?:/.*)?)$", src)
        if m:
            forms.append(m.group(1))
        for form in list(forms):
            nfd = unicodedata.normalize("NFD", form)
            if nfd != form:
                forms.append(nfd)
        for form in forms:
            yield re.escape(form), mac or form.startswith("/private/"), "/"

    def _replace(self, m: "re.Match[str]") -> str:
        name = m.lastgroup or ""
        i = int(name[1:])
        dst = self._targets[i]
        tail = m.group(f"t{i}") or ""
        if is_windows_style(dst):
            whole = m.start() == 0 and m.end() == self._len
            sep = "\\" if (whole or "\\" in m.group(0)) else "/"
            root = dst if sep == "\\" else dst.replace("\\", "/")
        else:
            sep, root = "/", dst
        if tail:
            tail = re.sub(r"[\\/]+", lambda _: sep, tail)
        self.count += 1
        return root + tail

    def __call__(self, s: str) -> str:
        if self.regex is None or not s:
            return s
        self._len = len(s)
        return self.regex.sub(self._replace, s)

    def search(self, s: str) -> bool:
        return bool(self.regex and self.regex.search(s))


class Stats:
    def __init__(self):
        self.lines = 0
        self.changed_lines = 0
        self.unparseable = 0
        self.frozen_with_paths = 0
        self.ids_changed = 0
        self.foreign_windows_paths: List[str] = []
        self.files = 0

    def merge(self, other: "Stats") -> None:
        self.lines += other.lines
        self.changed_lines += other.changed_lines
        self.unparseable += other.unparseable
        self.frozen_with_paths += other.frozen_with_paths
        self.ids_changed += other.ids_changed
        self.files += other.files
        for p in other.foreign_windows_paths:
            if p not in self.foreign_windows_paths and len(self.foreign_windows_paths) < 5:
                self.foreign_windows_paths.append(p)


class Rewriter:
    def __init__(self, mapper: PathMapper, old_id: Optional[str], new_id: Optional[str],
                 dst_is_windows: bool, old_home: Optional[str] = None):
        self.mapper = mapper
        # Finds other paths under the old machine's home folder, so they can be reported.
        self.leftover = PathMapper([(old_home, old_home)]) if old_home else None
        self.old_id = old_id
        self.new_id = new_id if new_id and new_id != old_id else None
        self.dst_is_windows = dst_is_windows

    def _walk(self, obj, stats: Stats):
        if isinstance(obj, str):
            return self.mapper(obj)
        if isinstance(obj, list):
            return [self._walk(v, stats) for v in obj]
        if isinstance(obj, dict):
            kind = obj.get("type")
            # "type" is not always a string: JSON schemas in tool definitions have
            # a property literally named "type" whose value is an object.
            if isinstance(kind, str) and kind in FROZEN_BLOCK_TYPES:
                if self.mapper.search(json.dumps(obj, ensure_ascii=False)):
                    stats.frozen_with_paths += 1
                return obj
            out = {}
            for k, v in obj.items():
                if k == "sessionId" and self.new_id and v == self.old_id:
                    out[k] = self.new_id
                    stats.ids_changed += 1
                elif k in OPAQUE_KEYS:
                    out[k] = v
                else:
                    out[self.mapper(k) if isinstance(k, str) else k] = self._walk(v, stats)
            return out
        return obj

    def rewrite_file(self, src: Path, dst: Path) -> Stats:
        stats = Stats()
        stats.files = 1
        with open(dst, "wb") as out:
            for line in iter_raw_lines(src):
                stats.lines += 1
                rec = parse_line(line)
                if rec is None:
                    if line.strip():
                        stats.unparseable += 1
                    out.write(line + b"\n")
                    continue
                before = self.mapper.count + stats.ids_changed
                new = self._walk(rec, stats)
                if self.mapper.count + stats.ids_changed != before:
                    stats.changed_lines += 1
                    line = json.dumps(new, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if self.leftover and len(stats.foreign_windows_paths) < 5:
                    for m in self.leftover.regex.finditer(line.decode("utf-8", "replace")):
                        sample = m.group(0).replace("\\\\", "\\").rstrip(_TRAILING_PUNCT)  # undo JSON escaping
                        if self.mapper.search(sample) or sample in stats.foreign_windows_paths:
                            continue
                        stats.foreign_windows_paths.append(sample)
                        if len(stats.foreign_windows_paths) >= 5:
                            break
                if not self.dst_is_windows and len(stats.foreign_windows_paths) < 5:
                    for m in RAW_WINDOWS_PATH_RE.finditer(line):
                        sample = m.group(0).decode("utf-8", "replace").replace("\\\\", "\\").rstrip(_TRAILING_PUNCT)
                        if self.mapper.search(sample):
                            continue  # the project root itself, left inside a signed block
                        if sample not in stats.foreign_windows_paths:
                            stats.foreign_windows_paths.append(sample)
                        if len(stats.foreign_windows_paths) >= 5:
                            break
                out.write(line + b"\n")
            out.flush()
            os.fsync(out.fileno())
        return stats


def verify_jsonl(original: Path, rewritten: Path) -> None:
    """The rewritten file must have the same number of lines and parse as well."""
    a_lines = a_bad = b_lines = b_bad = 0
    for line in iter_raw_lines(original):
        a_lines += 1
        a_bad += line.strip() != b"" and parse_line(line) is None
    for line in iter_raw_lines(rewritten):
        b_lines += 1
        b_bad += line.strip() != b"" and parse_line(line) is None
    if a_lines != b_lines or b_bad > a_bad:
        raise HandoffError(f"verification failed for {rewritten.name}: "
                           f"{a_lines}->{b_lines} lines, {a_bad}->{b_bad} unparseable")


# --------------------------------------------------------------------------
# Bundles
# --------------------------------------------------------------------------

def _tar_filter(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    if not (info.isfile() or info.isdir()):
        return None  # skip symlinks, devices, fifos
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def safe_extract(bundle: Path, dest: Path) -> None:
    try:
        tar = tarfile.open(bundle, "r:*")
    except (tarfile.TarError, OSError) as e:
        raise HandoffError(f"cannot open bundle {bundle}: {e}")
    with tar:
        members = tar.getmembers()
        for m in members:
            parts = PurePosixPath(m.name).parts
            if (m.name.startswith(("/", "\\")) or ".." in parts or ":" in m.name
                    or "\\" in m.name or not (m.isfile() or m.isdir())):
                raise HandoffError(f"refusing unsafe bundle entry: {m.name}")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, members=members, filter="data")
        else:  # pragma: no cover - Python < 3.12 without the backport
            tar.extractall(dest, members=members)


def read_bundle(bundle: Path, workdir: Path):
    safe_extract(bundle, workdir)
    manifest_path = workdir / "manifest.json"
    manifest = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            raise HandoffError("bundle manifest.json is not valid JSON")
        if manifest.get("format", 1) > BUNDLE_FORMAT:
            raise HandoffError("bundle was made by a newer claude-handoff; upgrade first")
    transcripts = sorted((workdir / "session").glob("*.jsonl"))
    if len(transcripts) != 1:
        raise HandoffError(f"bundle should contain exactly one session, found {len(transcripts)}")
    transcript = transcripts[0]
    history = workdir / "file-history" / transcript.stem
    return transcript, manifest, (history if history.is_dir() else None)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_list(args) -> int:
    config = expand_path(args.config_dir) if args.config_dir else default_config_dir()
    projects = config / "projects"
    if not projects.is_dir():
        raise HandoffError(f"no projects folder in {config}")
    if args.cwd:
        folders = [resolve_project_dir(config, local_canonical(args.cwd))]
    else:
        folders = [d for d in projects.iterdir() if d.is_dir()]
    files = [p for d in folders if d.is_dir() for p in d.glob("*.jsonl")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if args.limit:
        files = files[: args.limit]
    sessions = [scan_session(p) for p in files]

    if args.json:
        print(json.dumps([{
            "id": s.session_id, "modified": dt.datetime.fromtimestamp(s.mtime).isoformat(timespec="seconds"),
            "bytes": s.size, "cwd": s.cwd, "project_folder": s.path.parent.name,
            "title": s.title, "first_prompt": s.first_prompt,
        } for s in sessions], ensure_ascii=False, indent=2))
        return 0
    if not sessions:
        info("No sessions found.")
        return 0

    width = shutil.get_terminal_size((120, 20)).columns
    for s in sessions:
        when = dt.datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M")
        project = s.cwd or s.path.parent.name
        info(f"{s.session_id}  {when}  {human_size(s.size):>8}  {project}")
        info(f"    {shorten(s.label, max(20, width - 4))}")
    info(f"\n{len(sessions)} session(s) in {projects}")
    return 0


def cmd_where(args) -> int:
    config = expand_path(args.config_dir) if args.config_dir else default_config_dir()
    transcript = find_session(config, args.session)
    s = scan_session(transcript)
    side = sidecar_dir(transcript)
    history = config / "file-history" / transcript.stem
    info(f"Session     {transcript.stem}")
    info(f"Project     {s.cwd or '(unknown)'}")
    info(f"Title       {shorten(s.label, 100)}")
    info(f"Transcript  {transcript}  ({human_size(s.size)})")
    info(f"Sidecar     {side if side else '(none)'}")
    info(f"Checkpoints {history if history.is_dir() else '(none)'}")
    info("")
    info("Copy both to another machine, e.g.:")
    q = (lambda x: f'"{x}"') if host_is_windows() else shlex.quote
    info(f"  scp {q(str(transcript))} user@host:~/")
    if side:
        info(f"  scp -r {q(str(side))} user@host:~/")
    info("or bundle them into one file:  claude-handoff pack " + transcript.stem[:8])
    return 0


def cmd_pack(args) -> int:
    config = expand_path(args.config_dir) if args.config_dir else default_config_dir()
    transcript = Path(args.session) if args.session.endswith(".jsonl") and Path(args.session).is_file() \
        else find_session(config, args.session)
    check_not_active(transcript)
    s = scan_session(transcript)
    sid = transcript.stem
    out = expand_path(args.output) if args.output else Path.cwd() / f"{sid[:8]}{BUNDLE_SUFFIX}"
    if out.exists() and not args.force:
        raise HandoffError(f"{out} already exists (use --force to overwrite)")
    manifest = {
        "format": BUNDLE_FORMAT,
        "tool": "claude-handoff",
        "tool_version": __version__,
        "session_id": sid,
        "cwd": s.cwd,
        "cwds": s.cwds,
        "title": s.title,
        "source_platform": sys.platform,
        "packed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".handoff-", suffix=".tmp", dir=str(out.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(data)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(data))
            tar.add(str(transcript), arcname=f"session/{sid}.jsonl", filter=_tar_filter)
            side = sidecar_dir(transcript)
            if side:
                tar.add(str(side), arcname=f"session/{sid}", filter=_tar_filter)
            history = config / "file-history" / sid
            if args.with_history and history.is_dir():
                tar.add(str(history), arcname=f"file-history/{sid}", filter=_tar_filter)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()
    info(f"Packed {sid} ({human_size(s.size)}) from {s.cwd or transcript.parent.name}")
    info(f"  -> {out}")
    info("Copy it to the other machine and run:  claude-handoff import "
         f"{out.name} --to-cwd <project path there>")
    return 0


def _parse_maps(values: Sequence[str]) -> List[Tuple[str, str]]:
    pairs = []
    for v in values or []:
        if "=" not in v:
            raise HandoffError(f"--map expects OLD=NEW, got: {v}")
        old, new = v.split("=", 1)
        pairs.append((normalize_root(old), normalize_root(new)))
    return pairs


def _make_dirs(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _private(path: Path) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _copy_tree(src: Path, dst: Path, rewriter: Rewriter, stats: Stats) -> None:
    """Copy a sidecar or history folder, rewriting any transcripts inside it."""
    for root, dirs, files in os.walk(src, followlinks=False):
        rel = Path(root).relative_to(src)
        target_root = dst / rel
        _make_dirs(target_root)
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            s = Path(root) / name
            if s.is_symlink():
                warn(f"skipping symlink {s}")
                continue
            d = target_root / name
            if name.endswith(".jsonl"):
                stats.merge(rewriter.rewrite_file(s, d))
                verify_jsonl(s, d)
            else:
                shutil.copyfile(s, d)
            _private(d)


def cmd_import(args) -> int:
    dst_config = expand_path(args.config_dir) if args.config_dir else default_config_dir()
    src_config = expand_path(args.from_config_dir) if args.from_config_dir else default_config_dir()

    with tempfile.TemporaryDirectory(prefix="claude-handoff-") as work:
        workdir = Path(work)
        manifest: Dict = {}
        history: Optional[Path] = None
        source = Path(os.path.expanduser(args.source))

        if source.is_file() and source.name.endswith((".tar.gz", ".tgz")):
            transcript, manifest, history = read_bundle(source, workdir / "bundle")
            origin = f"bundle {source.name}"
        elif source.is_file():
            transcript = source
            origin = str(source)
            check_not_active(transcript)
        elif source.is_dir():
            raise HandoffError(f"{source} is a folder; pass the .jsonl file inside it")
        else:
            transcript = find_session(src_config, args.source)
            origin = f"{src_config}"
            check_not_active(transcript)
            candidate = src_config / "file-history" / transcript.stem
            history = candidate if candidate.is_dir() else None

        old_id = transcript.stem
        if not SESSION_ID_RE.match(old_id):
            warn(f"'{old_id}' does not look like a session id; resume may not find it")
        side = sidecar_dir(transcript)
        new_id = str(uuid.uuid4()) if args.new_id else old_id

        # Where did the session run, and where should it resume?
        cwds = session_cwds(transcript)
        if args.from_cwd:
            from_cwd = normalize_root(args.from_cwd)
        elif manifest.get("cwd"):
            from_cwd = normalize_root(manifest["cwd"])
        elif cwds:
            from_cwd = normalize_root(cwds[0])
        else:
            raise HandoffError("the transcript records no working directory; pass --from-cwd")

        if args.to_cwd:
            to_cwd = local_canonical(args.to_cwd)
        elif is_windows_style(from_cwd) == host_is_windows() and Path(from_cwd).is_dir():
            to_cwd = local_canonical(from_cwd)
        else:
            raise HandoffError(f"the session ran in {from_cwd}, which is not on this machine;\n"
                               "pass --to-cwd with the project's path here")
        if is_windows_style(to_cwd) == host_is_windows() and not Path(to_cwd).is_dir():
            warn(f"{to_cwd} does not exist yet. Clone or create the project there before resuming.")

        main_pair = [(from_cwd, to_cwd)] if from_cwd != to_cwd else []
        for extra in cwds[1:]:
            extra = normalize_root(extra)
            inside = (extra.lower().startswith(from_cwd.lower()) if is_windows_style(from_cwd)
                      else extra.startswith(from_cwd + "/"))
            if not inside and extra != from_cwd:
                warn(f"session also ran in {extra}, outside the project root; "
                     "add --map OLD=NEW if that path should change too")
        extra_pairs = _parse_maps(args.map)
        pairs = main_pair + extra_pairs

        project_dir = resolve_project_dir(dst_config, to_cwd, args.project_dir)
        dst_transcript = project_dir / f"{new_id}.jsonl"
        dst_side = project_dir / new_id
        dst_history = dst_config / "file-history" / new_id

        try:
            same_file = dst_transcript.exists() and dst_transcript.samefile(transcript)
        except OSError:
            same_file = False
        if same_file or (dst_transcript.resolve() == transcript.resolve()):
            raise HandoffError("source and destination are the same file.\n"
                               "Use --new-id to fork the session, or point --config-dir / --to-cwd elsewhere.")

        if dst_transcript.exists() and not args.force:
            raise HandoffError(f"{dst_transcript} already exists.\n"
                               "Use --new-id to import a separate copy, or --force to replace it "
                               "(the old file is kept as a .bak).")

        mapper = PathMapper(pairs)
        old_home = home_of(from_cwd)
        if old_home and old_home == home_of(to_cwd) and is_windows_style(from_cwd) == is_windows_style(to_cwd):
            old_home = None  # same home on both sides; nothing to report
        rewriter = Rewriter(mapper, old_id, new_id, dst_is_windows=is_windows_style(to_cwd),
                            old_home=old_home)

        info(f"Session    {old_id}" + (f"  ->  new id {new_id}" if new_id != old_id else ""))
        info(f"From       {from_cwd}   ({origin})")
        info(f"To         {to_cwd}")
        info(f"Into       {project_dir}")
        for old, new in extra_pairs:
            info(f"Also map   {old}  ->  {new}")

        # Stage everything next to the destination so the final moves are renames.
        stage_parent = project_dir if not args.dry_run else workdir
        if not args.dry_run:
            _make_dirs(project_dir)
        stage = Path(tempfile.mkdtemp(prefix=".handoff-", dir=str(stage_parent)))
        try:
            stats = Stats()
            staged_transcript = stage / f"{new_id}.jsonl"
            stats.merge(rewriter.rewrite_file(transcript, staged_transcript))
            verify_jsonl(transcript, staged_transcript)
            _private(staged_transcript)
            if side:
                _copy_tree(side, stage / new_id, rewriter, stats)
            staged_history = None
            if history and not args.no_history:
                staged_history = stage / "_file-history"
                _copy_tree(history, staged_history, rewriter, stats)

            info("")
            info(f"Rewrote    {mapper.count} path reference(s) on {stats.changed_lines} of "
                 f"{stats.lines} line(s) across {stats.files} file(s)")
            if stats.ids_changed:
                info(f"Renamed    {stats.ids_changed} sessionId field(s)")
            if stats.frozen_with_paths:
                info(f"Left as-is {stats.frozen_with_paths} thinking block(s) mentioning the old path "
                     "(they carry a signature, so they stay untouched)")
            if stats.unparseable:
                warn(f"{stats.unparseable} line(s) were not valid JSON and were copied unchanged")
            if stats.foreign_windows_paths:
                warn("paths from the old machine are still in the transcript, e.g.\n         "
                     + "\n         ".join(stats.foreign_windows_paths[:3])
                     + "\n         add --map OLD=NEW if Claude should see them translated")

            if args.dry_run:
                info("\nDry run: nothing was written.")
                return 0

            try:
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                if args.keep_mtime:
                    st = transcript.stat()
                    os.utime(staged_transcript, (st.st_atime, st.st_mtime))
                # Sidecar first, transcript last: the .jsonl appearing is what makes
                # the session visible, so it must not appear half-imported.
                if side:
                    if dst_side.exists():
                        dst_side.rename(dst_side.with_name(f"{new_id}.bak-{stamp}"))
                    os.replace(stage / new_id, dst_side)
                if staged_history:
                    if dst_history.exists():
                        dst_history.rename(dst_history.with_name(f"{new_id}.bak-{stamp}"))
                    _make_dirs(dst_history.parent)
                    shutil.move(str(staged_history), str(dst_history))
                if dst_transcript.exists():
                    backup = dst_transcript.with_name(f"{dst_transcript.name}.bak-{stamp}")
                    dst_transcript.rename(backup)
                    info(f"Backed up  existing transcript to {backup.name}")
                os.replace(staged_transcript, dst_transcript)
            except PermissionError as e:
                raise HandoffError(f"could not move files into place ({e}).\n"
                                   "On Windows this usually means Claude Code has the session open; "
                                   "close it and retry. Nothing was half-written.")
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    days = cleanup_days(dst_config)
    age_days = (time.time() - dst_transcript.stat().st_mtime) / 86400
    if age_days > days:
        warn(f"this transcript is {age_days:.0f} days old and cleanupPeriodDays is {days};\n"
             "         Claude Code will delete it on next start. Re-run without --keep-mtime\n"
             f"         or raise cleanupPeriodDays in {dst_config / 'settings.json'}.")

    info(f"\nDone. Resume it with:")
    quote = (lambda s: f'"{s}"') if is_windows_style(to_cwd) else shlex.quote
    info(f"  cd {quote(to_cwd)}")
    env_default = os.environ.get("CLAUDE_CONFIG_DIR")
    needs_env = (dst_config != (expand_path(env_default) if env_default else Path.home() / ".claude"))
    if needs_env and not is_windows_style(to_cwd):
        info(f"  CLAUDE_CONFIG_DIR={quote(str(dst_config))} claude --resume {new_id}")
    elif needs_env:
        info(f'  $env:CLAUDE_CONFIG_DIR = "{dst_config}"; claude --resume {new_id}')
    else:
        info(f"  claude --resume {new_id}")
    if is_windows_style(from_cwd) != is_windows_style(to_cwd):
        info("\nTip: the history still shows commands from the old OS. Start with something like\n"
             f"\"This project now lives at {to_cwd} on {'Windows' if is_windows_style(to_cwd) else 'Linux'}.\"")
    info("Note: Claude desktop may not list imported sessions in its sidebar; "
         "resume from a terminal.")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-handoff",
        description="Move Claude Code sessions between machines, project paths and accounts.",
        epilog="Run 'claude-handoff <command> -h' for details on each command.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    ls = sub.add_parser("list", help="show sessions with their titles and first prompts")
    ls.add_argument("--config-dir", help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    ls.add_argument("--cwd", help="only sessions for this project path")
    ls.add_argument("-n", "--limit", type=int, default=20, help="how many to show (0 = all, default 20)")
    ls.add_argument("--json", action="store_true", help="machine-readable output")
    ls.set_defaults(func=cmd_list)

    wh = sub.add_parser("where", help="show where a session's files are (id from /status)")
    wh.add_argument("session", help="session id or unique prefix, e.g. from /status")
    wh.add_argument("--config-dir", help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    wh.set_defaults(func=cmd_where)

    pk = sub.add_parser("pack", help="bundle one session into a single file to copy elsewhere")
    pk.add_argument("session", help="session id, unique id prefix, or path to a .jsonl")
    pk.add_argument("--config-dir", help="Claude config dir to read from")
    pk.add_argument("-o", "--output", help=f"output file (default: ./<id>{BUNDLE_SUFFIX})")
    pk.add_argument("--with-history", action="store_true",
                    help="include file-history checkpoints (used by /rewind)")
    pk.add_argument("--force", action="store_true", help="overwrite the output file")
    pk.set_defaults(func=cmd_pack)

    im = sub.add_parser("import", help="install a session here, rewriting its paths")
    im.add_argument("source", help="a bundle, a .jsonl file, or a session id in --from-config-dir")
    im.add_argument("--to-cwd", help="project path on this machine (default: same as the source)")
    im.add_argument("--from-cwd", help="project path the session ran in (default: read from the transcript)")
    im.add_argument("--config-dir", help="config dir to import into (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    im.add_argument("--from-config-dir", help="config dir to look up a session id in")
    im.add_argument("--map", action="append", metavar="OLD=NEW",
                    help="extra path prefix to rewrite (repeatable)")
    im.add_argument("--new-id", action="store_true", help="give the copy a new session id (a fork)")
    im.add_argument("--force", action="store_true", help="replace an existing copy (kept as .bak)")
    im.add_argument("--keep-mtime", action="store_true",
                    help="keep the original modification time (may trigger the 30-day cleanup)")
    im.add_argument("--no-history", action="store_true", help="do not copy file-history checkpoints")
    im.add_argument("--project-dir", help="exact projects/<folder> to write into (for very long paths)")
    im.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    im.set_defaults(func=cmd_import)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # Windows consoles and emoji
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except HandoffError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

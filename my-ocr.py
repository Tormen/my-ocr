#!/usr/bin/env python3
"""my-ocr -- make scanned documents searchable, with DEVONthink's OCR.

A PDF or picture goes in; a searchable PDF takes its place, the original goes
to the Trash. `my-ocr --help` says how to drive it.

One file, in this order:
  1. output, errors, config
  2. the PDF tools: facts, the result check, the Producer tag
  3. DEVONthink
  4. one document, one run, the mutex, alerts
  5. status, reset, the admin flags, main
  6. --run-tests

Runs on any python3 from 3.9 on, standard library only: my-scan starts it from
environments whose PATH finds Apple's /usr/bin/python3 first.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import fnmatch
import getpass
import hashlib
import io
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Callable, Dict, Final, List, Literal, NoReturn, Optional, Sequence, Tuple, cast

SCRIPT_NAME: Final = "my-ocr"
SCRIPT_VERSION: Final = "1.1"
SCRIPT_COMMIT: Final = "unstamped"
REPOSITORY: Final = "Tormen/my-ocr"      # whose tags --version may read as releases

# The mark of a document that went through OCR. my-scan files a document only
# when its PDF Producer contains it (its OCR_PRODUCER_TAG), and my-ocr skips
# such a document as already done -- both as a CONTAINS match. It stays
# "FineReader": the engine inside DEVONthink is ABBYY FineReader, and every
# document OCRed before DEVONthink carries the same word.
PRODUCER_TAG: Final = "FineReader"
PRODUCER_OCRED: Final = f"{PRODUCER_TAG} (my-ocr: OCR by DEVONthink/ABBYY)"
PRODUCER_NOTEXT: Final = f"{PRODUCER_TAG} (my-ocr: no text found -- original kept)"

# what `file --brief` starts with for a document my-ocr can OCR
INPUT_TYPES: Final = ("PDF", "JPEG", "PNG", "TIFF")

# DEVONthink: its process, its preferences, the one group my-ocr works in
DT_PROCESS: Final = "DEVONthink"
DT_DEFAULTS: Final = "com.devon-technologies.think"
DT_GROUP: Final = "/my-ocr"


@dataclass
class Outside:
    """Every outside thing my-ocr talks to, in one place -- the tests point
    each one at a stand-in."""

    dt_app: Path = Path("/Applications/DEVONthink.app")
    # DEVONthink's ABBYY OCR add-on, installed per user by DEVONthink > Install
    # Add-ons. Without it DEVONthink logs "Unable to OCR document" and its
    # `ocr file` never answers (horse, 2026-10-25: 2 x 600 s, then the scan
    # was filed as FAILED for what was the engine's fault).
    dt_ocr_helper: Path = Path.home() / "Library/Application Support/DEVONthink/Abbyy/DTOCRHelper.app"
    osascript: str = "/usr/bin/osascript"
    # osascript gets OCR_TIMEOUT plus this before it is killed: AppleScript's own
    # 'with timeout' fires first and says so; the kill is for a DEVONthink that
    # no longer answers at all.
    osa_grace: int = 30
    my_notify: str = "/LINKS/bin/my-notify"
    trash_dir: Path = Path.home() / ".Trash"


EXT = Outside()


# --- 1. output, errors -----------------------------------------------------

class OcrError(Exception):
    """The tool's one failure: a code naming the source line, and what to know."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(f"{code}: {text}")
        self.code = code
        self.text = text


def err(code: str, text: str) -> NoReturn:
    raise OcrError(code, text)


class _Terminated(Exception):
    """SIGTERM: a caller's time limit, launchd, kill."""


class Out:
    """Where every line goes. The narration goes to STDOUT -- my-scan runs
    my-ocr inside its own report and drops stdout under its -Q -- and errors
    and warnings to stderr. -Q silences the narration on the terminal, never
    the LOGFILE."""

    def __init__(self) -> None:
        self.quiet = False
        self.vrb = False
        self.dbg_on = False
        self.log: Optional[IO[str]] = None

    def record(self, text: str) -> None:
        if self.log is None:
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for line in text.splitlines() or [""]:
            self.log.write(f"{stamp} [{os.getpid()}] {line}\n")
        self.log.flush()

    def msg(self, text: str) -> None:
        self.record(text)
        if not self.quiet:
            print(text, flush=True)

    def det(self, text: str) -> None:
        if self.vrb:
            self.msg(text)

    def dbg(self, text: str) -> None:
        if self.dbg_on:
            self.msg(" ~~~ " + text)

    def warn(self, text: str) -> None:
        self.record("WARNING: " + text)
        print("WARNING: " + text, file=sys.stderr, flush=True)

    def error(self, text: str) -> None:
        self.record(text)
        print(text, file=sys.stderr, flush=True)

    def result(self, text: str) -> None:
        """What a command was asked for (status, --version): shown under -Q too."""
        self.record(text)
        print(text, flush=True)


OUT = Out()
_phase = "starting"


def phase(text: str) -> None:
    """What the run is doing now -- the Ctrl-C line names it."""
    global _phase
    _phase = text
    OUT.dbg(f"phase: {text}")


# --- 1. config -------------------------------------------------------------

DEFAULT_CONFIG: Final = '''\
## my-ocr.conf -- KEY="value" lines. my-ocr READS this file, it never runs it:
## no commands, no functions; $NAME and ${NAME} expand a key set above, or the
## environment. Searched, first found wins (my-ocr --help). This text IS the
## defaults: a file holding only the lines it changes is complete.

## where a document goes that could not be OCRed: "" = it stays where it is
## (--move-failed-to <DIR> sets it per run; my-scan passes its FAILED folder)
MOVE_FAILED_TO=""

## file names (shell patterns, space-separated) that are never OCRed and are
## left exactly where they are; "" = none
NO_OCR_FILENAME_PATTERNS="no-ocr-scan*"

## reveal the finished file in the Finder: auto = only when a terminal
## started the run, always, never
REVEAL_IN_FINDER="auto"

## seconds DEVONthink gets for ONE document before my-ocr quits it and tries
## once more; a second time is the end of the run
OCR_TIMEOUT="600"

## seconds a run waits for its turn while another one is OCRing
MUTEX_TIMEOUT="1800"

## my-ocr's own state: the mutex and each run's work folder (copies only --
## never an original)
STATE_DIR="/var/lib/mine/$USER/my-ocr"

## the record of every run -- it gets every line, also under -Q.
## LOG="" keeps none (-l / --log turns it on for one run)
LOGFILE="/var/log/mine/$USER/my-ocr.log"
LOG=""

## where a STUCK alert also leaves a file, so it is still there tomorrow
STUCK_MARKER_DIR="$HOME/Desktop"
'''

Reveal = Literal["auto", "always", "never"]


@dataclass(frozen=True)
class Settings:
    """The config, parsed: every key typed, every default from DEFAULT_CONFIG."""

    files: Tuple[Path, ...]
    move_failed_to: str
    no_ocr_patterns: Tuple[str, ...]
    reveal: Reveal
    ocr_timeout: int
    mutex_timeout: int
    state_dir: Path
    logfile: Path
    log: bool
    stuck_marker_dir: Path

    def __post_init__(self) -> None:
        assert self.ocr_timeout > 0 and self.mutex_timeout >= 0, "checked where the config is read"


def config_search_paths() -> List[Path]:
    """Where the config is looked for, in order (after $MY_OCR_CONFIG and --config)."""
    site = Path(os.environ.get("LINKS", "/LINKS")) / "default"
    return [site / f"{SCRIPT_NAME}.conf", site / SCRIPT_NAME, Path.home() / f".{SCRIPT_NAME}.conf",
            Path("/etc") / f"{SCRIPT_NAME}.conf", Path("/usr/local/etc") / f"{SCRIPT_NAME}.conf"]


_KEY_LINE: Final = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_NAME_REF: Final = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _expand(text: str, names: Dict[str, str], where: str) -> str:
    if "$(" in text or "`" in text:
        err("00001", f"{where}: a command substitution -- my-ocr reads its config, it never runs it")

    def one(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        if name in names:
            return names[name]
        if name in os.environ:
            return os.environ[name]
        if name == "USER":
            return getpass.getuser()
        if name == "HOME":
            return str(Path.home())
        err("00002", f"{where}: ${name} is not set (a key above, or the environment)")

    return _NAME_REF.sub(one, text)


def parse_value(raw: str, names: Dict[str, str], where: str) -> str:
    """One shell word: unquoted, "double" ($ expanded) and 'single' parts, a
    leading ~ as $HOME, then nothing but a # comment."""
    out: List[str] = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "'":
            j = raw.find("'", i + 1)
            if j < 0:
                err("00003", f"{where}: a ' is not closed")
            out.append(raw[i + 1:j])
            i = j + 1
        elif c == '"':
            j = i + 1
            part: List[str] = []
            while j < len(raw) and raw[j] != '"':
                if raw[j] == "\\" and j + 1 < len(raw) and raw[j + 1] in '"\\$`':
                    j += 1
                part.append(raw[j])
                j += 1
            if j >= len(raw):
                err("00004", f"{where}: a \" is not closed")
            out.append(_expand("".join(part), names, where))
            i = j + 1
        elif c in " \t":
            rest = raw[i:].strip()
            if rest and not rest.startswith("#"):
                err("00005", f"{where}: more than one word -- quote the value")
            break
        else:
            m = re.match(r"[^'\" \t]+", raw[i:])
            assert m is not None, "c is none of the characters the word stops at"
            word = m.group(0)
            i += len(word)
            if not out and (word == "~" or word.startswith("~/")):
                word = str(Path.home()) + word[1:]
            out.append(_expand(word, names, where))
    return "".join(out)


def read_config_text(text: str, where: str, names: Dict[str, str]) -> List[str]:
    """Add the KEY=value lines of `text` to `names`; return the keys it set."""
    keys: List[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _KEY_LINE.match(s)
        if m is None:
            err("00006", f"{where}:{n}: not a KEY=\"value\" line -- my-ocr reads its config, it never runs it: {s[:60]}")
        names[m.group(1)] = parse_value(m.group(2), names, f"{where}:{n}")
        keys.append(m.group(1))
    return keys


def _seconds(value: str, key: str, where: str, least: int) -> int:
    if not value.isdigit() or int(value) < least:
        err("00007", f"{where}: {key}=\"{value}\" -- a whole number of seconds, at least {least}")
    return int(value)


def load_settings(override: Optional[str]) -> Settings:
    """The defaults, then the first config found on top -- a host with none
    runs on the defaults."""
    phase("reading the config")
    names: Dict[str, str] = {}
    defaults = read_config_text(DEFAULT_CONFIG, "the built-in defaults", names)
    chosen = override or os.environ.get("MY_OCR_CONFIG")
    found: Optional[Path] = None
    if chosen:
        if not Path(chosen).is_file():
            err("00008", f"config '{chosen}' not found (--config / $MY_OCR_CONFIG)")
        found = Path(chosen)
    else:
        found = next((p for p in config_search_paths() if p.is_file()), None)
    files: Tuple[Path, ...] = ()
    if found is not None:
        try:
            text = found.read_text(encoding="utf-8")
        except OSError as e:
            err("00009", f"cannot read the config {found}: {e.strerror}")
        for key in read_config_text(text, str(found), names):
            if key not in defaults:
                OUT.warn(f"{found}: {key} is not a my-ocr setting -- ignored ('{SCRIPT_NAME} --create-config' lists them)")
        files = (found,)
    where = str(found) if found is not None else "the built-in defaults"
    reveal = names["REVEAL_IN_FINDER"]
    if reveal not in ("auto", "always", "never"):
        err("00010", f"{where}: REVEAL_IN_FINDER=\"{reveal}\" -- auto, always or never")
    log = names["LOG"]
    if log not in ("", "0", "1"):
        err("00011", f"{where}: LOG=\"{log}\" -- \"1\" (on) or \"\" (off)")
    return Settings(
        files=files,
        move_failed_to=names["MOVE_FAILED_TO"],
        no_ocr_patterns=tuple(names["NO_OCR_FILENAME_PATTERNS"].split()),
        reveal="always" if reveal == "always" else "never" if reveal == "never" else "auto",
        ocr_timeout=_seconds(names["OCR_TIMEOUT"], "OCR_TIMEOUT", where, 1),
        mutex_timeout=_seconds(names["MUTEX_TIMEOUT"], "MUTEX_TIMEOUT", where, 0),
        state_dir=Path(names["STATE_DIR"]),
        logfile=Path(names["LOGFILE"]),
        log=log == "1",
        stuck_marker_dir=Path(names["STUCK_MARKER_DIR"]))


# --- 2. the PDF tools ------------------------------------------------------

def _tool(name: str) -> Optional[str]:
    """Homebrew's first: a LaunchDaemon's PATH has neither, and Apple ships none."""
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None


def required_tools() -> Dict[str, str]:
    """pdfinfo, pdftotext, qpdf: a result nothing has checked never replaces an
    original, so a host without them refuses to start."""
    found = {n: _tool(n) for n in ("pdfinfo", "pdftotext", "qpdf")}
    missing = [n for n, p in found.items() if p is None]
    if missing:
        err("00012", f"{', '.join(missing)} not found -- every result is checked with them before it "
                     f"replaces an original (brew install poppler qpdf)")
    return {n: p for n, p in found.items() if p is not None}


def _run(argv: Sequence[str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, errors="replace", timeout=timeout)


@dataclass(frozen=True)
class PdfFacts:
    """What pdfinfo says about a file; parses=False when it does not read it."""

    parses: bool
    pages: Optional[int]
    producer: str
    encrypted: bool


def pdf_facts(tools: Dict[str, str], path: Path) -> PdfFacts:
    r = _run([tools["pdfinfo"], str(path)])
    if r.returncode != 0:
        return PdfFacts(False, None, "", False)
    info: Dict[str, str] = {}
    for line in r.stdout.splitlines():
        k, sep, v = line.partition(":")
        if sep and k not in info:
            info[k.strip()] = v.strip()
    pages = info.get("Pages", "")
    return PdfFacts(True, int(pages) if pages.isdigit() else None, info.get("Producer", ""),
                    info.get("Encrypted", "no") != "no")


def file_type(path: Path) -> str:
    """The first word of `file --brief`: PDF, JPEG, PNG, TIFF, ..."""
    r = _run(["/usr/bin/file", "--brief", str(path)])
    words = r.stdout.split()
    return words[0] if r.returncode == 0 and words else ""


def text_chars(tools: Dict[str, str], path: Path) -> int:
    r = _run([tools["pdftotext"], str(path), "-"])
    return len("".join(r.stdout.split())) if r.returncode == 0 else 0


def qpdf_check(tools: Dict[str, str], path: Path) -> int:
    """qpdf's exit code, which is GRADED: 0 clean, 3 warnings only, anything
    else real damage. qpdf walks every object and stream; pdfinfo reads only
    the trailer and the page tree, so a mangled body passes it."""
    return _run([tools["qpdf"], "--check", str(path)], timeout=300).returncode


@dataclass(frozen=True)
class Verdict:
    """The result check: refused (why), or usable -- and whether it holds no text."""

    why: str = ""
    no_text: bool = False
    notes: Tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return not self.why


def verify_result(tools: Dict[str, str], result: Path, pages_in: Optional[int]) -> Verdict:
    """Look at the result while the original is still there: from here on the
    result REPLACES it.

    PAGES are what matters: a truncated or half-recognised result comes back
    with fewer pages than went in, and nothing else notices. MORE pages is not
    a failure (the OCR splits a sheet it takes for several pages). NO TEXT is
    not a failure either: everything else here is the evidence that the OCR
    ran correctly, and a page with nothing to recognise -- a photograph, a
    blank sheet -- is a finding about the DOCUMENT."""
    facts = pdf_facts(tools, result)
    if not facts.parses:
        return Verdict(why="it does not parse as a PDF")
    if facts.pages is None:
        return Verdict(why="its page count cannot be read")
    if pages_in is not None and facts.pages < pages_in:
        return Verdict(why=f"it LOST pages -- {pages_in} went in, {facts.pages} came out")
    notes: List[str] = []
    q = qpdf_check(tools, result)
    if q == 3:
        notes.append("qpdf reports warnings (not damage)")
    elif q != 0:
        return Verdict(why=f"qpdf reports it as structurally damaged (exit {q})")
    if pages_in is not None and facts.pages > pages_in:
        notes.append(f"the OCR split pages -- {pages_in} in, {facts.pages} out")
    return Verdict(no_text=text_chars(tools, result) == 0, notes=tuple(notes))


def tag_producer(tools: Dict[str, str], path: Path, producer: str) -> str:
    """Write `producer` into the PDF's /Producer, in place; "" = done, else why not.

    qpdf's JSON round trip, not a rewrite: the page content is copied through
    untouched and the file grows by the metadata alone (Ghostscript would stamp
    its own /Producer over it). It overwrites a document, so it installs only
    on proof -- the copy parses, kept every page, and qpdf calls it sound --
    and through a rename, so no reader ever sees half a file."""
    import json
    before = pdf_facts(tools, path).pages
    if before is None:
        return "its page count cannot be read"
    work = path.with_name(f".{path.name}.my-ocr-tag.{os.getpid()}")
    js, new = work.with_suffix(".json"), work.with_suffix(".new")
    try:
        if _run([tools["qpdf"], str(path), "--json-output", str(js)], timeout=300).returncode != 0:
            return "qpdf could not read it as JSON"
        try:
            d = json.loads(js.read_text(encoding="utf-8"))
            objs = d["qpdf"][1]
            trailer = objs["trailer"]["value"]
            ref = trailer.get("/Info")
            if isinstance(ref, str) and f"obj:{ref}" in objs and isinstance(objs[f"obj:{ref}"].get("value"), dict):
                objs[f"obj:{ref}"]["value"]["/Producer"] = "u:" + producer
            else:
                hdr = d["qpdf"][0]
                nid = int(hdr.get("maxobjectid", 0)) + 1
                hdr["maxobjectid"] = nid
                objs[f"obj:{nid} 0 R"] = {"value": {"/Producer": "u:" + producer}}
                trailer["/Info"] = f"{nid} 0 R"
            js.write_text(json.dumps(d), encoding="utf-8")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return "could not set /Producer in qpdf's JSON"
        if _run([tools["qpdf"], str(path), f"--update-from-json={js}", str(new)], timeout=300).returncode != 0:
            return "qpdf could not write the tagged copy"
        after = pdf_facts(tools, new)
        if after.pages != before:
            return f"the tagged copy has {after.pages if after.pages is not None else 'no readable'} page(s), not {before}"
        if qpdf_check(tools, new) not in (0, 3):
            return "the tagged copy does not survive qpdf --check"
        if producer not in after.producer:
            return f"the tagged copy reads Producer '{after.producer}'"
        shutil.copymode(str(path), str(new))
        os.replace(str(new), str(path))
        return ""
    except OSError as e:
        return f"{e.strerror or e}"
    finally:
        for p in (js, new):
            with contextlib.suppress(OSError):
                p.unlink()


# --- 3. DEVONthink ---------------------------------------------------------

# Every call is one osascript run with the paths as ARGUMENTS -- never pasted
# into the script text, so no name can break or change the script. Each one
# answers on one tab-separated line: OK ... or ERR <number> <message>. The tab
# is `sep`, made OUTSIDE the tell block: inside it, the word `tab` is one of
# DEVONthink's own terms, and "OK" & tab & "0" came back as "OKtab0" (ada,
# 2026-10-24).
# The group must be NAMED: without one, DEVONthink opens a group SELECTOR
# dialog and waits for a click nobody makes.
# A DEVONthink that the call itself starts answers before its databases are
# open: "Can't get inbox." (-1728, horse 2026-10-25, the first call after
# launch). So every script that reaches the Inbox first waits for it -- up to
# 60 s, after which the Inbox call fails and says so.
_AS_WAIT_INBOX: Final = '''\
        repeat 60 times
          try
            get inbox
            exit repeat
          on error number -1728
            delay 1
          end try
        end repeat
'''

_AS_OCR: Final = '''\
-- my-ocr:ocr
on run argv
  set sep to character id 9
  set inPath to item 1 of argv
  set outDir to item 2 of argv
  set secs to (item 3 of argv) as integer
  try
    with timeout of secs seconds
      tell application id "DNtp"
''' + _AS_WAIT_INBOX + '''        set g to create location "''' + DT_GROUP + '''" in inbox
        set r to ocr file inPath to g waiting for reply true
        if r is missing value then return "ERR" & sep & "0" & sep & "DEVONthink's OCR returned no document"
        set u to uuid of r
        set p to export record r to outDir DEVONtech_Storage false
        delete record r
        if (get record with uuid u) is missing value then
          set gone to "deleted"
        else
          set gone to "KEPT"
        end if
        return "OK" & sep & p & sep & u & sep & gone
      end tell
    end timeout
  on error m number n
    return "ERR" & sep & n & sep & m
  end try
end run
'''

# What a run killed in the middle left in my-ocr's own group -- that group
# only, never another record. The children are read ONCE (`get`) and deleted
# from that list: a loop over the live `children of g` shifts under its own
# deletes -- "Can't get item 2 of every child" (horse, 2026-10-25, two
# leftovers).
_AS_SWEEP: Final = '''\
-- my-ocr:sweep
on run argv
  set sep to character id 9
  try
    with timeout of 120 seconds
      tell application id "DNtp"
''' + _AS_WAIT_INBOX + '''        if not (exists record at "''' + DT_GROUP + '''" in inbox) then return "OK" & sep & "0"
        set g to get record at "''' + DT_GROUP + '''" in inbox
        set kids to get children of g
        set n to count of kids
        repeat with i from 1 to n
          delete record (item i of kids)
        end repeat
        return "OK" & sep & n
      end tell
    end timeout
  on error m number n
    return "ERR" & sep & n & sep & m
  end try
end run
'''

_AS_QUIT: Final = '''\
-- my-ocr:quit
on run argv
  set sep to character id 9
  try
    with timeout of 60 seconds
      tell application id "DNtp" to quit
    end timeout
    return "OK"
  on error m number n
    return "ERR" & sep & n & sep & m
  end try
end run
'''

# AppleScript's error numbers that say something about DEVONthink or the
# machine, not about the document: the run stops on them instead of filing
# every following document under FAILED.
OSA_NOT_AUTHORIZED: Final = -1743     # macOS Automation consent missing
OSA_TIMED_OUT: Final = -1712          # 'with timeout' expired
OSA_ENGINE_GONE: Final = (-600, -609, -903, -10810)   # not running / connection lost / cannot launch


@dataclass(frozen=True)
class Answer:
    """One osascript call: ok and its fields, or the error number and text."""

    ok: bool
    fields: Tuple[str, ...] = ()
    number: int = 0
    text: str = ""
    timed_out: bool = False


def osa(script: str, args: Sequence[str], timeout: int) -> Answer:
    try:
        r = subprocess.run([EXT.osascript, "-", *args], input=script, capture_output=True, text=True,
                           errors="replace", timeout=timeout + EXT.osa_grace)
    except subprocess.TimeoutExpired:
        return Answer(False, timed_out=True, number=OSA_TIMED_OUT,
                      text=f"no answer from DEVONthink in {timeout + EXT.osa_grace}s -- osascript was stopped")
    line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    parts = tuple(line.split("\t"))
    if r.returncode == 0 and parts[:1] == ("OK",):
        return Answer(True, fields=parts[1:])
    if r.returncode == 0 and parts[:1] == ("ERR",) and len(parts) >= 3:
        try:
            number = int(parts[1])
        except ValueError:
            number = 0
        return Answer(False, number=number, text="\t".join(parts[2:]), timed_out=number == OSA_TIMED_OUT)
    detail = (r.stderr.strip() or r.stdout.strip() or "no output").splitlines()[-1]
    return Answer(False, number=1, text=f"osascript rc {r.returncode}: {detail}")


def engine_problem(a: Answer) -> str:
    """Why the RUN cannot go on, from an answer -- "" when the answer is about
    one document."""
    if a.number == OSA_NOT_AUTHORIZED:
        return ("macOS does not let my-ocr control DEVONthink -- allow it once: System Settings > "
                "Privacy & Security > Automation, or run 'my-ocr <file>' in a terminal on this Mac "
                f"and answer the dialog [{a.number}: {a.text}]")
    if a.number in OSA_ENGINE_GONE:
        return f"DEVONthink cannot be reached [{a.number}: {a.text}]"
    if a.number == 1:
        return f"DEVONthink could not be asked [{a.text}]"
    return ""


def dt_settings() -> Dict[str, str]:
    """DEVONthink's OCR settings as my-ocr depends on them, read from its
    preferences -- never by starting it. A box DEVONthink leaves at its default
    has no key at all, so "no key" = on, and only `false` = off (seen on ada,
    2026-10-23: unticked -> false, ticked again -> key removed)."""
    r = _run(["/usr/bin/defaults", "export", DT_DEFAULTS, "-"])
    prefs: Dict[str, object] = {}
    try:
        loaded: object = plistlib.loads(r.stdout.encode()) if r.returncode == 0 else {}
    except Exception:   # plistlib raises several types for a malformed file  # noqa: BLE001
        loaded = {}
    if isinstance(loaded, dict):
        prefs = {str(k): v for k, v in cast(Dict[object, object], loaded).items()}

    def box(key: str) -> str:
        return "off" if prefs.get(key) is False else "on"

    langs = prefs.get("OCRPlugin.OCRLanguages")
    return {
        "languages": ",".join(str(x) for x in cast(List[object], langs)) if isinstance(langs, list) else "?",
        "deskew": box("OCRPlugin.AutoCorrectSkew"),
        "orientation": box("OCRPlugin.AutoRotate"),
    }


def dt_running() -> str:
    r = _run(["/usr/bin/pgrep", "-x", DT_PROCESS])
    return " ".join(r.stdout.split())


# --- 4. alerts -------------------------------------------------------------

def send_alert(subject: str, text: str, body: str) -> int:
    """The one way an alert leaves my-ocr -- --test-mail goes through it too,
    so a passing test says something about the real ones. One my-notify call
    is a notification AND a mail."""
    try:
        return subprocess.run([EXT.my_notify, "--", subject, text, "--mail", subject, body], timeout=120).returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        OUT.warn(f"my-notify could not be run: {e}")
        return 1


def notify(subject: str, text: str) -> None:
    """A notification only -- for what the person at the machine should see."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run([EXT.my_notify, "--", subject, text], timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Incident:
    """Every alert of a run belongs to ONE incident: STUCK, then FAILED or
    RESOLVED. A notification for whoever is at the Mac, a mail for whoever is
    not, and ONE marker file that is still there tomorrow -- moved to the Trash
    when the incident resolves, because a marker is there to be acted on."""

    def __init__(self, s: Settings) -> None:
        self.s = s
        self.marker: Optional[Path] = None

    def stuck(self, subject: str, text: str) -> None:
        OUT.msg(f" >>> !!! {text}")
        if send_alert(subject, text, text) != 0:
            OUT.warn("my-notify failed -- the alert may not have gone out")
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            if self.marker is None:
                self.marker = self.s.stuck_marker_dir / f",,{SCRIPT_NAME}.STUCK.{datetime.datetime.now():%Y-%m-%d_%H%M.%S}.txt"
                self.marker.write_text(f"{stamp} {subject}\n{text}\n", encoding="utf-8")
                OUT.msg(f"    > wrote marker {self.marker}")
            else:
                with self.marker.open("a", encoding="utf-8") as f:
                    f.write(f"\n{stamp} {subject}\n{text}\n")
        except OSError as e:
            OUT.warn(f"could not write the marker in {self.s.stuck_marker_dir}: {e.strerror}")

    def resolved(self, how: str) -> None:
        if self.marker is None:
            return
        moved = trash(self.marker)
        text = f"RESOLVED: {how} [marker {'moved to the Trash' if moved else 'NOT moved: ' + str(self.marker)}]"
        OUT.msg(f" >>> {text}")
        if send_alert(f"{SCRIPT_NAME} RESOLVED", text, text) != 0:
            OUT.warn("my-notify failed -- the RESOLVED mail may not have gone out")
        self.marker = None


# --- 4. files: the Trash, the quarantine ------------------------------------

def free_name(folder: Path, name: str) -> Path:
    """`name` in `folder`, or -- when it is taken -- with a time stamp, the way
    the Finder does it. Never onto an existing file: that destroyed what was
    there, and failed outright when it belonged to another user."""
    p = folder / name
    if not (p.exists() or p.is_symlink()):
        return p
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    for extra in (f" {datetime.datetime.now():%H-%M-%S}", f" {datetime.datetime.now():%H-%M-%S} {os.getpid()}"):
        p = folder / (f"{stem}{extra}.{ext}" if ext else f"{stem}{extra}")
        if not (p.exists() or p.is_symlink()):
            return p
    err("00013", f"no free name for '{name}' in {folder}")


def trash(path: Path, name: Optional[str] = None) -> bool:
    """MOVE `path` to the Trash ('trash' does not work on a network share)."""
    try:
        shutil.move(str(path), str(free_name(EXT.trash_dir, name or path.name)))
        return True
    except OSError:
        return False


def quarantine(s: Settings, path: Path) -> str:
    """Where a document that could not be OCRed now is, said in words."""
    if not s.move_failed_to:
        return f"kept where it is: {path}"
    try:
        target = free_name(Path(s.move_failed_to), path.name)
        shutil.move(str(path), str(target))
        return f"moved to {target}"
    except OSError as e:
        return f"could NOT be moved to {s.move_failed_to} ({e.strerror}) -- it is still at {path}"


# --- 4. one document --------------------------------------------------------

Outcome = Literal["ocred", "notext", "refused", "failed"]


@dataclass(frozen=True)
class Done:
    """What happened to one document, and where it is now."""

    outcome: Outcome
    path: Path
    detail: str = ""


class StopRun(Exception):
    """The run cannot go on: DEVONthink is unreachable, or stuck twice."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


@dataclass
class Run:
    """One run's state: settings, tools, its work folder, the incident."""

    s: Settings
    tools: Dict[str, str]
    work: Path
    incident: Incident
    n: int = 0


def install(result: Path, target: Path, original: Path) -> str:
    """Put `result` in place of `original` at `target`; "" = done, else why not.

    The document is never absent, at any moment: the original is COPIED to the
    Trash first, then the result -- already beside the target, on the same
    disk -- is renamed over it in one step. A run killed in between leaves the
    original in place and a spare copy in the Trash, never a hole."""
    tmp = target.with_name(f".{target.name}.my-ocr.{os.getpid()}.new")
    try:
        shutil.copyfile(str(result), str(tmp))
        with contextlib.suppress(OSError):
            shutil.copymode(str(original), str(tmp))
        try:
            shutil.copy2(str(original), str(free_name(EXT.trash_dir, original.name)))
        except OSError as e:
            return f"could not put the original into the Trash first ({e.strerror}) -- nothing replaced"
        os.replace(str(tmp), str(target))
        if original != target:
            original.unlink()          # its copy is in the Trash
        return ""
    except OSError as e:
        return f"{e.strerror or e}"
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def ask_devonthink(run: Run, copy: Path, out: Path) -> Answer:
    """One OCR, retried ONCE when DEVONthink does not answer in time: quit it,
    and the same copy goes again. A second time ends the run."""
    a = osa(_AS_OCR, [str(copy), str(out), str(run.s.ocr_timeout)], run.s.ocr_timeout)
    if not a.timed_out:
        return a
    run.incident.stuck(f"{SCRIPT_NAME} STUCK -- retrying",
                       f"DEVONthink did not finish '{copy.name}' in {run.s.ocr_timeout}s [{a.text}]. "
                       f"Quitting it and trying once more.")
    q = osa(_AS_QUIT, [], 60)
    OUT.msg(f"    > DEVONthink: quit {'done' if q.ok else 'FAILED [' + q.text + ']'}")
    for p in out.iterdir():
        p.unlink()
    a = osa(_AS_OCR, [str(copy), str(out), str(run.s.ocr_timeout)], run.s.ocr_timeout)
    if a.timed_out:
        text = (f"DEVONthink did not finish '{copy.name}' in {run.s.ocr_timeout}s twice -- the run stops here; "
                f"documents not reached yet are untouched [{a.text}]")
        run.incident.stuck(f"{SCRIPT_NAME} STUCK -- gave up", text)
    else:
        run.incident.resolved("the retry got an answer from DEVONthink")
    return a


def ocr_one(run: Run, original: Path, kind: str, pages_in: Optional[int]) -> Done:
    """OCR one document, and leave it in exactly one of four states.

    DEVONthink only ever sees a COPY in my-ocr's work folder. The original is
    touched once, at the very end, after the result has passed the check."""
    run.n += 1
    wd = run.work / str(run.n)
    out = wd / "out"
    out.mkdir(parents=True)
    copy = wd / ("in." + ("pdf" if kind == "PDF" else original.suffix.lower().lstrip(".") or kind.lower()))
    shutil.copyfile(str(original), str(copy))

    a = ask_devonthink(run, copy, out)
    if a.timed_out:
        where = quarantine(run.s, original)
        raise StopRun("00014", f"'{original.name}' defeated DEVONthink twice -- {where}. {a.text}")
    if not a.ok:
        problem = engine_problem(a)
        if problem:
            raise StopRun("00015", problem)
        return Done("failed", original, f"DEVONthink could not OCR it [{a.number}: {a.text}] -- {quarantine(run.s, original)}")
    if len(a.fields) >= 3 and a.fields[2] != "deleted":
        OUT.warn(f"DEVONthink kept its record of '{original.name}' (uuid {a.fields[1]}) -- the next run removes it")

    made = [p for p in out.iterdir() if not p.name.startswith(".")]
    if len(made) != 1:
        return Done("failed", original, f"DEVONthink's export left {len(made)} file(s), not 1 -- "
                                        f"{quarantine(run.s, original)}")
    result = made[0]
    # a result that does not take the original's place goes to the Trash under
    # a name that says whose it is
    spoil = f"{original.stem}.my-ocr-result.pdf"

    v = verify_result(run.tools, result, pages_in)
    for note in v.notes:
        OUT.msg(f"    > note: {note} ('{original.name}')")
    if not v.usable:
        trashed = trash(result, spoil)
        where = quarantine(run.s, original)
        notify(f"[{SCRIPT_NAME}] OCR REFUSED: {original.name}",
               f"{v.why}. The original was NOT replaced -- {where}. Look at it; it probably needs scanning again.")
        return Done("refused", original, f"{v.why} -- the original was NOT replaced, {where}; "
                                         f"the result {'went to the Trash' if trashed else 'stays in ' + str(result)}")

    if v.no_text and kind == "PDF":
        # nothing to recognise: the DOCUMENT is the original, the OCR output is
        # strictly worse -- tag the original so no later run OCRs it again
        why = tag_producer(run.tools, original, PRODUCER_NOTEXT)
        if not why:
            trash(result, spoil)
            return Done("notext", original, "no text to find -- the original was kept and marked as done")
        OUT.msg(f"    > could not mark the original ({why}) -- keeping the OCR result instead")

    why = tag_producer(run.tools, result, PRODUCER_OCRED)
    if why:
        trashed = trash(result, spoil)
        where = quarantine(run.s, original)
        return Done("refused", original, f"the result could not be marked as OCRed ({why}) -- the original was "
                                         f"NOT replaced, {where}; the result {'went to the Trash' if trashed else 'stays in ' + str(result)}")

    target = original if kind == "PDF" else original.with_suffix(".pdf")
    if target != original and (target.exists() or target.is_symlink()):
        return Done("failed", original, f"'{target.name}' already exists beside it -- nothing was replaced; "
                                        f"{quarantine(run.s, original)}")
    why = install(result, target, original)
    if why:
        return Done("failed", original, f"the result could not be put in place: {why}")
    return Done("notext" if v.no_text else "ocred", target,
                "no text to find -- filed as OCRed" if v.no_text else "")


# --- 4. the inputs ----------------------------------------------------------

Skip = Literal["dir", "marker", "type", "unreadable", "already", "encrypted", "encrypted-named", "twice"]


@dataclass(frozen=True)
class Item:
    """One name the run was given, judged: OCR it (kind, pages), or skip it (why)."""

    given: str
    path: Path
    kind: str = ""
    pages: Optional[int] = None
    skip: Optional[Skip] = None
    detail: str = ""


def judge(s: Settings, tools: Dict[str, str], given: str, seen: Dict[str, str], force: bool) -> Item:
    p = Path(given)
    if p.is_dir():
        return Item(given, p, skip="dir")
    for pat in s.no_ocr_patterns:
        if fnmatch.fnmatchcase(p.name, pat):
            return Item(given, p, skip="marker", detail=pat)
    real = os.path.realpath(given)
    if real in seen:
        return Item(given, p, skip="twice", detail=seen[real])
    seen[real] = given
    kind = file_type(p)
    if kind not in INPUT_TYPES:
        return Item(given, p, skip="type", detail=kind or "unreadable")
    if kind != "PDF":
        return Item(given, p, kind=kind, pages=1 if kind != "TIFF" else None)
    f = pdf_facts(tools, p)
    if not f.parses:
        return Item(given, p, skip="unreadable", detail="pdfinfo does not read it")
    if f.encrypted:
        if re.search(r"___encrypted\.pdf$", p.name, re.IGNORECASE):
            return Item(given, p, skip="encrypted-named")
        return Item(given, p, skip="encrypted")
    # CONTAINS, not equals: "FineReader (my-ocr: ...)" is already done too
    if PRODUCER_TAG in f.producer and not force:
        return Item(given, p, skip="already", detail=f.producer)
    return Item(given, p, kind="PDF", pages=f.pages)


# --- 4. the mutex -----------------------------------------------------------

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def owner_of(mutex: Path) -> Tuple[Optional[int], str]:
    """The pid in the mutex's owner file, and the whole line; (None, "") when
    there is none yet -- an owner file not written yet is NOT an absent owner."""
    try:
        line = (mutex / "owner").read_text(encoding="utf-8").strip()
    except OSError:
        return None, ""
    m = re.match(r"pid=(\d+)", line)
    return (int(m.group(1)) if m else None), line


def write_owner(mutex: Path, doing: str) -> None:
    tmp = mutex / f".owner.{os.getpid()}"
    tmp.write_text(f"pid={os.getpid()} at={datetime.datetime.now():%Y-%m-%dT%H:%M:%S} doing={doing}\n",
                   encoding="utf-8")
    os.replace(str(tmp), str(mutex / "owner"))


def take_mutex(s: Settings) -> Path:
    """One OCR at a time: an atomic mkdir, waited for. Only a PROVABLY dead
    owner is broken, and never silently -- a long OCR is not a crashed one."""
    phase("waiting for the OCR mutex")
    mutex = s.state_dir / "mutex"
    waited, said = 0, False
    while True:
        try:
            mutex.mkdir()
            write_owner(mutex, "starting")
            return mutex
        except FileExistsError:
            pid, line = owner_of(mutex)
            if pid is not None and not _alive(pid):
                OUT.msg(f" >>> breaking a stale OCR mutex: its owner {pid} is gone ({line})")
                with contextlib.suppress(OSError):
                    (mutex / "owner").unlink()
                with contextlib.suppress(OSError):
                    mutex.rmdir()
                continue
            if not said:
                OUT.msg(f" >>> another OCR is running ({line or 'owner not written yet'}) -- waiting for our turn "
                        f"(at most {s.mutex_timeout}s)")
                said = True
            if waited >= s.mutex_timeout:
                err("00016", f"waited {waited}s for the OCR mutex {mutex} -- it is held by {line or 'an unknown run'}")
            time.sleep(1)
            waited += 1


def release_mutex(mutex: Path) -> None:
    pid, _ = owner_of(mutex)
    if pid != os.getpid():
        OUT.warn(f"the OCR mutex {mutex} is not ours (owner {pid}) -- left alone")
        return
    with contextlib.suppress(OSError):
        (mutex / "owner").unlink()
    try:
        mutex.rmdir()
    except OSError as e:
        OUT.warn(f"could not release the OCR mutex {mutex}: {e.strerror}")


def stale_work(s: Settings) -> List[Path]:
    """Work folders of runs that are gone -- copies only, never an original."""
    root = s.state_dir / "work"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if not (p.name.isdigit() and _alive(int(p.name))))


# --- 4. one run --------------------------------------------------------------

@dataclass
class Tally:
    done: List[Done] = field(default_factory=lambda: [])
    skipped: List[Item] = field(default_factory=lambda: [])


def gather(c: Cli) -> List[str]:
    """The names to OCR: the command line and --file. stdin ONLY when neither
    gave any: from a script, a LaunchDaemon or ssh, stdin is an open pipe
    nobody writes to -- reading it would block for ever, holding the mutex."""
    names = list(c.files)
    if c.file_list:
        try:
            names += [x for x in Path(c.file_list).read_text(encoding="utf-8").splitlines() if x.strip()]
        except OSError as e:
            err("00017", f"--file {c.file_list}: {e.strerror}")
    if not names and not c.files_given and not c.file_list:
        if sys.stdin.isatty():
            err("00018", f"no <FILE> to OCR -- name them, pipe them in, or use --file ({SCRIPT_NAME} --help)")
        phase("reading the names on stdin")
        names = [x.rstrip("\n") for x in sys.stdin if x.strip()]
    for n in names:
        if not os.path.lexists(n):
            err("00019", f"'{n}' does not exist")
    return names


def interactive() -> bool:
    """A person started this: a terminal on any of the three streams."""
    return any(s.isatty() for s in (sys.stdin, sys.stdout, sys.stderr))


def summary(t: Tally) -> List[str]:
    k: Dict[str, List[Done]] = {o: [d for d in t.done if d.outcome == o] for o in ("ocred", "notext", "refused", "failed")}
    sk: Dict[str, int] = {}
    for i in t.skipped:
        sk[i.skip or ""] = sk.get(i.skip or "", 0) + 1
    total = len(t.done) + len(t.skipped)
    lines = [f">RESULT< {total} file(s): {len(k['ocred'])} OCRed, {len(k['notext'])} with no text, "
             f"{len(k['refused'])} refused, {len(k['failed'])} failed, {len(t.skipped)} skipped"]
    if t.skipped:
        names = {"already": "already OCRed", "type": "not a PDF/JPEG/PNG/TIFF", "unreadable": "unreadable",
                 "dir": "a folder", "marker": "marked no-OCR", "encrypted": "ENCRYPTED",
                 "encrypted-named": "encrypted (___encrypted)", "twice": "named twice"}
        lines.append(">RESULT<   skipped: " + ", ".join(f"{n} {names.get(w, w)}" for w, n in sorted(sk.items())))
    for d in k["notext"]:
        lines.append(f">RESULT<   no text: {d.path}  ({d.detail})")
    for d in k["refused"]:
        lines.append(f">RESULT<   REFUSED: {d.path}\n>RESULT<            {d.detail}")
    for d in k["failed"]:
        lines.append(f">RESULT<   FAILED: {d.path}\n>RESULT<           {d.detail}")
    for i in t.skipped:
        if i.skip == "encrypted":
            lines.append(f">RESULT<   ENCRYPTED, decrypt it and run again: {i.path}")
    return lines


def cmd_ocr(s: Settings, c: Cli) -> int:
    phase("checking the tools")
    tools = required_tools()
    names = gather(c)
    if not EXT.dt_app.is_dir():
        err("00020", f"DEVONthink is not installed at {EXT.dt_app} -- my-ocr OCRs with it (brew install --cask devonthink)")
    if not EXT.dt_ocr_helper.is_dir():
        err("00021", f"DEVONthink's ABBYY OCR add-on is missing ({EXT.dt_ocr_helper}) -- install it in DEVONthink > "
                     "Install Add-ons; nothing was touched")
    try:
        s.state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        err("00022", f"cannot create STATE_DIR {s.state_dir}: {e.strerror}")
    mutex = take_mutex(s)
    work = s.state_dir / "work" / str(os.getpid())
    t = Tally()
    stop: Optional[StopRun] = None
    try:
        for old in stale_work(s):
            shutil.rmtree(old, ignore_errors=True)
            OUT.det(f"    > removed the work folder of run {old.name}, which is gone (copies only)")
        work.mkdir(parents=True)
        run = Run(s, tools, work, Incident(s))
        phase("judging the files")
        items: List[Item] = []
        seen: Dict[str, str] = {}
        for n in names:
            it = judge(s, tools, n, seen, c.force)
            (t.skipped if it.skip else items).append(it)
            if it.skip:
                OUT.det(f"    > skip {n}: {it.skip}{' (' + it.detail + ')' if it.detail else ''}")
        OUT.msg(f" >>> {len(items)} of {len(names)} file(s) to OCR with DEVONthink"
                + (f" ({len(t.skipped)} skipped)" if t.skipped else ""))
        if items:
            phase("clearing DEVONthink's my-ocr group")
            a = osa(_AS_SWEEP, [], 120)
            if not a.ok:
                stop = StopRun("00023", engine_problem(a) or f"DEVONthink did not answer [{a.number}: {a.text}]")
            elif a.fields[:1] != ("0",):
                OUT.msg(f"    > removed {a.fields[0]} leftover record(s) from DEVONthink's {DT_GROUP} group")
        for k, it in enumerate(items, 1):
            if stop is not None:
                break
            phase(f"OCRing {it.path.name} ({k}/{len(items)})")
            write_owner(mutex, f"{it.path.name} ({k}/{len(items)})")
            started = time.monotonic()
            try:
                d = ocr_one(run, it.path, it.kind, it.pages)
            except StopRun as e:
                stop = e
                break
            t.done.append(d)
            word = {"ocred": "OCRed", "notext": "no text", "refused": "REFUSED", "failed": "FAILED"}[d.outcome]
            OUT.msg(f" >>> {word}: {d.path}  [{time.monotonic() - started:.0f}s]" + (f"\n    > {d.detail}" if d.detail and d.outcome in ("refused", "failed") else ""))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        release_mutex(mutex)
    for line in summary(t):
        OUT.msg(line)
    last = next((d.path for d in reversed(t.done) if d.outcome in ("ocred", "notext")), None)
    if last is not None and (s.reveal == "always" or (s.reveal == "auto" and interactive())):
        if subprocess.run(["/usr/bin/open", "-R", str(last)]).returncode == 0:
            OUT.det(f"    > revealed {last} in the Finder")
    if stop is not None:
        reached = len(t.done) + len(t.skipped)
        err(stop.code, f"{stop.text} [{reached} of {len(names)} file(s) dealt with; the rest are untouched]")
    return 0


# --- 5. status, reset -------------------------------------------------------

def cmd_status(s: Settings) -> int:
    """What my-ocr is doing and what it needs -- read-only: it starts nothing,
    takes no lock and never launches DEVONthink. my-scan reads the OCR line."""
    mutex = s.state_dir / "mutex"
    if mutex.is_dir():
        pid, line = owner_of(mutex)
        doing = re.search(r"doing=(.*)$", line)
        if pid is None or _alive(pid):
            OUT.result(f"OCR       BUSY -- {doing.group(1) if doing else '<not yet known>'}   [{line or 'owner not written yet'}]")
        else:
            OUT.result(f"OCR       STALE LOCK -- owner {pid} is gone ({line}) -- '{SCRIPT_NAME} reset go'")
    else:
        OUT.result("OCR       idle")
    if EXT.dt_app.is_dir():
        pids = dt_running()
        OUT.result(f"ENGINE    DEVONthink installed, {'running [pid ' + pids + ']' if pids else 'not running'}   [{EXT.dt_app}]")
        st = dt_settings()
        OUT.result(f"SETTINGS  languages {st['languages']}, deskew {st['deskew']}, page orientation {st['orientation']}"
                   f"   [{DT_DEFAULTS}]")
        OUT.result(f"ADD-ON    ABBYY OCR installed   [{EXT.dt_ocr_helper}]" if EXT.dt_ocr_helper.is_dir() else
                   f"ADD-ON    ABBYY OCR MISSING -- OCR cannot run (DEVONthink > Install Add-ons)   [{EXT.dt_ocr_helper}]")
    else:
        OUT.result(f"ENGINE    DEVONthink NOT INSTALLED at {EXT.dt_app} -- OCR cannot run (brew install --cask devonthink)")
    # what my-ocr takes, as `file --brief` names it -- my-scan reads this line
    # instead of keeping a list of its own, and takes nothing without it
    OUT.result(f"TYPES     {', '.join(INPUT_TYPES)}")
    tools = {n: _tool(n) for n in ("pdfinfo", "pdftotext", "qpdf")}
    missing = [n for n, p in tools.items() if p is None]
    OUT.result("TOOLS     " + (f"MISSING {', '.join(missing)} -- brew install poppler qpdf" if missing else "pdfinfo, pdftotext, qpdf"))
    stale = stale_work(s)
    OUT.result("HEALTH    " + (f"ATTENTION -- {len(stale)} work folder(s) of runs that are gone -- '{SCRIPT_NAME} reset go'"
                               if stale else "ok") + f"   [work folders of gone runs {len(stale)}]")
    OUT.result(f"CONFIG    {s.files[0] if s.files else 'none found -- built-in defaults (' + SCRIPT_NAME + ' --create-config prints them)'}")
    OUT.det(f"          state {s.state_dir}")
    OUT.det(f"          failed documents go to {s.move_failed_to or '(nowhere: they stay where they are)'}")
    return 0


def cmd_reset(s: Settings, go: bool) -> int:
    """my-ocr's machinery back to clean after a run that was killed: a mutex
    whose owner is gone, the work folders of gone runs (copies only). An
    original is never part of it -- my-ocr replaces one only in a single
    rename -- and a LIVE run is never touched. Shows; 'go' does."""
    found = 0
    mutex = s.state_dir / "mutex"
    if mutex.is_dir():
        pid, line = owner_of(mutex)
        if pid is not None and not _alive(pid):
            found += 1
            OUT.msg(f" >>> {'removed' if go else 'would remove'} the OCR mutex of run {pid}, which is gone ({line})")
            if go:
                with contextlib.suppress(OSError):
                    (mutex / "owner").unlink()
                mutex.rmdir()
        else:
            OUT.msg(f" >>> an OCR is running ({line or 'owner not written yet'}) -- left alone")
    for p in stale_work(s):
        found += 1
        OUT.msg(f" >>> {'removed' if go else 'would remove'} the work folder of run {p.name}, which is gone (copies only): {p}")
        if go:
            shutil.rmtree(p)
    if not found:
        OUT.msg(" >>> nothing to reset")
    elif not go:
        OUT.msg(f"    > nothing was changed -- '{SCRIPT_NAME} reset go' does it")
    OUT.det("    > DEVONthink's my-ocr group is cleared by the next run, which starts it anyway")
    return 0


# --- 5. the admin flags -----------------------------------------------------

def build_id() -> str:
    return hashlib.sha256(Path(os.path.realpath(__file__)).read_bytes()).hexdigest()[:12]


def version_line() -> str:
    """The exact bytes (build id) and, in a checkout of my-ocr's OWN repository,
    git's describe string -- a copy in a foreign repo must not read that one's
    tags as its releases. The link is followed first: /LINKS/bin/my-ocr points
    into another repo."""
    me = Path(os.path.realpath(__file__))
    desc = SCRIPT_COMMIT

    def git(*a: str) -> str:
        r = subprocess.run(["git", "-C", str(me.parent), *a], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""

    try:
        if git("remote", "get-url", "origin").endswith((REPOSITORY, REPOSITORY + ".git")) \
                and git("ls-files", me.name) == me.name:
            desc = git("describe", "--tags", "--long", "--always", "--dirty") or desc
    except (OSError, subprocess.SubprocessError):
        pass
    return f"{SCRIPT_NAME} {SCRIPT_VERSION} ({desc}, build {build_id()})"


def cmd_test_mail(s: Settings) -> int:
    """ONE alert through send_alert(), the call the real STUCK alerts make."""
    body = (f"This is a test mail from '{SCRIPT_NAME} --test-mail'. It went out through\n"
            f"send_alert(), the call my-ocr's real STUCK alerts use.\n\n"
            f"Host:    {socket.gethostname().split('.')[0]}\nUser:    {getpass.getuser()}\n"
            f"Time:    {datetime.datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}\n"
            f"Config:  {s.files[0] if s.files else 'none (built-in defaults)'}\nVersion: {version_line()}")
    rc = send_alert(f"{SCRIPT_NAME} test mail", "test alert -- a test mail went out too", body)
    if rc != 0:
        err("00024", f"test mail NOT sent to the my-notify default recipient [my-notify rc {rc}]")
    OUT.result(f" >>> test mail sent to the my-notify default recipient [my-notify rc {rc}]")
    return 0


def create_config(target: Optional[str]) -> int:
    if target is None:
        sys.stdout.write(DEFAULT_CONFIG)
        return 0
    p = Path(target)
    try:
        with p.open("x", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG)
    except FileExistsError:
        err("00025", f"{p} exists -- never written over (compare: {SCRIPT_NAME} --create-config | diff - {p})")
    OUT.result(f" >>> wrote the default config to {p}")
    return 0


# --- 5. the command line ------------------------------------------------------

HELP: Final = f"""\
usage: {SCRIPT_NAME} [OPTIONS] [--] [<FILE>..]
       {SCRIPT_NAME} [OPTIONS] status | reset [go]

Make scanned PDFs and pictures (JPEG, PNG, TIFF) searchable with DEVONthink's
OCR (the ABBYY FineReader engine). Each document that is OCRed is REPLACED
by the searchable PDF -- a picture by a PDF of the same name -- and the
original goes to the Trash. Nothing is replaced before the result has passed
the check: it parses, kept every page, qpdf calls it sound.

<FILE>s come from the command line and --file; from stdin only when neither
names any. Skipped, and left where they are: documents already OCRed (their
PDF Producer contains "{PRODUCER_TAG}"), encrypted ones (listed), names
matching NO_OCR_FILENAME_PATTERNS, folders, other file types.

The outcome of each document:
  OCRed     the searchable PDF is in its place
  no text   nothing to recognise (a photograph, a blank page): a PDF original
            is KEPT and marked as done, so no later run OCRs it again
  REFUSED   the result failed the check -- the original was NOT replaced
  FAILED    DEVONthink could not OCR it
REFUSED and FAILED documents move to MOVE_FAILED_TO / --move-failed-to when
one is set, and otherwise stay where they are. One OCR runs at a time: a
second run waits for its turn. DEVONthink gets OCR_TIMEOUT seconds per
document; then my-ocr quits it and tries once more, alerting (notification,
mail, a marker on the Desktop). A second time stops the run: that document
counts as FAILED, the documents after it are not touched, the exit code is 1.

COMMANDS
  status                  what my-ocr is doing and what it needs -- read-only;
                          -V adds the paths (--status / -S: the same)
  reset [go]              clear what a killed run left: a mutex whose owner
                          is gone, its work folder (copies only). Shows what
                          it would do; 'go' does it. A live run is left alone.

OPTIONS
  -f, --file <FILE>         read the names to OCR from FILE, one per line
  --move-failed-to <DIR>    where REFUSED and FAILED documents go (else MOVE_FAILED_TO)
  -F, --force               OCR documents that are already OCRed, too
  -Q, --quiet               nothing on the terminal but errors
  -V, --verbose             also each skipped file, and the paths
  -D, --debug               also the diagnostics (~~~); implies -V
  -l, --log / -L, --no-log  keep the LOGFILE record of this run / keep none
  --config <FILE>           read FILE instead of searching for one
  --create-config [<FILE>]  the default config, to stdout or into a new FILE
  --test-mail               send one alert the way a STUCK alert goes, and exit
  --run-tests [<FILTER>]    the built-in tests (TAP) -- all, or those whose
                            name holds FILTER
  --version                 the exact build
  -h, --help                this

CONFIG  KEY="value" lines, read and never run -- the first found of
  $MY_OCR_CONFIG, --config <FILE>,
  {', '.join(str(p).replace(str(Path.home()), "~", 1) for p in config_search_paths())}
  None found: the defaults that --create-config prints.

EXIT  0 the run finished (each document's outcome is in the report);
      1 an error -- one line on stderr; 130 Ctrl-C; 143 SIGTERM;
      70 a bug -- one line, the trace in a file

EXAMPLES
  {SCRIPT_NAME} scan.pdf
  {SCRIPT_NAME} --move-failed-to ~/failed *.pdf
  ls *.pdf | {SCRIPT_NAME}"""


@dataclass
class Cli:
    command: str = "ocr"                  # ocr, status, reset, help
    go: bool = False
    files: List[str] = field(default_factory=lambda: [])
    files_given: bool = False
    file_list: Optional[str] = None
    move_failed_to: Optional[str] = None
    force: bool = False
    quiet: bool = False
    vrb: bool = False
    dbg: bool = False
    log: Optional[bool] = None
    config: Optional[str] = None


# the verbs the FineReader version had, answered instead of being read as files
GONE_VERBS: Final = {
    "setup": "is not needed any more -- my-ocr asks DEVONthink directly, nothing is wired up. "
             f"'{SCRIPT_NAME} status' says what is missing",
    "install": "is not needed any more -- see 'setup'",
    "uninstall": "has nothing to remove any more -- the FineReader Folder Action of the old my-ocr is "
                 "removed with FineReader itself",
    "stamp-version": "is gone -- '--version' names the exact bytes by themselves",
}


def parse_cli(argv: Sequence[str]) -> Cli:
    """Options anywhere before '--'; a bare word is a FILE, except the verbs."""
    c = Cli()
    it = iter(argv)
    rest = False
    for a in it:
        if rest or not a.startswith("-") or a == "-":
            if not rest and not c.files and c.command == "ocr" and a in ("status", "reset"):
                c.command = a
            elif not rest and c.command == "reset" and a == "go":
                c.go = True
            elif not rest and not c.files and a in GONE_VERBS and not os.path.lexists(a):
                err("00026", f"'{a}' {GONE_VERBS[a]}")
            else:
                c.files.append(a)
                c.files_given = True
            continue
        key, eq, val = a.partition("=")
        if a == "--":
            rest = True
            c.files_given = True
        elif a in ("-S", "--status"):
            c.command = "status"
        elif a == "--test-mail":
            c.command = "test-mail"
        elif a in ("-Q", "--quiet"):
            c.quiet = True
        elif a in ("-V", "--verbose"):
            c.vrb = True
        elif a in ("-D", "--debug"):
            c.dbg = True
        elif a in ("-F", "--force"):
            c.force = True
        elif a in ("-l", "--log"):
            c.log = True
        elif a in ("-L", "--no-log"):
            c.log = False
        elif key in ("-f", "--file", "--move-failed-to", "--config"):
            v = val if eq else next(it, None)
            if not v:
                err("00027", f"{key} needs a value ({SCRIPT_NAME} --help)")
            if key == "--move-failed-to":
                if not Path(v).is_dir():
                    err("00028", f"--move-failed-to '{v}' is not a folder -- create it, or leave it out to keep "
                                 f"failed documents where they are")
                c.move_failed_to = v
            elif key == "--config":
                c.config = v
            else:
                c.file_list = v
        elif a in ("-h", "--help"):
            c.command = "help"
        else:
            verb = a.lstrip("-")
            hint = f" -- you mean `{SCRIPT_NAME} {verb}`" if verb in ("reset",) else f" ({SCRIPT_NAME} --help)"
            err("00029", f"no such option '{a}'{hint}")
    if c.command in ("status", "reset") and c.files:
        err("00030", f"'{c.command}' takes no files: {' '.join(c.files)[:60]}")
    return c


def setup_output(s: Settings, c: Cli, argv: Sequence[str]) -> None:
    OUT.dbg_on = c.dbg
    OUT.vrb = c.dbg or c.vrb
    OUT.quiet = c.quiet and not OUT.vrb      # -V beats -Q
    if c.log if c.log is not None else s.log:
        try:
            s.logfile.parent.mkdir(parents=True, exist_ok=True)
            OUT.log = s.logfile.open("a", encoding="utf-8")
        except OSError as e:
            err("00031", f"cannot write the LOGFILE {s.logfile}: {e.strerror} (-L runs without one)")
        OUT.record(f"==== {SCRIPT_NAME} {shlex.join(argv)}   [in {Path.cwd()}]")


def run(argv: Sequence[str]) -> int:
    first, _, value = (argv[0] if argv else "").partition("=")
    if first == "--run-tests":
        return run_tests(argv[1] if len(argv) > 1 else "")
    if first == "--version":
        OUT.result(version_line())
        return 0
    if first == "--create-config":
        return create_config(value or (argv[1] if len(argv) > 1 else None))
    c = parse_cli(argv)
    if c.command == "help" or (not argv and sys.stdin.isatty()):
        OUT.result(HELP)
        return 0
    s = load_settings(c.config)
    if c.move_failed_to is not None:
        s = dataclasses.replace(s, move_failed_to=c.move_failed_to)
    elif s.move_failed_to and not Path(s.move_failed_to).is_dir():
        err("00032", f"MOVE_FAILED_TO '{s.move_failed_to}' is not a folder ({s.files[0] if s.files else 'config'})")
    setup_output(s, c, argv)
    if c.command == "test-mail":
        return cmd_test_mail(s)
    if c.command == "status":
        return cmd_status(s)
    if c.command == "reset":
        return cmd_reset(s, c.go)
    return cmd_ocr(s, c)


def trace_file() -> Path:
    log = Path(OUT.log.name) if OUT.log is not None else None
    folder = log.parent if log is not None and os.access(log.parent, os.W_OK) else Path(tempfile.gettempdir())
    return folder / f"{SCRIPT_NAME}.{datetime.datetime.now():%Y-%m-%d_%H%M%S}.{os.getpid()}.trace"


def main(argv: Sequence[str]) -> int:
    """Every way a run ends: 0; 1 with ONE error line; 130 Ctrl-C; 143 SIGTERM;
    70 and a trace file on a bug -- never a traceback on the screen."""
    try:
        return run(argv)
    except OcrError as e:
        OUT.error(f"ERROR {e.code} [1]: {e.text}")
        return 1
    except KeyboardInterrupt:
        OUT.error(f"{SCRIPT_NAME}: interrupted (Ctrl-C) while {_phase} -- no original was touched by the "
                  f"document in hand; run it again to go on")
        return 130
    except _Terminated:
        OUT.error(f"{SCRIPT_NAME}: stopped (SIGTERM) while {_phase} -- no original was touched by the "
                  f"document in hand; run it again to go on")
        return 143
    except OSError as e:
        OUT.error(f"ERROR 00033 [1]: while {_phase}: {e.strerror or e}" + (f": {e.filename}" if e.filename else ""))
        return 1
    except Exception as e:   # a bug: one line here, the trace in a file  # noqa: BLE001
        where = trace_file()
        try:
            where.write_text(f"{version_line()}\nargv: {list(argv)}\n\n{traceback.format_exc()}", encoding="utf-8")
        except OSError:
            where = Path("(not written)")
        OUT.error(f"ERROR 00034 [70]: unexpected {type(e).__name__} while {_phase} -- the trace: {where}")
        return 70
    finally:
        if OUT.log is not None:
            OUT.log.close()
            OUT.log = None


def _on_term(signum: int, frame: object) -> None:
    raise _Terminated()


# --- 6. --run-tests ----------------------------------------------------------
# TAP: the plan first, then "ok K - name" / "not ok K - name" as each ends.
# DEVONthink is a stub osascript that follows a script of answers; my-notify a
# stub that records its arguments; the Trash a folder of the test's own.

def t_mkpdf(path: Path, pages: int, producer: str, text: str = "Hello OCR") -> Path:
    """A small, valid PDF: `pages` pages, a Producer, a text layer or none."""
    body = f"BT /F1 24 Tf 40 100 Td ({text}) Tj ET".encode()
    objs: Dict[int, bytes] = {}
    nxt = 3
    stream_id = font_id = 0
    if text:
        stream_id, font_id, nxt = nxt, nxt + 1, nxt + 2
        objs[stream_id] = b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream"
        objs[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    page_ids = list(range(nxt, nxt + pages))
    info_id = nxt + pages
    for pid in page_ids:
        extra = f" /Contents {stream_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >>" if text else " /Resources << >>"
        objs[pid] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200]{extra} >>".encode()
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {pages} >>".encode()
    objs[info_id] = f"<< /Producer ({producer}) >>".encode()
    out = b"%PDF-1.4\n"
    offs: Dict[int, int] = {}
    for num in sorted(objs):
        offs[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    x, top = len(out), max(offs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % top
    for n in range(1, top):
        out += b"%010d 00000 n \n" % offs.get(n, 0)
    out += f"trailer\n<< /Size {top} /Root 1 0 R /Info {info_id} 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    path.write_bytes(out)
    return path


# The stub osascript: reads the script on stdin, answers by its first line.
# For OCR it takes the next line of <dir>/engine.plan:
#   ok <file>       export <file> (as DEVONthink would: the record's name, "in.pdf")
#   err <n> <text>  answer ERR <n> <text>
#   hang            sleep long (my-ocr's timeout fires)
# and records every call in <dir>/engine.calls.
_T_ENGINE: Final = r'''#!/usr/bin/env python3
import os, shutil, sys, time
d = os.environ["MY_OCR_T_DIR"]
script = sys.stdin.read()
op = script.splitlines()[0].split(":")[-1]
with open(os.path.join(d, "engine.calls"), "a") as f:
    f.write(op + " " + " ".join(sys.argv[2:]) + "\n")
if op != "ocr":
    print("OK\t0"); sys.exit(0)
plan = os.path.join(d, "engine.plan")
lines = open(plan).read().splitlines()
step, rest = (lines[0], lines[1:]) if lines else ("ok", [])
open(plan, "w").write("".join(x + "\n" for x in rest))
word, _, arg = step.partition(" ")
if word == "hang":
    time.sleep(60); sys.exit(0)
if word == "err":
    n, _, text = arg.partition(" ")
    print("ERR\t" + n + "\t" + text); sys.exit(0)
src = arg or sys.argv[2]
dst = os.path.join(sys.argv[3], "in.pdf")
shutil.copyfile(src, dst)
print("OK\t" + dst + "\tUUID-1\tdeleted")
'''

_T_NOTIFY: Final = r'''#!/bin/sh
{ printf '%s|' "$@" | tr '\n' ' '; echo; } >> "$MY_OCR_T_DIR/notify.calls"
exit "$(cat "$MY_OCR_T_DIR/notify.rc" 2>/dev/null || echo 0)"
'''


class T:
    """One test: its folder, its stubs, what it found wrong."""

    def __init__(self, folder: Path) -> None:
        self.dir = folder
        self.bad: List[str] = []
        (folder / "Trash").mkdir()
        (folder / "failed").mkdir()
        (folder / "app").mkdir()
        (folder / "helper").mkdir()
        for name, text in (("osascript", _T_ENGINE), ("my-notify", _T_NOTIFY)):
            (folder / name).write_text(text, encoding="utf-8")
            (folder / name).chmod(0o755)

    def check(self, ok: bool, what: str) -> None:
        if not ok:
            self.bad.append(what)

    def config(self, extra: str = "") -> Path:
        p = self.dir / "conf"
        p.write_text(f'STATE_DIR="{self.dir}/state"\nREVEAL_IN_FINDER="never"\nOCR_TIMEOUT="2"\n'
                     f'MUTEX_TIMEOUT="3"\nSTUCK_MARKER_DIR="{self.dir}"\nLOGFILE="{self.dir}/log"\n' + extra,
                     encoding="utf-8")
        return p

    def plan(self, *steps: str) -> None:
        (self.dir / "engine.plan").write_text("".join(x + "\n" for x in steps), encoding="utf-8")

    def calls(self, name: str) -> List[str]:
        p = self.dir / f"{name}.calls"
        return p.read_text(encoding="utf-8").splitlines() if p.is_file() else []

    def run(self, argv: Sequence[str], stdin: str = "") -> Tuple[int, str, str]:
        """The tool in-process, as from a shell: rc, stdout, stderr."""
        out, errs = io.StringIO(), io.StringIO()
        old_in = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
                rc = main(list(argv))
        finally:
            sys.stdin = old_in
            OUT.__init__()
        return rc, out.getvalue(), errs.getvalue()

    def ocr(self, *args: str, stdin: str = "") -> Tuple[int, str, str]:
        return self.run(["--config", str(self.config()), *args], stdin=stdin)

    def producer(self, p: Path) -> str:
        return pdf_facts(required_tools(), p).producer

    def pages(self, p: Path) -> Optional[int]:
        return pdf_facts(required_tools(), p).pages

    def trashed(self) -> List[str]:
        return sorted(x.name for x in (self.dir / "Trash").iterdir())


def t_verify_result(t: T) -> None:
    """The check between a mangled OCR and a lost document."""
    tools = required_tools()
    one = t_mkpdf(t.dir / "one.pdf", 1, "")
    three = t_mkpdf(t.dir / "three.pdf", 3, "")
    five = t_mkpdf(t.dir / "five.pdf", 5, "")
    v = verify_result(tools, one, 3)
    t.check(not v.usable and "LOST pages" in v.why, f"3 in, 1 out must be refused for LOST pages: {v}")
    t.check(verify_result(tools, three, 3).usable, "equal pages are accepted")
    v = verify_result(tools, five, 3)
    t.check(v.usable and any("split" in n for n in v.notes), f"MORE pages is accepted and noted: {v}")
    (t.dir / "empty.pdf").write_bytes(b"")
    t.check(not verify_result(tools, t.dir / "empty.pdf", 3).usable, "an empty file is refused")
    (t.dir / "cut.pdf").write_bytes(three.read_bytes()[:400])
    v = verify_result(tools, t.dir / "cut.pdf", 3)
    t.check(not v.usable, f"a truncated PDF is refused: {v}")
    notext = t_mkpdf(t.dir / "notext.pdf", 3, "", text="")
    v = verify_result(tools, notext, 3)
    t.check(v.usable and v.no_text, f"NO text is accepted AND flagged, not refused: {v}")
    notext1 = t_mkpdf(t.dir / "notext1.pdf", 1, "", text="")
    v = verify_result(tools, notext1, 3)
    t.check(not v.usable and "LOST pages" in v.why, f"no text does not wave lost pages through: {v}")


def t_tag_in_place(t: T) -> None:
    tools = required_tools()
    doc = t_mkpdf(t.dir / "doc.pdf", 3, "SomeScanner", text="")
    doc.chmod(0o640)
    why = tag_producer(tools, doc, PRODUCER_NOTEXT)
    t.check(why == "", f"tagged: {why}")
    f = pdf_facts(tools, doc)
    t.check(PRODUCER_TAG in f.producer and f.pages == 3, f"Producer and pages after the tag: {f}")
    t.check(qpdf_check(tools, doc) in (0, 3), "still passes qpdf --check")
    t.check(doc.stat().st_mode & 0o777 == 0o640, f"its mode is kept: {oct(doc.stat().st_mode & 0o777)}")
    t.check(sorted(x.name for x in t.dir.iterdir() if x.name.startswith(".")) == [], "no temp file left beside it")
    noinfo = t.dir / "noinfo.pdf"
    noinfo.write_bytes(t_mkpdf(t.dir / "x.pdf", 2, "").read_bytes().replace(b" /Info ", b" /NoInfo "))
    t.check(tag_producer(tools, noinfo, PRODUCER_OCRED) == "" and PRODUCER_OCRED in t.producer(noinfo),
            "a PDF with no /Info gets one")


def t_ocr_replaces_in_place(t: T) -> None:
    """The everyday path, as my-scan calls it."""
    doc = t_mkpdf(t.dir / "scan.pdf", 2, "SomeScanner", text="")
    before = doc.read_bytes()
    t.plan("ok " + str(t_mkpdf(t.dir / "res.pdf", 2, "")))
    rc, out, errs = t.ocr("--move-failed-to", str(t.dir / "failed"), str(doc))
    t.check(rc == 0, f"rc {rc}: {errs}")
    t.check(PRODUCER_OCRED == t.producer(doc), f"the original's place holds the tagged result: {t.producer(doc)!r}")
    t.check(t.trashed() == ["scan.pdf"] and (t.dir / "Trash" / "scan.pdf").read_bytes() == before,
            f"the ORIGINAL bytes are in the Trash: {t.trashed()}")
    t.check(">RESULT< 1 file(s): 1 OCRed" in out, f"the summary: {out}")
    t.check(not any((t.dir / "state" / "work").iterdir()), "the work folder is gone")
    t.check(not (t.dir / "state" / "mutex").exists(), "the mutex is released")
    calls = t.calls("engine")
    t.check(calls[:1] == ["sweep "] and calls[1].startswith("ocr ") and "/state/work/" in calls[1],
            f"DEVONthink saw a COPY in the work folder, after the sweep: {calls}")


def t_mkpng(path: Path) -> Path:
    """A real 1x1 PNG -- `file` must recognise it."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"\0\0")) + chunk(b"IEND", b""))
    return path


def t_image_becomes_pdf(t: T) -> None:
    img = t_mkpng(t.dir / "photo.png")
    t.plan("ok " + str(t_mkpdf(t.dir / "res.pdf", 1, "")))
    rc, out, errs = t.ocr(str(img))
    t.check(rc == 0 and (t.dir / "photo.pdf").is_file() and not img.exists(), f"photo.png -> photo.pdf: {out}{errs}")
    t.check(t.trashed() == ["photo.png"], f"the picture is in the Trash: {t.trashed()}")
    t.check(t.calls("engine")[1].split()[1].endswith("/in.png"), f"DEVONthink got it as a picture: {t.calls('engine')}")
    t_mkpng(img)
    t.plan("ok " + str(t_mkpdf(t.dir / "res.pdf", 1, "")))
    rc, out, errs = t.ocr(str(img))
    t.check(rc == 0 and img.exists() and "FAILED" in out and "already exists" in out,
            f"a PDF of that name is never overwritten: {out}{errs}")


def t_refused_rolls_back(t: T) -> None:
    """A result that LOST a page: the original stays the document, untouched."""
    doc = t_mkpdf(t.dir / "scan.pdf", 3, "SomeScanner")
    before = doc.read_bytes()
    t.plan("ok " + str(t_mkpdf(t.dir / "res.pdf", 2, "")))
    rc, out, errs = t.ocr("--move-failed-to", str(t.dir / "failed"), str(doc))
    q = t.dir / "failed" / "scan.pdf"
    t.check(rc == 0, f"a refusal is an outcome, not an error: rc {rc} {errs}")
    t.check(not doc.exists() and q.is_file() and q.read_bytes() == before, "the original is quarantined, unchanged")
    t.check("REFUSED" in out and "LOST pages" in out, f"said, with the reason: {out}")
    t.check(any("OCR REFUSED" in c for c in t.calls("notify")), f"the user is told: {t.calls('notify')}")
    t.check(t.trashed() == ["scan.my-ocr-result.pdf"], f"the bad result went to the Trash, named: {t.trashed()}")


def t_no_text_keeps_original(t: T) -> None:
    doc = t_mkpdf(t.dir / "photo.pdf", 1, "SomeScanner", text="")
    t.plan("ok " + str(t_mkpdf(t.dir / "res.pdf", 1, "", text="")))
    rc, out, errs = t.ocr(str(doc))
    t.check(rc == 0 and t.producer(doc) == PRODUCER_NOTEXT, f"the original is kept and marked: {t.producer(doc)!r} {errs}")
    t.check("1 with no text" in out, f"reported on its own: {out}")
    rc, out, _ = t.ocr(str(doc))
    t.check(rc == 0 and "1 already OCRed" in out and len(t.calls("engine")) == 2,
            f"a marked original is never OCRed again: {out} {t.calls('engine')}")


def t_engine_error_is_one_document(t: T) -> None:
    a = t_mkpdf(t.dir / "a.pdf", 1, "S")
    b = t_mkpdf(t.dir / "b.pdf", 1, "S")
    t.plan("err 0 DEVONthink's OCR returned no document", "ok " + str(t_mkpdf(t.dir / "res.pdf", 1, "")))
    rc, out, errs = t.ocr("--move-failed-to", str(t.dir / "failed"), str(a), str(b))
    t.check(rc == 0 and (t.dir / "failed" / "a.pdf").is_file() and t.producer(b) == PRODUCER_OCRED,
            f"a fails alone, b is OCRed: {out}{errs}")


def t_engine_unreachable_stops_untouched(t: T) -> None:
    """No consent, no DEVONthink: the run stops and NOTHING is quarantined --
    every document is still where it was, to be retried."""
    a = t_mkpdf(t.dir / "a.pdf", 1, "S")
    b = t_mkpdf(t.dir / "b.pdf", 1, "S")
    t.plan("err -1743 Not authorized to send Apple events to DEVONthink.")
    rc, _, errs = t.ocr("--move-failed-to", str(t.dir / "failed"), str(a), str(b))
    t.check(rc == 1 and "Automation" in errs and a.is_file() and b.is_file()
            and not any((t.dir / "failed").iterdir()), f"stop, untouched: rc {rc} {errs}")
    t.check("ERROR" in errs and errs.count("\n") == 1, f"ONE error line: {errs!r}")


def t_no_addon_refuses_untouched(t: T) -> None:
    """Without the OCR add-on DEVONthink never answers: the run must refuse
    before asking it, and leave every document where it was -- not wait
    twice and file the document as FAILED."""
    a = t_mkpdf(t.dir / "a.pdf", 1, "S")
    (t.dir / "helper").rmdir()
    rc, _, errs = t.ocr("--move-failed-to", str(t.dir / "failed"), str(a))
    t.check(rc == 1 and "Install Add-ons" in errs and errs.count("\n") == 1, f"ONE error naming the fix: rc {rc} {errs!r}")
    t.check(a.is_file() and not any((t.dir / "failed").iterdir()), "the document is untouched")
    t.check(t.calls("engine") == [], "DEVONthink was not asked")
    _, out, _ = t.ocr("status")
    t.check(any(x.startswith("ADD-ON    ABBYY OCR MISSING") for x in out.splitlines()), f"status says so: {out}")


def t_timeout_retry_resolves(t: T) -> None:
    a = t_mkpdf(t.dir / "a.pdf", 1, "S")
    t.plan("hang", "ok " + str(t_mkpdf(t.dir / "res.pdf", 1, "")))
    rc, out, errs = t.ocr(str(a))
    t.check(rc == 0 and t.producer(a) == PRODUCER_OCRED, f"OCRed on the retry: {out}{errs}")
    ops = [c.split()[0] for c in t.calls("engine")]
    t.check(ops == ["sweep", "ocr", "quit", "ocr"], f"quit between the two tries: {ops}")
    subjects = [c.split("|")[1] for c in t.calls("notify")]
    t.check(subjects == [f"{SCRIPT_NAME} STUCK -- retrying", f"{SCRIPT_NAME} RESOLVED"], f"one incident: {subjects}")
    t.check(not list(t.dir.glob(",,my-ocr.STUCK.*")) and any(x.startswith(",,my-ocr.STUCK.") for x in t.trashed()),
            "the marker went to the Trash when it resolved")


def t_timeout_twice_stops(t: T) -> None:
    a = t_mkpdf(t.dir / "a.pdf", 1, "S")
    b = t_mkpdf(t.dir / "b.pdf", 1, "S")
    t.plan("hang", "hang")
    rc, _, errs = t.ocr("--move-failed-to", str(t.dir / "failed"), str(a), str(b))
    t.check(rc == 1 and "twice" in errs, f"rc 1, said: {rc} {errs}")
    t.check((t.dir / "failed" / "a.pdf").is_file() and b.is_file(), "a is FAILED, b is untouched")
    subjects = [c.split("|")[1] for c in t.calls("notify")]
    t.check(subjects == [f"{SCRIPT_NAME} STUCK -- retrying", f"{SCRIPT_NAME} STUCK -- gave up"], f"alerts: {subjects}")
    t.check(list(t.dir.glob(",,my-ocr.STUCK.*")) != [], "the marker stays: the incident is not over")
    t.check(not (t.dir / "state" / "mutex").exists(), "the mutex is released on the way out")


def t_original_never_absent(t: T) -> None:
    """ADVERSARIAL: the Trash refuses the copy. The original must still be
    there, unchanged -- a replace that trashed first and failed later lost it."""
    doc = t_mkpdf(t.dir / "scan.pdf", 1, "S")
    before = doc.read_bytes()
    (t.dir / "Trash").chmod(0o500)
    try:
        t.plan("ok " + str(t_mkpdf(t.dir / "res.pdf", 1, "")))
        rc, out, _ = t.ocr(str(doc))
    finally:
        (t.dir / "Trash").chmod(0o700)
    t.check(rc == 0 and doc.read_bytes() == before and "FAILED" in out and "nothing replaced" in out,
            f"untouched, and said: {out}")
    t.check(sorted(x.name for x in t.dir.iterdir() if x.name.startswith(".")) == [], "no temp file left beside it")


def t_skips(t: T) -> None:
    done = t_mkpdf(t.dir / "done.pdf", 1, PRODUCER_OCRED)
    old = t_mkpdf(t.dir / "old.pdf", 1, "ABBYY FineReader 12")
    marked = t_mkpdf(t.dir / "no-ocr-scan1.pdf", 1, "S")
    txt = t.dir / "note.txt"
    txt.write_text("hello", encoding="utf-8")
    rc, out, errs = t.ocr("-V", str(done), str(old), str(marked), str(txt), str(t.dir / "Trash"), str(done))
    t.check(rc == 0 and "6 file(s): 0 OCRed" in out and "2 already OCRed" in out and t.calls("engine") == [],
            f"nothing for DEVONthink: {out}{errs}")
    t.check(marked.is_file() and txt.is_file(), "skipped files stay where they are")
    t.plan("ok " + str(t_mkpdf(t.dir / "res.pdf", 1, "")))
    rc, out, _ = t.ocr("-F", str(old))
    t.check(rc == 0 and t.producer(old) == PRODUCER_OCRED, f"-F OCRs it again: {out}")


def t_stdin_only_when_no_names(t: T) -> None:
    """Names given: stdin is NOT read -- an open pipe nobody closes must not
    hold the mutex for ever."""
    doc = t_mkpdf(t.dir / "scan.pdf", 1, "S")
    t.plan("ok " + str(t_mkpdf(t.dir / "res.pdf", 1, "")))
    p = subprocess.Popen([sys.executable, os.path.realpath(__file__), "--config", str(t.config()), str(doc)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env={**os.environ, "MY_OCR_T_DIR": str(t.dir), "MY_OCR_T_SEAMS": str(t.dir)})
    try:
        p.wait(timeout=60)          # stdin stays OPEN: nobody writes to it, nobody closes it
    except subprocess.TimeoutExpired:
        p.kill()
        t.check(False, "it blocked on the open stdin")
    out, errs = p.communicate()
    t.check(p.returncode == 0 and "1 OCRed" in out, f"finished without stdin: {p.returncode} {out}{errs}")
    doc2 = t_mkpdf(t.dir / "two.pdf", 1, "S")
    t.plan("ok " + str(t_mkpdf(t.dir / "res.pdf", 1, "")))
    rc, out, _ = t.ocr(stdin=f"{doc2}\n")
    t.check(rc == 0 and t.producer(doc2) == PRODUCER_OCRED, f"names from stdin when none were given: {out}")


def t_mutex(t: T) -> None:
    s_dir = t.dir / "state"
    mutex = s_dir / "mutex"
    mutex.mkdir(parents=True)
    (mutex / "owner").write_text("pid=999999 at=x doing=old\n", encoding="utf-8")
    doc = t_mkpdf(t.dir / "a.pdf", 1, "S")
    t.plan("ok " + str(t_mkpdf(t.dir / "res.pdf", 1, "")))
    rc, out, _ = t.ocr(str(doc))
    t.check(rc == 0 and "breaking a stale OCR mutex: its owner 999999 is gone" in out, f"a dead owner is broken, said: {out}")
    mutex.mkdir()
    (mutex / "owner").write_text(f"pid={os.getppid()} at=x doing=b.pdf\n", encoding="utf-8")
    rc, out, errs = t.ocr(str(doc))
    t.check(rc == 1 and "held by pid=" in errs and mutex.is_dir(), f"a LIVE owner is waited for, never broken: {errs}")
    (mutex / "owner").unlink()
    rc, out, errs = t.ocr(str(doc))
    t.check(rc == 1 and mutex.is_dir(), "an owner file not written yet is not a dead owner")
    mutex.rmdir()


def t_status(t: T) -> None:
    """my-scan reads the first line: '^OCR       BUSY' or idle."""
    rc, out, _ = t.ocr("status")
    t.check(rc == 0 and out.splitlines()[0] == "OCR       idle", f"idle: {out}")
    t.check("TYPES     PDF, JPEG, PNG, TIFF" in out.splitlines(), f"the types my-scan takes from it: {out}")
    t.check(any(x.startswith("ADD-ON    ABBYY OCR installed") for x in out.splitlines()), f"the add-on: {out}")
    t.check(not (t.dir / "state").exists(), "status creates nothing")
    m = t.dir / "state" / "mutex"
    m.mkdir(parents=True)
    (m / "owner").write_text(f"pid={os.getpid()} at=x doing=scan.pdf (1/2)\n", encoding="utf-8")
    rc, out, _ = t.ocr("--status")
    t.check(re.match(r"^OCR       BUSY -- scan\.pdf \(1/2\)", out) is not None, f"busy, with the document: {out}")
    (m / "owner").write_text("pid=999999 at=x doing=x\n", encoding="utf-8")
    rc, out, _ = t.ocr("-Q", "status")
    t.check("STALE LOCK" in out and "reset go" in out, f"a stale lock says what to do, also under -Q: {out}")
    t.check(t.calls("engine") == [], "status never asks DEVONthink")


def t_reset_two_phase(t: T) -> None:
    m = t.dir / "state" / "mutex"
    m.mkdir(parents=True)
    (m / "owner").write_text("pid=999999 at=x doing=x\n", encoding="utf-8")
    w = t.dir / "state" / "work" / "999998"
    w.mkdir(parents=True)
    live = t.dir / "state" / "work" / str(os.getpid())
    live.mkdir()
    rc, out, _ = t.ocr("reset")
    t.check(rc == 0 and m.is_dir() and w.is_dir() and "would remove" in out, f"the plain run changes nothing: {out}")
    rc, out, _ = t.ocr("reset", "go")
    t.check(rc == 0 and not m.exists() and not w.exists() and live.is_dir(), f"go: the dead run's only: {out}")
    rc, out, _ = t.ocr("reset", "go")
    t.check(rc == 0 and "nothing to reset" in out, f"a second run finds nothing: {out}")


def t_config_read_never_run(t: T) -> None:
    p = t.dir / "conf2"
    p.write_text('OCR_TIMEOUT="$(touch pwned)"\n', encoding="utf-8")
    rc, _, errs = t.run(["--config", str(p), "status"])
    t.check(rc == 1 and "never runs it" in errs and not (t.dir / "pwned").exists(), errs)
    p.write_text('OCR_TIMEOUT="soon"\n', encoding="utf-8")
    rc, _, errs = t.run(["--config", str(p), "status"])
    t.check(rc == 1 and "OCR_TIMEOUT" in errs, errs)
    p.write_text('NOPE="1"\n', encoding="utf-8")
    rc, _, errs = t.run(["--config", str(p), "status"])
    t.check(rc == 0 and "NOPE is not a my-ocr setting" in errs, f"an unknown key is warned about: {errs}")


def t_create_config(t: T) -> None:
    rc, out, _ = t.run(["--create-config"])
    names: Dict[str, str] = {}
    t.check(rc == 0 and read_config_text(out, "x", names) != [], "the default config parses")
    p = t.dir / "new.conf"
    rc, _, _ = t.run(["--create-config", str(p)])
    rc2, _, errs = t.run([f"--create-config={p}"])
    t.check(rc == 0 and p.is_file() and rc2 == 1 and "never written over" in errs, errs)


def t_output_layers(t: T) -> None:
    def one() -> Tuple[str, str]:
        doc = t_mkpdf(t.dir / "a.pdf", 1, "S")
        skip = t_mkpdf(t.dir / "b.pdf", 1, PRODUCER_OCRED)
        t.plan("ok " + str(t_mkpdf(t.dir / "res.pdf", 1, "")))
        return str(doc), str(skip)

    plain = t.ocr(*one())
    quiet = t.ocr("-Q", "--log", *one())
    verbose = t.ocr("-V", *one())
    t.check(plain[1] != "" and quiet[1] == "", f"-Q: nothing on the terminal: {quiet[1]!r}")
    t.check(all(x in verbose[1] for x in plain[1].splitlines() if "[" not in x) and len(verbose[1]) > len(plain[1]),
            "-V adds to the default, never replaces it")
    t.check("1 OCRed" in (t.dir / "log").read_text(encoding="utf-8"), "-Q spares the terminal, not the log")
    rc, _, errs = t.run(["--nope"])
    t.check(rc == 1 and "no such option '--nope'" in errs, errs)
    rc, _, errs = t.run(["setup"])
    t.check(rc == 1 and "not needed any more" in errs, f"an old verb is answered: {errs}")
    rc, _, errs = t.run(["--move-failed-to", str(t.dir / "nowhere"), "x.pdf"])
    t.check(rc == 1 and "not a folder" in errs, errs)


def t_ctrl_c(t: T) -> None:
    """A REAL SIGINT while waiting for the mutex: rc 130, one line, the other
    run's mutex untouched."""
    m = t.dir / "state" / "mutex"
    m.mkdir(parents=True)
    (m / "owner").write_text(f"pid={os.getpid()} at=x doing=x\n", encoding="utf-8")
    doc = t_mkpdf(t.dir / "a.pdf", 1, "S")
    t.config('MUTEX_TIMEOUT="60"\n')
    p = subprocess.Popen([sys.executable, os.path.realpath(__file__), "--config", str(t.dir / "conf"), str(doc)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env={**os.environ, "MY_OCR_T_DIR": str(t.dir), "MY_OCR_T_SEAMS": str(t.dir)})
    time.sleep(2)
    p.send_signal(signal.SIGINT)
    _, errs = p.communicate(timeout=30)
    t.check(p.returncode == 130 and "interrupted (Ctrl-C) while waiting for the OCR mutex" in errs
            and "Traceback" not in errs and len(errs.strip().splitlines()) == 1, f"rc {p.returncode}: {errs!r}")
    t.check((m / "owner").is_file(), "another run's mutex is left alone")
    p = subprocess.Popen([sys.executable, os.path.realpath(__file__), "--config", str(t.dir / "conf"), str(doc)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env={**os.environ, "MY_OCR_T_DIR": str(t.dir), "MY_OCR_T_SEAMS": str(t.dir)})
    time.sleep(2)
    p.send_signal(signal.SIGTERM)
    _, errs = p.communicate(timeout=30)
    t.check(p.returncode == 143 and "stopped (SIGTERM)" in errs and "Traceback" not in errs, f"SIGTERM: {errs!r}")


def t_bug_trace(t: T) -> None:
    def boom(*a: object, **k: object) -> Answer:
        raise ZeroDivisionError("a bug")

    g = globals()
    real = g["osa"]
    g["osa"] = boom
    try:
        rc, _, errs = t.ocr(str(t_mkpdf(t.dir / "a.pdf", 1, "S")))
    finally:
        g["osa"] = real
    m = re.search(r"the trace: (\S+)", errs)
    t.check(rc == 70 and "unexpected ZeroDivisionError" in errs and "Traceback" not in errs, errs)
    t.check(m is not None and "Traceback" in Path(m.group(1)).read_text(encoding="utf-8"), "the trace is in the file")
    t.check(not (t.dir / "state" / "mutex").exists(), "the mutex is released after a bug too")
    if m:
        Path(m.group(1)).unlink()


def t_test_mail(t: T) -> None:
    rc, out, _ = t.ocr("--test-mail")
    c = t.calls("notify")
    t.check(rc == 0 and "test mail sent" in out and len(c) == 1 and "--mail" in c[0], f"{out} {c}")
    (t.dir / "notify.rc").write_text("3", encoding="utf-8")
    rc, _, errs = t.ocr("--test-mail")
    t.check(rc == 1 and "NOT sent" in errs and "rc 3" in errs, f"a failed send is an error: {errs}")


def t_version(t: T) -> None:
    rc, out, _ = t.run(["--version"])
    t.check(rc == 0 and out.startswith(f"{SCRIPT_NAME} {SCRIPT_VERSION} (") and f"build {build_id()})" in out, out)


def t_help_claims(t: T) -> None:
    """Every option the help names is one the parser takes."""
    for opt in re.findall(r"^\s+(-\w), (--[\w-]+)|^\s+(--[\w-]+)", HELP, re.MULTILINE):
        for o in opt:
            if o and o not in ("--run-tests", "--version", "--create-config", "--test-mail", "--help", "-h"):
                try:
                    parse_cli([o, "x"] if o not in ("-f", "--file", "--move-failed-to", "--config") else [o, str(t.dir)])
                except OcrError as e:
                    t.check("needs a value" not in e.text and "no such option" not in e.text, f"{o}: {e.text}")


def t_python39_grammar(t: T) -> None:
    """my-scan may start it with Apple's python3 (3.9): the source must parse there."""
    import ast
    src = Path(os.path.realpath(__file__)).read_text(encoding="utf-8")
    try:
        ast.parse(src, feature_version=(3, 9))
    except SyntaxError as e:
        t.check(False, f"not 3.9 grammar: line {e.lineno}: {e.msg}")
    old = "/usr/bin/python3"
    if os.access(old, os.X_OK):
        r = subprocess.run([old, "-c", f"import ast,sys; ast.parse(open({os.path.realpath(__file__)!r}).read())"],
                           capture_output=True, text=True, timeout=60)
        t.check(r.returncode == 0, f"{old} parses it: {r.stderr}")


def t_applescript_words(t: T) -> None:
    """Inside `tell application id "DNtp"`, AppleScript reads words through
    DEVONthink's dictionary: `tab` is one of its terms, and the answers came
    back as "OKtab0". The stub engine cannot see that -- this test can."""
    for name in ("_AS_OCR", "_AS_SWEEP", "_AS_QUIT"):
        script: str = globals()[name]
        t.check(re.search(r"\btab\b", script) is None, f"{name} uses the bare word tab")
        t.check(name == "_AS_QUIT" or "set sep to character id 9" in script.split("tell application")[0],
                f"{name}: the separator is made before the tell block")


def t_waits_for_inbox(t: T) -> None:
    """Started by the call itself, DEVONthink answers before its Inbox is open
    and the run stopped on "Can't get inbox." (horse, 2026-10-25). The stub
    engine cannot see that -- this test can: each script waits before its
    first word about the Inbox."""
    for name in ("_AS_OCR", "_AS_SWEEP"):
        script: str = globals()[name]
        body = script.split('tell application id "DNtp"', 1)[1]
        t.check(body.lstrip("\n").startswith(_AS_WAIT_INBOX), f"{name} waits for the Inbox first")


def t_sweep_reads_children_once(t: T) -> None:
    """Deleting while looping over the live `children of g` fails at the second
    leftover. The stub engine cannot see that -- this test can."""
    t.check("repeat with c in (children of g)" not in _AS_SWEEP and "set kids to get children of g" in _AS_SWEEP,
            "the sweep deletes from a list read once")


def t_err_codes_unique(t: T) -> None:
    src = Path(os.path.realpath(__file__)).read_text(encoding="utf-8")
    codes = re.findall(r'(?:err|StopRun)\("(\d{5})"', src) + re.findall(r'ERROR (\d{5}) \[', src)
    dup = sorted({c for c in codes if codes.count(c) > 1})
    t.check(dup == [], f"each code names ONE line: {dup}")
    t.check(codes == sorted(codes), "numbered in file order")


TESTS: Final[List[Callable[[T], None]]] = [
    t_verify_result, t_tag_in_place, t_ocr_replaces_in_place, t_image_becomes_pdf, t_refused_rolls_back,
    t_no_text_keeps_original, t_engine_error_is_one_document, t_engine_unreachable_stops_untouched,
    t_no_addon_refuses_untouched,
    t_timeout_retry_resolves, t_timeout_twice_stops, t_original_never_absent, t_skips, t_stdin_only_when_no_names,
    t_mutex, t_status, t_reset_two_phase, t_config_read_never_run, t_create_config, t_output_layers, t_ctrl_c,
    t_bug_trace, t_test_mail, t_version, t_help_claims, t_python39_grammar, t_applescript_words, t_waits_for_inbox, t_sweep_reads_children_once,
    t_err_codes_unique,
]


def use_test_seams(folder: str) -> None:
    """Point every outside thing at the test's own stand-ins."""
    d = Path(folder)
    EXT.dt_app, EXT.osascript, EXT.my_notify, EXT.trash_dir, EXT.osa_grace = \
        d / "app", str(d / "osascript"), str(d / "my-notify"), d / "Trash", 1
    EXT.dt_ocr_helper = d / "helper"


def run_tests(name_filter: str) -> int:
    defined = {n for n in globals() if re.fullmatch(r"t_[a-z0-9_]+", n) and n not in ("t_mkpdf", "t_mkpng")}
    listed = {f.__name__ for f in TESTS}
    if defined != listed:
        print(f"Bail out! test functions and TESTS differ: {sorted(defined ^ listed)}")
        return 1
    chosen = [f for f in TESTS if name_filter in f.__name__]
    print(f"1..{len(chosen)}", flush=True)
    saved = dataclasses.replace(EXT)
    bad, home = 0, Path.cwd()
    for k, test in enumerate(chosen, 1):
        with tempfile.TemporaryDirectory(prefix="my-ocr-tests.") as d:
            t = T(Path(d).resolve())
            os.chdir(t.dir)
            os.environ["MY_OCR_T_DIR"] = str(t.dir)
            use_test_seams(str(t.dir))
            try:
                test(t)
            except Exception as e:   # a test that crashes fails, and the next one runs  # noqa: BLE001
                t.bad.append(f"crashed: {type(e).__name__}: {e}")
                t.bad.extend(traceback.format_exc().splitlines())
            finally:
                os.chdir(home)
                OUT.__init__()
                for f in dataclasses.fields(EXT):
                    setattr(EXT, f.name, getattr(saved, f.name))
        print(f"{'not ok' if t.bad else 'ok'} {k} - {test.__name__}", flush=True)
        for b in t.bad:
            print(f"#   {b}")
        bad += bool(t.bad)
    return 1 if bad else 0


if __name__ == "__main__":
    if os.environ.get("MY_OCR_T_SEAMS"):          # a test runs this file as a subprocess
        use_test_seams(os.environ["MY_OCR_T_SEAMS"])
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _on_term)
    sys.exit(main(sys.argv[1:]))

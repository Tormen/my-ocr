# my-ocr

Makes scanned PDFs and pictures searchable from the shell, with **DEVONthink**'s
OCR (DEVONthink Pro 4 and its ABBYY FineReader add-on). Each document is
replaced in place by a searchable PDF; the original goes to the Trash.
**Together with [my-scan](../../sh/my-scan/) it is especially useful**: my-scan
receives the scanner's delivery and files the result, my-ocr is its unattended
OCR step. It works on its own just as well.

```sh
my-ocr scan.pdf                  # OCR in place; waits when another OCR is running
printf '%s\n' a.pdf b.png | my-ocr
my-ocr --file list.txt --move-failed-to ~/failed
```

Input may be **PDF, JPEG, PNG or TIFF**. A picture becomes a PDF of the same
name beside it (never over an existing one).

## What happens to one document

1. **Checks.** Skipped and left where they are: documents already OCRed (their
   PDF Producer contains `FineReader`), encrypted PDFs (listed at the end),
   names matching `NO_OCR_FILENAME_PATTERNS`, folders, other file types, a file
   named twice. `--force` OCRs already-OCRed documents again.
2. **A copy goes to DEVONthink** -- never the original -- into a group of its
   own, `/my-ocr` in the global Inbox. DEVONthink OCRs it, my-ocr exports the
   result and deletes DEVONthink's record again.
3. **The result is checked** while the original is still untouched:
   - it parses as a PDF;
   - its page count is **not lower** than the input's (more is fine: the OCR
     may split a sheet it takes for two pages);
   - `qpdf --check` passes -- qpdf walks every object and stream, where
     `pdfinfo` reads only the page tree. Its exit is graded: 0 clean, 3
     warnings only, anything else damage.
4. **It is marked**: my-ocr writes `FineReader (my-ocr: OCR by DEVONthink/ABBYY)`
   into the PDF Producer (DEVONthink leaves it empty). That mark is how
   my-scan, and every later run, knows the document is done. The tag is a qpdf
   JSON round trip -- the page content is copied through untouched -- and is
   checked again before it is installed.
5. **It takes the original's place.** The original is *copied* to the Trash
   first, then the result is renamed over it in one step. The document is
   never absent: a run killed in between leaves the original in place and a
   spare copy in the Trash.

Every document ends in exactly one of four outcomes, each reported:

| outcome | what it means | where the document is |
|---|---|---|
| OCRed | the searchable PDF is in place | where it was |
| no text | nothing to recognise -- a photograph, a blank page | a PDF original is **kept** and marked `FineReader (my-ocr: no text found -- original kept)`, so no later run OCRs it again; for a picture, the OCR's PDF |
| REFUSED | the result failed the check -- the original was NOT replaced; you get a notification | `--move-failed-to` / `MOVE_FAILED_TO`, else where it was; the result is in the Trash, named `<name>.my-ocr-result.pdf` |
| FAILED | DEVONthink could not OCR it | the same |

No text is not a failure: the checks are the evidence that the OCR ran
correctly, and a page with nothing on it is a finding about the document. It is
reported on a line of its own, so that "forty of forty found nothing" -- a
misconfigured OCR -- cannot hide in the success total.

## When DEVONthink gets stuck

- **One OCR at a time.** A `mkdir` mutex; a second run waits for its turn
  (`MUTEX_TIMEOUT`). Only an owner that is provably gone (`kill -0`) is broken,
  and never silently -- a long OCR is not a crashed one.
- **One retry.** DEVONthink gets `OCR_TIMEOUT` seconds per document. Then
  my-ocr quits it and hands it the same copy once more. A second time stops
  the run: that document counts as FAILED, the ones after it are not touched,
  and the exit code is 1.
- **When DEVONthink cannot be reached at all** -- the macOS Automation consent
  is missing, the app is gone -- the run stops at once and *nothing* is moved:
  every document is still where it was, to be retried.
- **One incident, one marker, a few mails.** `STUCK -- retrying`, then
  `STUCK -- gave up` or `RESOLVED`: a notification and a mail each (through
  `my-notify`), and one marker file on the Desktop holding the incident's
  timeline. When it resolves, the marker goes to the Trash.
- **Nothing is left behind.** Each run works in a folder of its own under
  `STATE_DIR` (copies only) and removes it; a run killed outright leaves one
  the next run removes. DEVONthink records a killed run left in `/my-ocr` are
  deleted by the next run -- that group only, never another record.

## Commands

```sh
my-ocr [OPTIONS] [FILES]    OCR them (the files from the command line, --file, else stdin)
my-ocr status               is an OCR running, on which document; DEVONthink and its settings; HEALTH
my-ocr reset [go]           clear what a killed run left: a mutex whose owner is gone, its work folder
my-ocr --version            the version, git's describe string, a build id of these exact bytes
my-ocr --run-tests [FILTER] the built-in tests (TAP); DEVONthink is a stub, nothing is OCRed
my-ocr --test-mail          one alert (notification + mail) the way a STUCK alert goes
```

`status` is read-only: it takes no lock and never starts DEVONthink. It reads
DEVONthink's OCR settings from its preferences: languages, *Deskew* and *Page
orientation* (a box at its default has no key, so no key = on). `reset` shows
what it would do; `reset go` does it. A live run is never touched, and no
document is ever part of a reset -- my-ocr replaces one only in a single
rename.

stdin is read only when no file was named any other way: from a script, a
LaunchDaemon or ssh, stdin is an open pipe nobody writes to, and reading it
would block for ever while holding the OCR mutex.

## Configuration

`my-ocr --create-config` prints the default config; `--create-config <FILE>`
writes it (never over an existing file). The first found of `$MY_OCR_CONFIG`,
`--config <FILE>`, `/LINKS/default/my-ocr.conf`, `/LINKS/default/my-ocr`,
`~/.my-ocr.conf`, `/etc/my-ocr.conf`, `/usr/local/etc/my-ocr.conf` is read --
as `KEY="value"` lines, never run. The printed text IS the defaults, so a file
holding only the lines it changes is complete.

| key | default | meaning |
|---|---|---|
| `MOVE_FAILED_TO` | `""` | where REFUSED and FAILED documents go; `""` = they stay put |
| `NO_OCR_FILENAME_PATTERNS` | `no-ocr-scan*` | file names never OCRed |
| `REVEAL_IN_FINDER` | `auto` | `auto` (runs started from a terminal), `always`, `never` |
| `OCR_TIMEOUT` | `600` | s DEVONthink gets for one document, before the one retry |
| `MUTEX_TIMEOUT` | `1800` | s a run waits for its turn |
| `STATE_DIR` | `/var/lib/mine/$USER/my-ocr` | the mutex and each run's work folder |
| `LOGFILE` / `LOG` | `/var/log/mine/$USER/my-ocr.log` / `""` | the record of a run; `-l` keeps one for that run |
| `STUCK_MARKER_DIR` | `$HOME/Desktop` | where the STUCK marker lands |

Recognition languages and the auto-corrections are DEVONthink's own settings
(Settings > OCR); `status` shows them.

## The FineReader engine: `my-ocr-finereader`

`my-ocr-finereader.sh`, deployed as `my-ocr-finereader`, is the shell my-ocr
v1.1 that drove ABBYY FineReader Pro 12 through an Automator Folder Action
(`setup [go]`, `uninstall [go]`, `reset [go]`, `status`, its own
`--run-tests`; `my-ocr-finereader --help`). It is kept for Macs where
FineReader's Automator step works, and is called directly; the only change
from v1.1 is its own name in the workflow it writes.

**Its Automator workflow runs as a Folder Action -- never from a
LaunchDaemon.** `/usr/bin/automator` does not run a workflow itself: it hands
it to Automator Runner, a helper that exists only inside the user's GUI login
session. A job in `/Library/LaunchDaemons` runs in the system context --
`UserName` changes the user, not the session -- so there `automator` exits
255, "Couldn't communicate with a helper application", whatever the workflow
does (ada, 2026-09-02: the old `eu.no-panic.folderAction_my-ocr`). macOS
Folder Actions run in the session, which is why `setup` attaches the workflow
to `,ocr.in` that way. The same holds for any Automator job, e.g. a scan
pick-up.

## Requirements

- macOS with DEVONthink Pro 4 and its ABBYY FineReader OCR add-on (DEVONthink >
  Install Add-ons, per user). Without the add-on my-ocr refuses to start and
  touches nothing; `my-ocr status` shows it on its ADD-ON line. The first
  run asks for the macOS Automation consent to control DEVONthink -- answer it
  once at the Mac, or allow it in System Settings > Privacy & Security >
  Automation.
- `pdfinfo`, `pdftotext` (poppler) and `qpdf`. my-ocr refuses to start without
  them: a result nothing has checked never replaces an original.
- `python3` 3.9 or later, standard library only.
- `my-notify`, for the alerts.

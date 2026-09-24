# my-ocr

Drives a single-instance OCR application (ABBYY FineReader) from the shell, and
guarantees that only one OCR run exists at a time.

Built for and verified against **ABBYY FineReader Pro for Mac 12.1.14**
(`/Applications/FineReader.app`, bundle `com.abbyy.FineReaderPro`) -- a GUI-only
application with no command line. **Together with [my-scan](../my-scan/) it is
especially useful**: my-scan receives the scanner's delivery and files the
result, my-ocr turns that FineReader into an unattended, scriptable OCR step.
my-ocr is usable on its own from the shell; the OCR itself is not -- without
that FineReader installed there is nothing to drive.

```sh
my-ocr scan.pdf                  # OCR in place; blocks until it is our turn
printf '%s\n' a.pdf b.png | my-ocr
my-ocr --file list.txt
```

Input may be **PDF, PNG, JPEG or TIFF** — a picture is turned into a searchable
PDF, and counts as one page. Each file is replaced in place on success.

## Why it exists

FineReader on macOS has no command line. The only automation hook is an
Automator action, and Automator drives it through a Folder Action: files land
in an input folder, the application writes results to an output folder. That is
a three-step round trip -- `--part-1`, the OCR action, `--part-2` -- and only
one of them can be in flight, because there is one GUI application and one
Automator wiring.

my-ocr wraps that round trip in a blocking, synchronous call. Callers just run
`my-ocr <files>` and wait; the queueing, the mutex and the Automator handshake
are its problem, not theirs.

## The single-instance guarantee

A `mkdir` mutex, which is the atomic test-and-set in POSIX shell. Every caller
queues at it rather than each having to arrange not to collide. A mutex whose
owner is provably dead (`kill -0`) is broken automatically and never silently;
a long OCR is not a crashed one, so a live owner is always waited for.

Concurrent runs overlap by design -- one queues while another works -- so every
per-run file carries the pid of the run that owns it. That is what makes
staleness decidable instead of guessed, and what lets a crashed run's leftovers
be swept without touching a live one's.

## Commands

```sh
my-ocr [FLAGS] [FILES]      OCR those files (default)
my-ocr status               what is running, on which document, what is queued
my-ocr setup [go]           generate the Automator workflow, bind the Folder Action
my-ocr uninstall [go]       remove that wiring again
my-ocr reset [go]           clean up after a crashed run; retry what was stranded
my-ocr --version            version, commit and a build id of these exact bytes
my-ocr --run-tests          the regression suite; starts no OCR
my-ocr --test-mail          one test alert (notification + mail) the STUCK way
```

`setup` and `uninstall` are dry runs unless given `go`. `reset` never deletes a
document: anything stranded mid-flight is *moved* to `RESET_RETRY_TO` and the
command to retry it is printed. It refuses while a run is live.

## Configuration

`my-ocr --create-config` prints the default config; `--create-config <FILE>`
writes it (never over an existing file). The first found of `$MY_OCR_CONFIG`,
`--config <FILE>`, `/LINKS/default/my-ocr.conf`, `/LINKS/default/my-ocr`,
`~/.my-ocr.conf`, `/etc/my-ocr.conf`, `/usr/local/etc/my-ocr.conf` is read.

The printed text IS my-ocr's defaults: it loads that first and the file on top.
So the file is optional, and one that holds only the lines it changes is
complete. Every setting, with the reasons behind each default, is in the
`--create-config` output:

| key | default | meaning |
|---|---|---|
| `S_PATH` | `~/Public/,ocr.in` | the folder the Folder Action watches |
| `T_PATH` | `~/Public/,ocr.out` | where the OCR application writes its results |
| `RESET_RETRY_TO` | `~/Public/,ocr.retry` | where `reset` moves stranded documents |
| `MOVE_FAILED_TO` | `""` | where failed originals go; `""` = leave them in place |
| `STUCK_MARKER_DIR` | `~/Desktop` | where the STUCK marker lands |
| `OCR_LANGUAGES` | `German,English,French,GermanLuxembourg` | recognition languages |
| `NO_OCR_FILENAME_PATTERNS` | `no-ocr-scan*` | filenames never OCRed |
| `REVEAL_IN_FINDER` | `auto` | `auto` (terminal runs only), `always`, `never` |
| `OCRAPP_MRC` | `0` | "Compress images using MRC" |
| `OCRAPP_IMAGE_QUALITY_MRC` | `2` | image quality when MRC is on: 0 Low, 1 Balanced, 2 High |
| `OCRAPP_IMAGE_QUALITY_NO_MRC` | `1` | image quality when MRC is off |
| `OCRAPP_SPLIT_FACING_PAGES` | `0` | the OCR application's "Split facing pages" |
| `OCRAPP_MUTEX_TIMEOUT` | `1800` | s a run queues for its turn |
| `OCRAPP_TIMEOUT` | `1800` | s to wait for the OCR application, or for a dead run's batch |
| `STALL_ALERT_AFTER` | `300` | s without progress before the first alert |
| `STALL_REALERT_EVERY` | `3600` | s between further alerts |
| `OCRAPP_IDLE_GIVEUP` | `120` | s of near-zero cpu while stalled before recovering; 0 = never |
| `OCRAPP_IDLE_MAX_CPU_PER_MIN` | `3` | "near zero": s of cpu per minute |
| `OCRAPP_TRIGGER_WAIT` | `60` | s without the OCR application before re-making the hand-over once; 0 = never |
| `FA_RESTART_WAIT` | `10` | s to wait for a restarted Folder Actions dispatcher |
| `OSASCRIPT_TIMEOUT` | `180` | s any osascript call may take |

The workflow carries `S_PATH`, `T_PATH` and the three export keys, so changing
any of them needs `my-ocr setup go` again; `status` reports a MISMATCH for the
export keys. "Split facing pages" is set by every run.

`OCRAPP_MRC` and the two qualities are the export controls of the Automator step; the quality
follows the MRC switch, so each mode carries its own. They are written into the
workflow by `setup go` -- nothing changes them at run time -- and `status` reads
them back out of the installed workflow and reports a MISMATCH when the config
has changed since. A quality other than 0, 1 or 2 is refused: the Automator step
offers only those three, and 3 makes the OCR application abort.

MRC is off by default because, measured on ten real scans, it redrew the text as
a 1-bit mask (visibly fuzzier), erased a QR code, and made the files larger; the
recognised text is the same either way.

"Split facing pages" is off because a document feeder delivers single sheets, so
every split is a false positive. It is an application preference, so it is set
at the start of every run (while the application is not running) and reported
when it had to be changed.

## Checking the result before it replaces the original

The result replaces the original in place, and the original goes to the Trash a
moment earlier -- so an unchecked bad result is a lost document. Before that
happens my-ocr requires all of:

- it parses as a PDF
- its page count is **not lower** than the input's. More is fine: with
  "Split facing pages" on, the OCR application cuts a sheet it believes holds
  two pages in two.
- its Producer names the OCR application -- proof the result is really its
  output and not a passthrough or a stale file from an earlier run
- `qpdf --check` passes. qpdf walks every object and stream; `pdfinfo` reads
  only the trailer, catalogue and page tree, so a valid catalogue over damaged
  content passes that and fails this. Its exit is graded: 0 clean, 3 warnings
  only, 2 damage.
**No text is NOT a refusal.** The checks above are the evidence that the OCR
application ran correctly -- there is no exit code to read, so they are all the
evidence there is. A page with nothing to recognise then yields no text, and
that is a finding about the DOCUMENT (a photograph, a blank sheet), not a fault
in the run. Such a result is **filed as done** like any other success, and
reported on a line of its own, so that "forty of forty found nothing" -- a
misconfigured OCR -- cannot hide inside the success total.

**The ORIGINAL is kept, and tagged.** The OCR application's output for such a
page is strictly worse -- one A3 photograph came back as 6.1 MB split across
two A4 pages, cutting the picture in half, against 665 KB for the original. But
the original carries no Producer, so a later run would OCR it again for ever.
So my-ocr writes the Producer onto the original instead:

    Producer: FineReader (my-ocr: no text found -- original kept)

and discards the OCR output (to the Trash, recoverable). The tag is written
with qpdf's JSON round-trip, which copies the page content through untouched --
the file grows by the metadata alone, roughly 64 bytes -- and it is verified
before it is installed: same page count, still sound under `qpdf --check`.
Ghostscript cannot do this, as `pdfwrite` stamps its own `/Producer` over
anything you set.

If the input was a JPEG, PNG or TIFF there is no PDF original to tag, so the
OCR application's PDF is kept instead, as it always was.

Deliberately NOT checked: file size, which legitimately moves either way with
the export settings (with MRC a much smaller result is the healthy outcome); and
image counts, because MRC splits one scan image into mask/foreground/background
layers.

**A refusal is a clean rollback:** the bad result is trashed, the ORIGINAL is
kept and moved to `--move-failed-to`, and you are notified with the reason and
where the document now is.

**A refusal is reported as REFUSED, not as FAILED** -- with the reason, per
document. The two are different outcomes and call for different actions: a
failure means the OCR could not run, a refusal means it ran and produced
something unusable, most often because the page carries no text to find. A
photograph of an object scanned on the flatbed refuses every time, and the
honest report of it is "there was nothing to OCR", not "the file FAILED".

## Requirements

- macOS with the OCR application installed, exposing its Automator action
- `pdfinfo` (poppler) and `convert` (ImageMagick) for validating input
- A `trash` command, so nothing is deleted outright

`setup` checks all of them and names anything missing -- including a helper that
is merely *unset*, which is a capability that would otherwise fail at the first
file needing it.

## Notes

- Files already carrying the OCR application's `Producer` tag are skipped unless
  `--force`; encrypted PDFs are detected and listed rather than mangled.
- Every run makes sure window restore is off for the OCR application
  (`defaults write com.abbyy.FineReaderPro ApplePersistenceIgnoreState -bool
  YES`, as the user running it). After a crash, macOS would otherwise ask at
  the next launch whether to reopen its windows, and an unattended run would
  wait on that dialog. It is written once and reported then; later runs find it
  off and say nothing.
- `--move-failed-to <DIR>` quarantines what could not be OCRed, so a caller does
  not have to work out which files those were.
- stdin is read only when no filenames were given another way -- otherwise a
  caller with an open-but-idle pipe would block for ever holding the OCR lock.
- The Automator step runs a shell from Automator's own list. `/bin/dash` is not
  in it, and a workflow naming one it does not offer does nothing at all, with
  no error anywhere.

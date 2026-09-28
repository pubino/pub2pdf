# pub2pdf

Bulk-convert Microsoft Publisher (`.pub`) files to PDF before Publisher goes away.

## Why

Microsoft is retiring Publisher: support ends in October 2026 and it is being removed
from Microsoft 365. After that, the `.pub` files that organizations have accumulated
on shared drives (signs, flyers, floor plans, handouts) have no supported way to open.
Publisher never ran on macOS either.

pub2pdf finds every `.pub` file under a directory tree, which is often a large network
share, and writes a PDF of each one into the same folder, next to the original. People
can keep finding files where they always have, and the content stays readable without
Publisher.

## How it works

1. `find_pub.py` walks a directory tree using parallel `os.scandir` calls. This is fast
   on SMB/NFS mounts, even over a VPN. It prints every `.pub` path to stdout and
   progress to stderr.
2. `pub2pdf.py` copies each file to a local staging area and converts it with
   [LibreOffice](https://www.libreoffice.org/), which reads Publisher files via
   [libmspub](https://wiki.documentfoundation.org/DLP/Libraries/libmspub). It then copies
   the PDF back beside the source through a temp file and a rename.
3. It uses a locally installed `soffice` if it finds one. Otherwise it builds and runs a
   Docker image with LibreOffice and Microsoft core fonts (`docker/Dockerfile.converter`),
   so nothing needs to be installed on the host.

## Usage

Requires Python 3.9+ (standard library only) and either LibreOffice or Docker.

```zsh
# 1. Find the files
python3 find_pub.py /Volumes/share > pub_files_list.txt

# 2. Preview what would happen
python3 pub2pdf.py --list pub_files_list.txt --dry-run

# 3. Convert
python3 pub2pdf.py --list pub_files_list.txt

# Or scan and convert in one step
python3 pub2pdf.py --scan /Volumes/share
```

### Existing PDFs are never overwritten

Many `.pub` files already have a PDF next to them, often exported by hand from Publisher.
Two options control what happens to those:

| Option | Behavior when `Name.pdf` already exists |
| --- | --- |
| *(default)* | Skip. The existing PDF is left alone. |
| `--conflict-suffix " (converted)"` | Write `Name (converted).pdf` beside it. PDFs made by an earlier pub2pdf run are recognized and skipped, so re-runs don't create duplicates. PDFs that can't be read (for example, open in another app over SMB) are also skipped. |
| `--force` | Overwrite `Name.pdf`. |

Re-running is safe: files that were already converted are skipped.

### Other options

| Option | Default | |
| --- | --- | --- |
| `--backend auto\|local\|docker` | `auto` | `auto` uses local LibreOffice if found, otherwise Docker |
| `--batch-size N` | 10 | Files per LibreOffice invocation |
| `--workers N` | 2 | Parallel batches |
| `--staging DIR` | `~/.cache/pub2pdf` | Local work area. Docker Desktop shares `/Users` by default, so keep it under your home directory. |
| `--report FILE` | `pub2pdf_report.csv` | Per-file status: `converted`, `skipped` or `failed`, with a reason |

The exit code is `0` when everything converted or was skipped, `1` if any file failed,
and `2` for usage or setup errors.

## Fidelity

LibreOffice renders most Publisher documents accurately, including text, shapes, images,
gradients and tables. Complex layouts can differ slightly, most often through font
substitution or text overlapping artwork. Spot-check important documents, and keep the
`.pub` originals if you can.

## Tests

```zsh
# Unit tests (mocked converter), no dependencies
python3 -m venv .venv && .venv/bin/python -m unittest discover -s tests -v

# Full suite in Docker, including end-to-end conversion through real LibreOffice
./run_tests.sh

# Also convert your own .pub samples as part of the suite
PUB2PDF_SAMPLES_HOST=/path/to/sample/pubs ./run_tests.sh
```

## License

MIT

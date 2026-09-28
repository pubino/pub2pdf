#!/usr/bin/env python3
"""Convert Microsoft Publisher (.pub) files to PDF, writing each PDF next to its source.

Inputs are either a list file (e.g. the stdout of find_pub.py) or one or more
directories to scan. Conversion uses LibreOffice: a local `soffice` if one is
installed, otherwise the pub2pdf-converter Docker image (built on demand).

Files are staged locally before conversion so LibreOffice never works directly
against the SMB share, and each PDF is copied back via a temp file + rename.
"""
import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
DOCKER_IMAGE = "pub2pdf-converter"
DOCKERFILE = os.path.join(HERE, "docker", "Dockerfile.converter")
# Docker Desktop only shares paths under /Users by default, so stage there.
DEFAULT_STAGING = os.path.expanduser("~/.cache/pub2pdf")
SOFFICE_CANDIDATES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "soffice",
    "libreoffice",
]
SECONDS_PER_FILE = 120


@dataclass
class Result:
    source: str
    pdf: str
    status: str  # converted | skipped | failed | would-convert
    message: str = ""


def read_list(path):
    """Read .pub paths from a list file, ignoring progress/blank/non-.pub lines."""
    paths = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n").strip()
            if line.lower().endswith(".pub") and os.path.isabs(line):
                paths.append(line)
    return paths


def pdf_path_for(pub_path):
    return os.path.splitext(pub_path)[0] + ".pdf"


def conflict_path_for(pub_path, suffix):
    return os.path.splitext(pub_path)[0] + suffix + ".pdf"


# LibreOffice writes its Producer either as plain text or as hex-encoded UTF-16.
_LO_MARKERS = (b"LibreOffice", "LibreOffice".encode("utf-16-be").hex().upper().encode())


def made_by_libreoffice(pdf_path):
    """True if the PDF was produced by LibreOffice (i.e. by a previous pub2pdf run).

    Raises OSError if it cannot be read (e.g. SMB returns EINTR/ECANCELED while
    another app has it open); callers must not treat that as "not ours".
    """
    with open(pdf_path, "rb") as fh:
        data = fh.read()
    return _LO_MARKERS[0] in data or _LO_MARKERS[1] in data.upper()


def find_soffice():
    for candidate in SOFFICE_CANDIDATES:
        resolved = shutil.which(candidate) if not os.path.isabs(candidate) else (
            candidate if os.access(candidate, os.X_OK) else None)
        if resolved:
            return resolved
    return None


class Converter:
    """Runs LibreOffice over a directory of staged .pub files."""

    def __init__(self, backend, soffice=None, runner=subprocess.run):
        self.backend = backend
        self.soffice = soffice
        self.run = runner

    def command(self, in_dir, out_dir, names, profile_dir):
        if self.backend == "local":
            return [self.soffice, f"-env:UserInstallation=file://{profile_dir}",
                    "--headless", "--norestore", "--nologo", "--nolockcheck",
                    "--convert-to", "pdf", "--outdir", out_dir,
                    *[os.path.join(in_dir, n) for n in names]]
        return ["docker", "run", "--rm", "--network", "none",
                "-v", f"{in_dir}:/in:ro", "-v", f"{out_dir}:/out",
                DOCKER_IMAGE, "--convert-to", "pdf", "--outdir", "/out",
                *[f"/in/{n}" for n in names]]

    def convert(self, in_dir, out_dir, names, profile_dir):
        cmd = self.command(in_dir, out_dir, names, profile_dir)
        proc = self.run(cmd, capture_output=True, text=True,
                        timeout=SECONDS_PER_FILE * max(1, len(names)))
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def ensure_docker_image(runner=subprocess.run):
    if runner(["docker", "image", "inspect", DOCKER_IMAGE],
              capture_output=True).returncode == 0:
        return
    print(f"Building Docker image {DOCKER_IMAGE} (first run only)...", file=sys.stderr)
    proc = runner(["docker", "build", "-f", DOCKERFILE, "-t", DOCKER_IMAGE, HERE])
    if proc.returncode != 0:
        raise RuntimeError("docker build failed")


def copy_back(src_pdf, dest_pdf):
    tmp = dest_pdf + ".pub2pdf-tmp"
    shutil.copyfile(src_pdf, tmp)
    os.replace(tmp, dest_pdf)


def convert_batch(converter, batch, staging_root):
    """Convert a batch of (pub, dest_pdf) pairs; returns a Result per pair."""
    work = tempfile.mkdtemp(prefix="batch-", dir=staging_root)
    in_dir, out_dir, profile = (os.path.join(work, d) for d in ("in", "out", "profile"))
    for d in (in_dir, out_dir, profile):
        os.makedirs(d)
    results, staged = [], []
    try:
        for i, (pub, dest) in enumerate(batch):
            # Prefix with an index: identical basenames exist in different folders.
            name = f"{i:04d}_{os.path.basename(pub)}"
            try:
                shutil.copyfile(pub, os.path.join(in_dir, name))
                staged.append((pub, dest, name))
            except OSError as exc:
                results.append(Result(pub, dest, "failed", f"copy-in: {exc}"))
        if not staged:
            return results
        try:
            code, output = converter.convert(in_dir, out_dir, [n for _, _, n in staged], profile)
        except subprocess.TimeoutExpired:
            code, output = -1, "LibreOffice timed out"
        for pub, dest, name in staged:
            produced = os.path.join(out_dir, os.path.splitext(name)[0] + ".pdf")
            if os.path.isfile(produced) and os.path.getsize(produced) > 0:
                try:
                    copy_back(produced, dest)
                    results.append(Result(pub, dest, "converted"))
                except OSError as exc:
                    results.append(Result(pub, dest, "failed", f"copy-out: {exc}"))
            else:
                detail = output.strip().splitlines()[-1] if output.strip() else "no output"
                results.append(Result(pub, dest, "failed",
                                      f"LibreOffice exit {code}: {detail}"))
        return results
    finally:
        shutil.rmtree(work, ignore_errors=True)


def convert_all(pubs, converter, staging_root, batch_size=10, workers=2,
                force=False, dry_run=False, log=print, conflict_suffix=None):
    results, todo = [], []
    for pub in dict.fromkeys(pubs):  # de-duplicate, keep order
        pdf = pdf_path_for(pub)
        if not os.path.isfile(pub):
            results.append(Result(pub, pdf, "failed", "source not found"))
            continue
        if not force and os.path.exists(pdf):
            # Never clobber an existing PDF (it may be a hand-exported original).
            if not conflict_suffix:
                results.append(Result(pub, pdf, "skipped", "PDF already exists"))
                continue
            try:
                ours = made_by_libreoffice(pdf)
            except OSError as exc:
                results.append(Result(pub, pdf, "skipped",
                                      f"could not read existing PDF, left alone: {exc}"))
                continue
            if ours:
                results.append(Result(pub, pdf, "skipped", "already converted by pub2pdf"))
                continue
            pdf = conflict_path_for(pub, conflict_suffix)
            if os.path.exists(pdf):
                results.append(Result(pub, pdf, "skipped", "PDF already exists"))
                continue
        if dry_run:
            results.append(Result(pub, pdf, "would-convert"))
        else:
            todo.append((pub, pdf))
    if not todo:
        return results

    os.makedirs(staging_root, exist_ok=True)
    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(convert_batch, converter, b, staging_root) for b in batches]
        for fut in as_completed(futures):
            for r in fut.result():
                done += 1
                log(f"[{done}/{len(todo)}] {r.status.upper()}: {r.pdf}"
                    + (f" ({r.message})" if r.message else ""))
                results.append(r)
    return results


def write_report(results, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["status", "source", "pdf", "message"])
        for r in results:
            w.writerow([r.status, r.source, r.pdf, r.message])


def pick_backend(requested, runner=subprocess.run):
    if requested in ("auto", "local"):
        soffice = find_soffice()
        if soffice:
            return Converter("local", soffice=soffice, runner=runner)
        if requested == "local":
            raise RuntimeError("LibreOffice (soffice) not found; use --backend docker")
    if shutil.which("docker") is None:
        raise RuntimeError("Neither LibreOffice nor Docker is available")
    ensure_docker_image(runner)
    return Converter("docker", runner=runner)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--list", help="file of .pub paths (e.g. find_pub.py output)")
    src.add_argument("--scan", nargs="+", metavar="DIR", help="directories to scan for .pub files")
    ap.add_argument("--backend", choices=["auto", "local", "docker"], default="auto")
    ap.add_argument("--force", action="store_true", help="overwrite PDFs that already exist")
    ap.add_argument("--conflict-suffix", metavar="TEXT",
                    help='when a non-pub2pdf PDF already exists, write "<name>TEXT.pdf" instead '
                         '(e.g. " (converted)")')
    ap.add_argument("--dry-run", action="store_true", help="show what would be converted")
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--staging", default=DEFAULT_STAGING)
    ap.add_argument("--report", default="pub2pdf_report.csv", help="CSV results file")
    args = ap.parse_args(argv)

    if args.list:
        pubs = read_list(args.list)
    else:
        from find_pub import scan_share
        pubs = [p for d in args.scan for p in scan_share(d, echo=False)]
    print(f"{len(pubs)} .pub files to consider", file=sys.stderr)
    if not pubs:
        print("ERROR: no .pub files found in input", file=sys.stderr)
        return 2

    start = time.time()
    try:
        converter = None if args.dry_run else pick_backend(args.backend)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if converter:
        print(f"Backend: {converter.backend}", file=sys.stderr)
    results = convert_all(pubs, converter, args.staging, args.batch_size, args.workers,
                          args.force, args.dry_run, conflict_suffix=args.conflict_suffix)
    write_report(results, args.report)

    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    print(f"\n[COMPLETE] {summary} in {time.time() - start:.1f}s. Report: {args.report}",
          file=sys.stderr)
    for r in results:
        if r.status == "failed":
            print(f"FAILED: {r.source} ({r.message})", file=sys.stderr)
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())

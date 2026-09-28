import os
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pub2pdf  # noqa: E402


def fake_runner(fail_names=(), returncode=0):
    """Mimic soffice/docker: write a PDF into the out dir for each staged input."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "docker":
            out_dir = cmd[cmd.index("-v", cmd.index("-v") + 1) + 1].split(":")[0]
            inputs = [a[len("/in/"):] for a in cmd if a.startswith("/in/")]
        else:
            out_dir = cmd[cmd.index("--outdir") + 1]
            inputs = [os.path.basename(a) for a in cmd if a.endswith(".pub")]
        for name in inputs:
            if not any(f in name for f in fail_names):
                with open(os.path.join(out_dir, os.path.splitext(name)[0] + ".pdf"), "wb") as fh:
                    fh.write(b"%PDF-1.4 fake")
        return SimpleNamespace(returncode=returncode, stdout="", stderr="Error: source file could not be loaded")

    run.calls = calls
    return run


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.staging = os.path.join(self.root, "staging")
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def make_pub(self, rel, content=b"pubdata"):
        path = os.path.join(self.root, "share", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def run_all(self, pubs, runner, **kw):
        conv = pub2pdf.Converter("local", soffice="/fake/soffice", runner=runner)
        return pub2pdf.convert_all(pubs, conv, self.staging, log=self.logs.append, **kw)


class TestReadList(Base):
    def test_ignores_progress_and_non_pub_lines(self):
        lst = os.path.join(self.root, "list.txt")
        with open(lst, "w") as fh:
            fh.write("Starting search in: /Volumes/share\n"
                     "/Volumes/share/a/Sign- Up Arrow.pub\n"
                     "[PROGRESS] Directories scanned: 870 | Matches: 3\n"
                     "\n"
                     "/Volumes/share/b/UPPER.PUB\r\n"
                     "/Volumes/share/b/notes.txt\n"
                     "relative/path.pub\n")
        self.assertEqual(pub2pdf.read_list(lst),
                         ["/Volumes/share/a/Sign- Up Arrow.pub", "/Volumes/share/b/UPPER.PUB"])

    def test_pdf_path_same_folder(self):
        self.assertEqual(pub2pdf.pdf_path_for("/x/y/My File.PUB"), "/x/y/My File.pdf")


class TestConvertAll(Base):
    def test_writes_pdf_next_to_each_source(self):
        a = self.make_pub("Events/A/Diagram.pub", b"A")
        b = self.make_pub("Events/B/Diagram.pub", b"B")  # same basename, other folder
        results = self.run_all([a, b], fake_runner())
        self.assertEqual(sorted(r.status for r in results), ["converted", "converted"])
        for p in (a, b):
            pdf = pub2pdf.pdf_path_for(p)
            self.assertTrue(os.path.isfile(pdf), pdf)
            with open(pdf, "rb") as fh:
                self.assertTrue(fh.read().startswith(b"%PDF"))
        self.assertFalse(any(f.endswith(".pub2pdf-tmp")
                             for _, _, fs in os.walk(self.root) for f in fs))
        self.assertEqual(os.listdir(self.staging), [], "staging dirs must be cleaned up")
        self.assertEqual(len(self.logs), 2)

    def test_existing_pdf_is_never_overwritten_without_force(self):
        a = self.make_pub("A/x.pub")
        pdf = pub2pdf.pdf_path_for(a)
        with open(pdf, "wb") as fh:
            fh.write(b"hand-made original")
        past = time.time() - 1000  # older than the .pub: still must not be touched
        os.utime(pdf, (past, past))
        runner = fake_runner()
        results = self.run_all([a], runner)
        self.assertEqual([r.status for r in results], ["skipped"])
        self.assertEqual(runner.calls, [])
        with open(pdf, "rb") as fh:
            self.assertEqual(fh.read(), b"hand-made original")
        results = self.run_all([a], runner, force=True)
        self.assertEqual([r.status for r in results], ["converted"])
        with open(pdf, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"%PDF"))

    def test_rerun_skips_already_converted(self):
        a = self.make_pub("A/x.pub")
        runner = fake_runner()
        self.run_all([a], runner)
        results = self.run_all([a], runner)
        self.assertEqual([r.status for r in results], ["skipped"])
        self.assertEqual(len(runner.calls), 1)

    def write_pdf(self, path, body):
        with open(path, "wb") as fh:
            fh.write(body)

    def test_conflict_suffix_keeps_original_and_writes_converted_copy(self):
        a = self.make_pub("A/Sign.pub")
        original = pub2pdf.pdf_path_for(a)
        self.write_pdf(original, b"%PDF-1.4 /Producer(Microsoft Publisher 2016)")
        runner = fake_runner()
        results = self.run_all([a], runner, conflict_suffix=" (converted)")
        alt = os.path.join(os.path.dirname(a), "Sign (converted).pdf")
        self.assertEqual([(r.status, r.pdf) for r in results], [("converted", alt)])
        with open(original, "rb") as fh:
            self.assertIn(b"Microsoft Publisher", fh.read(), "original must be untouched")
        with open(alt, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"%PDF"))
        # Re-running must not convert again or create "(converted) (converted)".
        results = self.run_all([a], runner, conflict_suffix=" (converted)")
        self.assertEqual([r.status for r in results], ["skipped"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(sorted(os.listdir(os.path.dirname(a))),
                         ["Sign (converted).pdf", "Sign.pdf", "Sign.pub"])

    def test_conflict_suffix_skips_pdfs_pub2pdf_already_made(self):
        plain = self.make_pub("A/plain.pub")
        hexed = self.make_pub("B/hexed.pub")
        self.write_pdf(pub2pdf.pdf_path_for(plain), b"%PDF-1.6 /Producer(LibreOffice 7.4)")
        hex_producer = "LibreOffice 7.4".encode("utf-16-be").hex().upper().encode()
        self.write_pdf(pub2pdf.pdf_path_for(hexed), b"%PDF-1.6 /Producer<FEFF" + hex_producer + b">")
        runner = fake_runner()
        results = self.run_all([plain, hexed], runner, conflict_suffix=" (converted)")
        self.assertEqual([r.message for r in results], ["already converted by pub2pdf"] * 2)
        self.assertEqual(runner.calls, [])

    def test_conflict_suffix_unreadable_existing_pdf_is_skipped(self):
        a = self.make_pub("A/x.pub")
        self.write_pdf(pub2pdf.pdf_path_for(a), b"%PDF-1.6 /Producer(LibreOffice 7.4)")
        runner = fake_runner()
        err = OSError(89, "Operation canceled")  # what SMB returns while Preview holds the file
        with mock.patch.object(pub2pdf, "made_by_libreoffice", side_effect=err):
            results = self.run_all([a], runner, conflict_suffix=" (converted)")
        self.assertEqual(results[0].status, "skipped")
        self.assertIn("could not read existing PDF", results[0].message)
        self.assertEqual(runner.calls, [])
        self.assertFalse(os.path.exists(pub2pdf.conflict_path_for(a, " (converted)")))

    def test_conflict_suffix_dry_run_reports_alternate_name(self):
        a = self.make_pub("A/x.pub")
        self.write_pdf(pub2pdf.pdf_path_for(a), b"%PDF-1.4 /Producer(Acrobat Distiller)")
        results = self.run_all([a], fake_runner(), conflict_suffix=" (converted)", dry_run=True)
        self.assertEqual(results[0].status, "would-convert")
        self.assertTrue(results[0].pdf.endswith("x (converted).pdf"))
        self.assertFalse(os.path.exists(results[0].pdf))

    def test_failed_conversion_is_reported_not_silent(self):
        good = self.make_pub("A/good.pub")
        bad = self.make_pub("A/bad.pub")
        results = {r.source: r for r in self.run_all([good, bad], fake_runner(fail_names=["bad"]))}
        self.assertEqual(results[good].status, "converted")
        self.assertEqual(results[bad].status, "failed")
        self.assertIn("could not be loaded", results[bad].message)
        self.assertFalse(os.path.exists(pub2pdf.pdf_path_for(bad)))

    def test_missing_source_and_dedupe(self):
        a = self.make_pub("A/x.pub")
        missing = os.path.join(self.root, "share", "nope.pub")
        runner = fake_runner()
        results = self.run_all([a, a, missing], runner)
        self.assertEqual(sorted(r.status for r in results), ["converted", "failed"])
        self.assertEqual(len(runner.calls), 1)

    def test_timeout_marks_batch_failed(self):
        a = self.make_pub("A/x.pub")

        def runner(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        results = self.run_all([a], runner)
        self.assertEqual(results[0].status, "failed")
        self.assertIn("timed out", results[0].message)

    def test_dry_run_writes_nothing(self):
        a = self.make_pub("A/x.pub")
        runner = fake_runner()
        results = self.run_all([a], runner, dry_run=True)
        self.assertEqual([r.status for r in results], ["would-convert"])
        self.assertEqual(runner.calls, [])
        self.assertFalse(os.path.exists(pub2pdf.pdf_path_for(a)))

    def test_batching(self):
        pubs = [self.make_pub(f"D{i}/f{i}.pub") for i in range(7)]
        runner = fake_runner()
        results = self.run_all(pubs, runner, batch_size=3, workers=2)
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual([r.status for r in results].count("converted"), 7)

    def test_docker_backend_command(self):
        a = self.make_pub("A/x.pub")
        runner = fake_runner()
        conv = pub2pdf.Converter("docker", runner=runner)
        results = pub2pdf.convert_all([a], conv, self.staging, log=self.logs.append)
        self.assertEqual(results[0].status, "converted")
        cmd = runner.calls[0]
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertIn(pub2pdf.DOCKER_IMAGE, cmd)
        self.assertIn("/in/0000_x.pub", cmd)


class TestBackendAndMain(Base):
    def test_local_backend_errors_when_soffice_missing(self):
        with mock.patch.object(pub2pdf, "find_soffice", return_value=None):
            with self.assertRaises(RuntimeError):
                pub2pdf.pick_backend("local")

    def test_auto_falls_back_to_docker_and_builds_image(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=1 if cmd[:3] == ["docker", "image", "inspect"] else 0)
        with mock.patch.object(pub2pdf, "find_soffice", return_value=None), \
             mock.patch.object(pub2pdf.shutil, "which", return_value="/usr/bin/docker"):
            conv = pub2pdf.pick_backend("auto", runner=runner)
        self.assertEqual(conv.backend, "docker")
        self.assertEqual(calls[1][:2], ["docker", "build"])

    def test_main_exit_codes_and_report(self):
        good = self.make_pub("A/good.pub")
        bad = self.make_pub("A/bad.pub")
        lst = os.path.join(self.root, "list.txt")
        with open(lst, "w") as fh:
            fh.write(f"{good}\n{bad}\n")
        report = os.path.join(self.root, "report.csv")
        conv = pub2pdf.Converter("local", soffice="/fake", runner=fake_runner(fail_names=["bad"]))
        with mock.patch.object(pub2pdf, "pick_backend", return_value=conv):
            code = pub2pdf.main(["--list", lst, "--staging", self.staging, "--report", report])
        self.assertEqual(code, 1, "any failure must yield a non-zero exit")
        with open(report) as fh:
            text = fh.read()
        self.assertIn("converted", text)
        self.assertIn("failed", text)

    def test_main_empty_input_is_an_error(self):
        lst = os.path.join(self.root, "empty.txt")
        open(lst, "w").close()
        self.assertEqual(pub2pdf.main(["--list", lst]), 2)


@unittest.skipUnless(pub2pdf.find_soffice(), "LibreOffice not installed (runs in the Docker test image)")
class TestRealLibreOffice(Base):
    """End-to-end through a real soffice. A text file named .pub exercises staging,
    invocation and copy-back; set PUB2PDF_SAMPLE_DIR to also convert real .pub files."""

    def converter(self):
        return pub2pdf.Converter("local", soffice=pub2pdf.find_soffice())

    def test_pipeline_end_to_end(self):
        a = self.make_pub("Some Folder/Sign- Up Arrow.pub", b"Hello from pub2pdf\n")
        results = pub2pdf.convert_all([a], self.converter(), self.staging, log=self.logs.append)
        self.assertEqual(results[0].status, "converted", results[0].message)
        with open(pub2pdf.pdf_path_for(a), "rb") as fh:
            self.assertEqual(fh.read(5), b"%PDF-")

    def test_real_pub_samples(self):
        sample_dir = os.environ.get("PUB2PDF_SAMPLE_DIR")
        if not sample_dir:
            self.skipTest("PUB2PDF_SAMPLE_DIR not set")
        import shutil
        pubs = []
        for name in os.listdir(sample_dir):
            if name.lower().endswith(".pub"):
                dest = os.path.join(self.root, "share", name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(os.path.join(sample_dir, name), dest)
                pubs.append(dest)
        self.assertTrue(pubs, "PUB2PDF_SAMPLE_DIR contains no .pub files")
        results = pub2pdf.convert_all(pubs, self.converter(), self.staging, log=self.logs.append)
        for r in results:
            self.assertEqual(r.status, "converted", f"{r.source}: {r.message}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

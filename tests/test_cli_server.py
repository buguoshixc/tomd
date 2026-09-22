"""Tests for the CLI and the local web service.

The CLI is tested through :func:`tomd.cli.main` rather than a subprocess: it is
faster, it works identically on Windows, and the exit code is the contract that
matters for pipeline use.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tomd.cli import build_parser, main


class TestParser:
    def test_no_inputs_prints_help(self, capsys):
        assert main([]) == 2
        assert "usage:" in capsys.readouterr().out.lower()

    def test_unknown_flag_exits_with_usage_error(self):
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--not-a-flag"])
        assert excinfo.value.code == 2

    def test_common_flags_parse(self):
        args = build_parser().parse_args(
            ["a.pdf", "-o", "out", "--no-report", "--workers", "4", "--pattern", "*.pdf"]
        )
        assert args.output == "out"
        assert args.no_report is True
        assert args.workers == 4
        assert args.pattern == ["*.pdf"]


class TestFormatsListing:
    def test_lists_formats_and_exits_zero(self, capsys):
        assert main(["--formats"]) == 0
        out = capsys.readouterr().out
        assert "pdf" in out and "docx" in out and "html" in out
        assert "supported formats" in out

    def test_mentions_unavailable_engines(self, capsys):
        main(["--formats"])
        out = capsys.readouterr().out
        assert "Optional engines not installed" in out or "converters registered" in out

    def test_states_that_ocr_is_out_of_scope(self, capsys):
        main(["--formats"])
        assert "OCR" in capsys.readouterr().out


class TestSingleFile:
    def test_prints_markdown_to_stdout(self, fixture, capsys):
        code = main([str(fixture("review.html")), "--stdout", "--no-front-matter", "--no-report"])
        out = capsys.readouterr().out
        assert code == 0
        assert "# Quarterly review" in out

    def test_writes_to_explicit_output(self, fixture, tmp_path, capsys):
        target = tmp_path / "out.md"
        code = main([str(fixture("people.csv")), "-o", str(target)])
        assert code == 0
        assert "| name | role |" in target.read_text(encoding="utf-8")

    def test_default_output_goes_to_dot_md(self, fixture, tmp_path):
        target_dir = tmp_path / "src"
        target_dir.mkdir()
        source = target_dir / "people.csv"
        source.write_bytes(fixture("people.csv").read_bytes())
        code = main([str(source)])
        assert code == 0
        assert (target_dir / ".md" / "people.md").exists()

    def test_stdout_writes_no_file(self, fixture, tmp_path):
        target_dir = tmp_path / "src"
        target_dir.mkdir()
        source = target_dir / "people.csv"
        source.write_bytes(fixture("people.csv").read_bytes())
        main([str(source), "--stdout", "--no-report"])
        assert not (target_dir / ".md").exists()

    def test_output_dir_creates_a_file_inside_it(self, fixture, tmp_path):
        outdir = tmp_path / "out"
        code = main([str(fixture("people.csv")), "-o", str(outdir) + "/"])
        assert code == 0
        assert (outdir / "people.md").exists()

    def test_missing_file_returns_usage_error(self, tmp_path, capsys):
        assert main([str(tmp_path / "nope.txt")]) == 2

    def test_scanned_pdf_returns_nonzero(self, fixture, capsys):
        assert main([str(fixture("scanned.pdf")), "--no-report"]) == 1

    def test_no_report_flag_removes_report(self, fixture, tmp_path):
        target = tmp_path / "a.md"
        main([str(fixture("review.html")), "-o", str(target), "--no-report"])
        assert "Conversion report" not in target.read_text(encoding="utf-8")

    def test_no_front_matter_flag(self, fixture, tmp_path):
        target = tmp_path / "b.md"
        main([str(fixture("review.html")), "-o", str(target), "--no-front-matter"])
        assert not target.read_text(encoding="utf-8").startswith("---")

    def test_report_json_flag_writes_sidecar(self, fixture, tmp_path):
        target = tmp_path / "c.md"
        main([str(fixture("review.html")), "-o", str(target), "--report-json"])
        payload = json.loads(target.with_suffix(".md.json").read_text(encoding="utf-8"))
        assert payload["format"] == "html"


class TestBatch:
    def _make_tree(self, tmp_path, fixtures):
        root = tmp_path / "docs"
        (root / "sub").mkdir(parents=True)
        (root / "people.csv").write_bytes((fixtures / "people.csv").read_bytes())
        (root / "sub" / "review.html").write_bytes((fixtures / "review.html").read_bytes())
        return root

    def test_converts_a_directory_tree(self, tmp_path, fixtures, capsys):
        root = self._make_tree(tmp_path, fixtures)
        code = main([str(root), "--out", str(tmp_path / "out")])
        assert code == 0
        assert (tmp_path / "out" / "people.md").exists()
        assert (tmp_path / "out" / "sub" / "review.md").exists()

    def test_quiet_mode_suppresses_progress(self, tmp_path, fixtures, capsys):
        root = self._make_tree(tmp_path, fixtures)
        main([str(root), "--out", str(tmp_path / "out"), "--quiet"])
        assert "converting" not in capsys.readouterr().err

    def test_pattern_filter(self, tmp_path, fixtures):
        root = self._make_tree(tmp_path, fixtures)
        main([str(root), "--out", str(tmp_path / "out"), "--pattern", "*.csv", "--quiet"])
        out = tmp_path / "out"
        assert (out / "people.md").exists()
        assert not (out / "sub" / "review.md").exists()

    def test_no_recursive_skips_subdirs(self, tmp_path, fixtures):
        root = self._make_tree(tmp_path, fixtures)
        main([str(root), "--out", str(tmp_path / "out"), "--no-recursive", "--quiet"])
        assert not (tmp_path / "out" / "sub" / "review.md").exists()

    def test_workers_flag(self, tmp_path, fixtures):
        root = self._make_tree(tmp_path, fixtures)
        code = main([str(root), "--out", str(tmp_path / "out"), "--workers", "3", "--quiet"])
        assert code == 0

    def test_report_to_writes_json(self, tmp_path, fixtures):
        root = self._make_tree(tmp_path, fixtures)
        report = tmp_path / "report.json"
        main([str(root), "--out", str(tmp_path / "out"), "--report-to", str(report), "--quiet"])
        assert json.loads(report.read_text(encoding="utf-8"))["total"] == 2

    def test_multiple_files_sharing_a_parent(self, tmp_path, fixtures, capsys):
        root = tmp_path / "many"
        root.mkdir()
        for name in ("people.csv", "review.html"):
            (root / name).write_bytes((fixtures / name).read_bytes())
        code = main([
            str(root / "people.csv"), str(root / "review.html"),
            "--out", str(tmp_path / "out"), "--quiet",
        ])
        assert code == 0
        assert (tmp_path / "out" / "people.md").exists()

    def test_mixing_files_and_directories_is_rejected(self, tmp_path, fixtures, capsys):
        root = self._make_tree(tmp_path, fixtures)
        code = main([str(root), str(root / "people.csv")])
        assert code == 2
        assert "mixing" in capsys.readouterr().err


class TestServer:
    @pytest.fixture()
    def server(self):
        from tomd.server import Handler, free_port

        port = free_port(18100)
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_index_page_is_served_with_a_token(self, server):
        with urllib.request.urlopen(server + "/", timeout=10) as response:
            body = response.read().decode("utf-8")
            cookie = response.headers.get("Set-Cookie", "")
        assert "tomd" in body
        assert "tomd_token=" in cookie
        assert "__TOKEN__" not in body

    def test_health_endpoint(self, server):
        with urllib.request.urlopen(server + "/api/health", timeout=10) as response:
            assert json.loads(response.read())["ok"] is True

    def test_formats_endpoint(self, server):
        with urllib.request.urlopen(server + "/api/formats", timeout=10) as response:
            payload = json.loads(response.read())
        assert "pdf" in payload["formats"]

    def test_unknown_path_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(server + "/nope", timeout=10)
        assert excinfo.value.code == 404

    def test_convert_without_token_is_forbidden(self, server):
        request = urllib.request.Request(
            server + "/api/convert", data=b"x", method="POST",
            headers={"Content-Type": "text/plain"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)
        assert excinfo.value.code == 403

    def test_convert_round_trip(self, server, fixture):
        import secrets
        import tomd.server as server_module

        payload = fixture("review.html").read_bytes()
        body, content_type = _multipart(payload, "review.html")
        request = urllib.request.Request(
            server + "/api/convert", data=body, method="POST",
            headers={"Content-Type": content_type, "X-Tomd-Token": server_module._TOKEN},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
        assert result["format"] == "html"
        assert "# Quarterly review" in result["markdown"]
        assert result["status"] == "ok"

    def test_convert_rejects_a_non_multipart_body(self, server):
        import tomd.server as server_module

        request = urllib.request.Request(
            server + "/api/convert", data=b"plain text", method="POST",
            headers={"Content-Type": "text/plain", "X-Tomd-Token": server_module._TOKEN},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)
        assert excinfo.value.code == 400

    def test_hostile_filename_cannot_escape_the_temp_dir(self, server, fixture):
        import tomd.server as server_module

        payload = fixture("people.csv").read_bytes()
        body, content_type = _multipart(payload, "../../../../evil.csv")
        request = urllib.request.Request(
            server + "/api/convert", data=body, method="POST",
            headers={"Content-Type": content_type, "X-Tomd-Token": server_module._TOKEN},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
        assert result["status"] == "ok"
        assert "| name | role |" in result["markdown"]

    def test_refuses_a_non_loopback_bind_without_opt_in(self, capsys):
        from tomd.server import serve

        assert serve("0.0.0.0", 18999) == 2
        assert "refusing to bind" in capsys.readouterr().err


def _multipart(payload: bytes, filename: str) -> tuple[bytes, str]:
    """Build a minimal multipart/form-data body."""
    boundary = "----tomdtest"
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        payload,
        b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="front_matter"\r\n\r\n',
        b"1\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"

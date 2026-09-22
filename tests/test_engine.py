"""Tests for the engine: options, output layout, assets, reports, batch runs."""

from __future__ import annotations

import json

import pytest

from tomd import ConvertOptions, build_options, convert, convert_many, render
from tomd.engine import (
    AssetWriter,
    build_front_matter,
    build_report,
    iter_source_files,
    output_for_batch,
    write_batch_report,
)
from tomd.model import Asset, Document, Status


class TestConvertOptions:
    def test_build_options_ignores_unknown(self):
        options = build_options(nonsense=1, csv_max_rows=7)
        assert options.csv_max_rows == 7
        assert not hasattr(options, "nonsense")

    def test_build_options_skips_none(self):
        options = build_options(csv_max_rows=None)
        assert options.csv_max_rows == 50

    def test_as_dict_is_plain_data(self, tmp_path):
        data = ConvertOptions(output_dir=tmp_path).as_dict()
        assert isinstance(data["output_dir"], str)

    def test_replace_returns_a_copy(self):
        base = ConvertOptions()
        changed = base.replace(report=False)
        assert base.report is True and changed.report is False


class TestDocument:
    def test_warning_downgrades_status(self):
        doc = Document()
        assert doc.status is Status.OK
        doc.warn("x", "something")
        assert doc.status is Status.PARTIAL

    def test_status_only_worsens(self):
        doc = Document(status=Status.FAILED)
        doc.set_status(Status.OK)
        assert doc.status is Status.FAILED

    def test_markdown_joins_parts(self):
        doc = Document()
        doc.add("a", "", "b")
        assert doc.markdown == "a\n\nb\n"

    def test_empty_parts_ignored(self):
        doc = Document()
        doc.add("   ", "\n")
        assert doc.markdown == ""
        assert doc.is_empty()

    def test_collapses_excess_blank_lines(self):
        doc = Document()
        doc.add("a\n\n\n\n\nb")
        assert "\n\n\n" not in doc.markdown


class TestAssetWriter:
    def test_writes_and_maps(self, tmp_path):
        writer = AssetWriter(tmp_path, "assets")
        mapping = writer.write_all([Asset(filename="a.png", data=b"123")])
        assert mapping["a.png"] == "assets/a.png"
        assert (tmp_path / "assets" / "a.png").read_bytes() == b"123"

    def test_identical_content_written_once(self, tmp_path):
        writer = AssetWriter(tmp_path, "assets")
        writer.write_all([Asset(filename="a.png", data=b"same")])
        writer.write_all([Asset(filename="b.png", data=b"same")])
        assert len(list((tmp_path / "assets").iterdir())) == 1

    def test_name_collision_with_different_content_is_disambiguated(self, tmp_path):
        writer = AssetWriter(tmp_path, "assets")
        writer.write_all([Asset(filename="a.png", data=b"one")])
        mapping = writer.write_all([Asset(filename="a.png", data=b"two")])
        assert mapping["a.png"] != "assets/a.png"
        assert len(list((tmp_path / "assets").iterdir())) == 2

    def test_hostile_filename_is_sanitised(self, tmp_path):
        writer = AssetWriter(tmp_path, "assets")
        mapping = writer.write_all([Asset(filename="../../evil.png", data=b"x")])
        assert ".." not in mapping["../../evil.png"]
        assert str(tmp_path.resolve()) in str((tmp_path / "assets" / "evil.png").resolve())

    def test_no_assets_is_a_no_op(self, tmp_path):
        assert AssetWriter(tmp_path).write_all([]) == {}


class TestOutputLayout:
    def test_default_is_dot_md_beside_source(self, tmp_path):
        from tomd.engine import default_output_path

        source = tmp_path / "report.pdf"
        assert default_output_path(source, ConvertOptions()).parent.name == ".md"

    def test_batch_mirrors_tree(self, tmp_path):
        root = tmp_path / "in"
        source = root / "sub" / "deep" / "a.txt"
        options = ConvertOptions(output_dir=tmp_path / "out")
        assert output_for_batch(source, root, options) == tmp_path / "out" / "sub" / "deep" / "a.md"

    def test_batch_flattens_when_asked(self, tmp_path):
        root = tmp_path / "in"
        source = root / "sub" / "a.txt"
        options = ConvertOptions(output_dir=tmp_path / "out", mirror=False)
        assert output_for_batch(source, root, options) == tmp_path / "out" / "a.md"

    def test_batch_target_is_always_a_relative_leaf(self, tmp_path):
        """A hostile relative path cannot climb out of the output directory."""
        from tomd.security import SecurityError

        root = tmp_path / "in"
        source = root / ".." / ".." / "evil.txt"
        with pytest.raises(SecurityError):
            output_for_batch(source, root, ConvertOptions(output_dir=tmp_path / "out"))

    def test_ensure_within_blocks_a_sibling_directory(self, tmp_path):
        from tomd.security import SecurityError, ensure_within

        with pytest.raises(SecurityError):
            ensure_within(tmp_path / "out", tmp_path / "elsewhere" / "a.md")


class TestIterSourceFiles:
    def test_finds_files_recursively(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("b")
        names = [p.name for p in iter_source_files(tmp_path)]
        assert names == ["a.txt", "b.txt"]

    def test_skips_noise_directories(self, tmp_path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "x.js").write_text("x")
        (tmp_path / "keep.txt").write_text("k")
        assert [p.name for p in iter_source_files(tmp_path)] == ["keep.txt"]

    def test_glob_filter(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.md").write_text("b")
        assert [p.name for p in iter_source_files(tmp_path, patterns=["*.md"])] == ["b.md"]

    def test_exclude_filter(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        assert [p.name for p in iter_source_files(tmp_path, exclude=["a.txt"])] == ["b.txt"]

    def test_non_recursive(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("b")
        (tmp_path / "a.txt").write_text("a")
        assert [p.name for p in iter_source_files(tmp_path, recursive=False)] == ["a.txt"]

    def test_deterministic_order(self, tmp_path):
        for name in ("c.txt", "a.txt", "b.txt"):
            (tmp_path / name).write_text("x")
        assert [p.name for p in iter_source_files(tmp_path)] == ["a.txt", "b.txt", "c.txt"]


class TestReport:
    def test_report_lists_warnings(self, fixture, outdir):
        options = ConvertOptions(report=True, front_matter=False)
        result = convert(fixture("scanned.pdf"), options)
        report = build_report(result, options)
        assert "scanned-pdf" in report
        assert "<!-- tomd:report -->" in report
        assert "<!-- /tomd/report -->" not in report

    def test_report_says_clean_when_no_warnings(self, fixture):
        options = ConvertOptions()
        result = convert(fixture("people.csv"), options)
        assert "No issues detected." in build_report(result, options)

    def test_front_matter_records_format_and_status(self, fixture):
        result = convert(fixture("review.html"), ConvertOptions())
        matter = build_front_matter(result, ConvertOptions())
        assert "format: html" in matter
        assert matter.startswith("---")


class TestConvert:
    def test_write_creates_file(self, fixture, outdir):
        result = convert(fixture("people.csv"), ConvertOptions(output_dir=outdir), write=True)
        assert result.written and result.output.exists()
        assert result.output.suffix == ".md"

    def test_no_write_leaves_no_file(self, fixture, outdir):
        result = convert(fixture("people.csv"), ConvertOptions(output_dir=outdir))
        assert result.output is None
        assert not outdir.exists()

    def test_missing_file_fails_cleanly(self, tmp_path):
        result = convert(tmp_path / "nope.txt")
        assert result.status is Status.FAILED
        assert any(w.code == "not-a-file" for w in result.warnings)

    def test_overwrite_false_never_clobbers(self, fixture, outdir):
        options = ConvertOptions(output_dir=outdir)
        first = convert(fixture("people.csv"), options, write=True)
        original = first.output.read_text(encoding="utf-8")
        second = convert(fixture("people.csv"), options, write=True)
        assert second.output != first.output
        assert first.output.read_text(encoding="utf-8") == original

    def test_overwrite_true_replaces(self, fixture, outdir):
        options = ConvertOptions(output_dir=outdir, overwrite=True)
        first = convert(fixture("people.csv"), options, write=True)
        second = convert(fixture("people.csv"), options, write=True)
        assert first.output == second.output

    def test_force_format_overrides_detection(self, fixture, outdir):
        result = convert(fixture("people.csv"), ConvertOptions(force_format="text"))
        assert result.format.confidence == "forced"
        assert result.format.key == "text"

    def test_report_json_sidecar(self, fixture, outdir):
        options = ConvertOptions(output_dir=outdir, report_json=True)
        result = convert(fixture("review.html"), options, write=True)
        sidecar = result.output.with_suffix(".md.json")
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["format"] == "html"
        assert payload["status"] == "ok"

    def test_result_dataclass_is_jsonable(self, fixture):
        result = convert(fixture("report.pdf"))
        json.dumps(result.to_dict())  # must not raise

    def test_render_without_write(self, fixture):
        result = convert(fixture("review.html"))
        assert "Quarterly review" in render(result)

    def test_assets_are_written_and_linked(self, fixture, outdir):
        options = ConvertOptions(output_dir=outdir, extract_images=True)
        result = convert(fixture("diagram.png"), options, write=True)
        assert len(result.assets) == 1
        assert result.assets[0].exists()
        assert "assets/diagram.png" in result.output.read_text(encoding="utf-8")

    def test_assets_skipped_for_stdout_style_runs(self, fixture):
        result = convert(fixture("diagram.png"), ConvertOptions())
        assert result.assets == []


class TestBatch:
    def test_converts_a_tree(self, tmp_path, fixtures):
        root = tmp_path / "in"
        root.mkdir()
        for name in ("people.csv", "review.html", "notes.txt"):
            (root / name).write_bytes((fixtures / name).read_bytes())

        options = ConvertOptions(output_dir=tmp_path / "out")
        summary = convert_many(root, options)
        assert summary.total == 3
        assert summary.failed == 0
        assert (tmp_path / "out" / "people.md").exists()
        assert (tmp_path / "out" / "review.md").exists()

    def test_second_run_skips_existing(self, tmp_path, fixtures):
        root = tmp_path / "in"
        root.mkdir()
        (root / "people.csv").write_bytes((fixtures / "people.csv").read_bytes())
        options = ConvertOptions(output_dir=tmp_path / "out")

        convert_many(root, options)
        summary = convert_many(root, options)
        assert summary.skipped == 1

    def test_overwrite_reruns_everything(self, tmp_path, fixtures):
        root = tmp_path / "in"
        root.mkdir()
        (root / "people.csv").write_bytes((fixtures / "people.csv").read_bytes())
        options = ConvertOptions(output_dir=tmp_path / "out")
        convert_many(root, options)
        summary = convert_many(root, options.replace(overwrite=True))
        assert summary.skipped == 0

    def test_broken_file_does_not_stop_the_batch(self, tmp_path, fixtures):
        root = tmp_path / "in"
        root.mkdir()
        (root / "good.csv").write_bytes((fixtures / "people.csv").read_bytes())
        (root / "broken.pdf").write_bytes(b"%PDF-1.7\ngarbage")
        summary = convert_many(root, ConvertOptions(output_dir=tmp_path / "out"))
        assert summary.total == 2
        assert (tmp_path / "out" / "good.md").exists()

    def test_mirrors_subdirectories(self, tmp_path, fixtures):
        root = tmp_path / "in"
        (root / "a" / "b").mkdir(parents=True)
        (root / "a" / "b" / "n.txt").write_bytes((fixtures / "notes.txt").read_bytes())
        convert_many(root, ConvertOptions(output_dir=tmp_path / "out"))
        assert (tmp_path / "out" / "a" / "b" / "n.md").exists()

    def test_parallel_matches_serial(self, tmp_path, fixtures):
        root = tmp_path / "in"
        root.mkdir()
        for name in ("people.csv", "review.html", "notes.txt", "guide.md"):
            (root / name).write_bytes((fixtures / name).read_bytes())
        serial = convert_many(root, ConvertOptions(output_dir=tmp_path / "s", overwrite=True))
        parallel = convert_many(
            root, ConvertOptions(output_dir=tmp_path / "p", overwrite=True), max_workers=4
        )
        assert serial.total == parallel.total == 4

    def test_on_result_callback_fires_per_file(self, tmp_path, fixtures):
        root = tmp_path / "in"
        root.mkdir()
        (root / "people.csv").write_bytes((fixtures / "people.csv").read_bytes())
        seen = []
        convert_many(root, ConvertOptions(output_dir=tmp_path / "out"), on_result=seen.append)
        assert len(seen) == 1

    def test_batch_report_is_written(self, tmp_path, fixtures):
        root = tmp_path / "in"
        root.mkdir()
        (root / "people.csv").write_bytes((fixtures / "people.csv").read_bytes())
        summary = convert_many(root, ConvertOptions(output_dir=tmp_path / "out"))
        target = write_batch_report(summary, tmp_path / "report.json")
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["total"] == 1

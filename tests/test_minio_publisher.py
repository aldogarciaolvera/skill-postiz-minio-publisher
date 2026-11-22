"""Unit tests for scripts/minio_publisher.py."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import minio_publisher as mp  # noqa: E402


class TestNormalization(unittest.TestCase):
    def test_normalize_slug_accents_spaces_and_symbols(self) -> None:
        self.assertEqual(mp.normalize_slug("GTA VI / Edición Especial!"), "gta-vi-edicion-especial")
        self.assertEqual(mp.normalize_slug("  Mi_Proyecto 2026  "), "mi_proyecto-2026")
        self.assertEqual(mp.normalize_slug("___---"), "item")

    def test_normalize_filename_preserves_lowercase_extension(self) -> None:
        self.assertEqual(mp.normalize_filename("Reel 01 (Final Versión).MOV"), "reel-01-final-version.mov")
        self.assertEqual(mp.normalize_filename("Licencia Música.PDF"), "licencia-musica.pdf")

    def test_normalize_project_path_rejects_traversal(self) -> None:
        self.assertEqual(mp.normalize_project_path(None), "")
        self.assertEqual(mp.normalize_project_path("Mi Canal / Episodio 1"), "mi-canal/episodio-1")
        with self.assertRaises(mp.PublisherError):
            mp.normalize_project_path("../secreto")


class TestPreparationAndValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_file(self, name: str, content: bytes = b"data") -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def test_rejects_unsupported_video_extension(self) -> None:
        webm = self._make_file("clip.webm", b"video")
        with self.assertRaises(mp.PublisherError) as ctx:
            mp.prepare_assets(
                video=str(webm),
                thumbnail=None,
                extras=[],
                project=None,
                alias="minio",
                bucket="postiz",
                base_url="https://s3.atomsystems.org/postiz",
                check_remote=False,
            )
        self.assertEqual(ctx.exception.code, "invalid_extension")

    def test_accepts_allowed_video_thumbnail_and_extras_with_project(self) -> None:
        video = self._make_file("Gameplay Final 01.MKV", b"123456")
        thumb = self._make_file("Miniatura Principal.PNG", b"img")
        timeline = self._make_file("Línea Tiempo.fcpxml", b"<xml/>")
        license_file = self._make_file("Licencia Audio.txt", b"CC-BY")

        plan = mp.prepare_assets(
            video=str(video),
            thumbnail=str(thumb),
            extras=[str(timeline), str(license_file)],
            project="Proyecto GTA VI",
            alias="minio",
            bucket="postiz",
            base_url="https://s3.atomsystems.org/postiz",
            check_remote=False,
        )

        self.assertEqual(plan["destination_mode"], "project_subfolder")
        self.assertEqual(plan["project_normalized"], "proyecto-gta-vi")
        self.assertEqual(
            plan["video"]["mc_target"],
            "minio/postiz/proyecto-gta-vi/gameplay-final-01.mkv",
        )
        self.assertEqual(
            plan["video"]["public_url"],
            "https://s3.atomsystems.org/postiz/proyecto-gta-vi/gameplay-final-01.mkv",
        )
        self.assertTrue(plan["video"]["postiz_attachable"])
        self.assertTrue(plan["thumbnail"]["postiz_attachable"])
        self.assertEqual(len(plan["extras"]), 2)
        self.assertFalse(plan["extras"][0]["postiz_attachable"])
        self.assertEqual(
            plan["extras"][0]["public_url"],
            "https://s3.atomsystems.org/postiz/proyecto-gta-vi/linea-tiempo.fcpxml",
        )

    def test_detects_normalized_filename_collision(self) -> None:
        video = self._make_file("clip-1.mp4", b"video")
        extra_a = self._make_file("nota 1.txt", b"a")
        extra_b = self._make_file("Nota-1.txt", b"b")

        with self.assertRaises(mp.PublisherError) as ctx:
            mp.prepare_assets(
                video=str(video),
                thumbnail=None,
                extras=[str(extra_a), str(extra_b)],
                project="mi-proyecto",
                alias="minio",
                bucket="postiz",
                base_url="https://s3.atomsystems.org/postiz",
                check_remote=False,
            )
        self.assertEqual(ctx.exception.code, "target_collision")

    def test_batch_videos_with_custom_names(self) -> None:
        v1 = self._make_file("Reel_01_ES.mp4", b"v1")
        v2 = self._make_file("Reel_02_ES.mp4", b"v2")

        plan = mp.prepare_assets(
            video=[str(v1), str(v2)],
            thumbnail=None,
            extras=[],
            project="gta-vi/reels-es",
            alias="minio",
            bucket="postiz",
            base_url="https://s3.atomsystems.org/postiz",
            check_remote=False,
            video_names=["reel_01_01_es.mp4", "reel_01_02_es"],
        )

        self.assertEqual(len(plan["videos"]), 2)
        self.assertEqual(
            [v["object_key"] for v in plan["videos"]],
            ["gta-vi/reels-es/reel_01_01_es.mp4", "gta-vi/reels-es/reel_01_02_es.mp4"],
        )

    def test_video_dir_discovers_sorted_reels(self) -> None:
        reels_dir = self.root / "ES"
        reels_dir.mkdir()
        (reels_dir / "Reel_02_ES.mp4").write_bytes(b"v2")
        (reels_dir / "Reel_01_ES.mp4").write_bytes(b"v1")
        (reels_dir / "notes.txt").write_text("ignore", encoding="utf-8")

        plan = mp.prepare_assets(
            video=[],
            video_dirs=[str(reels_dir)],
            thumbnail=None,
            extras=[],
            project="gta-vi/reels-es",
            alias="minio",
            bucket="postiz",
            base_url="https://s3.atomsystems.org/postiz",
            check_remote=False,
        )
        self.assertEqual(
            [v["normalized_name"] for v in plan["videos"]],
            ["reel_01_es.mp4", "reel_02_es.mp4"],
        )

    def test_rejects_long_horizontal_and_oversized_videos(self) -> None:
        clip = self._make_file("Parte1.mp4", b"video")

        with patch.object(
            mp,
            "probe_video",
            return_value={"duration_sec": 5500.0, "width": 1080, "height": 1920},
        ):
            with self.assertRaises(mp.PublisherError) as ctx_dur:
                mp.prepare_assets(
                    video=str(clip),
                    thumbnail=None,
                    extras=[],
                    project=None,
                    alias="minio",
                    bucket="postiz",
                    base_url="https://s3.atomsystems.org/postiz",
                    check_remote=False,
                )
            self.assertEqual(ctx_dur.exception.code, "reel_too_long")

        with patch.object(
            mp,
            "probe_video",
            return_value={"duration_sec": 64.0, "width": 1920, "height": 1080},
        ):
            with self.assertRaises(mp.PublisherError) as ctx_orient:
                mp.prepare_assets(
                    video=str(clip),
                    thumbnail=None,
                    extras=[],
                    project=None,
                    alias="minio",
                    bucket="postiz",
                    base_url="https://s3.atomsystems.org/postiz",
                    check_remote=False,
                )
            self.assertEqual(ctx_orient.exception.code, "reel_not_vertical")

        with self.assertRaises(mp.PublisherError) as ctx_size:
            mp.validate_reel_constraints(
                {
                    "local_path": str(clip),
                    "original_name": "Parte1.mp4",
                    "size_mib": 600.0,
                    "media_probe": None,
                },
                max_duration_sec=180.0,
                max_size_mib=500.0,
                allow_landscape=False,
            )
        self.assertEqual(ctx_size.exception.code, "reel_too_large")


class TestProgressHelpers(unittest.TestCase):
    def test_parse_mc_progress_strips_ansi_and_extracts_metrics(self) -> None:
        raw = "\x1b[32m...Parte1.mp4:\x1b[0m       ============----------------  45.1% 2.7 GiB   2.0 MiB/s"
        parsed = mp.parse_mc_progress(raw)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertAlmostEqual(parsed["percent"], 45.1)
        self.assertEqual(parsed["transferred"], "2.7 GiB")
        self.assertEqual(parsed["speed"], "2.0 MiB/s")

    def test_format_eta_and_progress_bar(self) -> None:
        self.assertEqual(mp.format_eta(50.0, 30.0), "30s")
        self.assertEqual(mp.format_eta(25.0, 60.0), "3m 0s")
        self.assertEqual(mp.format_eta(100.0, 12.0), "0s")
        bar = mp.format_progress_bar("gta-vi/parte1.mp4", 50.0, "3.1 GiB", "10 MiB/s", "5m 0s", width=10)
        self.assertEqual(bar, "[#####-----]  50.0% | 3.1 GiB | 10 MiB/s | ETA 5m 0s | gta-vi/parte1.mp4")


class TestUploadAndTimezone(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_upload_executes_mc_cp_and_verifies(self) -> None:
        video = self.root / "Video Prueba.mp4"
        video.write_bytes(b"12345678")
        thumb = self.root / "Portada.jpg"
        thumb.write_bytes(b"img")

        calls: list[list[str]] = []
        uploaded_targets: set[str] = set()

        def fake_run_mc(args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            cmd = args[0]
            if cmd == "ls":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if cmd == "stat":
                target = args[-1]
                if target in uploaded_targets:
                    size = 8 if target.endswith(".mp4") else 3
                    payload = json.dumps({"status": "success", "size": size, "type": "file"})
                    return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="Object does not exist")
            if cmd == "cp":
                uploaded_targets.add(args[-1])
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            raise AssertionError(f"Unexpected mc args: {args}")

        progress_path = self.root / "progress.json"
        with (
            patch.object(mp, "run_mc", side_effect=fake_run_mc),
            patch.object(mp, "head_url", return_value={"accessible": True, "status_code": 200}),
        ):
            ns = mp.build_parser().parse_args(
                [
                    "upload",
                    "--video",
                    str(video),
                    "--thumbnail",
                    str(thumb),
                    "--project",
                    "Mi Serie",
                    "--quiet",
                    "--progress-file",
                    str(progress_path),
                ]
            )
            res = mp.cmd_upload(ns)

        self.assertEqual(res["video"]["upload_status"], "uploaded")
        self.assertEqual(res["thumbnail"]["upload_status"], "uploaded")
        self.assertEqual(
            res["postiz_attachment_suggestion"],
            {
                "path": "https://s3.atomsystems.org/postiz/mi-serie/video-prueba.mp4",
                "thumbnail": "https://s3.atomsystems.org/postiz/mi-serie/portada.jpg",
            },
        )
        cp_calls = [c for c in calls if c[0] == "cp"]
        self.assertEqual(len(cp_calls), 2)
        progress_data = json.loads(progress_path.read_text(encoding="utf-8"))
        self.assertEqual(progress_data["status"], "done")
        self.assertEqual(progress_data["completed_assets"], 2)
        self.assertEqual(progress_data["total_assets"], 2)
        self.assertEqual([a["status"] for a in progress_data["assets"]], ["uploaded", "uploaded"])
        self.assertEqual(progress_data["assets"][0]["http_status"], 200)
        self.assertTrue(progress_data["recent_events"])

    def test_upload_writes_error_state_to_progress_file_on_failure(self) -> None:
        video = self.root / "Clip Error.mp4"
        video.write_bytes(b"12345678")
        progress_path = self.root / "progress_err.json"

        def fake_run_mc(args: list[str]) -> subprocess.CompletedProcess[str]:
            cmd = args[0]
            if cmd == "ls":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if cmd == "stat":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="Object does not exist")
            if cmd == "cp":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="network error")
            raise AssertionError(f"Unexpected mc args: {args}")

        with patch.object(mp, "run_mc", side_effect=fake_run_mc):
            ns = mp.build_parser().parse_args(
                [
                    "upload",
                    "--video",
                    str(video),
                    "--project",
                    "Mi Serie",
                    "--quiet",
                    "--progress-file",
                    str(progress_path),
                ]
            )
            with self.assertRaises(mp.PublisherError) as ctx:
                mp.cmd_upload(ns)
        self.assertEqual(ctx.exception.code, "mc_cp_failed")
        progress_data = json.loads(progress_path.read_text(encoding="utf-8"))
        self.assertEqual(progress_data["status"], "error")
        self.assertEqual(progress_data["assets"][0]["status"], "error")
        self.assertIn("network error", progress_data["error"])

    def test_verify_succeeds_and_detects_missing_or_mismatched_assets(self) -> None:
        video = self.root / "Reel_01_EN.mp4"
        video.write_bytes(b"12345678")
        remote_sizes: dict[str, int] = {"minio/postiz/gta-vi/reel_01_en.mp4": 8}

        def fake_run_mc(args: list[str]) -> subprocess.CompletedProcess[str]:
            cmd = args[0]
            if cmd == "ls":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if cmd == "stat":
                target = args[-1]
                if target in remote_sizes:
                    payload = json.dumps({"status": "success", "size": remote_sizes[target], "type": "file"})
                    return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="Object does not exist")
            raise AssertionError(f"Unexpected mc args: {args}")

        ns = mp.build_parser().parse_args(["verify", "--video", str(video), "--project", "gta-vi"])
        with (
            patch.object(mp, "run_mc", side_effect=fake_run_mc),
            patch.object(mp, "head_url", return_value={"accessible": True, "status_code": 200}),
        ):
            res = mp.cmd_verify(ns)

        self.assertTrue(res["all_verified"])
        self.assertTrue(res["all_http_accessible"])
        self.assertEqual(res["video"]["upload_status"], "verified")
        self.assertEqual(
            res["postiz_attachment_suggestion"],
            "https://s3.atomsystems.org/postiz/gta-vi/reel_01_en.mp4",
        )

        remote_sizes["minio/postiz/gta-vi/reel_01_en.mp4"] = 4
        with patch.object(mp, "run_mc", side_effect=fake_run_mc):
            with self.assertRaises(mp.PublisherError) as ctx_size:
                mp.cmd_verify(ns)
        self.assertEqual(ctx_size.exception.code, "size_mismatch")

        remote_sizes.clear()
        with patch.object(mp, "run_mc", side_effect=fake_run_mc):
            with self.assertRaises(mp.PublisherError) as ctx_missing:
                mp.cmd_verify(ns)
        self.assertEqual(ctx_missing.exception.code, "remote_not_found")

    def test_watch_once_and_render_snapshot(self) -> None:
        progress_path = self.root / "watch.json"
        state = {
            "pid": 1234,
            "status": "uploading",
            "mc_folder": "minio/postiz/gta-vi/reels-en/",
            "total_assets": 2,
            "completed_assets": 1,
            "percent": 75.0,
            "total_mib": 200.0,
            "updated_at": "2026-11-22T06:45:00-06:00",
            "assets": [
                {
                    "index": 1,
                    "name": "reel_01_01_en.mp4",
                    "size_mib": 100.0,
                    "status": "uploaded",
                    "percent": 100.0,
                    "http_status": 200,
                    "public_url": "https://s3.atomsystems.org/postiz/gta-vi/reels-en/reel_01_01_en.mp4",
                },
                {
                    "index": 2,
                    "name": "reel_01_02_en.mp4",
                    "size_mib": 100.0,
                    "status": "uploading",
                    "percent": 50.0,
                    "transferred": "50.0 MiB",
                    "speed": "2.5 MiB/s",
                    "eta": "20s",
                    "public_url": "https://s3.atomsystems.org/postiz/gta-vi/reels-en/reel_01_02_en.mp4",
                },
            ],
            "recent_events": ["[06:45:00] Subido: reel_01_01_en.mp4 (HTTP 200)"],
            "error": None,
        }
        mp.write_progress_file(progress_path, state)

        rendered = mp.render_watch_snapshot(state)
        self.assertIn("SUBIENDO", rendered)
        self.assertIn("[OK]", rendered)
        self.assertIn("[SUBIENDO]", rendered)
        self.assertIn("50.0% | 50.0 MiB | 2.5 MiB/s | ETA 20s", rendered)

        stale_rendered = mp.render_watch_snapshot(state, stale_process=True)
        self.assertIn("INTERRUMPIDO", stale_rendered)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mp.main(["watch", "--progress-file", str(progress_path), "--once"])
        self.assertEqual(rc, 0)
        self.assertIn("Monitor de Subida", buf.getvalue())

        err_buf = io.StringIO()
        with contextlib.redirect_stdout(err_buf):
            missing_rc = mp.main(["watch", "--progress-file", str(self.root / "missing.json"), "--once"])
        self.assertEqual(missing_rc, 1)
        self.assertIn("progress_file_not_found", err_buf.getvalue())

        self.assertTrue(mp.is_pid_running(os.getpid()))
        self.assertFalse(mp.is_pid_running(0))

    def test_to_utc_explicit_offset(self) -> None:
        ns = mp.build_parser().parse_args(["to-utc", "--local-datetime", "2026-11-22T18:30:00-06:00"])
        res = mp.cmd_to_utc(ns)
        self.assertEqual(res["utc_iso"], "2026-11-23T00:30:00Z")
        self.assertEqual(res["utc_no_z"], "2026-11-23T00:30:00")


class TestBuildDescription(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_chapters_first_blank_line_then_credits_without_full_links(self) -> None:
        chapters = self.root / "chapters1.txt"
        chapters.write_text("00:00 Intro\n01:14 Huida\n", encoding="utf-8")
        credits_file = self.root / "youtube_credits.txt"
        credits_file.write_text(
            "Music Credits:\n- Track by Artist (freetouse.com) | License: https://freetouse.com/license\n",
            encoding="utf-8",
        )

        res = mp.build_description_payload(chapters_file=str(chapters), credits_file=str(credits_file))
        self.assertEqual(
            res["plain_text"],
            "00:00 Intro\n"
            "01:14 Huida\n"
            "\n"
            "Music Credits:\n"
            "- Track by Artist (freetouse.com) | License: https://freetouse .com/license",
        )
        self.assertEqual(
            res["html_content"],
            "<p>00:00 Intro</p>"
            "<p>01:14 Huida</p>"
            "<p></p>"
            "<p>Music Credits:</p>"
            "<p>- Track by Artist (freetouse.com) | License: https://freetouse .com/license</p>",
        )
        self.assertTrue(res["within_youtube_limit"])

    def test_keep_full_links_flag_preserves_urls(self) -> None:
        credits_file = self.root / "credits.txt"
        credits_file.write_text("License: https://freetouse.com/license\n", encoding="utf-8")
        res = mp.build_description_payload(credits_file=str(credits_file), keep_full_links=True)
        self.assertEqual(res["plain_text"], "License: https://freetouse.com/license")


if __name__ == "__main__":
    unittest.main()

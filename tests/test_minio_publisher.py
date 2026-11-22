"""Unit tests for scripts/minio_publisher.py."""

from __future__ import annotations

import json
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
                uploaded_targets.add(args[2])
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            raise AssertionError(f"Unexpected mc args: {args}")

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

    def test_to_utc_explicit_offset(self) -> None:
        ns = mp.build_parser().parse_args(["to-utc", "--local-datetime", "2026-11-22T18:30:00-06:00"])
        res = mp.cmd_to_utc(ns)
        self.assertEqual(res["utc_iso"], "2026-11-23T00:30:00Z")
        self.assertEqual(res["utc_no_z"], "2026-11-23T00:30:00")


if __name__ == "__main__":
    unittest.main()

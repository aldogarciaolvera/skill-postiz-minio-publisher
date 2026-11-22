# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""CLI helper for validating, normalizing, and uploading video assets to MinIO for Postiz."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_ALIAS = "minio"
DEFAULT_BUCKET = "postiz"
DEFAULT_BASE_URL = "https://s3.atomsystems.org/postiz"
DEFAULT_PROGRESS_FILE = Path(tempfile.gettempdir()) / "postiz_upload_progress.json"

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv"}
ALLOWED_THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

DEFAULT_MAX_REEL_DURATION_SEC = 180.0
DEFAULT_MAX_REEL_SIZE_MIB = 500.0

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MC_PROGRESS_RE = re.compile(
    r"(\d+(?:\.\d+)?)%\s+([0-9.]+\s*[KMGT]?i?B)\s+([0-9.]+\s*[KMGT]?i?B/s)"
)


class PublisherError(Exception):
    """Domain error with structured JSON exit code."""

    def __init__(self, message: str, code: str = "error", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


def normalize_slug(text: str, default: str = "item") -> str:
    """Normalize arbitrary text into a lowercase, ASCII, URL-safe slug."""
    decomposed = unicodedata.normalize("NFKD", text.strip())
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    collapsed = re.sub(r"[^a-z0-9_-]+", "-", ascii_only)
    collapsed = re.sub(r"-{2,}", "-", collapsed).strip("._-")
    return collapsed or default


def normalize_filename(filename: str) -> str:
    """Normalize a filename stem while preserving its lowercase extension."""
    path = Path(filename)
    suffix = path.suffix.lower()
    stem = normalize_slug(path.stem, default="archivo")
    return f"{stem}{suffix}"


def normalize_project_path(project: str | None) -> str:
    """Normalize an optional project folder path into safe S3 key segments."""
    if not project or not project.strip():
        return ""
    raw_segments = [seg for seg in re.split(r"[/\\]+", project.strip()) if seg and seg != "."]
    if any(seg == ".." for seg in raw_segments):
        raise PublisherError("Project path cannot contain '..' segments.", code="invalid_project")
    normalized = [normalize_slug(seg, default="proyecto") for seg in raw_segments]
    return "/".join(normalized)


def find_mc_executable() -> str:
    """Locate mc.exe or mc on PATH."""
    mc_path = shutil.which("mc.exe") or shutil.which("mc")
    if not mc_path:
        raise PublisherError(
            "MinIO client (mc.exe / mc) was not found in PATH.",
            code="mc_not_found",
        )
    return mc_path


def run_mc(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Execute an mc command with UTF-8 output capture."""
    mc_exe = find_mc_executable()
    return subprocess.run(
        [mc_exe, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def parse_ndjson(raw: str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON output emitted by mc --json."""
    items: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            items.append(parsed)
    return items


def mc_stat(mc_target: str) -> dict[str, Any] | None:
    """Return object metadata from mc stat --json, or None when absent."""
    proc = run_mc(["stat", "--json", mc_target])
    if proc.returncode != 0:
        return None
    records = parse_ndjson(proc.stdout)
    for rec in records:
        if rec.get("status") == "success":
            return rec
    return None


def mc_list_entries(mc_prefix: str) -> list[dict[str, Any]]:
    """List objects and folders under an mc prefix using mc ls --json."""
    target = mc_prefix if mc_prefix.endswith("/") else f"{mc_prefix}/"
    proc = run_mc(["ls", "--json", target])
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout).strip()
        if "does not exist" in stderr.lower() or "not found" in stderr.lower():
            return []
        raise PublisherError(
            f"Failed to list MinIO path '{target}': {stderr or 'unknown error'}",
            code="mc_ls_failed",
        )
    return [rec for rec in parse_ndjson(proc.stdout) if rec.get("status") == "success"]


def probe_video(video_path: Path) -> dict[str, Any] | None:
    """Return basic video stream metadata via ffprobe when available."""
    ffprobe = shutil.which("ffprobe.exe") or shutil.which("ffprobe")
    if not ffprobe:
        return None
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    fmt = data.get("format") or {}
    video_stream = next(
        (s for s in data.get("streams") or [] if s.get("codec_type") == "video"),
        {},
    )
    audio_stream = next(
        (s for s in data.get("streams") or [] if s.get("codec_type") == "audio"),
        {},
    )
    duration_raw = fmt.get("duration")
    return {
        "duration_sec": round(float(duration_raw), 2) if duration_raw else None,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "video_codec": video_stream.get("codec_name"),
        "audio_codec": audio_stream.get("codec_name"),
    }


def head_url(url: str, timeout: float = 10.0) -> dict[str, Any]:
    """Verify public HTTP(S) accessibility of an uploaded MinIO URL."""
    req = Request(
        url,
        method="HEAD",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) postiz-minio-publisher/1.0"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return {
                "accessible": 200 <= resp.status < 300,
                "status_code": resp.status,
                "content_type": resp.headers.get("Content-Type"),
                "content_length": resp.headers.get("Content-Length"),
            }
    except HTTPError as exc:
        return {"accessible": False, "status_code": exc.code, "error": str(exc)}
    except URLError as exc:
        return {"accessible": False, "status_code": None, "error": str(exc.reason)}


def validate_local_file(raw_path: str, role: str, allowed_extensions: set[str] | None) -> Path:
    """Validate that a local file exists, is non-empty, and matches allowed extensions."""
    path = Path(raw_path).expanduser()
    if not path.exists() or not path.is_file():
        raise PublisherError(
            f"{role} file does not exist: {raw_path}",
            code="file_not_found",
            details={"role": role, "path": raw_path},
        )
    size = path.stat().st_size
    if size <= 0:
        raise PublisherError(
            f"{role} file is empty (0 bytes): {raw_path}",
            code="empty_file",
            details={"role": role, "path": raw_path},
        )
    suffix = path.suffix.lower()
    if allowed_extensions is not None and suffix not in allowed_extensions:
        raise PublisherError(
            f"Unsupported {role} extension '{suffix or '<none>'}'. Allowed: {sorted(allowed_extensions)}",
            code="invalid_extension",
            details={"role": role, "path": str(path), "extension": suffix, "allowed": sorted(allowed_extensions)},
        )
    return path.resolve()


def resolve_video_target_name(local_path: Path, custom_name: str | None) -> str:
    """Compute normalized filename for a video, preserving or validating its extension."""
    if not custom_name or not custom_name.strip():
        return normalize_filename(local_path.name)
    candidate = Path(custom_name.strip())
    local_suffix = local_path.suffix.lower()
    if candidate.suffix:
        if candidate.suffix.lower() != local_suffix:
            raise PublisherError(
                f"Custom video name '{custom_name}' extension '{candidate.suffix.lower()}' does not match '{local_suffix}'.",
                code="invalid_extension",
                details={"custom_name": custom_name, "expected_extension": local_suffix},
            )
        return normalize_filename(candidate.name)
    return normalize_filename(f"{candidate.name}{local_suffix}")


def build_asset_plan(
    local_path: Path,
    role: str,
    project: str,
    alias: str,
    bucket: str,
    base_url: str,
    check_remote: bool,
    normalized_name_override: str | None = None,
) -> dict[str, Any]:
    """Build normalized target path, S3 target, and public URL for a single asset."""
    normalized_name = normalized_name_override or normalize_filename(local_path.name)
    object_key = f"{project}/{normalized_name}" if project else normalized_name
    mc_target = f"{alias}/{bucket}/{object_key}"
    public_url = f"{base_url.rstrip('/')}/{object_key}"
    local_size = local_path.stat().st_size

    remote_stat = mc_stat(mc_target) if check_remote else None
    return {
        "role": role,
        "local_path": str(local_path),
        "original_name": local_path.name,
        "normalized_name": normalized_name,
        "size_bytes": local_size,
        "size_mib": round(local_size / (1024 * 1024), 2),
        "object_key": object_key,
        "mc_target": mc_target,
        "public_url": public_url,
        "postiz_attachable": role in {"video", "thumbnail"},
        "exists_in_minio": remote_stat is not None,
        "remote_size_bytes": remote_stat.get("size") if remote_stat else None,
        "same_size_as_local": (remote_stat is not None and remote_stat.get("size") == local_size),
    }


def collect_video_inputs(videos: list[str], video_dirs: list[str] | None = None) -> list[str]:
    """Combine explicit --video paths and sorted video files discovered in --video-dir folders."""
    collected = list(videos)
    for raw_dir in video_dirs or []:
        dir_path = Path(raw_dir).expanduser()
        if not dir_path.exists() or not dir_path.is_dir():
            raise PublisherError(
                f"Video directory does not exist: {raw_dir}",
                code="dir_not_found",
                details={"path": raw_dir},
            )
        found = sorted(
            (
                p
                for p in dir_path.iterdir()
                if p.is_file() and p.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS
            ),
            key=lambda p: p.name.lower(),
        )
        if not found:
            raise PublisherError(
                f"No supported Reel video files ({sorted(ALLOWED_VIDEO_EXTENSIONS)}) found in: {raw_dir}",
                code="empty_video_dir",
                details={"path": raw_dir},
            )
        collected.extend(str(p) for p in found)
    return collected


def validate_reel_constraints(
    video_asset: dict[str, Any],
    max_duration_sec: float | None,
    max_size_mib: float | None,
    allow_landscape: bool,
) -> None:
    """Enforce Reels-only constraints (duration, vertical aspect ratio, and size)."""
    path_str = video_asset["local_path"]
    size_mib = float(video_asset["size_mib"])
    if max_size_mib is not None and max_size_mib > 0 and size_mib > max_size_mib:
        raise PublisherError(
            f"Reel '{video_asset['original_name']}' ({size_mib:.2f} MiB) exceeds max Reel size ({max_size_mib:.0f} MiB). Long videos are blocked by this Reels-only skill.",
            code="reel_too_large",
            details={"path": path_str, "size_mib": size_mib, "max_size_mib": max_size_mib},
        )

    probe = video_asset.get("media_probe")
    if not probe:
        return

    duration = probe.get("duration_sec")
    if max_duration_sec is not None and max_duration_sec > 0 and duration is not None and duration > max_duration_sec:
        raise PublisherError(
            f"Reel '{video_asset['original_name']}' duration ({duration:.2f}s) exceeds max Reel duration ({max_duration_sec:.0f}s). Long videos are blocked by this Reels-only skill.",
            code="reel_too_long",
            details={"path": path_str, "duration_sec": duration, "max_duration_sec": max_duration_sec},
        )

    width = probe.get("width")
    height = probe.get("height")
    if not allow_landscape and width and height and width > height:
        raise PublisherError(
            f"Video '{video_asset['original_name']}' is landscape ({width}x{height}). This skill only accepts vertical Reels/Shorts (height >= width).",
            code="reel_not_vertical",
            details={"path": path_str, "width": width, "height": height},
        )


def prepare_assets(
    video: str | list[str],
    thumbnail: str | None,
    extras: list[str],
    project: str | None,
    alias: str,
    bucket: str,
    base_url: str,
    check_remote: bool = True,
    video_names: list[str] | None = None,
    video_dirs: list[str] | None = None,
    max_duration_sec: float | None = DEFAULT_MAX_REEL_DURATION_SEC,
    max_size_mib: float | None = DEFAULT_MAX_REEL_SIZE_MIB,
    allow_landscape: bool = False,
) -> dict[str, Any]:
    """Validate and prepare normalized MinIO upload targets for Reels-only assets."""
    normalized_project = normalize_project_path(project)
    bucket_prefix = f"{alias}/{bucket}"

    raw_videos = [video] if isinstance(video, str) else list(video)
    video_list = collect_video_inputs(raw_videos, video_dirs)
    if not video_list:
        raise PublisherError("At least one --video or --video-dir path is required.", code="missing_video")

    names_list = list(video_names or [])
    if names_list and len(names_list) != len(video_list):
        raise PublisherError(
            f"Expected {len(video_list)} --name override(s) to match video count ({len(video_list)}), got {len(names_list)}.",
            code="invalid_video_names",
        )

    validated_inputs: list[tuple[str, Path, str | None]] = []
    for idx, raw_video in enumerate(video_list):
        v_path = validate_local_file(raw_video, "video", ALLOWED_VIDEO_EXTENSIONS)
        custom = names_list[idx] if names_list else None
        validated_inputs.append(("video", v_path, resolve_video_target_name(v_path, custom)))

    if thumbnail:
        validated_inputs.append(
            ("thumbnail", validate_local_file(thumbnail, "thumbnail", ALLOWED_THUMBNAIL_EXTENSIONS), None)
        )
    for extra in extras:
        validated_inputs.append(("extra", validate_local_file(extra, "extra", None), None))

    assets = [
        build_asset_plan(
            path,
            role,
            normalized_project,
            alias,
            bucket,
            base_url,
            check_remote,
            normalized_name_override=override,
        )
        for role, path, override in validated_inputs
    ]

    seen_keys: dict[str, str] = {}
    for asset in assets:
        key = asset["object_key"]
        if key in seen_keys:
            raise PublisherError(
                f"Filename normalization collision on '{key}' between '{seen_keys[key]}' and '{asset['local_path']}'.",
                code="target_collision",
                details={"object_key": key},
            )
        seen_keys[key] = asset["local_path"]

    video_assets = [a for a in assets if a["role"] == "video"]
    for va in video_assets:
        va["media_probe"] = probe_video(Path(va["local_path"]))
        validate_reel_constraints(va, max_duration_sec, max_size_mib, allow_landscape)

    project_exists: bool | None = None
    if normalized_project and check_remote:
        entries = mc_list_entries(f"{bucket_prefix}/{normalized_project}/")
        project_exists = len(entries) > 0

    thumb_asset = next((a for a in assets if a["role"] == "thumbnail"), None)
    extra_assets = [a for a in assets if a["role"] == "extra"]

    return {
        "alias": alias,
        "bucket": bucket,
        "destination_mode": "project_subfolder" if normalized_project else "bucket_root",
        "project_input": project or "",
        "project_normalized": normalized_project,
        "project_exists": project_exists,
        "mc_folder": f"{bucket_prefix}/{normalized_project}/" if normalized_project else f"{bucket_prefix}/",
        "video": video_assets[0],
        "videos": video_assets,
        "thumbnail": thumb_asset,
        "extras": extra_assets,
        "assets": assets,
    }


def cmd_list_projects(args: argparse.Namespace) -> dict[str, Any]:
    """List existing project folders and root files in minio/postiz."""
    entries = mc_list_entries(f"{args.alias}/{args.bucket}/")
    projects: list[str] = []
    root_files: list[dict[str, Any]] = []
    for entry in entries:
        key = str(entry.get("key") or "")
        if entry.get("type") == "folder" or key.endswith("/"):
            folder_name = key.rstrip("/")
            if folder_name:
                projects.append(folder_name)
        else:
            root_files.append(
                {
                    "key": key,
                    "size_bytes": entry.get("size"),
                    "last_modified": entry.get("lastModified"),
                }
            )
    return {
        "alias": args.alias,
        "bucket": args.bucket,
        "projects": sorted(set(projects)),
        "root_files": root_files,
    }


def prepare_assets_from_args(args: argparse.Namespace, check_remote: bool) -> dict[str, Any]:
    """Build normalized asset plan from shared CLI arguments."""
    return prepare_assets(
        video=args.video,
        thumbnail=args.thumbnail,
        extras=args.extra or [],
        project=args.project,
        alias=args.alias,
        bucket=args.bucket,
        base_url=args.base_url,
        check_remote=check_remote,
        video_names=args.name,
        video_dirs=args.video_dir,
        max_duration_sec=args.max_duration or None,
        max_size_mib=args.max_size_mib or None,
        allow_landscape=args.allow_landscape,
    )


def cmd_prepare(args: argparse.Namespace) -> dict[str, Any]:
    """Validate local Reels and inspect normalized MinIO targets without uploading."""
    return prepare_assets_from_args(args, check_remote=not args.no_remote_check)


def parse_mc_progress(raw_line: str) -> dict[str, Any] | None:
    """Extract percentage, transferred bytes, and speed from an mc cp progress line."""
    clean = ANSI_ESCAPE_RE.sub("", raw_line).strip()
    match = MC_PROGRESS_RE.search(clean)
    if not match:
        return None
    return {
        "percent": float(match.group(1)),
        "transferred": re.sub(r"\s+", " ", match.group(2).strip()),
        "speed": re.sub(r"\s+", " ", match.group(3).strip()),
    }


def format_eta(percent: float, elapsed_sec: float) -> str:
    """Estimate remaining time from elapsed duration and completion percentage."""
    if percent >= 100.0:
        return "0s"
    if percent <= 0.1 or elapsed_sec < 1.0:
        return "calculando..."
    remaining = max(0, int(round(elapsed_sec * (100.0 - percent) / percent)))
    hours, rem = divmod(remaining, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def format_progress_bar(
    label: str,
    percent: float,
    transferred: str,
    speed: str,
    eta: str,
    width: int = 20,
) -> str:
    """Format a single-line ASCII progress bar for stderr logging."""
    clamped = max(0.0, min(100.0, percent))
    filled = int(round((clamped / 100.0) * width))
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {clamped:5.1f}% | {transferred} | {speed} | ETA {eta} | {label}"


def write_progress_file(progress_file: Path | None, payload: dict[str, Any]) -> None:
    """Best-effort atomic write of JSON progress state for external monitors."""
    if progress_file is None:
        return
    tmp_path = progress_file.with_suffix(progress_file.suffix + ".tmp")
    encoded = json.dumps(payload, ensure_ascii=False)
    try:
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(5):
            try:
                tmp_path.write_text(encoded, encoding="utf-8")
                tmp_path.replace(progress_file)
                return
            except OSError:
                if attempt < 4:
                    time.sleep(0.02)
    except OSError:
        pass
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def read_progress_file(progress_file: Path) -> dict[str, Any] | None:
    """Read JSON progress state safely even during concurrent atomic writes."""
    if not progress_file.exists():
        return None
    for attempt in range(4):
        try:
            raw = progress_file.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (OSError, json.JSONDecodeError):
            if attempt < 3:
                time.sleep(0.02)
    return None


class BatchProgressTracker:
    """Maintains rich per-asset upload progress and persists snapshots to disk."""

    def __init__(self, plan: dict[str, Any], progress_file: Path | None) -> None:
        self.progress_file = progress_file
        assets = plan["assets"]
        total_bytes = sum(int(a["size_bytes"]) for a in assets)
        now_iso = datetime.now().astimezone().replace(microsecond=0).isoformat()
        self.state: dict[str, Any] = {
            "pid": os.getpid(),
            "status": "uploading",
            "mc_folder": plan["mc_folder"],
            "project_normalized": plan["project_normalized"],
            "total_assets": len(assets),
            "completed_assets": 0,
            "current_index": 1 if assets else None,
            "percent": 0.0,
            "total_bytes": total_bytes,
            "total_mib": round(total_bytes / (1024 * 1024), 2),
            "asset_index": 1 if assets else None,
            "label": assets[0]["object_key"] if assets else "",
            "transferred": "0 B",
            "speed": "0 B/s",
            "eta": "calculando...",
            "bar": "",
            "started_at": now_iso,
            "updated_at": now_iso,
            "assets": [
                {
                    "index": idx,
                    "role": a["role"],
                    "name": a["normalized_name"],
                    "object_key": a["object_key"],
                    "size_bytes": a["size_bytes"],
                    "size_mib": a["size_mib"],
                    "public_url": a["public_url"],
                    "status": "pending",
                    "percent": 0.0,
                    "transferred": "0 B",
                    "speed": "0 B/s",
                    "eta": "pendiente",
                    "http_status": None,
                    "http_accessible": None,
                    "error": None,
                }
                for idx, a in enumerate(assets, start=1)
            ],
            "recent_events": [],
            "error": None,
        }
        self._flush()

    def _compute_overall_percent(self) -> float:
        total_bytes = int(self.state["total_bytes"])
        items = self.state["assets"]
        if not items:
            return 100.0
        if total_bytes > 0:
            weighted = sum(
                float(item["size_bytes"]) * (max(0.0, min(100.0, float(item["percent"]))) / 100.0)
                for item in items
            )
            return round((weighted / total_bytes) * 100.0, 1)
        avg = sum(max(0.0, min(100.0, float(item["percent"]))) for item in items) / len(items)
        return round(avg, 1)

    def _record_event(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        events: list[str] = self.state["recent_events"]
        events.append(f"[{stamp}] {message}")
        if len(events) > 8:
            del events[0 : len(events) - 8]

    def _flush(self) -> None:
        self.state["updated_at"] = datetime.now().astimezone().replace(microsecond=0).isoformat()
        self.state["percent"] = self._compute_overall_percent()
        write_progress_file(self.progress_file, self.state)

    def start_asset(self, asset_index: int) -> None:
        item = self.state["assets"][asset_index - 1]
        item["status"] = "uploading"
        item["percent"] = 0.0
        item["transferred"] = "0 B"
        item["speed"] = "0 B/s"
        item["eta"] = "calculando..."
        self.state["current_index"] = asset_index
        self.state["asset_index"] = asset_index
        self.state["label"] = item["object_key"]
        self.state["transferred"] = "0 B"
        self.state["speed"] = "0 B/s"
        self.state["eta"] = "calculando..."
        self.state["bar"] = format_progress_bar(item["object_key"], 0.0, "0 B", "0 B/s", "calculando...")
        self._record_event(f"[{asset_index}/{self.state['total_assets']}] Iniciando {item['object_key']} ({item['size_mib']:.2f} MiB)")
        self._flush()

    def update_asset_progress(
        self,
        asset_index: int,
        percent: float,
        transferred: str,
        speed: str,
        eta: str,
        bar: str,
    ) -> None:
        item = self.state["assets"][asset_index - 1]
        item["status"] = "uploading"
        item["percent"] = percent
        item["transferred"] = transferred
        item["speed"] = speed
        item["eta"] = eta
        self.state["current_index"] = asset_index
        self.state["asset_index"] = asset_index
        self.state["label"] = item["object_key"]
        self.state["transferred"] = transferred
        self.state["speed"] = speed
        self.state["eta"] = eta
        self.state["bar"] = bar
        self._flush()

    def set_asset_verifying(self, asset_index: int) -> None:
        item = self.state["assets"][asset_index - 1]
        item["status"] = "verifying"
        item["percent"] = 100.0
        item["eta"] = "verificando..."
        self._record_event(f"[{asset_index}/{self.state['total_assets']}] Verificando {item['object_key']} en MinIO/HTTP...")
        self._flush()

    def finish_asset(self, asset_index: int, result: dict[str, Any]) -> None:
        item = self.state["assets"][asset_index - 1]
        status = str(result.get("upload_status") or "uploaded")
        http_check = result.get("http_check") or {}
        item["status"] = status
        item["percent"] = 100.0
        item["eta"] = "0s"
        item["http_status"] = http_check.get("status_code")
        item["http_accessible"] = http_check.get("accessible")
        self.state["completed_assets"] = sum(
            1 for a in self.state["assets"] if a["status"] in {"uploaded", "skipped_existing"}
        )
        http_note = f"HTTP {item['http_status']}" if item["http_status"] is not None else "sin HTTP HEAD"
        action_note = "Omitido (ya existía)" if status == "skipped_existing" else "Subido"
        self._record_event(f"[{asset_index}/{self.state['total_assets']}] {action_note}: {item['object_key']} ({http_note})")
        self._flush()

    def fail(self, message: str, asset_index: int | None = None) -> None:
        self.state["status"] = "error"
        self.state["error"] = message
        if asset_index is not None and 1 <= asset_index <= len(self.state["assets"]):
            item = self.state["assets"][asset_index - 1]
            item["status"] = "error"
            item["error"] = message
        self._record_event(f"ERROR: {message}")
        self._flush()

    def finish_all(self) -> None:
        self.state["status"] = "done"
        self.state["current_index"] = None
        self.state["eta"] = "0s"
        self._record_event(f"Subida finalizada ({self.state['completed_assets']}/{self.state['total_assets']} activos).")
        self._flush()


def _legacy_progress_payload(
    asset_index: int,
    total_assets: int,
    label: str,
    total_mib: float,
    percent: float,
    transferred: str,
    speed: str,
    eta: str,
) -> dict[str, Any]:
    """Build single-asset progress JSON for direct run_mc_cp callers without BatchProgressTracker."""
    return {
        "pid": os.getpid(),
        "status": "uploading",
        "asset_index": asset_index,
        "total_assets": total_assets,
        "label": label,
        "total_mib": total_mib,
        "percent": percent,
        "transferred": transferred,
        "speed": speed,
        "eta": eta,
        "bar": format_progress_bar(label, percent, transferred, speed, eta),
    }


def run_mc_cp(
    local_path: str,
    mc_target: str,
    label: str,
    total_mib: float,
    quiet: bool = False,
    progress_file: Path | None = None,
    asset_index: int = 1,
    total_assets: int = 1,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Execute mc cp with optional throttled progress streaming to stderr and progress callbacks."""
    if quiet:
        proc = run_mc(["cp", "-q", local_path, mc_target])
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            raise PublisherError(
                f"mc cp failed for '{local_path}' -> '{mc_target}': {err}",
                code="mc_cp_failed",
                details={"mc_target": mc_target},
            )
        return

    mc_exe = find_mc_executable()
    started = time.monotonic()
    last_stderr_emit = 0.0
    last_stderr_pct = -10.0
    last_cb_emit = 0.0
    last_cb_pct = -10.0
    stderr_interval = 1.5 if sys.stderr.isatty() else 5.0
    error_lines: list[str] = []

    sys.stderr.write(f"[upload {asset_index}/{total_assets}] Iniciando {label} ({total_mib:.2f} MiB)\n")
    sys.stderr.flush()
    if on_progress is None and progress_file is not None:
        write_progress_file(
            progress_file,
            _legacy_progress_payload(
                asset_index,
                total_assets,
                label,
                total_mib,
                0.0,
                "0 MiB",
                "0 MiB/s",
                "calculando...",
            ),
        )

    proc = subprocess.Popen(
        [mc_exe, "cp", local_path, mc_target],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    buf = b""

    def handle_segment(raw_bytes: bytes) -> None:
        nonlocal last_stderr_emit, last_stderr_pct, last_cb_emit, last_cb_pct
        text = ANSI_ESCAPE_RE.sub("", raw_bytes.decode("utf-8", errors="replace")).strip()
        if not text:
            return
        parsed = parse_mc_progress(text)
        if parsed is None:
            if not re.search(r"\b\d+\.\d+%\s+", text):
                error_lines.append(text)
                if len(error_lines) > 20:
                    del error_lines[0]
            return

        now = time.monotonic()
        pct = parsed["percent"]
        should_emit_cb = now - last_cb_emit >= 0.5 or pct - last_cb_pct >= 1.0 or pct >= 100.0
        should_emit_stderr = now - last_stderr_emit >= stderr_interval or pct - last_stderr_pct >= 5.0 or pct >= 100.0
        if not should_emit_cb and not should_emit_stderr:
            return

        eta = format_eta(pct, now - started)
        bar_line = format_progress_bar(
            label,
            pct,
            parsed["transferred"],
            parsed["speed"],
            eta,
        )

        if should_emit_cb:
            if on_progress is not None:
                on_progress(
                    {
                        "percent": pct,
                        "transferred": parsed["transferred"],
                        "speed": parsed["speed"],
                        "eta": eta,
                        "bar": bar_line,
                    }
                )
            elif progress_file is not None:
                write_progress_file(
                    progress_file,
                    _legacy_progress_payload(
                        asset_index,
                        total_assets,
                        label,
                        total_mib,
                        pct,
                        parsed["transferred"],
                        parsed["speed"],
                        eta,
                    ),
                )
            last_cb_emit = now
            last_cb_pct = pct

        if should_emit_stderr:
            sys.stderr.write(f"[upload {asset_index}/{total_assets}] {bar_line}\n")
            sys.stderr.flush()
            last_stderr_emit = now
            last_stderr_pct = pct

    while True:
        chunk = os.read(fd, 1024)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]+", buf)
        buf = parts.pop()
        for part in parts:
            handle_segment(part)

    if buf:
        handle_segment(buf)

    returncode = proc.wait()
    if returncode != 0:
        err_msg = " | ".join(error_lines) if error_lines else f"exit code {returncode}"
        if on_progress is None and progress_file is not None:
            write_progress_file(
                progress_file,
                {
                    "pid": os.getpid(),
                    "status": "error",
                    "label": label,
                    "error": err_msg,
                },
            )
        raise PublisherError(
            f"mc cp failed for '{local_path}' -> '{mc_target}': {err_msg}",
            code="mc_cp_failed",
            details={"mc_target": mc_target},
        )

    sys.stderr.write(f"[upload {asset_index}/{total_assets}] Completado {label} (100.0%)\n")
    sys.stderr.flush()


def upload_single_asset(
    asset: dict[str, Any],
    overwrite: bool,
    skip_existing: bool,
    verify_http: bool,
    quiet: bool = False,
    progress_file: Path | None = None,
    asset_index: int = 1,
    total_assets: int = 1,
    tracker: BatchProgressTracker | None = None,
) -> dict[str, Any]:
    """Upload one asset via mc.exe cp and verify remote state."""
    mc_target = asset["mc_target"]
    if tracker is not None:
        tracker.start_asset(asset_index)

    if asset["exists_in_minio"]:
        if skip_existing:
            if tracker is not None:
                tracker.set_asset_verifying(asset_index)
            http_check = head_url(asset["public_url"]) if verify_http else None
            result = {**asset, "upload_status": "skipped_existing", "http_check": http_check}
            if tracker is not None:
                tracker.finish_asset(asset_index, result)
            return result
        if not overwrite:
            raise PublisherError(
                f"Remote object already exists at '{mc_target}'. Re-run with --skip-existing or --overwrite.",
                code="already_exists",
                details={"mc_target": mc_target, "public_url": asset["public_url"]},
            )

    progress_cb: Callable[[dict[str, Any]], None] | None = None
    if tracker is not None:
        progress_cb = lambda info: tracker.update_asset_progress(
            asset_index,
            float(info["percent"]),
            str(info["transferred"]),
            str(info["speed"]),
            str(info["eta"]),
            str(info["bar"]),
        )

    run_mc_cp(
        local_path=asset["local_path"],
        mc_target=mc_target,
        label=asset["object_key"],
        total_mib=asset["size_mib"],
        quiet=quiet,
        progress_file=progress_file,
        asset_index=asset_index,
        total_assets=total_assets,
        on_progress=progress_cb,
    )

    if tracker is not None:
        tracker.set_asset_verifying(asset_index)

    verified_stat = mc_stat(mc_target)
    if verified_stat is None:
        raise PublisherError(
            f"Upload reported success but mc stat could not verify '{mc_target}'.",
            code="verify_stat_failed",
            details={"mc_target": mc_target},
        )

    http_check = head_url(asset["public_url"]) if verify_http else None
    result = {
        **asset,
        "upload_status": "uploaded",
        "exists_in_minio": True,
        "remote_size_bytes": verified_stat.get("size"),
        "same_size_as_local": verified_stat.get("size") == asset["size_bytes"],
        "http_check": http_check,
    }
    if tracker is not None:
        tracker.finish_asset(asset_index, result)
    return result


def build_upload_result(
    plan: dict[str, Any],
    asset_results: list[dict[str, Any]],
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble standardized upload/verify JSON payload including Postiz attachments."""
    video_results = [a for a in asset_results if a["role"] == "video"]
    video_res = video_results[0]
    thumb_res = next((a for a in asset_results if a["role"] == "thumbnail"), None)
    extra_res = [a for a in asset_results if a["role"] == "extra"]

    attachment_entries: list[dict[str, Any] | str] = [
        {"path": v["public_url"], "thumbnail": thumb_res["public_url"]} if thumb_res else v["public_url"]
        for v in video_results
    ]

    payload: dict[str, Any] = {
        **plan,
        "video": video_res,
        "videos": video_results,
        "thumbnail": thumb_res,
        "extras": extra_res,
        "assets": asset_results,
        "postiz_attachment_suggestion": attachment_entries[0],
        "postiz_attachments_suggestion": attachment_entries,
    }
    if extra_fields:
        payload.update(extra_fields)
    return payload


def resolve_upload_progress_file(args: argparse.Namespace) -> Path | None:
    """Resolve optional progress file path, defaulting to %TEMP% for interactive non-quiet runs."""
    if getattr(args, "no_progress_file", False):
        return None
    if args.progress_file:
        return Path(args.progress_file).expanduser()
    if args.quiet or "unittest" in sys.modules or "pytest" in sys.modules:
        return None
    return DEFAULT_PROGRESS_FILE


def cmd_upload(args: argparse.Namespace) -> dict[str, Any]:
    """Validate, upload confirmed assets to MinIO, and verify public URLs."""
    plan = prepare_assets_from_args(args, check_remote=True)
    progress_file = resolve_upload_progress_file(args)
    if progress_file is not None and not args.quiet:
        script_path = Path(__file__).resolve()
        sys.stderr.write(
            f'[progress] Monitor en PowerShell: uv run "{script_path}" watch --progress-file "{progress_file}"\n'
        )
        sys.stderr.flush()

    tracker = BatchProgressTracker(plan, progress_file) if progress_file is not None else None
    total_assets = len(plan["assets"])
    uploaded_assets: list[dict[str, Any]] = []
    current_idx: int | None = None
    try:
        for idx, asset in enumerate(plan["assets"], start=1):
            current_idx = idx
            uploaded_assets.append(
                upload_single_asset(
                    asset,
                    overwrite=args.overwrite,
                    skip_existing=args.skip_existing,
                    verify_http=not args.no_http_verify,
                    quiet=args.quiet,
                    progress_file=progress_file,
                    asset_index=idx,
                    total_assets=total_assets,
                    tracker=tracker,
                )
            )
    except PublisherError as exc:
        if tracker is not None:
            tracker.fail(exc.message, asset_index=current_idx)
        raise

    if tracker is not None:
        tracker.finish_all()

    extra: dict[str, Any] = {}
    if progress_file is not None:
        extra["progress_file"] = str(progress_file)
    return build_upload_result(plan, uploaded_assets, extra_fields=extra or None)


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    """Verify assets uploaded manually in PowerShell exist in MinIO, match size, and pass HTTP HEAD."""
    plan = prepare_assets_from_args(args, check_remote=True)
    missing = [a["mc_target"] for a in plan["assets"] if not a["exists_in_minio"]]
    if missing:
        raise PublisherError(
            f"Missing {len(missing)} asset(s) in MinIO: {', '.join(missing)}",
            code="remote_not_found",
            details={"missing_targets": missing},
        )

    mismatched = [
        {
            "mc_target": a["mc_target"],
            "local_size_bytes": a["size_bytes"],
            "remote_size_bytes": a["remote_size_bytes"],
        }
        for a in plan["assets"]
        if not a["same_size_as_local"]
    ]
    if mismatched:
        raise PublisherError(
            f"Size mismatch for {len(mismatched)} asset(s) in MinIO.",
            code="size_mismatch",
            details={"mismatched_targets": mismatched},
        )

    verify_http = not args.no_http_verify
    verified_assets = [
        {
            **asset,
            "upload_status": "verified",
            "http_check": head_url(asset["public_url"]) if verify_http else None,
        }
        for asset in plan["assets"]
    ]
    all_http_accessible = (
        all(bool((a.get("http_check") or {}).get("accessible")) for a in verified_assets)
        if verify_http
        else True
    )
    return build_upload_result(
        plan,
        verified_assets,
        extra_fields={
            "all_verified": True,
            "all_http_accessible": all_http_accessible,
        },
    )


def is_pid_running(pid: int) -> bool:
    """Check whether a process ID is currently alive without sending a signal."""
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ctypes.GetLastError() == 5
        try:
            exit_code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def enable_windows_vt_mode() -> bool:
    """Enable ANSI VT escape processing on Windows consoles when running interactively."""
    if os.name != "nt" or not sys.stdout.isatty():
        return sys.stdout.isatty()
    try:
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        if not handle or handle == -1:
            return False
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


def render_watch_snapshot(state: dict[str, Any], stale_process: bool = False) -> str:
    """Format a human-readable PowerShell monitor screen from progress JSON."""
    status_raw = str(state.get("status") or "uploading")
    if stale_process and status_raw not in {"done", "error"}:
        status_label = "INTERRUMPIDO (proceso finalizado inesperadamente)"
    elif status_raw == "done":
        status_label = "COMPLETADO"
    elif status_raw == "error":
        status_label = "ERROR"
    else:
        status_label = "SUBIENDO"

    mc_folder = state.get("mc_folder") or ""
    total_assets = int(state.get("total_assets") or len(state.get("assets") or []))
    completed_assets = int(state.get("completed_assets") or 0)
    overall_pct = float(state.get("percent") or 0.0)
    total_mib = float(state.get("total_mib") or 0.0)
    updated_at = state.get("updated_at") or ""

    lines = [
        "=" * 76,
        "  Postiz & MinIO Reels Publisher — Monitor de Subida (PowerShell)",
        "=" * 76,
        f"Destino : {mc_folder or '<bucket>'}",
        f"Estado  : {status_label}   |   Progreso global: {overall_pct:5.1f}% ({completed_assets}/{total_assets} | {total_mib:.2f} MiB)",
    ]
    if updated_at:
        lines.append(f"Actualiz.: {updated_at}   |   PID: {state.get('pid', '-')}")
    lines.append("-" * 76)

    assets = state.get("assets") or []
    if assets:
        status_tags = {
            "pending": "[ESPERA]  ",
            "uploading": "[SUBIENDO]",
            "verifying": "[VERIF.]  ",
            "uploaded": "[OK]      ",
            "skipped_existing": "[OMITIDO] ",
            "error": "[ERROR]   ",
        }
        for item in assets:
            idx = item.get("index", "?")
            item_status = str(item.get("status") or "pending")
            tag = status_tags.get(item_status, f"[{item_status.upper()}]")
            name = item.get("name") or item.get("object_key") or "archivo"
            size_mib = float(item.get("size_mib") or 0.0)
            extra_note = ""
            if item_status in {"uploaded", "skipped_existing"}:
                http_code = item.get("http_status")
                if http_code is not None:
                    extra_note = f" | HTTP {http_code}"
            elif item_status == "error" and item.get("error"):
                extra_note = f" | {item['error']}"
            lines.append(f"{tag} {idx}/{total_assets}  {name} ({size_mib:.2f} MiB){extra_note}")
            if item_status == "uploading":
                bar = format_progress_bar(
                    name,
                    float(item.get("percent") or 0.0),
                    str(item.get("transferred") or "0 B"),
                    str(item.get("speed") or "0 B/s"),
                    str(item.get("eta") or "calculando..."),
                )
                lines.append(f"           {bar}")
    else:
        bar = state.get("bar")
        if bar:
            lines.append(str(bar))

    events = state.get("recent_events") or []
    if events:
        lines.append("-" * 76)
        lines.append("Eventos recientes:")
        for ev in events[-6:]:
            lines.append(f"  {ev}")

    if status_raw == "done" and assets:
        lines.append("-" * 76)
        lines.append("URLs verificadas:")
        for item in assets:
            lines.append(f"  - {item.get('public_url')}")
    elif state.get("error"):
        lines.append("-" * 76)
        lines.append(f"Detalle error: {state['error']}")

    lines.append("=" * 76)
    return "\n".join(lines)


def cmd_watch(args: argparse.Namespace) -> int:
    """Render live upload progress in a PowerShell terminal from a progress JSON file."""
    progress_file = Path(args.progress_file).expanduser()
    interval = max(0.2, float(args.interval))

    if args.once:
        state = read_progress_file(progress_file)
        if state is None:
            raise PublisherError(
                f"Progress file does not exist or is unreadable: {progress_file}",
                code="progress_file_not_found",
                details={"progress_file": str(progress_file)},
            )
        print(render_watch_snapshot(state))
        return 1 if state.get("status") == "error" else 0

    vt_enabled = enable_windows_vt_mode()
    waiting_shown = False
    last_rendered = ""
    dead_checks = 0

    try:
        while True:
            state = read_progress_file(progress_file)
            if state is None:
                if not waiting_shown:
                    print(f"Esperando archivo de progreso: {progress_file} (Ctrl+C para salir)...")
                    waiting_shown = True
                time.sleep(interval)
                continue

            status_raw = str(state.get("status") or "uploading")
            pid = state.get("pid")
            stale = False
            if status_raw not in {"done", "error"} and isinstance(pid, int):
                if is_pid_running(pid):
                    dead_checks = 0
                else:
                    dead_checks += 1
                    stale = dead_checks >= 2

            screen = render_watch_snapshot(state, stale_process=stale)
            if screen != last_rendered:
                if vt_enabled:
                    sys.stdout.write("\x1b[2J\x1b[H" + screen + "\n")
                else:
                    sys.stdout.write(("\n" if last_rendered else "") + screen + "\n")
                sys.stdout.flush()
                last_rendered = screen

            if status_raw == "done":
                return 0
            if status_raw == "error" or stale:
                return 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nMonitor detenido por el usuario.")
        return 0


def parse_local_datetime(value: str) -> datetime:
    """Parse a local date/time string or 'now' using the OS local timezone."""
    local_tz = datetime.now().astimezone().tzinfo
    if value.strip().lower() == "now":
        return datetime.now(local_tz).replace(microsecond=0)

    normalized = value.strip().replace("T", " ")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PublisherError(
            f"Invalid datetime '{value}'. Expected 'now' or ISO format like '2026-11-22 18:30'.",
            code="invalid_datetime",
        ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz)
    return dt.replace(microsecond=0)


def sanitize_description_links(text: str) -> str:
    """Break full http(s) links with a space before the TLD so YouTube avoids external-link blocks."""
    return re.sub(
        r"(https?://[^\s./]+(?:\.[^\s./]+)*?)(\.[a-z]{2,})(?=[\s/)|]|$)",
        r"\1 \2",
        text,
        flags=re.IGNORECASE,
    )


def read_trimmed_lines(file_path: str, role: str) -> list[str]:
    """Read a UTF-8 text file and trim leading/trailing blank lines."""
    path = validate_local_file(file_path, role, None)
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    while raw_lines and not raw_lines[0].strip():
        raw_lines.pop(0)
    while raw_lines and not raw_lines[-1].strip():
        raw_lines.pop()
    return [line.rstrip() for line in raw_lines]


def build_description_payload(
    chapters_file: str | None = None,
    credits_file: str | None = None,
    intro: str | None = None,
    keep_full_links: bool = False,
) -> dict[str, Any]:
    """Build Postiz HTML description with chapters first, blank line, and credits second."""
    sections: list[list[str]] = []
    if intro and intro.strip():
        intro_lines = [line.rstrip() for line in intro.strip().splitlines()]
        sections.append(intro_lines)
    if chapters_file:
        chapters_lines = read_trimmed_lines(chapters_file, "chapters")
        if chapters_lines:
            sections.append(chapters_lines)
    if credits_file:
        credits_lines = read_trimmed_lines(credits_file, "credits")
        if not keep_full_links:
            credits_lines = [sanitize_description_links(line) for line in credits_lines]
        if credits_lines:
            sections.append(credits_lines)

    if not sections:
        raise PublisherError(
            "Provide at least one non-empty --chapters, --credits, or --intro input.",
            code="empty_description",
        )

    combined_lines: list[str] = []
    for idx, section in enumerate(sections):
        if idx > 0:
            combined_lines.append("")
        combined_lines.extend(section)

    html_paragraphs = [
        f"<p>{html.escape(line, quote=False)}</p>" if line else "<p></p>"
        for line in combined_lines
    ]
    plain_text = "\n".join(combined_lines)
    return {
        "plain_text": plain_text,
        "html_content": "".join(html_paragraphs),
        "line_count": len(combined_lines),
        "char_count": len(plain_text),
        "within_youtube_limit": len(plain_text) <= 5000,
    }


def cmd_build_description(args: argparse.Namespace) -> dict[str, Any]:
    """Build Postiz HTML description from chapters and credits text files."""
    return build_description_payload(
        chapters_file=args.chapters,
        credits_file=args.credits,
        intro=args.intro,
        keep_full_links=args.keep_full_links,
    )


def cmd_to_utc(args: argparse.Namespace) -> dict[str, Any]:
    """Convert a local datetime string into UTC ISO-8601 strings for Postiz MCP."""
    local_dt = parse_local_datetime(args.local_datetime)
    utc_dt = local_dt.astimezone(timezone.utc)
    return {
        "local_iso": local_dt.isoformat(),
        "local_tz": str(local_dt.tzinfo),
        "utc_iso": utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "utc_no_z": utc_dt.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def add_common_minio_flags(parser: argparse.ArgumentParser) -> None:
    """Attach shared MinIO flags to subcommands."""
    parser.add_argument("--alias", default=DEFAULT_ALIAS, help="mc alias name (default: minio)")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="MinIO bucket name (default: postiz)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Public base URL for the bucket")


def add_asset_flags(parser: argparse.ArgumentParser) -> None:
    """Attach shared asset input flags to prepare and upload subcommands."""
    parser.add_argument(
        "--video",
        action="append",
        default=[],
        help="Local Reel video path (.mp4, .mov, .mkv). Repeatable for batch uploads.",
    )
    parser.add_argument(
        "--video-dir",
        action="append",
        default=[],
        help="Directory containing Reel videos (.mp4, .mov, .mkv). Repeatable.",
    )
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        help="Optional normalized filename override per Reel (in matching order).",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=DEFAULT_MAX_REEL_DURATION_SEC,
        help="Maximum Reel duration in seconds (default: 180; 0 to disable).",
    )
    parser.add_argument(
        "--max-size-mib",
        type=float,
        default=DEFAULT_MAX_REEL_SIZE_MIB,
        help="Maximum Reel size in MiB (default: 500; 0 to disable).",
    )
    parser.add_argument(
        "--allow-landscape",
        action="store_true",
        help="Allow horizontal/landscape clips (width > height); vertical is enforced by default.",
    )
    parser.add_argument("--thumbnail", default=None, help="Optional local thumbnail image (.jpg, .jpeg, .png, .webp)")
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help="Optional companion project file for MinIO (subtitles, credits, etc.). Repeatable.",
    )
    parser.add_argument("--project", default=None, help="Optional project subfolder name; omit for bucket root")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description="MinIO asset validator and uploader for Postiz MCP.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_p = subparsers.add_parser("list-projects", help="List existing project folders in MinIO bucket")
    add_common_minio_flags(list_p)
    list_p.set_defaults(func=cmd_list_projects)

    prep_p = subparsers.add_parser("prepare", help="Validate files and preview normalized MinIO paths without uploading")
    add_common_minio_flags(prep_p)
    add_asset_flags(prep_p)
    prep_p.add_argument("--no-remote-check", action="store_true", help="Skip remote mc stat/ls checks")
    prep_p.set_defaults(func=cmd_prepare)

    up_p = subparsers.add_parser("upload", help="Upload validated assets to MinIO and verify resulting URLs")
    add_common_minio_flags(up_p)
    add_asset_flags(up_p)
    up_p.add_argument("--overwrite", action="store_true", help="Overwrite if remote object already exists")
    up_p.add_argument("--skip-existing", action="store_true", help="Reuse remote object without re-uploading if it exists")
    up_p.add_argument("--no-http-verify", action="store_true", help="Skip HTTP HEAD verification on public URL")
    up_p.add_argument("--quiet", "-q", action="store_true", help="Suppress stderr progress bar output")
    up_p.add_argument("--progress-file", default=None, help="Optional JSON file path updated with live upload progress")
    up_p.add_argument("--no-progress-file", action="store_true", help="Disable writing JSON progress file")
    up_p.set_defaults(func=cmd_upload)

    ver_p = subparsers.add_parser("verify", help="Verify assets already uploaded to MinIO and check public URLs")
    add_common_minio_flags(ver_p)
    add_asset_flags(ver_p)
    ver_p.add_argument("--no-http-verify", action="store_true", help="Skip HTTP HEAD verification on public URL")
    ver_p.set_defaults(func=cmd_verify)

    watch_p = subparsers.add_parser("watch", help="Watch live upload progress in a PowerShell terminal")
    watch_p.add_argument(
        "--progress-file",
        default=str(DEFAULT_PROGRESS_FILE),
        help=f"JSON progress file written by upload (default: {DEFAULT_PROGRESS_FILE})",
    )
    watch_p.add_argument("--interval", type=float, default=0.5, help="Refresh interval in seconds (default: 0.5)")
    watch_p.add_argument("--once", action="store_true", help="Print a single progress snapshot and exit")
    watch_p.set_defaults(func=cmd_watch)

    utc_p = subparsers.add_parser("to-utc", help="Convert local OS date/time to UTC ISO-8601 for Postiz")
    utc_p.add_argument("--local-datetime", required=True, help="'now' or local datetime (e.g. '2026-11-22 18:30')")
    utc_p.set_defaults(func=cmd_to_utc)

    desc_p = subparsers.add_parser(
        "build-description",
        help="Build Postiz HTML description (chapters first, blank line, credits second)",
    )
    desc_p.add_argument("--chapters", default=None, help="Path to chapters .txt file (placed first, verbatim)")
    desc_p.add_argument("--credits", default=None, help="Path to credits .txt file (placed after chapters)")
    desc_p.add_argument("--intro", default=None, help="Optional intro text placed before chapters")
    desc_p.add_argument(
        "--keep-full-links",
        action="store_true",
        help="Keep full https:// URLs intact instead of inserting a space before the TLD",
    )
    desc_p.set_defaults(func=cmd_build_description)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint emitting JSON to stdout (or live terminal UI for watch)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "watch":
            return int(cmd_watch(args))
        result = args.func(args)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        return 0
    except PublisherError as exc:
        print(
            json.dumps(
                {"ok": False, "code": exc.code, "error": exc.message, "details": exc.details},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""CLI helper for validating, normalizing, and uploading video assets to MinIO for Postiz."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_ALIAS = "minio"
DEFAULT_BUCKET = "postiz"
DEFAULT_BASE_URL = "https://s3.atomsystems.org/postiz"

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


def cmd_prepare(args: argparse.Namespace) -> dict[str, Any]:
    """Validate local Reels and inspect normalized MinIO targets without uploading."""
    return prepare_assets(
        video=args.video,
        thumbnail=args.thumbnail,
        extras=args.extra or [],
        project=args.project,
        alias=args.alias,
        bucket=args.bucket,
        base_url=args.base_url,
        check_remote=not args.no_remote_check,
        video_names=args.name,
        video_dirs=args.video_dir,
        max_duration_sec=args.max_duration or None,
        max_size_mib=args.max_size_mib or None,
        allow_landscape=args.allow_landscape,
    )


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
    try:
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = progress_file.with_suffix(progress_file.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(progress_file)
    except OSError:
        pass


def run_mc_cp(
    local_path: str,
    mc_target: str,
    label: str,
    total_mib: float,
    quiet: bool = False,
    progress_file: Path | None = None,
    asset_index: int = 1,
    total_assets: int = 1,
) -> None:
    """Execute mc cp with optional throttled progress streaming to stderr."""
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
    last_emit = 0.0
    last_pct = -10.0
    error_lines: list[str] = []

    sys.stderr.write(f"[upload {asset_index}/{total_assets}] Iniciando {label} ({total_mib:.2f} MiB)\n")
    sys.stderr.flush()
    write_progress_file(
        progress_file,
        {
            "pid": os.getpid(),
            "status": "uploading",
            "asset_index": asset_index,
            "total_assets": total_assets,
            "label": label,
            "total_mib": total_mib,
            "percent": 0.0,
            "transferred": "0 MiB",
            "speed": "0 MiB/s",
            "eta": "calculando...",
            "bar": format_progress_bar(label, 0.0, "0 MiB", "0 MiB/s", "calculando..."),
        },
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
        nonlocal last_emit, last_pct
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
        if now - last_emit >= 5.0 or pct - last_pct >= 5.0 or pct >= 100.0:
            eta = format_eta(pct, now - started)
            bar_line = format_progress_bar(
                label,
                pct,
                parsed["transferred"],
                parsed["speed"],
                eta,
            )
            sys.stderr.write(f"[upload {asset_index}/{total_assets}] {bar_line}\n")
            sys.stderr.flush()
            write_progress_file(
                progress_file,
                {
                    "pid": os.getpid(),
                    "status": "uploading",
                    "asset_index": asset_index,
                    "total_assets": total_assets,
                    "label": label,
                    "total_mib": total_mib,
                    "percent": pct,
                    "transferred": parsed["transferred"],
                    "speed": parsed["speed"],
                    "eta": eta,
                    "bar": bar_line,
                },
            )
            last_emit = now
            last_pct = pct

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
) -> dict[str, Any]:
    """Upload one asset via mc.exe cp and verify remote state."""
    mc_target = asset["mc_target"]
    if asset["exists_in_minio"]:
        if skip_existing:
            http_check = head_url(asset["public_url"]) if verify_http else None
            return {**asset, "upload_status": "skipped_existing", "http_check": http_check}
        if not overwrite:
            raise PublisherError(
                f"Remote object already exists at '{mc_target}'. Re-run with --skip-existing or --overwrite.",
                code="already_exists",
                details={"mc_target": mc_target, "public_url": asset["public_url"]},
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
    )

    verified_stat = mc_stat(mc_target)
    if verified_stat is None:
        raise PublisherError(
            f"Upload reported success but mc stat could not verify '{mc_target}'.",
            code="verify_stat_failed",
            details={"mc_target": mc_target},
        )

    http_check = head_url(asset["public_url"]) if verify_http else None
    return {
        **asset,
        "upload_status": "uploaded",
        "exists_in_minio": True,
        "remote_size_bytes": verified_stat.get("size"),
        "same_size_as_local": verified_stat.get("size") == asset["size_bytes"],
        "http_check": http_check,
    }


def cmd_upload(args: argparse.Namespace) -> dict[str, Any]:
    """Validate, upload confirmed assets to MinIO, and verify public URLs."""
    plan = prepare_assets(
        video=args.video,
        thumbnail=args.thumbnail,
        extras=args.extra or [],
        project=args.project,
        alias=args.alias,
        bucket=args.bucket,
        base_url=args.base_url,
        check_remote=True,
        video_names=args.name,
        video_dirs=args.video_dir,
        max_duration_sec=args.max_duration or None,
        max_size_mib=args.max_size_mib or None,
        allow_landscape=args.allow_landscape,
    )
    progress_file = Path(args.progress_file).expanduser() if args.progress_file else None
    total_assets = len(plan["assets"])
    uploaded_assets = [
        upload_single_asset(
            asset,
            overwrite=args.overwrite,
            skip_existing=args.skip_existing,
            verify_http=not args.no_http_verify,
            quiet=args.quiet,
            progress_file=progress_file,
            asset_index=idx,
            total_assets=total_assets,
        )
        for idx, asset in enumerate(plan["assets"], start=1)
    ]
    write_progress_file(
        progress_file,
        {
            "pid": os.getpid(),
            "status": "done",
            "total_assets": total_assets,
            "percent": 100.0,
        },
    )
    video_results = [a for a in uploaded_assets if a["role"] == "video"]
    video_res = video_results[0]
    thumb_res = next((a for a in uploaded_assets if a["role"] == "thumbnail"), None)
    extra_res = [a for a in uploaded_assets if a["role"] == "extra"]

    attachment_entries: list[dict[str, Any] | str] = [
        {"path": v["public_url"], "thumbnail": thumb_res["public_url"]} if thumb_res else v["public_url"]
        for v in video_results
    ]

    return {
        **plan,
        "video": video_res,
        "videos": video_results,
        "thumbnail": thumb_res,
        "extras": extra_res,
        "assets": uploaded_assets,
        "postiz_attachment_suggestion": attachment_entries[0],
        "postiz_attachments_suggestion": attachment_entries,
    }


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
    up_p.set_defaults(func=cmd_upload)

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
    """CLI entrypoint emitting JSON to stdout."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
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

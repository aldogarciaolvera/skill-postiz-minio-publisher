# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""CLI helper for validating, normalizing, and uploading video assets to MinIO for Postiz."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_ALIAS = "minio"
DEFAULT_BUCKET = "postiz"
DEFAULT_BASE_URL = "https://s3.atomsystems.org/postiz"

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv"}
ALLOWED_THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


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
    req = Request(url, method="HEAD")
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


def build_asset_plan(
    local_path: Path,
    role: str,
    project: str,
    alias: str,
    bucket: str,
    base_url: str,
    check_remote: bool,
) -> dict[str, Any]:
    """Build normalized target path, S3 target, and public URL for a single asset."""
    normalized_name = normalize_filename(local_path.name)
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


def prepare_assets(
    video: str,
    thumbnail: str | None,
    extras: list[str],
    project: str | None,
    alias: str,
    bucket: str,
    base_url: str,
    check_remote: bool = True,
) -> dict[str, Any]:
    """Validate and prepare normalized MinIO upload targets for all assets."""
    normalized_project = normalize_project_path(project)
    bucket_prefix = f"{alias}/{bucket}"

    validated_inputs: list[tuple[str, Path]] = [
        ("video", validate_local_file(video, "video", ALLOWED_VIDEO_EXTENSIONS))
    ]
    if thumbnail:
        validated_inputs.append(
            ("thumbnail", validate_local_file(thumbnail, "thumbnail", ALLOWED_THUMBNAIL_EXTENSIONS))
        )
    for extra in extras:
        validated_inputs.append(("extra", validate_local_file(extra, "extra", None)))

    assets = [
        build_asset_plan(path, role, normalized_project, alias, bucket, base_url, check_remote)
        for role, path in validated_inputs
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

    project_exists: bool | None = None
    if normalized_project and check_remote:
        entries = mc_list_entries(f"{bucket_prefix}/{normalized_project}/")
        project_exists = len(entries) > 0

    video_asset = assets[0]
    video_asset["media_probe"] = probe_video(Path(video_asset["local_path"]))
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
        "video": video_asset,
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
    """Validate local assets and inspect normalized MinIO targets without uploading."""
    return prepare_assets(
        video=args.video,
        thumbnail=args.thumbnail,
        extras=args.extra or [],
        project=args.project,
        alias=args.alias,
        bucket=args.bucket,
        base_url=args.base_url,
        check_remote=not args.no_remote_check,
    )


def upload_single_asset(asset: dict[str, Any], overwrite: bool, skip_existing: bool, verify_http: bool) -> dict[str, Any]:
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

    proc = run_mc(["cp", asset["local_path"], mc_target])
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise PublisherError(
            f"mc cp failed for '{asset['local_path']}' -> '{mc_target}': {err}",
            code="mc_cp_failed",
            details={"mc_target": mc_target},
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
    )
    uploaded_assets = [
        upload_single_asset(
            asset,
            overwrite=args.overwrite,
            skip_existing=args.skip_existing,
            verify_http=not args.no_http_verify,
        )
        for asset in plan["assets"]
    ]
    video_res = next(a for a in uploaded_assets if a["role"] == "video")
    thumb_res = next((a for a in uploaded_assets if a["role"] == "thumbnail"), None)
    extra_res = [a for a in uploaded_assets if a["role"] == "extra"]

    attachment_entry: dict[str, Any] | str = (
        {"path": video_res["public_url"], "thumbnail": thumb_res["public_url"]}
        if thumb_res
        else video_res["public_url"]
    )

    return {
        **plan,
        "video": video_res,
        "thumbnail": thumb_res,
        "extras": extra_res,
        "assets": uploaded_assets,
        "postiz_attachment_suggestion": attachment_entry,
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
    parser.add_argument("--video", required=True, help="Local video path (.mp4, .mov, .mkv)")
    parser.add_argument("--thumbnail", default=None, help="Optional local thumbnail image (.jpg, .jpeg, .png, .webp)")
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help="Optional companion project file for MinIO (timelines, licenses, subtitles, etc.). Repeatable.",
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
    up_p.set_defaults(func=cmd_upload)

    utc_p = subparsers.add_parser("to-utc", help="Convert local OS date/time to UTC ISO-8601 for Postiz")
    utc_p.add_argument("--local-datetime", required=True, help="'now' or local datetime (e.g. '2026-11-22 18:30')")
    utc_p.set_defaults(func=cmd_to_utc)

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

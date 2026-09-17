"""
Clipify — Local YouTube-to-Vertical-Clip Engine
Backend: FastAPI + Uvicorn
Video:   yt-dlp (subprocess) + FFmpeg with NVENC h264_nvenc
"""

import json
import logging
import os
import random
import re
import shutil
import subprocess
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clipify")

# Paths
BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
STATIC_DIR = BASE_DIR / "static"
DOWNLOADS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)


# Startup cleanup
_JOB_MAX_AGE_HOURS = 48

def _cleanup_old_job_dirs() -> None:
    """
    Delete job directories older than _JOB_MAX_AGE_HOURS at startup.
    This prevents the downloads/ folder from filling up unboundedly.
    """
    import time
    now = time.time()
    cutoff = now - _JOB_MAX_AGE_HOURS * 3600
    removed = 0
    for entry in DOWNLOADS_DIR.iterdir():
        if entry.is_dir():
            try:
                mtime = entry.stat().st_mtime
                if mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except Exception:
                pass
    if removed:
        log.info("Startup cleanup: removed %d stale job folder(s) (>%dh old)", removed, _JOB_MAX_AGE_HOURS)


_cleanup_old_job_dirs()


# Tool resolution
# Resolve yt-dlp, ffmpeg, ffprobe to absolute paths at import time.
# This is the bulletproof fix for the yt-dlp RuntimeError: the exe lives
# in the user-scripts dir which isn't on the system PATH, so we search
# known locations and store the absolute path for all subprocess calls.

def _resolve_tool(name: str) -> Optional[str]:
    """Find an executable by name, searching PATH + known Windows install dirs."""
    # 1. Try standard PATH lookup first
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())

    # 2. Search common Windows install locations for Python user-scripts
    import site
    candidate_dirs: list[Path] = []

    try:
        user_site = site.getusersitepackages()
        if user_site:
            candidate_dirs.append(Path(user_site).parent / "Scripts")
    except Exception:
        pass

    # Also check the Python prefix Scripts dir
    candidate_dirs.append(Path(site.ENABLE_USER_SITE and site.getusersitepackages() or "").parent / "Scripts")
    candidate_dirs.append(Path(os.sys.prefix) / "Scripts")
    candidate_dirs.append(Path(os.sys.prefix) / "bin")

    # Common standalone install paths on Windows
    for drive in ("C:", "D:"):
        candidate_dirs.append(Path(f"{drive}\\yt-dlp"))
        candidate_dirs.append(Path(f"{drive}\\ffmpeg\\bin"))

    for d in candidate_dirs:
        if not d.is_dir():
            continue
        for ext in ("", ".exe", ".cmd", ".bat"):
            candidate = d / f"{name}{ext}"
            if candidate.is_file():
                return str(candidate.resolve())

    return None


def _resolve_all_tools() -> dict[str, Optional[str]]:
    """Resolve all required tools and add their directories to PATH."""
    tools = {}
    for name in ("yt-dlp", "ffmpeg", "ffprobe"):
        path = _resolve_tool(name)
        tools[name] = path
        if path:
            tool_dir = str(Path(path).parent)
            # Ensure the tool's directory is on PATH for any child processes
            current_path = os.environ.get("PATH", "")
            if tool_dir.lower() not in current_path.lower():
                os.environ["PATH"] = tool_dir + os.pathsep + current_path
            log.info("✓ %s → %s", name, path)
        else:
            log.warning("✗ '%s' not found — features that depend on it will fail", name)
    return tools


_TOOLS = _resolve_all_tools()
YT_DLP_PATH: Optional[str] = _TOOLS.get("yt-dlp")
FFMPEG_PATH: Optional[str] = _TOOLS.get("ffmpeg")
FFPROBE_PATH: Optional[str] = _TOOLS.get("ffprobe")


# Browser cookie support
# YouTube often requires cookies to avoid "Sign in to confirm you're not a bot".
# We auto-detect installed browsers and let the user pick one.

_SUPPORTED_BROWSERS = ["chrome", "firefox", "edge", "brave", "opera", "vivaldi", "chromium", "safari"]

def _detect_browsers() -> list[str]:
    """Detect which supported browsers are likely installed."""
    found: list[str] = []
    if os.name == "nt":
        # Windows: check common install paths
        checks = {
            "chrome":  [r"Google\Chrome\Application\chrome.exe"],
            "edge":    [r"Microsoft\Edge\Application\msedge.exe"],
            "firefox": [r"Mozilla Firefox\firefox.exe"],
            "brave":   [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
            "opera":   [r"Opera\launcher.exe", r"Opera\opera.exe"],
            "vivaldi": [r"Vivaldi\Application\vivaldi.exe"],
        }
        prog_dirs = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        for browser, rel_paths in checks.items():
            for base in prog_dirs:
                if not base:
                    continue
                for rp in rel_paths:
                    if Path(base, rp).exists():
                        found.append(browser)
                        break
                if browser in found:
                    break
    else:
        # macOS / Linux: just check if the binary is on PATH
        for name in _SUPPORTED_BROWSERS:
            if shutil.which(name) or shutil.which(f"{name}-browser"):
                found.append(name)
    return found


DETECTED_BROWSERS: list[str] = _detect_browsers()
if DETECTED_BROWSERS:
    log.info("Detected browsers for cookie auth: %s", ", ".join(DETECTED_BROWSERS))
else:
    log.info("No browsers detected — cookie auth will be unavailable")


def _require_tool(name: str) -> str:
    """Return the resolved path for a tool, or raise with a helpful message."""
    path = _TOOLS.get(name)
    if not path:
        raise RuntimeError(
            f"'{name}' is not installed or not on PATH. "
            f"Install it with: pip install {name}"
            if name == "yt-dlp"
            else f"'{name}' is not installed or not on PATH. "
                 f"Download it from https://ffmpeg.org/download.html"
        )
    return path


# App
app = FastAPI(title="Clipify", version="2.1.0")

# Serve static assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Models
class JobStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    ZIPPING = "zipping"
    DONE = "done"
    FAILED = "failed"


class ClipMode(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"


class ManualClip(BaseModel):
    start: str
    end: str

class ClipRequest(BaseModel):
    url: str
    mode: ClipMode
    clips: Optional[list[ManualClip]] = None
    num_clips: Optional[int] = None    # (auto)
    browser: Optional[str] = None      # browser for cookie auth (e.g. "chrome")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not re.match(
            r"^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)",
            v,
        ):
            raise ValueError("Not a valid YouTube URL")
        return v


class JobInfo(BaseModel):
    job_id: str
    status: JobStatus
    progress: str
    clips: list[str]          # relative download paths
    error: Optional[str] = None
    error_detail: Optional[str] = None  # full traceback / cause chain
    video_title: Optional[str] = None
    thumbnail: Optional[str] = None
    created_at: Optional[str] = None
    elapsed_sec: Optional[float] = None
    logs: list[str] = []      # timestamped activity feed


# In-memory job store  (fine for single-user local app)
jobs: dict[str, JobInfo] = {}
# Track start times for elapsed computation
_job_start: dict[str, datetime] = {}


# Helpers
def _ts_to_seconds(ts: str) -> float:
    """Convert HH:MM:SS or MM:SS or SS to seconds.  Raises ValueError on bad input."""
    try:
        parts = ts.strip().split(":")
        parts = [float(p) for p in parts]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 1:
            return parts[0]
    except (ValueError, IndexError):
        pass
    raise ValueError(f"Invalid timestamp: '{ts}' — expected HH:MM:SS, MM:SS, or SS")


def _seconds_to_ts(sec: float) -> str:
    """Convert seconds to HH:MM:SS.mmm"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess, capture output.  On failure, raise with stderr."""
    log.info("CMD: %s", " ".join(cmd[:6]) + (" …" if len(cmd) > 6 else ""))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=os.environ.copy(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{Path(cmd[0]).stem} timed out after {timeout}s — "
            "the video may be too large or the network too slow"
        )
    except FileNotFoundError:
        tool_name = Path(cmd[0]).stem
        raise RuntimeError(
            f"'{tool_name}' executable not found at '{cmd[0]}'. "
            f"Please make sure it's installed and accessible."
        )

    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        detail = stderr or stdout or f"Command exited with code {result.returncode}"
        tool_name = Path(cmd[0]).stem
        log.error("CMD FAILED [%s]: %s", tool_name, detail[-500:])
        raise RuntimeError(f"{tool_name} failed: {detail[-500:]}")
    return result


def _get_video_duration(path: str) -> float:
    """Get duration of a video file in seconds via ffprobe."""
    ffprobe = _require_tool("ffprobe")
    result = _run([
        ffprobe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ], timeout=30)
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe returned invalid duration: {result.stdout.strip()!r}")


def _sanitize_filename(name: str) -> str:
    """Remove characters that are invalid in Windows filenames."""
    # Strip non-ASCII to avoid encoding issues on some systems
    cleaned = name.encode("ascii", errors="ignore").decode("ascii")
    cleaned = re.sub(r'[<>:"/\\|?*]', '', cleaned).strip()
    # Replace spaces/runs of whitespace with underscores for URL safety
    cleaned = re.sub(r'\s+', '_', cleaned)
    # Remove leading/trailing underscores or dots
    cleaned = cleaned.strip('_.')
    # Truncate and fallback
    cleaned = cleaned[:80]
    return cleaned if cleaned else "clip"


# Nvenc detection
_nvenc_available: Optional[bool] = None

def _check_nvenc() -> bool:
    """Test if h264_nvenc is available on this system."""
    global _nvenc_available
    if _nvenc_available is not None:
        return _nvenc_available
    ffmpeg = _TOOLS.get("ffmpeg")
    if not ffmpeg:
        _nvenc_available = False
        return False
    try:
        result = _run([
            ffmpeg, "-hide_banner", "-f", "lavfi", "-i", "nullsrc=s=256x256:d=1",
            "-c:v", "h264_nvenc", "-f", "null", "-",
        ], check=False, timeout=15)
        _nvenc_available = result.returncode == 0
    except Exception:
        _nvenc_available = False
    log.info("NVENC available: %s", _nvenc_available)
    return _nvenc_available


# Core pipeline

def fetch_video_info(url: str, browser: Optional[str] = None) -> dict:
    """Fetch video metadata (title, thumbnail, duration) via --dump-json."""
    yt_dlp = _require_tool("yt-dlp")
    cmd = [
        yt_dlp, "--no-playlist", "--skip-download",
        "--dump-json",
        "--no-warnings",
        "--js-runtimes", "node",
    ]
    if browser:
        cmd += ["--cookies-from-browser", browser]
    cmd.append(url)
    result = _run(cmd, timeout=60)

    try:
        info = json.loads(result.stdout)
        title = info.get("title", "Untitled")
        thumb = info.get("thumbnail", "")
        duration = info.get("duration")
        return {"title": title, "thumbnail": thumb, "duration": duration}
    except (json.JSONDecodeError, KeyError):
        # Fallback: try simple --print approach
        log.warning("--dump-json parse failed, falling back to --print")
        cmd2 = [
            yt_dlp, "--no-playlist", "--skip-download",
            "--print", "%(title)s",
            "--print", "%(thumbnail)s",
            "--js-runtimes", "node",
        ]
        if browser:
            cmd2 += ["--cookies-from-browser", browser]
        cmd2.append(url)
        result2 = _run(cmd2, timeout=60)
        lines = result2.stdout.strip().splitlines()
        title = lines[0] if len(lines) > 0 else "Untitled"
        thumb = lines[1] if len(lines) > 1 else ""
        return {"title": title, "thumbnail": thumb, "duration": None}


def download_video(url: str, job_dir: Path, job: JobInfo, browser: Optional[str] = None) -> Path:
    """Download best mp4+m4a via yt-dlp, return path to merged file."""
    yt_dlp = _require_tool("yt-dlp")
    output_template = str(job_dir / "source.%(ext)s")
    cmd = [
        yt_dlp,
        "--no-playlist",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "--no-mtime",
        "--js-runtimes", "node",
        "-o", output_template,
    ]
    if browser:
        cmd += ["--cookies-from-browser", browser]
    cmd.append(url)

    log.info("Downloading: %s", url)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=os.environ.copy(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"yt-dlp executable not found at '{yt_dlp}'. "
            "Please reinstall with: pip install yt-dlp"
        )

    last_pct = ""
    output_lines = []
    for line in proc.stdout:
        line = line.strip()
        if line:
            output_lines.append(line)
            if len(output_lines) > 20:
                output_lines.pop(0)
        # Parse yt-dlp percentage lines like "[download]  45.2% of ..."
        if "[download]" in line and "%" in line:
            match = re.search(r'(\d+\.?\d*)%', line)
            if match and match.group(1) != last_pct:
                last_pct = match.group(1)
                job.progress = f"Downloading… {last_pct}%"

    proc.wait()

    if proc.returncode != 0:
        err = "\n".join(output_lines) or "Unknown error"
        if "Sign in" in err or "age" in err.lower():
            raise RuntimeError("This video requires age verification or sign-in — yt-dlp cannot download it")
        if "Private video" in err or "private" in err.lower():
            raise RuntimeError("This video is private and cannot be downloaded")
        if "Video unavailable" in err or "unavailable" in err.lower():
            raise RuntimeError("This video is unavailable — it may have been removed or is region-locked")
        if "HTTP Error 429" in err or "Too Many Requests" in err:
            raise RuntimeError("YouTube rate limit hit — please wait a few minutes and try again")
        if "Could not copy" in err and "cookie database" in err:
            raise RuntimeError("Could not access browser cookies. Please close your browser completely and try again, or select a different browser in the UI.")
        if "Failed to decrypt with DPAPI" in err:
            raise RuntimeError("Could not decrypt browser cookies due to Windows/browser security. Please try a different browser in the UI.")
        raise RuntimeError(f"yt-dlp download failed: {err[-500:]}")

    # Find the downloaded file
    for f in job_dir.iterdir():
        if f.name.startswith("source") and f.suffix == ".mp4":
            log.info("Downloaded: %s (%.1f MB)", f.name, f.stat().st_size / 1e6)
            return f
    raise FileNotFoundError("yt-dlp did not produce an mp4 file — the video format may be unsupported")


def download_video_section(
    url: str,
    job_dir: Path,
    start_ts: str,
    end_ts: str,
    output_stem: str,
    job: JobInfo,
    browser: Optional[str] = None,
) -> Path:
    """
    Download only a specific time range from a YouTube video using
    yt-dlp's --download-sections flag.  This avoids downloading the
    entire video when only a short clip is needed.
    """
    yt_dlp = _require_tool("yt-dlp")
    output_template = str(job_dir / f"{output_stem}.%(ext)s")
    section_spec = f"*{start_ts}-{end_ts}"
    cmd = [
        yt_dlp,
        "--no-playlist",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "--no-mtime",
        "--js-runtimes", "node",
        "--download-sections", section_spec,
        "--force-keyframes-at-cuts",
        "-o", output_template,
    ]
    if browser:
        cmd += ["--cookies-from-browser", browser]
    cmd.append(url)

    log.info("Downloading section %s → %s", start_ts, end_ts)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=os.environ.copy(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"yt-dlp executable not found at '{yt_dlp}'. "
            "Please reinstall with: pip install yt-dlp"
        )

    last_pct = ""
    output_lines = []
    for line in proc.stdout:
        line = line.strip()
        if line:
            output_lines.append(line)
            if len(output_lines) > 20:
                output_lines.pop(0)
        if "[download]" in line and "%" in line:
            match = re.search(r'(\d+\.?\d*)%', line)
            if match and match.group(1) != last_pct:
                last_pct = match.group(1)
                job.progress = f"Downloading section… {last_pct}%"

    proc.wait()

    if proc.returncode != 0:
        err = "\n".join(output_lines) or "Unknown error"
        if "Sign in" in err or "age" in err.lower():
            raise RuntimeError("This video requires age verification or sign-in — yt-dlp cannot download it")
        if "Private video" in err or "private" in err.lower():
            raise RuntimeError("This video is private and cannot be downloaded")
        if "Video unavailable" in err or "unavailable" in err.lower():
            raise RuntimeError("This video is unavailable — it may have been removed or is region-locked")
        if "HTTP Error 429" in err or "Too Many Requests" in err:
            raise RuntimeError("YouTube rate limit hit — please wait a few minutes and try again")
        if "Could not copy" in err and "cookie database" in err:
            raise RuntimeError("Could not access browser cookies. Please close your browser completely and try again, or select a different browser in the UI.")
        if "Failed to decrypt with DPAPI" in err:
            raise RuntimeError("Could not decrypt browser cookies due to Windows/browser security. Please try a different browser in the UI.")
        raise RuntimeError(f"yt-dlp section download failed: {err[-500:]}")

    # Find the downloaded file — yt-dlp may append section info to the filename
    candidates = sorted(
        [f for f in job_dir.iterdir() if f.name.startswith(output_stem) and f.suffix == ".mp4"],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        result = candidates[0]
        log.info("Section downloaded: %s (%.1f MB)", result.name, result.stat().st_size / 1e6)
        return result
    raise FileNotFoundError("yt-dlp --download-sections did not produce an mp4 file")


def generate_auto_clips(
    video_duration: float,
    n: int,
) -> list[tuple[float, float]]:
    """
    Divide the video duration by the number of clips,
    creating N sequential clips of equal length.
    """
    if video_duration <= 0 or n <= 0:
        return []

    clip_len = video_duration / n
    clips: list[tuple[float, float]] = []
    
    for i in range(n):
        start = i * clip_len
        end = (i + 1) * clip_len
        clips.append((start, end))

    log.info("Split %.1fs video into %d clips of %.1fs each", video_duration, n, clip_len)
    return clips


def _build_ffmpeg_cmd(
    source: Path,
    output: Path,
    start_sec: float,
    duration: float,
    *,
    use_nvenc: bool,
) -> list[str]:
    """Build the ffmpeg command for a given encoder."""
    ffmpeg = _require_tool("ffmpeg")
    cmd = [ffmpeg, "-y"]
    if use_nvenc:
        cmd += ["-hwaccel", "cuda"]
    cmd += [
        "-ss", _seconds_to_ts(start_sec),
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", "crop=ih*9/16:ih",
    ]
    if use_nvenc:
        cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "19"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "19"]
    cmd += ["-c:a", "copy", "-movflags", "+faststart", str(output)]
    return cmd


def crop_clip(
    source: Path,
    output: Path,
    start_sec: float,
    end_sec: float,
) -> None:
    """
    Cut + crop a segment to 9:16 vertical.
    Tries NVENC first; auto-falls back to libx264 if GPU encoding fails.
    """
    duration = end_sec - start_sec
    use_nvenc = _check_nvenc()

    cmd = _build_ffmpeg_cmd(source, output, start_sec, duration, use_nvenc=use_nvenc)
    try:
        _run(cmd, timeout=600)
        return
    except RuntimeError:
        if not use_nvenc:
            raise  # CPU encoding also failed — bail out
        log.warning("NVENC failed on this clip, falling back to libx264")

    # Libx264 fallback
    cmd_fallback = _build_ffmpeg_cmd(source, output, start_sec, duration, use_nvenc=False)
    _run(cmd_fallback, timeout=600)


def crop_to_vertical(
    source: Path,
    output: Path,
) -> None:
    """
    Crop an already-trimmed video to 9:16 vertical (center crop).
    Used for section-downloaded files that don't need time seeking.
    Tries NVENC first; auto-falls back to libx264.
    """
    ffmpeg = _require_tool("ffmpeg")
    use_nvenc = _check_nvenc()

    def _build(nvenc: bool) -> list[str]:
        cmd = [ffmpeg, "-y"]
        if nvenc:
            cmd += ["-hwaccel", "cuda"]
        cmd += ["-i", str(source), "-vf", "crop=ih*9/16:ih"]
        if nvenc:
            cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "19"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "19"]
        cmd += ["-c:a", "copy", "-movflags", "+faststart", str(output)]
        return cmd

    if use_nvenc:
        try:
            _run(_build(True), timeout=600)
            return
        except RuntimeError:
            log.warning("NVENC failed, falling back to libx264")

    _run(_build(False), timeout=600)


def make_zip(files: list[Path], zip_path: Path) -> None:
    """Package clip files into a .zip archive."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)


def cleanup_source(job_dir: Path) -> None:
    """Delete the large source video to reclaim disk space."""
    for f in job_dir.iterdir():
        if f.name.startswith("source"):
            size_mb = f.stat().st_size / 1e6
            f.unlink(missing_ok=True)
            log.info("Cleaned up source: %s (%.1f MB freed)", f.name, size_mb)


def _log_to_job(job_id: str, message: str) -> None:
    """Push a timestamped log entry to the job's activity feed."""
    job = jobs.get(job_id)
    if job:
        ts = datetime.now().strftime("%H:%M:%S")
        job.logs.append(f"[{ts}] {message}")
        # Keep last 50 entries to avoid memory bloat
        if len(job.logs) > 50:
            job.logs = job.logs[-50:]


def _update_elapsed(job_id: str) -> None:
    """Update elapsed time on a job."""
    job = jobs.get(job_id)
    start = _job_start.get(job_id)
    if job and start:
        job.elapsed_sec = round(
            (datetime.now(timezone.utc) - start).total_seconds(), 1
        )


# Unified pipeline

def _process_job(
    job_id: str,
    url: str,
    clip_segments: Optional[list[tuple[str, str, float, float]]] = None,
    num_auto_clips: Optional[int] = None,
    browser: Optional[str] = None,
) -> None:
    """
    Unified background worker for both manual and auto clip modes.

    For manual mode, pass `clip_segments` as [(start_str, end_str, start_sec, end_sec), ...].
    For auto mode, pass `num_auto_clips` as the desired clip count.
    """
    is_auto = num_auto_clips is not None
    mode_label = "Auto" if is_auto else "Manual"
    count_label = num_auto_clips if is_auto else len(clip_segments or [])

    job = jobs[job_id]
    job_dir = DOWNLOADS_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    _log_to_job(job_id, f"Job started — {mode_label} mode ({count_label} clips)")
    _log_to_job(job_id, f"URL: {url}")

    try:
        # 0. fetch metadata
        try:
            _log_to_job(job_id, "Fetching video metadata…")
            info = fetch_video_info(url, browser=browser)
            job.video_title = info["title"]
            job.thumbnail = info["thumbnail"]
            _log_to_job(job_id, f"Video: {info['title']}")
        except Exception as e:
            _log_to_job(job_id, f"⚠ Metadata fetch skipped: {e}")

        safe_title = _sanitize_filename(job.video_title or "clip")
        encoder = "NVENC (GPU)" if _check_nvenc() else "libx264 (CPU)"
        clip_paths: list[Path] = []

        if is_auto:
            # ━━ AUTO MODE: download full video, then split ━━
            job.status = JobStatus.DOWNLOADING
            job.progress = "Downloading full video…"
            _log_to_job(job_id, "Auto mode → downloading full video…")
            _log_to_job(job_id, "Format: best mp4 video + m4a audio, merged to mp4")
            _update_elapsed(job_id)
            source = download_video(url, job_dir, job, browser=browser)
            file_size = source.stat().st_size / 1e6
            _log_to_job(job_id, f"Download complete: {file_size:.1f} MB")

            job.status = JobStatus.PROCESSING
            _update_elapsed(job_id)
            job.progress = "Dividing video into equal clips…"
            duration = _get_video_duration(str(source))
            _log_to_job(job_id, f"Video duration: {duration:.1f}s ({duration/60:.1f} min)")
            _log_to_job(job_id, f"Dividing into {num_auto_clips} equal segments…")
            selected = generate_auto_clips(duration, num_auto_clips)

            if not selected:
                raise RuntimeError("Could not extract any clips. The video may be too short.")

            clip_segments = []
            for i, (s, e) in enumerate(selected, 1):
                _log_to_job(job_id, f"  Clip {i}: {s:.1f}s → {e:.1f}s ({e-s:.1f}s)")
                clip_segments.append((f"{s:.1f}s", f"{e:.1f}s", s, e))

            _log_to_job(job_id, f"Encoder: {encoder}")
            _log_to_job(job_id, "Crop filter: crop=ih*9/16:ih (9:16 center crop)")
            total = len(clip_segments)
            for idx, (s_str, e_str, s_sec, e_sec) in enumerate(clip_segments, 1):
                clip_duration = e_sec - s_sec
                job.progress = f"Encoding clip {idx} of {total} ({encoder})…"
                _log_to_job(job_id, f"Encoding clip {idx}/{total}: {s_str} → {e_str} ({clip_duration:.1f}s)")
                _update_elapsed(job_id)
                clip_name = f"{safe_title}_{idx:02d}.mp4"
                clip_path = job_dir / clip_name
                crop_clip(source, clip_path, s_sec, e_sec)
                out_size = clip_path.stat().st_size / 1e6
                _log_to_job(job_id, f"  → {clip_name} ({out_size:.1f} MB)")
                clip_paths.append(clip_path)

            _log_to_job(job_id, "Cleaning up source video…")
            cleanup_source(job_dir)

        else:
            # ━━ MANUAL MODE: download only clipped sections ━━
            _log_to_job(job_id, "Manual mode → downloading only the clipped sections (not the full video)")
            _log_to_job(job_id, f"Encoder: {encoder}")
            _log_to_job(job_id, "Crop filter: crop=ih*9/16:ih (9:16 center crop)")
            total = len(clip_segments)

            for idx, (s_str, e_str, s_sec, e_sec) in enumerate(clip_segments, 1):
                clip_duration = e_sec - s_sec

                # Download just this section
                job.status = JobStatus.DOWNLOADING
                job.progress = f"Downloading clip {idx}/{total} ({s_str} → {e_str})…"
                _log_to_job(job_id, f"Downloading clip {idx}/{total}: {s_str} → {e_str} ({clip_duration:.1f}s)")
                _update_elapsed(job_id)
                section_stem = f"section_{idx:02d}"
                section_path = download_video_section(
                    url, job_dir, s_str, e_str, section_stem, job, browser=browser,
                )
                sec_size = section_path.stat().st_size / 1e6
                _log_to_job(job_id, f"  Section downloaded: {sec_size:.1f} MB")

                # Crop to 9:16 vertical
                job.status = JobStatus.PROCESSING
                job.progress = f"Cropping clip {idx}/{total} to 9:16 ({encoder})…"
                _log_to_job(job_id, f"Cropping clip {idx}/{total} to 9:16 vertical…")
                _update_elapsed(job_id)
                clip_name = f"{safe_title}_{idx:02d}.mp4"
                clip_path = job_dir / clip_name
                crop_to_vertical(section_path, clip_path)
                out_size = clip_path.stat().st_size / 1e6
                _log_to_job(job_id, f"  → {clip_name} ({out_size:.1f} MB)")
                clip_paths.append(clip_path)

                # Delete the section source immediately
                section_path.unlink(missing_ok=True)
                _log_to_job(job_id, f"  Cleaned up section source")

        # 5. package
        if len(clip_paths) > 1:
            job.status = JobStatus.ZIPPING
            job.progress = "Packaging clips into ZIP…"
            _log_to_job(job_id, f"Creating ZIP archive with {len(clip_paths)} clips…")
            _update_elapsed(job_id)
            zip_name = f"{safe_title}_clips.zip"
            zip_path = job_dir / zip_name
            make_zip(clip_paths, zip_path)
            zip_size = zip_path.stat().st_size / 1e6
            _log_to_job(job_id, f"ZIP created: {zip_name} ({zip_size:.1f} MB)")
            job.clips = [f"/download/{job_id}/{quote(zip_name)}"]
            for cp in clip_paths:
                job.clips.append(f"/download/{job_id}/{quote(cp.name)}")
        elif len(clip_paths) == 1:
            job.clips = [f"/download/{job_id}/{quote(clip_paths[0].name)}"]

        job.status = JobStatus.DONE
        job.progress = f"Done! {len(clip_paths)} clip(s) ready."
        _log_to_job(job_id, "✓ Job completed successfully")
        _update_elapsed(job_id)
        log.info("Job %s complete: %d %s clips", job_id, len(clip_paths), mode_label.lower())

    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error = str(exc)
        job.error_detail = traceback.format_exc()
        job.progress = f"Error: {exc}"
        _log_to_job(job_id, f"✗ FAILED: {exc}")
        tb_lines = traceback.format_exc().splitlines()
        _log_to_job(job_id, f"Cause: {tb_lines[-1] if tb_lines else 'unknown'}")
        _update_elapsed(job_id)
        log.exception("Job %s failed", job_id)


def process_manual(job_id: str, url: str, clips: list[ManualClip], browser: Optional[str] = None) -> None:
    """Background task: manual batch-clip mode."""
    # Pre-validate time ranges
    parsed_clips: list[tuple[str, str, float, float]] = []
    for c in clips:
        start_sec = _ts_to_seconds(c.start)
        end_sec = _ts_to_seconds(c.end)
        if start_sec >= end_sec:
            job = jobs[job_id]
            job.status = JobStatus.FAILED
            job.error = f"Start time ({c.start}) must be before end time ({c.end})"
            job.progress = f"Error: {job.error}"
            _log_to_job(job_id, f"✗ FAILED: {job.error}")
            return
        parsed_clips.append((c.start, c.end, start_sec, end_sec))

    _process_job(job_id, url, clip_segments=parsed_clips, browser=browser)


def process_auto(job_id: str, url: str, num_clips: int, browser: Optional[str] = None) -> None:
    """Background task: auto random-clip mode."""
    _process_job(job_id, url, num_auto_clips=num_clips, browser=browser)


# Api routes

@app.get("/")
async def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
async def health_check():
    """Report tool availability and versions — useful for frontend diagnostics."""
    tool_status = {}
    for name, path in _TOOLS.items():
        entry = {"found": path is not None, "path": path, "version": None}
        if path:
            try:
                if name == "yt-dlp":
                    r = _run([path, "--version"], check=False, timeout=10)
                else:
                    r = _run([path, "-version"], check=False, timeout=10)
                first_line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
                entry["version"] = first_line[:80]
            except Exception:
                entry["version"] = "unknown"
        tool_status[name] = entry

    nvenc = _check_nvenc()
    return {
        "status": "ok" if all(t["found"] for t in tool_status.values()) else "degraded",
        "tools": tool_status,
        "nvenc": nvenc,
        "browsers": DETECTED_BROWSERS,
    }


@app.get("/api/auth/browsers")
async def list_browsers():
    """Return detected browsers that can supply YouTube cookies."""
    return {"browsers": DETECTED_BROWSERS}


@app.post("/api/clip", response_model=JobInfo)
async def create_clip(req: ClipRequest, bg: BackgroundTasks):
    """Start a clipping job."""
    # Pre-flight: ensure yt-dlp is available before queueing
    if not YT_DLP_PATH:
        raise HTTPException(
            503,
            "yt-dlp is not installed or not found on this system. "
            "Install it with: pip install yt-dlp"
        )
    if not FFMPEG_PATH:
        raise HTTPException(
            503,
            "ffmpeg is not installed or not found on this system. "
            "Download it from https://ffmpeg.org/download.html"
        )

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)

    job = JobInfo(
        job_id=job_id,
        status=JobStatus.QUEUED,
        progress="Job queued…",
        clips=[],
        created_at=now.isoformat(),
        elapsed_sec=0,
    )
    jobs[job_id] = job
    _job_start[job_id] = now

    if req.mode == ClipMode.MANUAL:
        if not req.clips or len(req.clips) == 0:
            raise HTTPException(400, "clips required for manual mode")
        bg.add_task(process_manual, job_id, req.url, req.clips, req.browser)
    else:
        if not req.num_clips or req.num_clips < 1:
            raise HTTPException(400, "num_clips must be >= 1 for auto mode")
        if req.num_clips > 20:
            raise HTTPException(400, "Maximum 20 clips at a time")
        bg.add_task(process_auto, job_id, req.url, req.num_clips, req.browser)

    log.info("Created job %s [%s]", job_id, req.mode.value)
    return job


@app.get("/api/status/{job_id}", response_model=JobInfo)
async def get_status(job_id: str):
    """Poll job status."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    _update_elapsed(job_id)
    return jobs[job_id]


@app.get("/api/jobs")
async def list_jobs():
    """List all jobs (most recent first)."""
    return list(reversed(jobs.values()))


@app.get("/download/{job_id}/{filename}")
async def download_file(job_id: str, filename: str):
    """Serve a processed clip or zip."""
    # Sanitise to prevent path traversal
    safe_name = Path(filename).name
    file_path = DOWNLOADS_DIR / job_id / safe_name
    if not file_path.exists():
        raise HTTPException(404, "File not found")
    media_type = "application/zip" if safe_name.endswith(".zip") else "video/mp4"
    return FileResponse(
        str(file_path),
        media_type=media_type,
        filename=safe_name,
    )


@app.delete("/api/job/{job_id}")
async def delete_job(job_id: str):
    """Clean up a job's files and remove from memory."""
    job_dir = DOWNLOADS_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    jobs.pop(job_id, None)
    _job_start.pop(job_id, None)
    log.info("Deleted job %s", job_id)
    return {"detail": "deleted"}


@app.delete("/api/cleanup")
async def cleanup_all_jobs():
    """Delete ALL jobs — wipes every job folder from disk and clears memory."""
    count = len(jobs)
    disk_count = 0
    for entry in DOWNLOADS_DIR.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
            disk_count += 1
    jobs.clear()
    _job_start.clear()
    log.info("Bulk cleanup: removed %d in-memory job(s), %d folder(s) from disk", count, disk_count)
    return {"detail": "all jobs deleted", "jobs_removed": count, "folders_removed": disk_count}


# Entrypoint
if __name__ == "__main__":
    import uvicorn

    # Tool availability summary
    missing = [name for name, path in _TOOLS.items() if not path]
    if missing:
        log.warning("⚠ Missing tools: %s — some features will fail", ", ".join(missing))
    else:
        log.info("✓ All tools found (yt-dlp, ffmpeg, ffprobe)")

    _check_nvenc()  # warm up NVENC detection
    log.info("Starting Clipify on http://127.0.0.1:8000")
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)

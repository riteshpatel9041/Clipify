# Clipify

Local YouTube to Vertical Clip Engine.
Download YouTube videos and crop them to 9:16 vertical format for TikTok, Reels, and Shorts.
GPU-accelerated via NVIDIA NVENC, with automatic CPU fallback.

---

## Features

- **Manual mode** — define exact start/end timestamps for each clip
- **Auto mode** — split the video into N equal-length clips automatically
- **9:16 center-crop** — perfect for TikTok, Instagram Reels, YouTube Shorts
- **NVENC GPU encoding** — blazing-fast encoding on NVIDIA GPUs, falls back to libx264 on CPU
- **Browser cookie auth** — bypass YouTube bot-detection by using cookies from your installed browser
- **Live progress** — real-time status updates with an activity log
- **ZIP packaging** — multi-clip jobs are bundled into a single download
- **Job history** — quick access to previous jobs with one-click re-download

---

## Requirements

| Dependency | Install |
|---|---|
| Python 3.10+ | [python.org](https://www.python.org/downloads/) |
| `yt-dlp` | `pip install yt-dlp` |
| `ffmpeg` + `ffprobe` | [ffmpeg.org/download](https://ffmpeg.org/download.html) — add to PATH |
| Node.js (optional) | Used by yt-dlp's JS runtime for some videos |

---

## Quick Start

### 1. Clone / download
```bash
git clone https://github.com/yourname/clipify.git
cd clipify
```

### 2. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 3. Install FFmpeg (Windows)

1. Download a Windows build from [https://ffmpeg.org/download.html](https://ffmpeg.org/download.html)  
   (recommended: [gyan.dev builds](https://www.gyan.dev/ffmpeg/builds/))
2. Extract to `C:\ffmpeg\`
3. Add `C:\ffmpeg\bin` to your **System PATH**

Verify with:
```powershell
ffmpeg -version
ffprobe -version
```

### 4. Install yt-dlp
```bash
pip install yt-dlp
```

Verify with:
```bash
yt-dlp --version
```

### 5. Run Clipify

**Windows (one-click):**
```
run.bat
```
or
```powershell
.\run.ps1
```

**Manual:**
```bash
python app.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

---

## Usage

### Manual Mode
1. Paste a YouTube URL
2. Select **Manual** mode
3. Enter start/end times for each clip (HH:MM:SS)
4. Click **Generate Clips**

### Auto Mode
1. Paste a YouTube URL
2. Select **Auto-Clip** mode
3. Set the number of clips (1–20)
4. Click **Generate Clips**

The video is split into equal segments, each cropped to 9:16.

---

## YouTube Authentication (Bot Detection Fix)

YouTube may block downloads with a "Sign in to confirm you're not a bot" error. To fix this:

1. Make sure your browser (Chrome, Edge, Firefox, etc.) is **closed completely**
2. In the Clipify UI, find the **YouTube Authentication** section
3. Select your browser from the dropdown
4. Clipify will use your browser's YouTube cookies

> **Note:** Close the browser before generating clips — the cookie database is locked while it's open.

---

## Project Structure

```
clipify/
├── app.py              # FastAPI backend (main entry point)
├── requirements.txt    # Python dependencies
├── run.bat             # Windows one-click launcher
├── run.ps1             # PowerShell launcher
├── static/
│   └── index.html      # Single-page frontend
└── downloads/          # Processed clips stored here (auto-cleaned after 48h)
```

---

## Configuration

All configuration is at the top of `app.py`:

| Variable | Default | Description |
|---|---|---|
| `DOWNLOADS_DIR` | `./downloads` | Where clips are stored |
| `host` | `127.0.0.1` | Server bind address |
| `port` | `8000` | Server port |

---

## Troubleshooting

### `yt-dlp not found`
Make sure yt-dlp is installed: `pip install yt-dlp`  
On some systems it installs to the user scripts dir — restart your terminal after installing.

### `ffmpeg not found`
Download ffmpeg and add its `bin/` folder to your system PATH.

### `HTTP Error 429 / Too Many Requests`
YouTube is rate-limiting your IP. Wait a few minutes and try again.  
Using browser cookies (see Authentication section) can also help.

### `Could not copy cookie database`
Close your browser completely before generating clips. The cookie database is locked while the browser is open.

### NVENC not available / slow encoding
If you don't have an NVIDIA GPU, the app automatically falls back to CPU encoding (libx264). This is slower but produces identical quality.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/api/health` | Tool status and versions |
| `GET` | `/api/auth/browsers` | Detected browsers for cookie auth |
| `POST` | `/api/clip` | Start a clip job |
| `GET` | `/api/status/{job_id}` | Poll job status |
| `GET` | `/api/jobs` | List all jobs |
| `DELETE` | `/api/job/{job_id}` | Delete a specific job |
| `DELETE` | `/api/cleanup` | Delete all jobs and free disk space |
| `GET` | `/download/{job_id}/{filename}` | Download a clip or ZIP |

---

## License

MIT — do whatever you want with it.

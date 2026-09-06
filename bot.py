import os
import re
import time
import asyncio
import shutil
from pathlib import Path
from urllib.parse import urljoin, unquote

import aiohttp
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from telethon import TelegramClient


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

API_ID_RAW = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

if API_ID_RAW:
    try:
        API_ID = int(API_ID_RAW)
    except ValueError:
        API_ID = 0
else:
    API_ID = 0


DOWNLOAD_DIR = Path("downloads")
CONVERT_DIR = Path("converted")

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

CONVERT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

CHUNK_SIZE = 1024 * 1024

STATUS_INTERVAL = 2

# Telethon session file
TELETHON_SESSION = "downloads/movie_bot_mtproto"


# Global Telethon client
telethon_client = None


# =========================================================
# BASIC HELPERS
# =========================================================

def format_bytes(size):

    if size is None:
        return "Unknown"

    size = float(size)

    for unit in ["B", "KB", "MB", "GB", "TB"]:

        if size < 1024:
            return f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} PB"


def format_time(seconds):

    if not seconds or seconds < 0:
        return "--:--"

    seconds = int(seconds)

    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"

    return f"{m:02d}:{s:02d}"


def progress_bar(percent, length=20):

    percent = max(
        0,
        min(100, percent),
    )

    filled = int(
        length * percent / 100
    )

    return (
        "▰" * filled
        + "▱" * (length - filled)
    )


def is_http_url(text):

    return bool(
        re.match(
            r"^https?://",
            text.strip(),
            re.IGNORECASE,
        )
    )


def is_mediafire_url(url):

    url = url.lower()

    return (
        "mediafire.com/file/" in url
        or "mfi.re/" in url
    )


def safe_filename(name):

    name = unquote(name)

    name = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        name,
    )

    name = name.strip()

    if not name:
        return "video"

    return name[:200]


# =========================================================
# FILENAME
# =========================================================

def filename_from_headers(headers):

    value = headers.get(
        "Content-Disposition",
        "",
    )

    if not value:
        return None

    match = re.search(
        r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)",
        value,
        re.IGNORECASE,
    )

    if match:

        return unquote(
            match.group(1).strip('"')
        )

    match = re.search(
        r'filename\s*=\s*"([^"]+)"',
        value,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    match = re.search(
        r"filename\s*=\s*([^;]+)",
        value,
        re.IGNORECASE,
    )

    if match:
        return (
            match.group(1)
            .strip()
            .strip('"')
        )

    return None


def filename_from_url(url):

    clean = url.split("?", 1)[0]

    name = (
        clean.rstrip("/")
        .split("/")[-1]
    )

    name = unquote(name)

    return name or "video"


# =========================================================
# MEDIAFIRE
# =========================================================

async def resolve_mediafire_url(
    session,
    mediafire_url,
):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with session.get(
        mediafire_url,
        headers=headers,
        allow_redirects=True,
        timeout=aiohttp.ClientTimeout(
            total=120
        ),
    ) as response:

        if response.status != 200:

            raise RuntimeError(
                f"MediaFire HTTP {response.status}"
            )

        page_url = str(
            response.url
        )

        html = await response.text(
            errors="ignore"
        )

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    candidates = []

    # -----------------------------------------------------
    # Official download button
    # -----------------------------------------------------

    button = soup.select_one(
        "#downloadButton"
    )

    if button:

        href = button.get("href")

        if href:

            candidates.append(
                urljoin(
                    page_url,
                    href,
                )
            )

    # -----------------------------------------------------
    # All links
    # -----------------------------------------------------

    for tag in soup.find_all("a"):

        href = tag.get("href")

        if not href:
            continue

        href = urljoin(
            page_url,
            href,
        )

        low = href.lower()

        if (
            "download" in low
            or "mediafire.com" in low
        ):

            candidates.append(href)

    # -----------------------------------------------------
    # Direct download URLs in HTML/JS
    # -----------------------------------------------------

    patterns = [

        r'https?://download\d*\.mediafire\.com/[^\s"\']+',

        r'https?://download\d*\.mediafire\.com/[^\s"\'<>]+',

    ]

    for pattern in patterns:

        for match in re.findall(
            pattern,
            html,
            re.IGNORECASE,
        ):

            match = (
                match
                .replace("\\/", "/")
                .replace("&amp;", "&")
            )

            candidates.append(match)

    # -----------------------------------------------------
    # Unique candidates
    # -----------------------------------------------------

    unique = []

    for candidate in candidates:

        candidate = candidate.strip()

        if candidate not in unique:

            unique.append(candidate)

    if not unique:

        raise RuntimeError(
            "MediaFire direct download link မတွေ့ပါ။"
        )

    # -----------------------------------------------------
    # Check candidates
    # -----------------------------------------------------

    for candidate in unique:

        try:

            test_headers = {
                "User-Agent": headers["User-Agent"],
                "Referer": page_url,
                "Accept": "*/*",
            }

            async with session.get(
                candidate,
                headers=test_headers,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(
                    total=60
                ),
            ) as response:

                content_type = (
                    response.headers.get(
                        "Content-Type",
                        "",
                    ).lower()
                )

                final_url = str(
                    response.url
                )

                if (
                    content_type.startswith("video/")
                    or "application/octet-stream"
                    in content_type
                    or "binary/octet-stream"
                    in content_type
                ):

                    return (
                        final_url,
                        page_url,
                    )

        except Exception as error:

            print(
                "MediaFire candidate error:",
                repr(error),
            )

    # Let download validator
    # make final decision

    return (
        unique[0],
        page_url,
    )


# =========================================================
# FILE VALIDATION
# =========================================================

def looks_like_html(data):

    data = data.lstrip().lower()

    return (
        data.startswith(b"<html")
        or data.startswith(b"<!doctype")
        or data.startswith(b"<head")
        or data.startswith(b"<body")
        or data.startswith(b"<script")
    )


def looks_like_mp4(data):

    if len(data) >= 12:

        if data[4:8] == b"ftyp":

            return True

    if b"ftyp" in data[:64]:

        return True

    return False


def looks_like_video(
    data,
    content_type,
    filename,
):

    content_type = (
        content_type or ""
    ).lower()

    filename = (
        filename or ""
    ).lower()

    if content_type.startswith("video/"):

        return True

    if looks_like_mp4(data):

        return True

    extensions = (
        ".mp4",
        ".mkv",
        ".webm",
        ".avi",
        ".mov",
        ".m4v",
        ".ts",
        ".mpeg",
        ".mpg",
    )

    if filename.endswith(extensions):

        if not looks_like_html(data):

            return True

    return False


# =========================================================
# DOWNLOAD
# =========================================================

async def download_file(
    session,
    url,
    progress_message=None,
):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }

    started = time.monotonic()

    async with session.get(
        url,
        headers=headers,
        allow_redirects=True,
        timeout=aiohttp.ClientTimeout(
            total=None,
            sock_connect=60,
            sock_read=300,
        ),
    ) as response:

        if response.status != 200:

            raise RuntimeError(
                f"Download HTTP {response.status}"
            )

        final_url = str(
            response.url
        )

        content_type = (
            response.headers.get(
                "Content-Type",
                "",
            ).lower()
        )

        length_header = (
            response.headers.get(
                "Content-Length"
            )
        )

        try:

            total = (
                int(length_header)
                if length_header
                else 0
            )

        except ValueError:

            total = 0

        # -------------------------------------------------
        # First chunk
        # -------------------------------------------------

        first_chunk = (
            await response.content.read(
                CHUNK_SIZE
            )
        )

        if not first_chunk:

            raise RuntimeError(
                "Server က empty response ပြန်ပေးပါတယ်။"
            )

        # -------------------------------------------------
        # HTML protection
        # -------------------------------------------------

        if looks_like_html(
            first_chunk
        ):

            raise RuntimeError(
                "Video အစား HTML page ရရှိနေပါတယ်။ "
                "Direct download URL မမှန်ပါ။"
            )

        filename = (
            filename_from_headers(
                response.headers
            )
            or filename_from_url(
                final_url
            )
        )

        filename = safe_filename(
            filename
        )

        # -------------------------------------------------
        # Video validation
        # -------------------------------------------------

        if not looks_like_video(
            first_chunk,
            content_type,
            filename,
        ):

            raise RuntimeError(
                "Downloaded file က video file မဟုတ်ပါ။\n"
                f"Content-Type: "
                f"{content_type or 'Unknown'}\n"
                f"File: {filename}"
            )

        # -------------------------------------------------
        # Add extension if missing
        # -------------------------------------------------

        if "." not in Path(
            filename
        ).name:

            filename += ".mp4"

        output = (
            DOWNLOAD_DIR
            / filename
        )

        # -------------------------------------------------
        # Avoid overwrite
        # -------------------------------------------------

        if output.exists():

            timestamp = int(
                time.time()
            )

            output = (
                DOWNLOAD_DIR
                / f"{output.stem}_{timestamp}"
                f"{output.suffix}"
            )

        downloaded = 0
        last_status = 0

        with open(
            output,
            "wb",
        ) as file:

            file.write(
                first_chunk
            )

            downloaded += len(
                first_chunk
            )

            while True:

                chunk = (
                    await response.content.read(
                        CHUNK_SIZE
                    )
                )

                if not chunk:
                    break

                file.write(
                    chunk
                )

                downloaded += len(
                    chunk
                )

                now = time.monotonic()

                if (
                    progress_message
                    and now - last_status
                    >= STATUS_INTERVAL
                ):

                    last_status = now

                    elapsed = (
                        now - started
                    )

                    speed = (
                        downloaded / elapsed
                        if elapsed > 0
                        else 0
                    )

                    if total:

                        percent = (
                            downloaded
                            / total
                            * 100
                        )

                        eta = (
                            (total - downloaded)
                            / speed
                            if speed > 0
                            else 0
                        )

                        text = (
                            "╔══════════════════════════╗\n"
                            "║   📥 *DOWNLOAD STATUS*   ║\n"
                            "╚══════════════════════════╝\n\n"
                            "⬇️ *DOWNLOADING*\n\n"
                            f"📁 `{filename}`\n"
                            f"{progress_bar(percent)} "
                            f"*{percent:.1f}%*\n"
                            f"┣ {format_bytes(downloaded)} / "
                            f"{format_bytes(total)}\n"
                            f"┣ Speed: "
                            f"{format_bytes(speed)}/s\n"
                            f"┣ ETA: "
                            f"{format_time(eta)}\n"
                            f"┗ Elapsed: "
                            f"{format_time(elapsed)}"
                        )

                    else:

                        text = (
                            "╔══════════════════════════╗\n"
                            "║   📥 *DOWNLOAD STATUS*   ║\n"
                            "╚══════════════════════════╝\n\n"
                            "⬇️ *DOWNLOADING*\n\n"
                            f"📁 `{filename}`\n"
                            f"┣ Downloaded: "
                            f"{format_bytes(downloaded)}\n"
                            f"┣ Speed: "
                            f"{format_bytes(speed)}/s\n"
                            f"┗ Elapsed: "
                            f"{format_time(elapsed)}"
                        )

                    try:

                        await progress_message.edit_text(
                            text,
                            parse_mode="Markdown",
                        )

                    except Exception:

                        pass

        final_size = output.stat().st_size

        if final_size == 0:

            output.unlink(
                missing_ok=True
            )

            raise RuntimeError(
                "Downloaded file size = 0"
            )

        return (
            output,
            final_size,
            time.monotonic() - started,
        )


# =========================================================
# FFMPEG CHECK
# =========================================================

def ffmpeg_available():

    return (
        shutil.which("ffmpeg")
        is not None
        and
        shutil.which("ffprobe")
        is not None
    )


# =========================================================
# COMMAND RUNNER
# =========================================================

def _run_command(command):

    import subprocess

    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# =========================================================
# ASYNC FFMPEG INFO
# =========================================================

async def probe_video(
    input_file
):

    command = [

        "ffprobe",

        "-v",
        "error",

        "-show_entries",
        "format=format_name",

        "-show_entries",
        "stream=codec_type,codec_name",

        "-of",
        "default=noprint_wrappers=1",

        str(input_file),

    ]

    result = await asyncio.to_thread(
        _run_command,
        command,
    )

    if result.returncode != 0:

        raise RuntimeError(
            "FFprobe video information "
            "မဖတ်နိုင်ပါ။"
        )

    output = (
        result.stdout.lower()
    )

    has_video = (
        "codec_type=video"
        in output
    )

    has_audio = (
        "codec_type=audio"
        in output
    )

    format_match = re.search(
        r"format_name=([^\n]+)",
        output,
    )

    format_name = (
        format_match.group(1).strip()
        if format_match
        else ""
    )

    return {
        "has_video": has_video,
        "has_audio": has_audio,
        "format": format_name,
        "raw": output,
    }


# =========================================================
# MP4 CONVERSION
# =========================================================

async def convert_to_mp4(
    input_file,
    progress_message=None,
):

    if not ffmpeg_available():

        raise RuntimeError(
            "FFmpeg မတွေ့ပါ။"
        )

    info = await probe_video(
        input_file
    )

    if not info["has_video"]:

        raise RuntimeError(
            "ဒီ file ထဲမှာ Video Stream မရှိပါ။"
        )

    output_file = (
        CONVERT_DIR
        / f"{input_file.stem}.mp4"
    )

    if output_file.exists():

        timestamp = int(
            time.time()
        )

        output_file = (
            CONVERT_DIR
            / f"{input_file.stem}_{timestamp}.mp4"
        )

    # -----------------------------------------------------
    # Processing status
    # -----------------------------------------------------

    if progress_message:

        try:

            await progress_message.edit_text(
                "╔══════════════════════════╗\n"
                "║   ⚙️ *MP4 PROCESSING*    ║\n"
                "╚══════════════════════════╝\n\n"
                "🔍 Video codec/container စစ်နေပါတယ်...",
                parse_mode="Markdown",
            )

        except Exception:

            pass

    # -----------------------------------------------------
    # Fast REMUX
    # -----------------------------------------------------

    if "mp4" in info["format"].split(","):

        command = [

            "ffmpeg",

            "-y",

            "-i",
            str(input_file),

            "-map",
            "0",

            "-c",
            "copy",

            "-movflags",
            "+faststart",

            str(output_file),

        ]

        result = await asyncio.to_thread(
            _run_command,
            command,
        )

        if result.returncode == 0:

            return (
                output_file,
                "REMUX",
            )

    # -----------------------------------------------------
    # Full MP4 conversion
    # -----------------------------------------------------

    if progress_message:

        try:

            await progress_message.edit_text(
                "╔══════════════════════════╗\n"
                "║   ⚙️ *MP4 CONVERTING*    ║\n"
                "╚══════════════════════════╝\n\n"
                "🎞️ MP4 အဖြစ် convert လုပ်နေပါတယ်...\n\n"
                "⏳ Video size ကြီးရင် အချိန်ကြာနိုင်ပါတယ်။",
                parse_mode="Markdown",
            )

        except Exception:

            pass

    command = [

        "ffmpeg",

        "-y",

        "-i",
        str(input_file),

        # Video
        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "23",

        # Audio
        "-c:a",
        "aac",

        "-b:a",
        "128k",

        # MP4
        "-movflags",
        "+faststart",

        str(output_file),

    ]

    result = await asyncio.to_thread(
        _run_command,
        command,
    )

    if result.returncode != 0:

        error_text = (
            result.stderr[-2000:]
            if result.stderr
            else "Unknown FFmpeg error"
        )

        raise RuntimeError(
            "FFmpeg conversion failed:\n"
            f"{error_text}"
        )

    if not output_file.exists():

        raise RuntimeError(
            "FFmpeg output file မထွက်ပါ။"
        )

    if output_file.stat().st_size == 0:

        output_file.unlink(
            missing_ok=True
        )

        raise RuntimeError(
            "Converted MP4 size = 0"
        )

    return (
        output_file,
        "ENCODE",
    )


# =========================================================
# TELETHON PROGRESS
# =========================================================

async def update_upload_status(
    status,
    filename,
    current,
    total,
    started,
):

    elapsed = (
        time.monotonic()
        - started
    )

    if total <= 0:
        return

    percent = (
        current
        / total
        * 100
    )

    speed = (
        current / elapsed
        if elapsed > 0
        else 0
    )

    remaining = (
        total - current
    )

    eta = (
        remaining / speed
        if speed > 0
        else 0
    )

    text = (
        "╔══════════════════════════╗\n"
        "║   📤 *UPLOAD STATUS*     ║\n"
        "╚══════════════════════════╝\n\n"
        "⏫ *UPLOADING*\n\n"
        f"📁 `{filename}`\n"
        f"{progress_bar(percent)} "
        f"*{percent:.1f}%*\n\n"
        f"┣ {format_bytes(current)} / "
        f"{format_bytes(total)}\n"
        f"┣ Speed: "
        f"{format_bytes(speed)}/s\n"
        f"┣ ETA: "
        f"{format_time(eta)}\n"
        f"┗ Elapsed: "
        f"{format_time(elapsed)}"
    )

    try:

        await status.edit_text(
            text,
            parse_mode="Markdown",
        )

    except Exception as error:

        print(
            "Upload status edit error:",
            repr(error),
        )


async def upload_large_file(
    chat_id,
    status,
    file_path,
):

    global telethon_client

    if telethon_client is None:

        raise RuntimeError(
            "Telegram MTProto client မချိတ်ထားပါ။"
        )

    if not telethon_client.is_connected():

        await telethon_client.connect()

    total = file_path.stat().st_size

    started = time.monotonic()

    filename = file_path.name

    last_update = 0

    pending_task = None

    # -----------------------------------------------------
    # Progress callback
    # -----------------------------------------------------

    def progress_callback(
        current,
        total_size,
    ):

        nonlocal last_update
        nonlocal pending_task

        now = time.monotonic()

        # Prevent Telegram edit flooding
        if (
            current < total_size
            and now - last_update
            < STATUS_INTERVAL
        ):

            return

        last_update = now

        # Schedule async edit
        pending_task = asyncio.create_task(
            update_upload_status(
                status,
                filename,
                current,
                total_size,
                started,
            )
        )

    # -----------------------------------------------------
    # Start upload
    # -----------------------------------------------------

    await status.edit_text(
        "╔══════════════════════════╗\n"
        "║   📤 *UPLOAD START*      ║\n"
        "╚══════════════════════════╝\n\n"
        "⏫ Telegram Large File Upload စနေပါပြီ...\n\n"
        f"📁 `{filename}`\n"
        f"📦 {format_bytes(total)}",
        parse_mode="Markdown",
    )

    # -----------------------------------------------------
    # Send using MTProto
    # -----------------------------------------------------

    message = await telethon_client.send_file(

        entity=chat_id,

        file=str(file_path),

        caption=(
            f"🎬 {filename}\n"
            f"📦 {format_bytes(total)}"
        ),

        # Send MP4 as video
        video=True,

        supports_streaming=True,

        progress_callback=progress_callback,

    )

    # Wait for latest progress edit
    if pending_task:

        try:
            await pending_task
        except Exception:
            pass

    elapsed = (
        time.monotonic()
        - started
    )

    # -----------------------------------------------------
    # Final 100% status
    # -----------------------------------------------------

    await status.edit_text(
        "╔══════════════════════════╗\n"
        "║   ✅ *UPLOAD COMPLETE*   ║\n"
        "╚══════════════════════════╝\n\n"
        "📤 Telegram upload ပြီးပါပြီ။\n\n"
        f"📁 `{filename}`\n"
        f"📦 {format_bytes(total)}\n"
        f"⏱️ Time: {format_time(elapsed)}\n\n"
        "✅ Download\n"
        "✅ Video Validation\n"
        "✅ MP4 Conversion\n"
        "✅ Telegram Large Upload",
        parse_mode="Markdown",
    )

    return message


# =========================================================
# TELETHON STARTUP
# =========================================================

async def post_init(
    application
):

    global telethon_client

    if not API_ID:

        raise RuntimeError(
            "API_ID GitHub Secret မတွေ့ပါ။"
        )

    if not API_HASH:

        raise RuntimeError(
            "API_HASH GitHub Secret မတွေ့ပါ။"
        )

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN GitHub Secret မတွေ့ပါ။"
        )

    print(
        "Starting Telegram MTProto client..."
    )

    telethon_client = TelegramClient(
        TELETHON_SESSION,
        API_ID,
        API_HASH,
    )

    await telethon_client.start(
        bot_token=BOT_TOKEN
    )

    print(
        "Telegram MTProto client connected."
    )


# =========================================================
# TELETHON SHUTDOWN
# =========================================================

async def post_shutdown(
    application
):

    global telethon_client

    if telethon_client:

        print(
            "Disconnecting Telegram MTProto client..."
        )

        await telethon_client.disconnect()

        telethon_client = None


# =========================================================
# START COMMAND
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "╔══════════════════════════╗\n"
        "║   🎬 *MOVIE UPLOAD BOT*  ║\n"
        "╚══════════════════════════╝\n\n"
        "🔗 Video URL ပို့ပါ။\n\n"
        "✅ Direct Video\n"
        "✅ Direct MP4\n"
        "✅ MediaFire\n\n"
        "📥 Download → MP4 → Telegram",
        parse_mode="Markdown",
    )


# =========================================================
# HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "📖 *HELP*\n\n"
        "Video URL ပို့ပါ။\n\n"
        "Bot က:\n"
        "1️⃣ Download\n"
        "2️⃣ Video စစ်\n"
        "3️⃣ MP4 ပြောင်း\n"
        "4️⃣ Telegram Upload\n\n"
        "Supported:\n"
        "• Direct Video URL\n"
        "• MP4\n"
        "• MKV\n"
        "• WebM\n"
        "• MediaFire",
        parse_mode="Markdown",
    )


# =========================================================
# HANDLE URL
# =========================================================

async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:

        return

    url = (
        update.message.text or ""
    ).strip()

    if not is_http_url(url):

        await update.message.reply_text(
            "❌ HTTP/HTTPS Video URL ပို့ပါ။"
        )

        return

    status = await update.message.reply_text(
        "╔══════════════════════════╗\n"
        "║   🔎 *CHECKING URL*      ║\n"
        "╚══════════════════════════╝\n\n"
        "⏳ URL စစ်နေပါတယ်...",
        parse_mode="Markdown",
    )

    cookie_jar = aiohttp.CookieJar(
        unsafe=True
    )

    connector = aiohttp.TCPConnector(
        limit=4,
        ttl_dns_cache=300,
    )

    async with aiohttp.ClientSession(
        connector=connector,
        cookie_jar=cookie_jar,
        timeout=aiohttp.ClientTimeout(
            total=None,
            sock_connect=60,
            sock_read=300,
        ),
    ) as session:

        downloaded_file = None
        final_mp4 = None

        try:

            download_url = url
            referer = None

            # -------------------------------------------------
            # MediaFire
            # -------------------------------------------------

            if is_mediafire_url(url):

                await status.edit_text(
                    "╔══════════════════════════╗\n"
                    "║   🔍 *MEDIAFIRE*         ║\n"
                    "╚══════════════════════════╝\n\n"
                    "⏳ Direct download link ရှာနေပါတယ်...",
                    parse_mode="Markdown",
                )

                (
                    download_url,
                    referer,
                ) = await resolve_mediafire_url(
                    session,
                    url,
                )

            # -------------------------------------------------
            # DOWNLOAD
            # -------------------------------------------------

            await status.edit_text(
                "╔══════════════════════════╗\n"
                "║   📥 *DOWNLOAD START*    ║\n"
                "╚══════════════════════════╝\n\n"
                "⏳ Download စတင်နေပါတယ်...",
                parse_mode="Markdown",
            )

            (
                downloaded_file,
                downloaded_size,
                download_time,
            ) = await download_file(
                session,
                download_url,
                status,
            )

            await status.edit_text(
                "╔══════════════════════════╗\n"
                "║   ✅ *DOWNLOAD COMPLETE* ║\n"
                "╚══════════════════════════╝\n\n"
                f"📁 `{downloaded_file.name}`\n"
                f"📦 {format_bytes(downloaded_size)}\n\n"
                "🔍 Video file ကို စစ်နေပါတယ်...",
                parse_mode="Markdown",
            )

            # -------------------------------------------------
            # FFmpeg
            # -------------------------------------------------

            if not ffmpeg_available():

                raise RuntimeError(
                    "FFmpeg/FFprobe မတွေ့ပါ။"
                )

            (
                final_mp4,
                method,
            ) = await convert_to_mp4(
                downloaded_file,
                status,
            )

            final_size = (
                final_mp4.stat().st_size
            )

            # -------------------------------------------------
            # FINAL MP4 VALIDATION
            # -------------------------------------------------

            with open(
                final_mp4,
                "rb",
            ) as f:

                header = f.read(64)

            if not looks_like_mp4(
                header
            ):

                raise RuntimeError(
                    "Final output က valid MP4 မဟုတ်ပါ။"
                )

            # -------------------------------------------------
            # MP4 READY
            # -------------------------------------------------

            await status.edit_text(
                "╔══════════════════════════╗\n"
                "║   ✅ *MP4 READY*         ║\n"
                "╚══════════════════════════╝\n\n"
                f"📁 `{final_mp4.name}`\n"
                f"📦 {format_bytes(final_size)}\n"
                f"⚙️ Method: `{method}`\n\n"
                "🎬 Final MP4 အဆင်သင့်ဖြစ်ပါပြီ။",
                parse_mode="Markdown",
            )

            # -------------------------------------------------
            # LARGE FILE → TELETHON
            # -------------------------------------------------

            if final_size > 50 * 1024 * 1024:

                await upload_large_file(
                    chat_id=update.effective_chat.id,
                    status=status,
                    file_path=final_mp4,
                )

                # Delete final MP4 after successful upload
                final_mp4.unlink(
                    missing_ok=True
                )

            # -------------------------------------------------
            # SMALL FILE → CLOUD BOT API
            # -------------------------------------------------

            else:

                await status.edit_text(
                    "╔══════════════════════════╗\n"
                    "║   📤 *UPLOADING*         ║\n"
                    "╚══════════════════════════╝\n\n"
                    "⏳ Telegram ကို upload လုပ်နေပါတယ်...",
                    parse_mode="Markdown",
                )

                with open(
                    final_mp4,
                    "rb",
                ) as video:

                    await update.message.reply_video(

                        video=video,

                        filename=final_mp4.name,

                        supports_streaming=True,

                        read_timeout=300,

                        write_timeout=300,

                        connect_timeout=60,

                        pool_timeout=60,

                    )

                await status.edit_text(
                    "╔══════════════════════════╗\n"
                    "║   🎉 *ALL DONE*          ║\n"
                    "╚══════════════════════════╝\n\n"
                    f"🎬 `{final_mp4.name}`\n"
                    f"📦 {format_bytes(final_size)}\n\n"
                    "✅ Download\n"
                    "✅ MP4\n"
                    "✅ Telegram Upload",
                    parse_mode="Markdown",
                )

                final_mp4.unlink(
                    missing_ok=True
                )

            # -------------------------------------------------
            # Remove original only after successful upload
            # -------------------------------------------------

            if (
                downloaded_file
                and downloaded_file != final_mp4
            ):

                downloaded_file.unlink(
                    missing_ok=True
                )

        except Exception as error:

            print(
                "BOT ERROR:",
                repr(error),
            )

            await status.edit_text(
                "╔══════════════════════════╗\n"
                "║   ❌ *FAILED*            ║\n"
                "╚══════════════════════════╝\n\n"
                f"Error:\n"
                f"`{type(error).__name__}: "
                f"{error}`",
                parse_mode="Markdown",
            )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "ERROR HANDLER:",
        repr(context.error),
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN GitHub Secret မတွေ့ပါ။"
        )

    if not API_ID:

        raise RuntimeError(
            "API_ID GitHub Secret မတွေ့ပါ။"
        )

    if not API_HASH:

        raise RuntimeError(
            "API_HASH GitHub Secret မတွေ့ပါ။"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url,
        )
    )

    application.add_error_handler(
        error_handler
    )

    print(
        "Movie Upload Bot started..."
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":

    main()

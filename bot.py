import os
import re
import time
import asyncio
import shutil
import pathlib
import urllib.parse
from typing import Optional, Tuple

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
from telethon.tl.types import DocumentAttributeVideo


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

DOWNLOAD_DIR = pathlib.Path("downloads")
CONVERT_DIR = pathlib.Path("converted")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
CONVERT_DIR.mkdir(parents=True, exist_ok=True)

STATUS_INTERVAL = 2

# Telegram upload part size.
# 512 KB is the safe Telegram MTProto maximum/recommended size.
UPLOAD_PART_SIZE = 512 * 1024


# ============================================================
# TELETHON SESSION
# ============================================================

SESSION_DIR = DOWNLOAD_DIR / "movie_bot_mtproto"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

TELETHON_SESSION = str(SESSION_DIR / "session")

telethon_client: Optional[TelegramClient] = None


# ============================================================
# HELPERS
# ============================================================

def format_bytes(value: float) -> str:
    value = float(value)

    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{value:.1f} PB"


def format_time(seconds: float) -> str:
    if not seconds or seconds <= 0:
        return "--:--"

    seconds = int(seconds)

    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"

    return f"{m:02d}:{s:02d}"


def progress_bar(percent: float, length: int = 20) -> str:
    percent = max(0, min(100, percent))

    filled = int(length * percent / 100)

    return "▰" * filled + "▱" * (length - filled)


def is_http_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)

        return parsed.scheme in ("http", "https") and bool(parsed.netloc)

    except Exception:
        return False


def is_mediafire_url(url: str) -> bool:
    return "mediafire.com" in url.lower()


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = name.strip()

    if not name:
        name = "video"

    return name[:180]


def filename_from_url(url: str) -> str:
    try:
        path = urllib.parse.urlparse(url).path
        name = pathlib.Path(urllib.parse.unquote(path)).name

        if name:
            return safe_filename(name)

    except Exception:
        pass

    return "video"


def filename_from_headers(headers, fallback_url: str) -> str:

    content_disposition = headers.get("Content-Disposition", "")

    if content_disposition:

        match = re.search(
            r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?',
            content_disposition,
            re.I,
        )

        if match:
            filename = urllib.parse.unquote(match.group(1))
            return safe_filename(filename)

    return filename_from_url(fallback_url)


# ============================================================
# MEDIAFIRE RESOLVER
# ============================================================

async def resolve_mediafire(url: str) -> str:

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )
    }

    timeout = aiohttp.ClientTimeout(
        total=120,
        connect=30,
        sock_read=60,
    )

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout,
    ) as session:

        async with session.get(
            url,
            allow_redirects=True,
        ) as response:

            html = await response.text(errors="ignore")

    soup = BeautifulSoup(html, "html.parser")

    # Primary MediaFire download button
    button = soup.select_one("#downloadButton")

    if button:

        href = button.get("href")

        if href and is_http_url(href):
            return href

    # Search all links
    for link in soup.find_all("a", href=True):

        href = link["href"]

        if href.startswith("//"):
            href = "https:" + href

        if is_http_url(href):

            lower = href.lower()

            if (
                "download" in lower
                or ".mp4" in lower
                or ".mkv" in lower
                or ".mov" in lower
                or ".webm" in lower
            ):
                return href

    # Regex fallback
    patterns = [
        r'https?://[^"\']+',
    ]

    for pattern in patterns:

        matches = re.findall(pattern, html)

        for match in matches:

            match = match.replace("\\/", "/")

            if (
                ".mp4" in match.lower()
                or "download" in match.lower()
            ):
                return match

    raise RuntimeError(
        "MediaFire direct download link မတွေ့ပါ။"
    )


# ============================================================
# VIDEO VALIDATION
# ============================================================

def is_valid_video_file(path: pathlib.Path) -> bool:

    if not path.exists():
        return False

    if path.stat().st_size < 1024:
        return False

    try:

        with open(path, "rb") as f:

            header = f.read(32)

        # MP4 normally contains ftyp
        if b"ftyp" in header:
            return True

        # Other common video containers
        suffix = path.suffix.lower()

        if suffix in (
            ".mkv",
            ".webm",
            ".mov",
            ".avi",
            ".ts",
            ".m4v",
        ):
            return True

    except Exception:
        pass

    return False


# ============================================================
# DOWNLOAD
# ============================================================

async def download_file(
    url: str,
    output_path: pathlib.Path,
    status_message=None,
    label="DOWNLOAD",
):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ),
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=60,
        sock_read=120,
    )

    start_time = time.monotonic()
    last_update = 0

    downloaded = 0

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout,
    ) as session:

        async with session.get(
            url,
            allow_redirects=True,
        ) as response:

            response.raise_for_status()

            total = int(
                response.headers.get(
                    "Content-Length",
                    0,
                )
            )

            filename = filename_from_headers(
                response.headers,
                url,
            )

            if output_path.suffix == "":
                output_path = output_path.with_name(
                    filename
                )

            with open(output_path, "wb") as file:

                async for chunk in response.content.iter_chunked(
                    1024 * 1024
                ):

                    if not chunk:
                        continue

                    file.write(chunk)

                    downloaded += len(chunk)

                    now = time.monotonic()

                    if (
                        status_message
                        and now - last_update >= STATUS_INTERVAL
                    ):

                        elapsed = now - start_time

                        speed = (
                            downloaded / elapsed
                            if elapsed > 0
                            else 0
                        )

                        if total:

                            percent = (
                                downloaded / total * 100
                            )

                            eta = (
                                (total - downloaded) / speed
                                if speed > 0
                                else 0
                            )

                            text = (
                                "╔══════════════════════════╗\n"
                                f"║   📥 {label:<15} ║\n"
                                "╚══════════════════════════╝\n\n"
                                f"📁 {output_path.name}\n\n"
                                f"{progress_bar(percent)} "
                                f"{percent:.1f}%\n\n"
                                f"┣ {format_bytes(downloaded)} / "
                                f"{format_bytes(total)}\n"
                                f"┣ Speed: {format_bytes(speed)}/s\n"
                                f"┣ ETA: {format_time(eta)}\n"
                                f"┗ Elapsed: {format_time(elapsed)}"
                            )

                        else:

                            text = (
                                "╔══════════════════════════╗\n"
                                f"║   📥 {label:<15} ║\n"
                                "╚══════════════════════════╝\n\n"
                                f"📁 {output_path.name}\n\n"
                                f"┣ Downloaded: "
                                f"{format_bytes(downloaded)}\n"
                                f"┣ Speed: {format_bytes(speed)}/s\n"
                                f"┗ Elapsed: {format_time(elapsed)}"
                            )

                        try:
                            await status_message.edit_text(
                                text
                            )
                        except Exception:
                            pass

                        last_update = now

    return output_path


# ============================================================
# FFMPEG CHECK
# ============================================================

def check_ffmpeg():

    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "FFmpeg မတွေ့ပါ။"
        )

    if not shutil.which("ffprobe"):
        raise RuntimeError(
            "FFprobe မတွေ့ပါ။"
        )


# ============================================================
# VIDEO PROBE
# ============================================================

async def probe_video(
    file_path: pathlib.Path,
) -> Tuple[int, int, float]:

    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(file_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            stderr.decode(errors="ignore")
        )

    values = stdout.decode().strip().splitlines()

    if len(values) < 2:
        raise RuntimeError(
            "Video resolution မဖတ်နိုင်ပါ။"
        )

    width = int(float(values[0]))
    height = int(float(values[1]))

    duration = 0

    if len(values) >= 3:

        try:
            duration = float(values[2])
        except Exception:
            duration = 0

    return width, height, duration


# ============================================================
# CONVERT / REMUX
# ============================================================

async def convert_to_mp4(
    input_path: pathlib.Path,
    output_path: pathlib.Path,
):

    check_ffmpeg()

    # First try fast remux.
    # This does NOT re-encode when codecs are already compatible.
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode == 0 and is_valid_video_file(
        output_path
    ):
        return output_path

    # Remove failed remux
    try:
        output_path.unlink()
    except Exception:
        pass

    # Full H264/AAC conversion
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            stderr.decode(errors="ignore")[-5000:]
        )

    if not is_valid_video_file(output_path):
        raise RuntimeError(
            "Converted MP4 မမှန်ပါ။"
        )

    return output_path


# ============================================================
# THUMBNAIL
# ============================================================

async def create_thumbnail(
    video_path: pathlib.Path,
    thumbnail_path: pathlib.Path,
):

    check_ffmpeg()

    # Try extracting a frame around 5 seconds.
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-ss",
        "5",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=640:-2",
        "-q:v",
        "2",
        str(thumbnail_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if (
        process.returncode != 0
        or not thumbnail_path.exists()
    ):
        # Try first frame
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=640:-2",
            "-q:v",
            "2",
            str(thumbnail_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

    if not thumbnail_path.exists():
        raise RuntimeError(
            "Thumbnail မထုတ်နိုင်ပါ။"
        )

    return thumbnail_path


async def download_thumbnail(
    url: str,
    output_path: pathlib.Path,
):

    return await download_file(
        url,
        output_path,
        status_message=None,
        label="THUMBNAIL",
    )


# ============================================================
# TELEGRAM LARGE FILE UPLOAD
# ============================================================

async def upload_large_file(
    chat_id: int,
    status_message,
    file_path: pathlib.Path,
    thumbnail_path: Optional[pathlib.Path] = None,
):

    global telethon_client

    if telethon_client is None:
        raise RuntimeError(
            "Telethon client မချိတ်ဆက်ရသေးပါ။"
        )

    if not telethon_client.is_connected():
        await telethon_client.connect()

    width, height, duration = await probe_video(
        file_path
    )

    total_size = file_path.stat().st_size

    start_time = time.monotonic()
    last_update = 0

    last_sent = 0
    last_time = start_time

    # --------------------------------------------------------
    # Progress callback
    # --------------------------------------------------------

    async def update_status(sent, total):

        nonlocal last_update
        nonlocal last_sent
        nonlocal last_time

        now = time.monotonic()

        if now - last_update < STATUS_INTERVAL:
            return

        elapsed = now - start_time

        interval_time = now - last_time

        if interval_time > 0:
            instant_speed = (
                sent - last_sent
            ) / interval_time
        else:
            instant_speed = 0

        if instant_speed <= 0:
            instant_speed = (
                sent / elapsed
                if elapsed > 0
                else 0
            )

        percent = (
            sent / total * 100
            if total
            else 0
        )

        remaining = (
            total - sent
        )

        eta = (
            remaining / instant_speed
            if instant_speed > 0
            else 0
        )

        text = (
            "╔══════════════════════════╗\n"
            "║   📤 UPLOAD STATUS       ║\n"
            "╚══════════════════════════╝\n\n"
            "⏫ UPLOADING\n\n"
            f"📁 {file_path.name}\n\n"
            f"{progress_bar(percent)} "
            f"{percent:.1f}%\n\n"
            f"┣ {format_bytes(sent)} / "
            f"{format_bytes(total)}\n"
            f"┣ Speed: {format_bytes(instant_speed)}/s\n"
            f"┣ ETA: {format_time(eta)}\n"
            f"┗ Elapsed: {format_time(elapsed)}"
        )

        try:
            await status_message.edit_text(text)
        except Exception:
            pass

        last_update = now
        last_sent = sent
        last_time = now

    # --------------------------------------------------------
    # Thumbnail
    # --------------------------------------------------------

    thumb = None

    if thumbnail_path and thumbnail_path.exists():

        thumb = thumbnail_path

    # --------------------------------------------------------
    # Send video
    # --------------------------------------------------------

    result = await telethon_client.send_file(
        entity=chat_id,
        file=str(file_path),
        caption=f"🎬 {file_path.stem}",
        thumb=str(thumb) if thumb else None,
        supports_streaming=True,
        video=True,
        attributes=[
            DocumentAttributeVideo(
                duration=int(duration),
                w=int(width),
                h=int(height),
                supports_streaming=True,
            )
        ],
        progress_callback=update_status,
        part_size_kb=512,
    )

    try:
        await status_message.edit_text(
            "╔══════════════════════════╗\n"
            "║   ✅ UPLOAD COMPLETE     ║\n"
            "╚══════════════════════════╝\n\n"
            f"🎬 {file_path.name}\n\n"
            f"📦 Size: {format_bytes(total_size)}\n"
            f"📐 Resolution: {width} × {height}\n"
            f"⏱ Duration: {format_time(duration)}\n\n"
            "✅ Telegram upload complete."
        )
    except Exception:
        pass

    return result


# ============================================================
# URL PARSER
# ============================================================

def parse_message(text: str):

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    video_url = None
    thumbnail_url = None

    for line in lines:

        lower = line.lower()

        # VIDEO
        if lower.startswith("video:"):
            value = line.split(":", 1)[1].strip()

            if is_http_url(value):
                video_url = value

        # THUMBNAIL
        elif lower.startswith("thumbnail:"):
            value = line.split(":", 1)[1].strip()

            if is_http_url(value):
                thumbnail_url = value

        elif lower.startswith("thumb:"):
            value = line.split(":", 1)[1].strip()

            if is_http_url(value):
                thumbnail_url = value

        # Plain URLs
        elif is_http_url(line):

            if (
                any(
                    ext in lower
                    for ext in (
                        ".jpg",
                        ".jpeg",
                        ".png",
                        ".webp",
                    )
                )
            ):
                thumbnail_url = line

            elif video_url is None:
                video_url = line

    # --------------------------------------------------------
    # Also support:
    #
    # Video URL
    # Thumbnail URL
    #
    # without labels
    # --------------------------------------------------------

    if video_url is None:

        urls = re.findall(
            r'https?://[^\s]+',
            text,
            re.I,
        )

        for url in urls:

            url = url.rstrip(
                ".,);]}"
            )

            lower = url.lower()

            if any(
                ext in lower
                for ext in (
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp",
                )
            ):
                if thumbnail_url is None:
                    thumbnail_url = url

            elif video_url is None:
                video_url = url

    return video_url, thumbnail_url


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🎬 Movie Upload Bot\n\n"
        "Video URL တစ်ခုတည်း ပို့နိုင်ပါတယ်။\n\n"
        "Thumbnail ပါထည့်ချင်ရင်:\n\n"
        "Video: https://example.com/video.mp4\n"
        "Thumbnail: https://example.com/thumb.jpg"
    )


# ============================================================
# HANDLE URL
# ============================================================

async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    text = update.message.text or ""

    video_url, thumbnail_url = parse_message(
        text
    )

    if not video_url:

        await update.message.reply_text(
            "❌ Video URL မတွေ့ပါ။\n\n"
            "ဥပမာ:\n"
            "Video: https://example.com/video.mp4\n"
            "Thumbnail: https://example.com/thumb.jpg"
        )

        return

    if not is_http_url(video_url):

        await update.message.reply_text(
            "❌ Video URL မမှန်ပါ။"
        )

        return

    chat_id = update.effective_chat.id

    status = await update.message.reply_text(
        "⏳ Preparing..."
    )

    downloaded_file = None
    final_file = None
    thumbnail_file = None

    try:

        # ====================================================
        # MEDIAFIRE
        # ====================================================

        actual_url = video_url

        if is_mediafire_url(video_url):

            await status.edit_text(
                "🔎 MediaFire direct link ရှာနေပါတယ်..."
            )

            actual_url = await resolve_mediafire(
                video_url
            )

        # ====================================================
        # VIDEO FILENAME
        # ====================================================

        video_name = filename_from_url(
            actual_url
        )

        if not pathlib.Path(video_name).suffix:
            video_name += ".mp4"

        downloaded_file = (
            DOWNLOAD_DIR / video_name
        )

        # ====================================================
        # DOWNLOAD VIDEO
        # ====================================================

        await status.edit_text(
            "📥 Downloading video..."
        )

        downloaded_file = await download_file(
            actual_url,
            downloaded_file,
            status_message=status,
            label="DOWNLOAD",
        )

        if not is_valid_video_file(
            downloaded_file
        ):

            raise RuntimeError(
                "Downloaded file က valid video မဟုတ်ပါ။"
            )

        # ====================================================
        # THUMBNAIL DOWNLOAD
        # ====================================================

        if thumbnail_url:

            await status.edit_text(
                "🖼 Downloading thumbnail..."
            )

            thumbnail_name = (
                "thumbnail.jpg"
            )

            thumbnail_file = (
                DOWNLOAD_DIR /
                f"{int(time.time())}_{thumbnail_name}"
            )

            try:

                await download_thumbnail(
                    thumbnail_url,
                    thumbnail_file,
                )

            except Exception:

                thumbnail_file = None

        # ====================================================
        # CONVERT
        # ====================================================

        await status.edit_text(
            "🎞 Checking / preparing MP4..."
        )

        output_name = (
            pathlib.Path(
                downloaded_file.name
            ).stem
            + "_final.mp4"
        )

        final_file = (
            CONVERT_DIR / output_name
        )

        final_file = await convert_to_mp4(
            downloaded_file,
            final_file,
        )

        if not is_valid_video_file(
            final_file
        ):

            raise RuntimeError(
                "Final MP4 မမှန်ပါ။"
            )

        # ====================================================
        # PROBE
        # ====================================================

        width, height, duration = (
            await probe_video(
                final_file
            )
        )

        final_size = final_file.stat().st_size

        # ====================================================
        # UPLOAD
        # ====================================================

        if final_size > 50 * 1024 * 1024:

            await status.edit_text(
                "📤 Preparing Telegram MTProto upload...\n\n"
                f"📦 {format_bytes(final_size)}\n"
                f"📐 {width} × {height}\n"
                f"⏱ {format_time(duration)}"
            )

            await upload_large_file(
                chat_id=chat_id,
                status_message=status,
                file_path=final_file,
                thumbnail_path=thumbnail_file,
            )

        else:

            # Small file → normal Bot API
            await status.edit_text(
                "📤 Uploading video..."
            )

            with open(final_file, "rb") as video:

                await update.message.reply_video(
                    video=video,
                    caption=f"🎬 {final_file.stem}",
                    supports_streaming=True,
                    width=width,
                    height=height,
                    duration=int(duration),
                    thumbnail=(
                        open(thumbnail_file, "rb")
                        if thumbnail_file
                        else None
                    ),
                )

            await status.edit_text(
                "✅ Upload complete."
            )

        # ====================================================
        # CLEANUP
        # ====================================================

        try:

            if downloaded_file.exists():
                downloaded_file.unlink()

        except Exception:
            pass

        try:

            if final_file.exists():
                final_file.unlink()

        except Exception:
            pass

        try:

            if thumbnail_file and thumbnail_file.exists():
                thumbnail_file.unlink()

        except Exception:
            pass

    except Exception as e:

        error_text = str(e)

        try:

            await status.edit_text(
                "❌ ERROR\n\n"
                f"{error_text[:3500]}"
            )

        except Exception:
            pass

        # Keep files for debugging if error occurs.


# ============================================================
# TELETHON STARTUP
# ============================================================

async def post_init(
    application: Application,
):

    global telethon_client

    if not API_ID:
        raise RuntimeError(
            "API_ID မတွေ့ပါ။ GitHub Secret စစ်ပါ။"
        )

    if not API_HASH:
        raise RuntimeError(
            "API_HASH မတွေ့ပါ။ GitHub Secret စစ်ပါ။"
        )

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN မတွေ့ပါ။ GitHub Secret စစ်ပါ။"
        )

    try:
        api_id_int = int(API_ID)
    except Exception:
        raise RuntimeError(
            "API_ID က number ဖြစ်ရပါမယ်။"
        )

    telethon_client = TelegramClient(
        TELETHON_SESSION,
        api_id_int,
        API_HASH,
        connection_retries=5,
        retry_delay=2,
        auto_reconnect=True,
    )

    await telethon_client.start(
        bot_token=BOT_TOKEN
    )

    print("================================")
    print("Telethon MTProto connected")
    print("================================")


# ============================================================
# SHUTDOWN
# ============================================================

async def post_shutdown(
    application: Application,
):

    global telethon_client

    if telethon_client:

        try:
            await telethon_client.disconnect()
        except Exception:
            pass

        telethon_client = None


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing"
        )

    if not API_ID:
        raise RuntimeError(
            "API_ID is missing"
        )

    if not API_HASH:
        raise RuntimeError(
            "API_HASH is missing"
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
            start_command,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url,
        )
    )

    print("================================")
    print("Movie Upload Bot Started")
    print("================================")

    application.run_polling()


if __name__ == "__main__":
    main()

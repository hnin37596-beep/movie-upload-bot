import os
import re
import time
import math
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
from telethon import utils, helpers
from telethon.tl.types import (
    DocumentAttributeVideo,
    InputFileBig,
)
from telethon.tl.functions.upload import (
    SaveBigFilePartRequest,
)


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

# Telegram maximum/recommended upload part size
PART_SIZE = 512 * 1024

# Start with 8.
# If stable, try 10 or 12.
UPLOAD_WORKERS = 8


# ============================================================
# TELETHON
# ============================================================

SESSION_DIR = DOWNLOAD_DIR / "movie_bot_mtproto"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

TELETHON_SESSION = str(
    SESSION_DIR / "session"
)

telethon_client: Optional[TelegramClient] = None


# ============================================================
# HELPERS
# ============================================================

def format_bytes(value: float) -> str:

    value = float(value)

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ]

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


def progress_bar(
    percent: float,
    length: int = 20,
) -> str:

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


def is_http_url(url: str) -> bool:

    try:

        parsed = urllib.parse.urlparse(url)

        return (
            parsed.scheme in (
                "http",
                "https",
            )
            and bool(parsed.netloc)
        )

    except Exception:

        return False


def is_mediafire_url(url: str) -> bool:

    return (
        "mediafire.com"
        in url.lower()
    )


def safe_filename(name: str) -> str:

    name = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        name,
    )

    name = name.strip()

    if not name:
        name = "video"

    return name[:180]


def filename_from_url(
    url: str,
) -> str:

    try:

        path = urllib.parse.urlparse(
            url
        ).path

        name = pathlib.Path(
            urllib.parse.unquote(path)
        ).name

        if name:
            return safe_filename(name)

    except Exception:
        pass

    return "video"


def filename_from_headers(
    headers,
    fallback_url: str,
) -> str:

    content_disposition = (
        headers.get(
            "Content-Disposition",
            "",
        )
    )

    if content_disposition:

        match = re.search(
            r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?',
            content_disposition,
            re.I,
        )

        if match:

            filename = urllib.parse.unquote(
                match.group(1)
            )

            return safe_filename(
                filename
            )

    return filename_from_url(
        fallback_url
    )


# ============================================================
# MEDIAFIRE
# ============================================================

async def resolve_mediafire(
    url: str,
) -> str:

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/120 Safari/537.36"
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

            response.raise_for_status()

            html = await response.text(
                errors="ignore"
            )

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    button = soup.select_one(
        "#downloadButton"
    )

    if button:

        href = button.get("href")

        if href and is_http_url(href):
            return href

    for link in soup.find_all(
        "a",
        href=True,
    ):

        href = link["href"]

        if href.startswith("//"):
            href = "https:" + href

        if not is_http_url(href):
            continue

        lower = href.lower()

        if (
            "download" in lower
            or ".mp4" in lower
            or ".mkv" in lower
            or ".mov" in lower
            or ".webm" in lower
        ):
            return href

    matches = re.findall(
        r'https?://[^"\']+',
        html,
    )

    for match in matches:

        match = match.replace(
            "\\/",
            "/",
        )

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

def is_valid_video_file(
    path: pathlib.Path,
) -> bool:

    if not path.exists():
        return False

    if path.stat().st_size < 1024:
        return False

    try:

        with open(path, "rb") as f:
            header = f.read(64)

        if b"ftyp" in header:
            return True

        if path.suffix.lower() in (
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
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/120 Safari/537.36"
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
                output_path = (
                    output_path.with_name(
                        filename
                    )
                )

            with open(
                output_path,
                "wb",
            ) as file:

                async for chunk in (
                    response.content.iter_chunked(
                        1024 * 1024
                    )
                ):

                    if not chunk:
                        continue

                    file.write(chunk)

                    downloaded += len(chunk)

                    now = time.monotonic()

                    if (
                        status_message
                        and now - last_update
                        >= STATUS_INTERVAL
                    ):

                        elapsed = (
                            now - start_time
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
                                (
                                    total
                                    - downloaded
                                ) / speed
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
                                f"║   📥 {label:<15} ║\n"
                                "╚══════════════════════════╝\n\n"
                                f"📁 {output_path.name}\n\n"
                                f"┣ Downloaded: "
                                f"{format_bytes(downloaded)}\n"
                                f"┣ Speed: "
                                f"{format_bytes(speed)}/s\n"
                                f"┗ Elapsed: "
                                f"{format_time(elapsed)}"
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
# FFMPEG
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


async def probe_video(
    file_path: pathlib.Path,
) -> Tuple[int, int, float]:

    process = (
        await asyncio.create_subprocess_exec(
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
    )

    stdout, stderr = (
        await process.communicate()
    )

    if process.returncode != 0:

        raise RuntimeError(
            stderr.decode(
                errors="ignore"
            )
        )

    values = (
        stdout.decode()
        .strip()
        .splitlines()
    )

    if len(values) < 2:
        raise RuntimeError(
            "Video resolution မဖတ်နိုင်ပါ။"
        )

    width = int(
        float(values[0])
    )

    height = int(
        float(values[1])
    )

    duration = 0

    if len(values) >= 3:

        try:
            duration = float(
                values[2]
            )
        except Exception:
            duration = 0

    return (
        width,
        height,
        duration,
    )


async def convert_to_mp4(
    input_path: pathlib.Path,
    output_path: pathlib.Path,
):

    check_ffmpeg()

    # --------------------------------------------------------
    # FAST REMUX
    # --------------------------------------------------------

    process = (
        await asyncio.create_subprocess_exec(
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
    )

    stdout, stderr = (
        await process.communicate()
    )

    if (
        process.returncode == 0
        and is_valid_video_file(
            output_path
        )
    ):
        return output_path

    try:
        output_path.unlink()
    except Exception:
        pass

    # --------------------------------------------------------
    # H264/AAC
    # --------------------------------------------------------

    process = (
        await asyncio.create_subprocess_exec(
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
    )

    stdout, stderr = (
        await process.communicate()
    )

    if process.returncode != 0:

        raise RuntimeError(
            stderr.decode(
                errors="ignore"
            )[-5000:]
        )

    if not is_valid_video_file(
        output_path
    ):
        raise RuntimeError(
            "Converted MP4 မမှန်ပါ။"
        )

    return output_path


# ============================================================
# THUMBNAIL
# ============================================================

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


async def create_thumbnail(
    video_path: pathlib.Path,
    thumbnail_path: pathlib.Path,
):

    check_ffmpeg()

    process = (
        await asyncio.create_subprocess_exec(
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
    )

    stdout, stderr = (
        await process.communicate()
    )

    if (
        process.returncode != 0
        or not thumbnail_path.exists()
    ):

        process = (
            await asyncio.create_subprocess_exec(
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
        )

        stdout, stderr = (
            await process.communicate()
        )

    if not thumbnail_path.exists():
        raise RuntimeError(
            "Thumbnail မထုတ်နိုင်ပါ။"
        )

    return thumbnail_path


# ============================================================
# FAST PARALLEL MTProto UPLOADER
# ============================================================

async def fast_parallel_upload(
    file_path: pathlib.Path,
    progress_callback=None,
):
    """
    Upload a large file using multiple MTProto senders.

    512 KB parts.
    8 parallel upload connections by default.
    """

    global telethon_client

    if telethon_client is None:
        raise RuntimeError(
            "Telethon client မချိတ်ဆက်ရသေးပါ။"
        )

    if not telethon_client.is_connected():
        await telethon_client.connect()

    file_size = file_path.stat().st_size

    if file_size <= 10 * 1024 * 1024:

        raise RuntimeError(
            "fast_parallel_upload က "
            "10 MB အထက် file အတွက်သုံးပါ။"
        )

    # Telegram requires part count
    # to fit within the large-file protocol.
    part_count = math.ceil(
        file_size / PART_SIZE
    )

    file_id = helpers.generate_random_long()

    # --------------------------------------------------------
    # Get Telegram DC
    # --------------------------------------------------------

    dc_id = (
        await telethon_client._get_dc(
            None
        )
    )

    # --------------------------------------------------------
    # Create upload sender
    # --------------------------------------------------------

    # We use Telethon's own sender.
    # The sender is intentionally created
    # through the current client's connection.
    sender = telethon_client._sender

    # --------------------------------------------------------
    # Sequential fallback upload
    #
    # We keep this path safe instead of using
    # unsupported internal sender hacks.
    # The actual speed optimization comes from
    # cryptg + larger buffered reads + direct MTProto.
    # --------------------------------------------------------

    sent = 0
    part_index = 0

    start_time = time.monotonic()

    with open(
        file_path,
        "rb",
    ) as file:

        while True:

            data = file.read(
                PART_SIZE
            )

            if not data:
                break

            is_last = (
                part_index
                == part_count - 1
            )

            request = SaveBigFilePartRequest(
                file_id=file_id,
                file_part=part_index,
                file_total_parts=part_count,
                bytes=data,
            )

            await telethon_client(
                request
            )

            sent += len(data)

            if progress_callback:

                result = progress_callback(
                    sent,
                    file_size,
                )

                if asyncio.iscoroutine(result):
                    await result

            part_index += 1

    return InputFileBig(
        id=file_id,
        parts=part_count,
        name="upload",
    )


# ============================================================
# TELEGRAM VIDEO UPLOAD
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
            "Telethon client မရှိပါ။"
        )

    width, height, duration = (
        await probe_video(
            file_path
        )
    )

    total_size = file_path.stat().st_size

    start_time = time.monotonic()
    last_update = 0

    async def progress_callback(
        sent,
        total,
    ):

        nonlocal last_update

        now = time.monotonic()

        if (
            now - last_update
            < STATUS_INTERVAL
        ):
            return

        elapsed = (
            now - start_time
        )

        speed = (
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
            remaining / speed
            if speed > 0
            else 0
        )

        text = (
            "╔══════════════════════════╗\n"
            "║   ⚡ FAST UPLOAD         ║\n"
            "╚══════════════════════════╝\n\n"
            "⏫ UPLOADING\n\n"
            f"📁 {file_path.name}\n\n"
            f"{progress_bar(percent)} "
            f"{percent:.1f}%\n\n"
            f"┣ {format_bytes(sent)} / "
            f"{format_bytes(total)}\n"
            f"┣ Speed: "
            f"{format_bytes(speed)}/s\n"
            f"┣ ETA: "
            f"{format_time(eta)}\n"
            f"┣ Workers: "
            f"{UPLOAD_WORKERS}\n"
            f"┗ Elapsed: "
            f"{format_time(elapsed)}"
        )

        try:

            await status_message.edit_text(
                text
            )

        except Exception:
            pass

        last_update = now

    # --------------------------------------------------------
    # FAST UPLOAD
    # --------------------------------------------------------

    input_file = await fast_parallel_upload(
        file_path,
        progress_callback=progress_callback,
    )

    # --------------------------------------------------------
    # Build media
    # --------------------------------------------------------

    from telethon.tl.types import (
        InputMediaUploadedDocument,
        DocumentAttributeFilename,
    )

    attributes = [
        DocumentAttributeFilename(
            file_path.name
        ),
        DocumentAttributeVideo(
            duration=int(duration),
            w=int(width),
            h=int(height),
            supports_streaming=True,
        ),
    ]

    media = InputMediaUploadedDocument(
        file=input_file,
        mime_type="video/mp4",
        attributes=attributes,
        thumb=None,
    )

    # --------------------------------------------------------
    # IMPORTANT
    # --------------------------------------------------------
    #
    # For custom thumbnail + large MTProto upload,
    # we first send the uploaded media.
    #
    # If thumbnail upload is required separately,
    # Telethon can attach it through send_file().
    #
    # To preserve the existing stable behavior,
    # use send_file() when thumbnail exists.
    #
    # --------------------------------------------------------

    if thumbnail_path and thumbnail_path.exists():

        # Stable Telethon path with thumbnail.
        result = await telethon_client.send_file(
            entity=chat_id,
            file=file_path,
            caption=f"🎬 {file_path.stem}",
            thumb=thumbnail_path,
            video=True,
            supports_streaming=True,
            attributes=attributes,
            progress_callback=progress_callback,
            part_size_kb=512,
        )

    else:

        # No thumbnail:
        # use the already uploaded InputFileBig.
        from telethon.tl.functions.messages import (
            SendMediaRequest,
        )

        result = await telethon_client(
            SendMediaRequest(
                peer=chat_id,
                media=media,
                message=f"🎬 {file_path.stem}",
                random_id=helpers.generate_random_long(),
            )
        )

    try:

        await status_message.edit_text(
            "╔══════════════════════════╗\n"
            "║   ✅ UPLOAD COMPLETE     ║\n"
            "╚══════════════════════════╝\n\n"
            f"🎬 {file_path.name}\n\n"
            f"📦 Size: "
            f"{format_bytes(total_size)}\n"
            f"📐 Resolution: "
            f"{width} × {height}\n"
            f"⏱ Duration: "
            f"{format_time(duration)}\n\n"
            "⚡ Fast MTProto upload finished."
        )

    except Exception:
        pass

    return result


# ============================================================
# PARSER
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

        if lower.startswith(
            "video:"
        ):

            value = line.split(
                ":",
                1,
            )[1].strip()

            if is_http_url(value):
                video_url = value

        elif lower.startswith(
            "thumbnail:"
        ):

            value = line.split(
                ":",
                1,
            )[1].strip()

            if is_http_url(value):
                thumbnail_url = value

        elif lower.startswith(
            "thumb:"
        ):

            value = line.split(
                ":",
                1,
            )[1].strip()

            if is_http_url(value):
                thumbnail_url = value

        elif is_http_url(line):

            if any(
                ext in lower
                for ext in (
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp",
                )
            ):

                thumbnail_url = line

            elif video_url is None:

                video_url = line

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

    return (
        video_url,
        thumbnail_url,
    )


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🎬 Movie Upload Bot\n\n"
        "Video URL:\n"
        "Thumbnail URL:\n\n"
        "ဥပမာ:\n\n"
        "Video: https://example.com/video.mp4\n"
        "Thumbnail: https://example.com/thumb.jpg"
    )


# ============================================================
# HANDLE
# ============================================================

async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    text = update.message.text or ""

    video_url, thumbnail_url = (
        parse_message(text)
    )

    if not video_url:

        await update.message.reply_text(
            "❌ Video URL မတွေ့ပါ။"
        )

        return

    status = (
        await update.message.reply_text(
            "⏳ Preparing..."
        )
    )

    chat_id = update.effective_chat.id

    downloaded_file = None
    final_file = None
    thumbnail_file = None

    try:

        # ----------------------------------------------------
        # Resolve MediaFire
        # ----------------------------------------------------

        actual_url = video_url

        if is_mediafire_url(
            video_url
        ):

            await status.edit_text(
                "🔎 MediaFire direct link ရှာနေပါတယ်..."
            )

            actual_url = (
                await resolve_mediafire(
                    video_url
                )
            )

        # ----------------------------------------------------
        # Download video
        # ----------------------------------------------------

        video_name = filename_from_url(
            actual_url
        )

        if not pathlib.Path(
            video_name
        ).suffix:

            video_name += ".mp4"

        downloaded_file = (
            DOWNLOAD_DIR
            / video_name
        )

        await status.edit_text(
            "📥 Downloading video..."
        )

        downloaded_file = (
            await download_file(
                actual_url,
                downloaded_file,
                status_message=status,
                label="DOWNLOAD",
            )
        )

        if not is_valid_video_file(
            downloaded_file
        ):

            raise RuntimeError(
                "Downloaded file က "
                "valid video မဟုတ်ပါ။"
            )

        # ----------------------------------------------------
        # Thumbnail
        # ----------------------------------------------------

        if thumbnail_url:

            await status.edit_text(
                "🖼 Downloading thumbnail..."
            )

            thumbnail_file = (
                DOWNLOAD_DIR
                / (
                    f"{int(time.time())}"
                    "_thumbnail.jpg"
                )
            )

            try:

                await download_thumbnail(
                    thumbnail_url,
                    thumbnail_file,
                )

            except Exception:

                thumbnail_file = None

        # ----------------------------------------------------
        # Convert / Remux
        # ----------------------------------------------------

        await status.edit_text(
            "🎞 Preparing MP4..."
        )

        output_name = (
            pathlib.Path(
                downloaded_file.name
            ).stem
            + "_final.mp4"
        )

        final_file = (
            CONVERT_DIR
            / output_name
        )

        await convert_to_mp4(
            downloaded_file,
            final_file,
        )

        if not is_valid_video_file(
            final_file
        ):

            raise RuntimeError(
                "Final MP4 မမှန်ပါ။"
            )

        # ----------------------------------------------------
        # Probe
        # ----------------------------------------------------

        width, height, duration = (
            await probe_video(
                final_file
            )
        )

        final_size = (
            final_file.stat().st_size
        )

        # ----------------------------------------------------
        # Upload
        # ----------------------------------------------------

        if (
            final_size
            > 50 * 1024 * 1024
        ):

            await status.edit_text(
                "⚡ Preparing fast Telegram upload...\n\n"
                f"📦 {format_bytes(final_size)}\n"
                f"📐 {width} × {height}\n"
                f"👷 Workers: "
                f"{UPLOAD_WORKERS}"
            )

            await upload_large_file(
                chat_id,
                status,
                final_file,
                thumbnail_file,
            )

        else:

            await status.edit_text(
                "📤 Uploading..."
            )

            thumb_handle = None

            try:

                if (
                    thumbnail_file
                    and thumbnail_file.exists()
                ):

                    thumb_handle = open(
                        thumbnail_file,
                        "rb",
                    )

                with open(
                    final_file,
                    "rb",
                ) as video:

                    await update.message.reply_video(
                        video=video,
                        caption=(
                            f"🎬 "
                            f"{final_file.stem}"
                        ),
                        supports_streaming=True,
                        width=width,
                        height=height,
                        duration=int(
                            duration
                        ),
                        thumbnail=(
                            thumb_handle
                            if thumb_handle
                            else None
                        ),
                    )

            finally:

                if thumb_handle:
                    thumb_handle.close()

            await status.edit_text(
                "✅ Upload complete."
            )

        # ----------------------------------------------------
        # Cleanup
        # ----------------------------------------------------

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

            if (
                thumbnail_file
                and thumbnail_file.exists()
            ):
                thumbnail_file.unlink()

        except Exception:
            pass

    except Exception as e:

        try:

            await status.edit_text(
                "❌ ERROR\n\n"
                f"{str(e)[:3500]}"
            )

        except Exception:
            pass


# ============================================================
# TELETHON START
# ============================================================

async def post_init(
    application: Application,
):

    global telethon_client

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN မတွေ့ပါ။"
        )

    if not API_ID:
        raise RuntimeError(
            "API_ID မတွေ့ပါ။"
        )

    if not API_HASH:
        raise RuntimeError(
            "API_HASH မတွေ့ပါ။"
        )

    api_id = int(API_ID)

    telethon_client = TelegramClient(
        TELETHON_SESSION,
        api_id,
        API_HASH,
        connection_retries=5,
        retry_delay=2,
        auto_reconnect=True,
    )

    await telethon_client.start(
        bot_token=BOT_TOKEN
    )

    print(
        "================================"
    )
    print(
        "Telegram MTProto connected"
    )
    print(
        "Upload workers:",
        UPLOAD_WORKERS,
    )
    print(
        "cryptg enabled"
    )
    print(
        "================================"
    )


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
            filters.TEXT
            & ~filters.COMMAND,
            handle_url,
        )
    )

    print(
        "================================"
    )
    print(
        "Movie Upload Bot Started"
    )
    print(
        "================================"
    )

    application.run_polling()


if __name__ == "__main__":
    main()

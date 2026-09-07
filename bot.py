import os
import re
import time
import math
import asyncio
import shutil
from pathlib import Path
from urllib.parse import urlparse, unquote

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
from telethon import functions, types, helpers
from telethon.network.mtprotosender import MTProtoSender
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.errors import FloodWaitError, RPCError


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not API_ID:
    raise RuntimeError("API_ID is missing")

if not API_HASH:
    raise RuntimeError("API_HASH is missing")

API_ID = int(API_ID)

DOWNLOAD_DIR = Path("downloads")
CONVERT_DIR = Path("converted")

DOWNLOAD_DIR.mkdir(exist_ok=True)
CONVERT_DIR.mkdir(exist_ok=True)

STATUS_INTERVAL = 2

# Telegram MTProto parallel upload workers
UPLOAD_WORKERS = 8

# Telegram recommended max part size
PART_SIZE = 512 * 1024

# Bot API limit
BOT_API_LIMIT = 50 * 1024 * 1024


# ============================================================
# TELETHON CLIENT
# ============================================================

SESSION_PATH = str(DOWNLOAD_DIR / "movie_bot_mtproto")

telethon_client = TelegramClient(
    SESSION_PATH,
    API_ID,
    API_HASH,
)


# ============================================================
# BASIC HELPERS
# ============================================================

def format_bytes(value):
    if value is None:
        return "0 B"

    value = float(value)

    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if value < 1024:
            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{value:.2f} PB"


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
    percent = max(0, min(100, percent))

    filled = int(length * percent / 100)

    return "█" * filled + "░" * (length - filled)


def safe_filename(name):
    if not name:
        name = "video"

    name = unquote(name)

    name = re.sub(r'[\\/:*?"<>|]+', "_", name)

    name = name.strip()

    if not name:
        name = "video"

    return name


def is_http_url(url):
    if not url:
        return False

    try:
        p = urlparse(url)

        return p.scheme.lower() in ("http", "https")

    except Exception:
        return False


def is_mediafire_url(url):
    return "mediafire.com" in url.lower()


def get_filename_from_headers(headers):
    content_disposition = headers.get("Content-Disposition", "")

    match = re.search(
        r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)',
        content_disposition,
        re.I,
    )

    if match:
        return safe_filename(match.group(1))

    return None


def get_filename_from_url(url):
    try:
        path = urlparse(url).path

        name = Path(unquote(path)).name

        if name:
            return safe_filename(name)

    except Exception:
        pass

    return None


# ============================================================
# MEDIAFIRE RESOLVER
# ============================================================

async def resolve_mediafire(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
        )
    }

    timeout = aiohttp.ClientTimeout(total=120)

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:

        async with session.get(
            url,
            allow_redirects=True,
        ) as response:

            html = await response.text(errors="ignore")

    soup = BeautifulSoup(html, "html.parser")

    # MediaFire download button
    button = soup.select_one("#downloadButton")

    if button:
        href = button.get("href")

        if href and is_http_url(href):
            return href

    # Search anchors
    for a in soup.find_all("a", href=True):

        href = a["href"]

        text = a.get_text(" ", strip=True).lower()

        if (
            "download" in text
            or "download" in href.lower()
        ):
            if is_http_url(href):
                return href

    # Regex fallback
    patterns = [
        r'https?://[^"\']+download[^"\']+',
        r'https?://[^"\']+mediafireusercontent[^"\']+',
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            html,
            re.I,
        )

        if match:
            return match.group(0).replace("&amp;", "&")

    return None


# ============================================================
# DOWNLOAD
# ============================================================

async def download_file(
    url,
    output_path,
    status_message=None,
):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }

    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=60,
        sock_read=120,
    )

    start_time = time.monotonic()

    downloaded = 0
    last_update = 0

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
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

            header_name = get_filename_from_headers(
                response.headers
            )

            with open(output_path, "wb") as f:

                async for chunk in response.content.iter_chunked(
                    1024 * 1024
                ):

                    if not chunk:
                        continue

                    f.write(chunk)

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

                        if total > 0:
                            percent = (
                                downloaded / total * 100
                            )

                            remaining = (
                                (total - downloaded) / speed
                                if speed > 0
                                else 0
                            )

                            text = (
                                "⬇️ <b>Downloading...</b>\n\n"
                                f"{progress_bar(percent)} "
                                f"{percent:.1f}%\n\n"
                                f"📦 {format_bytes(downloaded)} / "
                                f"{format_bytes(total)}\n"
                                f"⚡ {format_bytes(speed)}/s\n"
                                f"⏱ {format_time(elapsed)}\n"
                                f"🕐 ETA {format_time(remaining)}"
                            )

                        else:
                            text = (
                                "⬇️ <b>Downloading...</b>\n\n"
                                f"📦 {format_bytes(downloaded)}\n"
                                f"⚡ {format_bytes(speed)}/s"
                            )

                        try:
                            await status_message.edit_text(
                                text,
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

                        last_update = now

    return {
        "size": downloaded,
        "header_filename": header_name,
        "final_url": str(response.url),
    }


# ============================================================
# VIDEO VALIDATION
# ============================================================

def is_valid_mp4(path):
    try:
        with open(path, "rb") as f:

            header = f.read(4096)

        if b"ftyp" in header:
            return True

    except Exception:
        pass

    return False


# ============================================================
# FFMPEG
# ============================================================

def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def ffprobe_available():
    return shutil.which("ffprobe") is not None


async def probe_video(path):
    if not ffprobe_available():
        return {
            "width": 0,
            "height": 0,
            "duration": 0,
        }

    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=0",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    text = stdout.decode(
        "utf-8",
        errors="ignore",
    )

    width = 0
    height = 0
    duration = 0

    for line in text.splitlines():

        if line.startswith("width="):
            try:
                width = int(line.split("=", 1)[1])
            except Exception:
                pass

        elif line.startswith("height="):
            try:
                height = int(line.split("=", 1)[1])
            except Exception:
                pass

        elif line.startswith("duration="):
            try:
                duration = float(
                    line.split("=", 1)[1]
                )
            except Exception:
                pass

    return {
        "width": width,
        "height": height,
        "duration": duration,
    }


async def convert_to_mp4(
    input_path,
    output_path,
    status_message=None,
):
    if not ffmpeg_available():
        raise RuntimeError(
            "FFmpeg is not installed."
        )

    if status_message:
        try:
            await status_message.edit_text(
                "🔄 <b>Preparing MP4...</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    # --------------------------------------------------------
    # First try fast remux
    # --------------------------------------------------------

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode == 0:
        return output_path

    # --------------------------------------------------------
    # Re-encode fallback
    # --------------------------------------------------------

    if status_message:
        try:
            await status_message.edit_text(
                "🎞️ <b>Converting to MP4...</b>\n\n"
                "This may take some time.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    if output_path.exists():
        try:
            output_path.unlink()
        except Exception:
            pass

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
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
        error = stderr.decode(
            "utf-8",
            errors="ignore",
        )

        raise RuntimeError(
            "FFmpeg conversion failed:\n"
            + error[-3000:]
        )

    return output_path


# ============================================================
# THUMBNAIL
# ============================================================

async def normalize_thumbnail(
    input_path,
    output_path,
):
    """
    Make Telegram-friendly JPEG thumbnail.
    """

    if not ffmpeg_available():
        shutil.copy2(
            input_path,
            output_path,
        )
        return output_path

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        (
            "scale=320:320:"
            "force_original_aspect_ratio=decrease"
        ),
        "-frames:v",
        "1",
        "-q:v",
        "5",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        shutil.copy2(
            input_path,
            output_path,
        )

    return output_path


async def extract_thumbnail(
    video_path,
    output_path,
):
    if not ffmpeg_available():
        return None

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-ss",
        "00:00:03",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        (
            "scale=320:320:"
            "force_original_aspect_ratio=decrease"
        ),
        "-q:v",
        "5",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        return None

    if output_path.exists():
        return output_path

    return None


async def download_thumbnail(
    url,
    output_path,
):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
        )
    }

    timeout = aiohttp.ClientTimeout(
        total=120
    )

    raw_path = output_path.with_suffix(".raw")

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:

        async with session.get(
            url,
            allow_redirects=True,
        ) as response:

            response.raise_for_status()

            with open(raw_path, "wb") as f:

                async for chunk in response.content.iter_chunked(
                    256 * 1024
                ):

                    f.write(chunk)

    try:
        await normalize_thumbnail(
            raw_path,
            output_path,
        )
    finally:
        try:
            raw_path.unlink()
        except Exception:
            pass

    return output_path


# ============================================================
# TELEGRAM PARALLEL UPLOADER
# ============================================================

class ParallelMTProtoUploader:

    def __init__(
        self,
        client,
        workers=UPLOAD_WORKERS,
        part_size=PART_SIZE,
    ):
        self.client = client
        self.workers = workers
        self.part_size = part_size

    async def _create_sender(self):
        """
        IMPORTANT:
        We use the CURRENT session DC.

        This avoids:
            Failed to get DC None (cdn = False)
        """

        dc_id = self.client.session.dc_id

        if not dc_id:
            raise RuntimeError(
                "Telegram session DC ID is unavailable."
            )

        # Get actual DC information
        dc = await self.client._get_dc(
            dc_id
        )

        auth_key = self.client.session.auth_key

        if auth_key is None:
            raise RuntimeError(
                "Telegram session AuthKey is unavailable."
            )

        sender = MTProtoSender(
            auth_key,
            loggers=self.client._log,
            retries=5,
            delay=1,
        )

        await sender.connect(
            self.client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=self.client._log,
                proxy=self.client._proxy,
            )
        )

        return sender

    async def _save_part(
        self,
        sender,
        file_id,
        part_index,
        total_parts,
        data,
    ):
        request = SaveBigFilePartRequest(
            file_id=file_id,
            file_part=part_index,
            file_total_parts=total_parts,
            bytes=data,
        )

        while True:

            try:

                await sender.send(
                    request
                )

                return

            except FloodWaitError as e:

                await asyncio.sleep(
                    e.seconds + 1
                )

            except RPCError:

                raise

    async def upload(
        self,
        file_path,
        progress_callback=None,
    ):
        file_path = Path(file_path)

        file_size = file_path.stat().st_size

        total_parts = math.ceil(
            file_size / self.part_size
        )

        file_id = helpers.generate_random_long()

        workers = min(
            self.workers,
            total_parts,
        )

        # ----------------------------------------------------
        # Create parallel MTProto connections
        # ----------------------------------------------------

        senders = []

        try:

            for _ in range(workers):

                sender = await self._create_sender()

                senders.append(sender)

            sent_bytes = 0
            progress_lock = asyncio.Lock()

            async def worker(
                worker_index,
                sender,
            ):
                nonlocal sent_bytes

                with open(
                    file_path,
                    "rb",
                ) as f:

                    # Each worker gets:
                    # worker_index,
                    # worker_index + workers,
                    # worker_index + workers*2...
                    for part_index in range(
                        worker_index,
                        total_parts,
                        workers,
                    ):

                        offset = (
                            part_index
                            * self.part_size
                        )

                        f.seek(offset)

                        data = f.read(
                            self.part_size
                        )

                        if not data:
                            continue

                        await self._save_part(
                            sender,
                            file_id,
                            part_index,
                            total_parts,
                            data,
                        )

                        async with progress_lock:

                            sent_bytes += len(data)

                            if progress_callback:
                                await progress_callback(
                                    sent_bytes,
                                    file_size,
                                )

            # ------------------------------------------------
            # START ALL WORKERS
            # ------------------------------------------------

            tasks = [
                asyncio.create_task(
                    worker(
                        i,
                        senders[i],
                    )
                )
                for i in range(workers)
            ]

            await asyncio.gather(
                *tasks
            )

        finally:

            # ------------------------------------------------
            # CLOSE ALL CONNECTIONS
            # ------------------------------------------------

            for sender in senders:

                try:
                    await sender.disconnect()
                except Exception:
                    pass

        return types.InputFileBig(
            id=file_id,
            parts=total_parts,
            name=file_path.name,
        )


# ============================================================
# LARGE FILE SEND WITH THUMBNAIL
# ============================================================

async def send_large_video(
    chat_id,
    video_path,
    caption,
    thumbnail_path=None,
    width=0,
    height=0,
    duration=0,
    status_message=None,
):
    video_path = Path(video_path)

    uploader = ParallelMTProtoUploader(
        telethon_client,
        workers=UPLOAD_WORKERS,
        part_size=PART_SIZE,
    )

    start_time = time.monotonic()

    async def progress_callback(
        sent,
        total,
    ):
        now = time.monotonic()

        elapsed = now - start_time

        speed = (
            sent / elapsed
            if elapsed > 0
            else 0
        )

        percent = (
            sent / total * 100
            if total > 0
            else 0
        )

        eta = (
            (total - sent) / speed
            if speed > 0
            else 0
        )

        if status_message:

            # Throttle edits
            last = getattr(
                progress_callback,
                "_last",
                0,
            )

            if (
                now - last
                >= STATUS_INTERVAL
                or sent >= total
            ):

                try:

                    await status_message.edit_text(
                        "⬆️ <b>Uploading to Telegram...</b>\n\n"
                        f"{progress_bar(percent)} "
                        f"{percent:.1f}%\n\n"
                        f"📦 {format_bytes(sent)} / "
                        f"{format_bytes(total)}\n"
                        f"⚡ {format_bytes(speed)}/s\n"
                        f"⏱ {format_time(elapsed)}\n"
                        f"🕐 ETA {format_time(eta)}\n\n"
                        f"🚀 Workers: {UPLOAD_WORKERS}",
                        parse_mode="HTML",
                    )

                except Exception:
                    pass

                progress_callback._last = now

    # --------------------------------------------------------
    # UPLOAD VIDEO ONLY ONCE
    # --------------------------------------------------------

    input_file = await uploader.upload(
        video_path,
        progress_callback,
    )

    # --------------------------------------------------------
    # THUMBNAIL
    # --------------------------------------------------------

    thumb_input = None

    if thumbnail_path:

        thumbnail_path = Path(
            thumbnail_path
        )

        if thumbnail_path.exists():

            try:

                # Thumbnail is small, standard Telethon upload
                thumb_input = await telethon_client.upload_file(
                    str(thumbnail_path),
                    part_size_kb=128,
                )

            except Exception:
                thumb_input = None

    # --------------------------------------------------------
    # DOCUMENT ATTRIBUTES
    # --------------------------------------------------------

    attributes = [
        types.DocumentAttributeFilename(
            file_name=video_path.name
        )
    ]

    if width > 0 and height > 0:

        attributes.append(
            types.DocumentAttributeVideo(
                duration=int(duration or 0),
                w=int(width),
                h=int(height),
                supports_streaming=True,
            )
        )

    # --------------------------------------------------------
    # SEND ALREADY UPLOADED FILE
    # --------------------------------------------------------

    peer = await telethon_client.get_input_entity(
        chat_id
    )

    media = types.InputMediaUploadedDocument(
        file=input_file,
        thumb=thumb_input,
        mime_type="video/mp4",
        attributes=attributes,
        force_file=False,
    )

    result = await telethon_client(
        functions.messages.SendMediaRequest(
            peer=peer,
            media=media,
            message=caption or "",
            random_id=helpers.generate_random_long(),
        )
    )

    return result


# ============================================================
# PARSE USER MESSAGE
# ============================================================

def parse_request(text):
    """
    Supports:

    Video: https://...
    Thumbnail: https://...
    """

    video_url = None
    thumbnail_url = None

    video_match = re.search(
        r'Video\s*:\s*(https?://\S+)',
        text,
        re.I,
    )

    if video_match:
        video_url = video_match.group(1).strip()

    thumb_match = re.search(
        r'Thumbnail\s*:\s*(https?://\S+)',
        text,
        re.I,
    )

    if thumb_match:
        thumbnail_url = thumb_match.group(1).strip()

    # If plain URL only
    if not video_url:

        urls = re.findall(
            r'https?://\S+',
            text,
            re.I,
        )

        if urls:
            video_url = urls[0].strip()

    return video_url, thumbnail_url


# ============================================================
# START COMMAND
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🎬 <b>Movie Upload Bot</b>\n\n"
        "Send me a video URL.\n\n"
        "Example:\n"
        "<code>Video: https://example.com/video.mp4</code>\n\n"
        "Optional:\n"
        "<code>Thumbnail: https://example.com/thumb.jpg</code>",
        parse_mode="HTML",
    )


# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    text = update.message.text or ""

    video_url, thumbnail_url = parse_request(
        text
    )

    if not video_url:

        await update.message.reply_text(
            "❌ Video URL မတွေ့ပါဘူး။\n\n"
            "ဥပမာ:\n"
            "Video: https://example.com/video.mp4"
        )

        return

    if not is_http_url(video_url):

        await update.message.reply_text(
            "❌ Invalid video URL."
        )

        return

    status = await update.message.reply_text(
        "🔎 <b>Checking URL...</b>",
        parse_mode="HTML",
    )

    video_download_url = video_url

    # --------------------------------------------------------
    # MEDIAFIRE
    # --------------------------------------------------------

    if is_mediafire_url(video_url):

        try:

            await status.edit_text(
                "🔎 <b>Resolving MediaFire...</b>",
                parse_mode="HTML",
            )

            resolved = await resolve_mediafire(
                video_url
            )

            if not resolved:
                raise RuntimeError(
                    "Could not resolve MediaFire download link."
                )

            video_download_url = resolved

        except Exception as e:

            await status.edit_text(
                "❌ <b>MediaFire Error</b>\n\n"
                f"<code>{str(e)[:1500]}</code>",
                parse_mode="HTML",
            )

            return

    # --------------------------------------------------------
    # FILE NAME
    # --------------------------------------------------------

    filename = get_filename_from_url(
        video_download_url
    )

    if not filename:

        filename = "video.mp4"

    if not filename.lower().endswith(
        (
            ".mp4",
            ".mkv",
            ".webm",
            ".mov",
            ".avi",
            ".m4v",
        )
    ):
        filename += ".mp4"

    filename = safe_filename(
        filename
    )

    download_path = (
        DOWNLOAD_DIR / filename
    )

    # Avoid overwrite
    if download_path.exists():

        timestamp = int(time.time())

        download_path = (
            DOWNLOAD_DIR
            / f"{download_path.stem}_{timestamp}"
            f"{download_path.suffix}"
        )

    # --------------------------------------------------------
    # DOWNLOAD
    # --------------------------------------------------------

    try:

        await status.edit_text(
            "⬇️ <b>Starting download...</b>",
            parse_mode="HTML",
        )

        result = await download_file(
            video_download_url,
            download_path,
            status,
        )

        # If server supplied filename
        header_filename = result.get(
            "header_filename"
        )

        if (
            header_filename
            and download_path.name == filename
        ):

            new_path = (
                download_path.parent
                / header_filename
            )

            if (
                new_path != download_path
                and not new_path.exists()
            ):
                try:
                    download_path.rename(
                        new_path
                    )
                    download_path = new_path
                except Exception:
                    pass

    except Exception as e:

        await status.edit_text(
            "❌ <b>Download Error</b>\n\n"
            f"<code>{str(e)[:2500]}</code>",
            parse_mode="HTML",
        )

        return

    # --------------------------------------------------------
    # VALIDATE
    # --------------------------------------------------------

    if not download_path.exists():

        await status.edit_text(
            "❌ Downloaded file not found."
        )

        return

    if download_path.stat().st_size == 0:

        await status.edit_text(
            "❌ Downloaded file is empty."
        )

        return

    # --------------------------------------------------------
    # CONVERT MP4
    # --------------------------------------------------------

    converted_path = (
        CONVERT_DIR
        / f"{download_path.stem}.mp4"
    )

    try:

        if (
            download_path.suffix.lower()
            == ".mp4"
            and is_valid_mp4(download_path)
        ):

            converted_path = download_path

        else:

            await convert_to_mp4(
                download_path,
                converted_path,
                status,
            )

    except Exception as e:

        await status.edit_text(
            "❌ <b>Video Conversion Error</b>\n\n"
            f"<code>{str(e)[:2500]}</code>",
            parse_mode="HTML",
        )

        return

    # --------------------------------------------------------
    # VIDEO INFO
    # --------------------------------------------------------

    try:

        info = await probe_video(
            converted_path
        )

    except Exception:

        info = {
            "width": 0,
            "height": 0,
            "duration": 0,
        }

    width = info.get(
        "width",
        0,
    )

    height = info.get(
        "height",
        0,
    )

    duration = info.get(
        "duration",
        0,
    )

    # --------------------------------------------------------
    # THUMBNAIL
    # --------------------------------------------------------

    thumbnail_path = None

    try:

        if thumbnail_url:

            thumbnail_path = (
                CONVERT_DIR
                / f"{converted_path.stem}_thumb.jpg"
            )

            await download_thumbnail(
                thumbnail_url,
                thumbnail_path,
            )

        else:

            thumbnail_path = (
                CONVERT_DIR
                / f"{converted_path.stem}_thumb.jpg"
            )

            extracted = await extract_thumbnail(
                converted_path,
                thumbnail_path,
            )

            if not extracted:
                thumbnail_path = None

    except Exception:

        thumbnail_path = None

    # --------------------------------------------------------
    # FILE SIZE
    # --------------------------------------------------------

    file_size = converted_path.stat().st_size

    # --------------------------------------------------------
    # CAPTION
    # --------------------------------------------------------

    caption = (
        f"🎬 <b>{converted_path.name}</b>\n\n"
        f"📦 Size: {format_bytes(file_size)}\n"
        f"📐 Resolution: "
        f"{width}×{height}\n"
        f"⏱ Duration: "
        f"{format_time(duration)}"
    )

    # --------------------------------------------------------
    # SEND
    # --------------------------------------------------------

    try:

        if file_size <= BOT_API_LIMIT:

            # ----------------------------------------------
            # SMALL FILE
            # ----------------------------------------------

            await status.edit_text(
                "⬆️ <b>Uploading...</b>",
                parse_mode="HTML",
            )

            with open(
                converted_path,
                "rb",
            ) as video_file:

                await update.message.reply_video(
                    video=video_file,
                    caption=caption,
                    parse_mode="HTML",
                    duration=int(duration or 0),
                    width=int(width or 0),
                    height=int(height or 0),
                    supports_streaming=True,
                    thumbnail=(
                        open(thumbnail_path, "rb")
                        if thumbnail_path
                        else None
                    ),
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                )

        else:

            # ----------------------------------------------
            # LARGE FILE
            # MTProto PARALLEL UPLOAD
            # ----------------------------------------------

            await status.edit_text(
                "🚀 <b>Starting parallel Telegram upload...</b>\n\n"
                f"📦 {format_bytes(file_size)}\n"
                f"⚡ Workers: {UPLOAD_WORKERS}\n"
                f"🧩 Part size: 512 KB",
                parse_mode="HTML",
            )

            await send_large_video(
                chat_id=update.effective_chat.id,
                video_path=converted_path,
                caption=caption,
                thumbnail_path=thumbnail_path,
                width=width,
                height=height,
                duration=duration,
                status_message=status,
            )

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:

        await status.edit_text(
            "❌ <b>Telegram Upload Error</b>\n\n"
            f"<code>{str(e)[:3000]}</code>",
            parse_mode="HTML",
        )

    finally:

        # ----------------------------------------------------
        # CLEAN TEMP FILES
        # ----------------------------------------------------

        try:
            if (
                download_path.exists()
                and download_path != converted_path
            ):
                download_path.unlink()
        except Exception:
            pass

        try:
            if (
                converted_path.exists()
                and converted_path != download_path
            ):
                converted_path.unlink()
        except Exception:
            pass

        if thumbnail_path:

            try:
                if thumbnail_path.exists():
                    thumbnail_path.unlink()
            except Exception:
                pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):
    print(
        "BOT ERROR:",
        repr(context.error),
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    print("=" * 60)
    print("Movie Upload Bot")
    print("=" * 60)

    print(
        "UPLOAD WORKERS:",
        UPLOAD_WORKERS,
    )

    print(
        "PART SIZE:",
        format_bytes(PART_SIZE),
    )

    print(
        "FFMPEG:",
        shutil.which("ffmpeg"),
    )

    print(
        "FFPROBE:",
        shutil.which("ffprobe"),
    )

    print("=" * 60)

    # --------------------------------------------------------
    # TELETHON LOGIN
    # --------------------------------------------------------

    await telethon_client.start(
        bot_token=BOT_TOKEN
    )

    me = await telethon_client.get_me()

    print(
        "Telethon connected:",
        me.username or me.id,
    )

    # --------------------------------------------------------
    # PYTHON TELEGRAM BOT
    # --------------------------------------------------------

    application = (
        Application.builder()
        .token(BOT_TOKEN)
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
            handle_message,
        )
    )

    application.add_error_handler(
        error_handler
    )

    print("Bot is running...")

    # Start polling
    await application.initialize()
    await application.start()

    if application.updater:

        await application.updater.start_polling()

    try:

        while True:
            await asyncio.sleep(3600)

    finally:

        if application.updater:
            await application.updater.stop()

        await application.stop()
        await application.shutdown()

        await telethon_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

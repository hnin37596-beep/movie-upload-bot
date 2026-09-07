import os
import re
import time
import math
import asyncio
import shutil
from pathlib import Path
from urllib.parse import urlparse, unquote
from dataclasses import dataclass

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

# ============================================================
# UPLOAD SPEED
# ============================================================

UPLOAD_WORKERS = 12

# Telegram recommended maximum part size
PART_SIZE = 512 * 1024

# Bot API cloud upload limit
BOT_API_LIMIT = 50 * 1024 * 1024


# ============================================================
# TELETHON
# ============================================================

SESSION_PATH = str(
    DOWNLOAD_DIR / "movie_bot_mtproto"
)

telethon_client = TelegramClient(
    SESSION_PATH,
    API_ID,
    API_HASH,
)


# ============================================================
# QUEUE
# ============================================================

@dataclass
class QueueItem:

    video_url: str
    thumbnail_url: str | None
    chat_id: int
    user_id: int
    number: int = 0


queue = asyncio.Queue()

queue_items = []

queue_lock = asyncio.Lock()

queue_worker_task = None

current_item = None

current_cancel_event = None


# ============================================================
# BASIC HELPERS
# ============================================================

def format_bytes(value):

    if value is None:
        return "0 B"

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
        return (
            f"{h:02d}:"
            f"{m:02d}:"
            f"{s:02d}"
        )

    return (
        f"{m:02d}:"
        f"{s:02d}"
    )


def progress_bar(
    percent,
    length=20,
):

    percent = max(
        0,
        min(100, percent),
    )

    filled = int(
        length * percent / 100
    )

    return (
        "█" * filled
        + "░" * (length - filled)
    )


def safe_filename(name):

    if not name:
        name = "video"

    name = unquote(name)

    name = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        name,
    )

    name = name.strip()

    if not name:
        name = "video"

    return name


def is_http_url(url):

    if not url:
        return False

    try:

        p = urlparse(url)

        return p.scheme.lower() in (
            "http",
            "https",
        )

    except Exception:

        return False


def is_mediafire_url(url):

    return (
        "mediafire.com"
        in url.lower()
    )


def get_filename_from_headers(
    headers,
):

    content_disposition = (
        headers.get(
            "Content-Disposition",
            "",
        )
    )

    match = re.search(
        r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)',
        content_disposition,
        re.I,
    )

    if match:

        return safe_filename(
            match.group(1)
        )

    return None


def get_filename_from_url(url):

    try:

        path = urlparse(url).path

        name = Path(
            unquote(path)
        ).name

        if name:

            return safe_filename(
                name
            )

    except Exception:

        pass

    return None


# ============================================================
# MEDIAFIRE
# ============================================================

async def resolve_mediafire(url):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/131.0 Safari/537.36"
        )
    }

    timeout = aiohttp.ClientTimeout(
        total=120
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
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

        href = button.get(
            "href"
        )

        if (
            href
            and is_http_url(href)
        ):

            return href

    for a in soup.find_all(
        "a",
        href=True,
    ):

        href = a["href"]

        text = a.get_text(
            " ",
            strip=True,
        ).lower()

        if (
            "download" in text
            or "download" in href.lower()
        ):

            if is_http_url(href):

                return href

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

            return (
                match.group(0)
                .replace(
                    "&amp;",
                    "&",
                )
            )

    return None


# ============================================================
# PARSE MULTIPLE LINKS
# ============================================================

def parse_multiple_requests(
    text,
):
    """
    Supported:

    Video: https://site/a.mp4
    Thumbnail: https://site/a.jpg

    Video: https://site/b.mp4
    Thumbnail: https://site/b.jpg

    Also supports:

    https://site/a.mp4
    https://site/b.mp4
    """

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    items = []

    current_video = None
    current_thumb = None

    for line in lines:

        video_match = re.match(
            r"^Video\s*:\s*(https?://\S+)",
            line,
            re.I,
        )

        thumb_match = re.match(
            r"^Thumbnail\s*:\s*(https?://\S+)",
            line,
            re.I,
        )

        if video_match:

            # Save previous item
            if current_video:

                items.append(
                    (
                        current_video,
                        current_thumb,
                    )
                )

            current_video = (
                video_match.group(1)
                .strip()
            )

            current_thumb = None

            continue

        if thumb_match:

            if current_video:

                current_thumb = (
                    thumb_match.group(1)
                    .strip()
                )

            continue

        # Plain URL
        urls = re.findall(
            r'https?://\S+',
            line,
            re.I,
        )

        for url in urls:

            url = url.strip()

            if not is_http_url(url):
                continue

            # If a Video is waiting,
            # save it first.
            if current_video:

                items.append(
                    (
                        current_video,
                        current_thumb,
                    )
                )

                current_video = None
                current_thumb = None

            items.append(
                (
                    url,
                    None,
                )
            )

    if current_video:

        items.append(
            (
                current_video,
                current_thumb,
            )
        )

    return items


# ============================================================
# FFMPEG
# ============================================================

def ffmpeg_available():

    return (
        shutil.which("ffmpeg")
        is not None
    )


def ffprobe_available():

    return (
        shutil.which("ffprobe")
        is not None
    )


async def probe_video(path):

    if not ffprobe_available():

        return {
            "width": 0,
            "height": 0,
            "duration": 0,
        }

    process = (
        await asyncio.create_subprocess_exec(
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
    )

    stdout, stderr = (
        await process.communicate()
    )

    text = stdout.decode(
        "utf-8",
        errors="ignore",
    )

    width = 0
    height = 0
    duration = 0

    for line in text.splitlines():

        if line.startswith(
            "width="
        ):

            try:
                width = int(
                    line.split(
                        "=",
                        1,
                    )[1]
                )
            except Exception:
                pass

        elif line.startswith(
            "height="
        ):

            try:
                height = int(
                    line.split(
                        "=",
                        1,
                    )[1]
                )
            except Exception:
                pass

        elif line.startswith(
            "duration="
        ):

            try:
                duration = float(
                    line.split(
                        "=",
                        1,
                    )[1]
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
    cancel_event=None,
):

    if not ffmpeg_available():

        raise RuntimeError(
            "FFmpeg is not installed."
        )

    input_path = Path(input_path)
    output_path = Path(output_path)

    # --------------------------------------------------------
    # MKV/other containers: first try a clean remux.
    # Only the first video and first audio stream are selected.
    # Subtitles, attachments and data streams are excluded.
    # This is important for MKV files because many of them can
    # be placed in MP4 without re-encoding the video.
    # --------------------------------------------------------

    if status_message:

        try:

            await status_message.edit_text(
                "🔄 <b>Preparing MP4...</b>",
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
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode == 0 and output_path.exists():

        return output_path

    # Remove failed/partial remux before fallback.
    if output_path.exists():

        try:
            output_path.unlink()
        except Exception:
            pass

    if cancel_event and cancel_event.is_set():

        raise asyncio.CancelledError()

    # --------------------------------------------------------
    # Fallback: H.264 + AAC conversion.
    # --------------------------------------------------------

    if status_message:

        try:

            await status_message.edit_text(
                "🎞️ <b>Converting to MP4...</b>\n\n"
                "Please wait...",
                parse_mode="HTML",
            )

        except Exception:
            pass

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
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

        # FFmpeg can leave a huge partial MP4 after a disk-full
        # failure. Delete it immediately so the queue does not
        # consume the remaining runner storage.
        if output_path.exists():

            try:
                output_path.unlink()
            except Exception:
                pass

        if "No space left on device" in error:

            raise RuntimeError(
                "FFmpeg conversion failed: No space left on device.\n\n"
                "The incomplete MP4 file was removed."
            )

        raise RuntimeError(
            "FFmpeg conversion failed:\n"
            + error[-3000:]
        )

    if not output_path.exists():

        raise RuntimeError(
            "FFmpeg finished but MP4 file was not created."
        )

    return output_path


# ============================================================
# THUMBNAIL
# ============================================================

async def normalize_thumbnail(
    input_path,
    output_path,
):

    if not ffmpeg_available():

        shutil.copy2(
            input_path,
            output_path,
        )

        return output_path

    process = (
        await asyncio.create_subprocess_exec(
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
    )

    stdout, stderr = (
        await process.communicate()
    )

    if process.returncode != 0:

        shutil.copy2(
            input_path,
            output_path,
        )

    return output_path


async def download_thumbnail(
    url,
    output_path,
):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/131.0 Safari/537.36"
        )
    }

    timeout = aiohttp.ClientTimeout(
        total=120
    )

    raw_path = output_path.with_suffix(
        ".raw"
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:

        async with session.get(
            url,
            allow_redirects=True,
        ) as response:

            response.raise_for_status()

            with open(
                raw_path,
                "wb",
            ) as f:

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


async def extract_thumbnail(
    video_path,
    output_path,
):

    if not ffmpeg_available():

        return None

    process = (
        await asyncio.create_subprocess_exec(
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
    )

    stdout, stderr = (
        await process.communicate()
    )

    if process.returncode != 0:

        return None

    if output_path.exists():

        return output_path

    return None


# ============================================================
# DOWNLOAD
# ============================================================

async def download_file(
    url,
    output_path,
    status_message=None,
    cancel_event=None,
):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/131.0 Safari/537.36"
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

            header_name = (
                get_filename_from_headers(
                    response.headers
                )
            )

            with open(
                output_path,
                "wb",
            ) as f:

                async for chunk in response.content.iter_chunked(
                    1024 * 1024
                ):

                    if (
                        cancel_event
                        and cancel_event.is_set()
                    ):

                        raise asyncio.CancelledError()

                    if not chunk:
                        continue

                    f.write(chunk)

                    downloaded += len(
                        chunk
                    )

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
                            downloaded
                            / elapsed
                            if elapsed > 0
                            else 0
                        )

                        if total > 0:

                            percent = (
                                downloaded
                                / total
                                * 100
                            )

                            remaining = (
                                (
                                    total
                                    - downloaded
                                )
                                / speed
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
                                f"🕐 ETA "
                                f"{format_time(remaining)}"
                            )

                        else:

                            text = (
                                "⬇️ <b>Downloading...</b>\n\n"
                                f"📦 "
                                f"{format_bytes(downloaded)}\n"
                                f"⚡ "
                                f"{format_bytes(speed)}/s"
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
    }


# ============================================================
# MP4 CHECK
# ============================================================

def is_valid_mp4(path):

    try:

        with open(
            path,
            "rb",
        ) as f:

            header = f.read(4096)

        return b"ftyp" in header

    except Exception:

        return False


# ============================================================
# PARALLEL MTProto UPLOADER
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

    async def _create_sender(
        self,
    ):

        # IMPORTANT:
        # Use current session DC.
        # Never use None.

        dc_id = (
            self.client.session.dc_id
        )

        if not dc_id:

            raise RuntimeError(
                "Telegram session DC ID "
                "is unavailable."
            )

        dc = await self.client._get_dc(
            dc_id
        )

        auth_key = (
            self.client.session.auth_key
        )

        if auth_key is None:

            raise RuntimeError(
                "Telegram session AuthKey "
                "is unavailable."
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
        cancel_event=None,
    ):

        if (
            cancel_event
            and cancel_event.is_set()
        ):

            raise asyncio.CancelledError()

        request = SaveBigFilePartRequest(
            file_id=file_id,
            file_part=part_index,
            file_total_parts=total_parts,
            bytes=data,
        )

        while True:

            if (
                cancel_event
                and cancel_event.is_set()
            ):

                raise asyncio.CancelledError()

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
        cancel_event=None,
    ):

        file_path = Path(
            file_path
        )

        file_size = (
            file_path.stat().st_size
        )

        total_parts = math.ceil(
            file_size
            / self.part_size
        )

        file_id = (
            helpers.generate_random_long()
        )

        workers = min(
            self.workers,
            total_parts,
        )

        senders = []

        try:

            # ----------------------------------------------
            # Create 12 connections
            # ----------------------------------------------

            for _ in range(
                workers
            ):

                if (
                    cancel_event
                    and cancel_event.is_set()
                ):

                    raise asyncio.CancelledError()

                sender = (
                    await self._create_sender()
                )

                senders.append(
                    sender
                )

            sent_bytes = 0

            progress_lock = (
                asyncio.Lock()
            )

            async def worker(
                worker_index,
                sender,
            ):

                nonlocal sent_bytes

                with open(
                    file_path,
                    "rb",
                ) as f:

                    for part_index in range(
                        worker_index,
                        total_parts,
                        workers,
                    ):

                        if (
                            cancel_event
                            and cancel_event.is_set()
                        ):

                            raise asyncio.CancelledError()

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
                            cancel_event,
                        )

                        async with progress_lock:

                            sent_bytes += len(
                                data
                            )

                            if progress_callback:

                                await progress_callback(
                                    sent_bytes,
                                    file_size,
                                )

            tasks = [
                asyncio.create_task(
                    worker(
                        i,
                        senders[i],
                    )
                )
                for i in range(
                    workers
                )
            ]

            await asyncio.gather(
                *tasks
            )

        finally:

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
# SEND LARGE VIDEO
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
    cancel_event=None,
):

    video_path = Path(
        video_path
    )

    uploader = (
        ParallelMTProtoUploader(
            telethon_client,
            workers=UPLOAD_WORKERS,
            part_size=PART_SIZE,
        )
    )

    start_time = time.monotonic()

    async def progress_callback(
        sent,
        total,
    ):

        now = time.monotonic()

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
            if total > 0
            else 0
        )

        eta = (
            (total - sent) / speed
            if speed > 0
            else 0
        )

        if status_message:

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
                        f"🚀 Workers: "
                        f"{UPLOAD_WORKERS}",
                        parse_mode="HTML",
                    )

                except Exception:
                    pass

                progress_callback._last = now

    # ========================================================
    # VIDEO UPLOAD — ONLY ONCE
    # ========================================================

    input_file = (
        await uploader.upload(
            video_path,
            progress_callback,
            cancel_event,
        )
    )

    if (
        cancel_event
        and cancel_event.is_set()
    ):

        raise asyncio.CancelledError()

    # ========================================================
    # THUMBNAIL
    # ========================================================

    thumb_input = None

    if thumbnail_path:

        thumbnail_path = Path(
            thumbnail_path
        )

        if thumbnail_path.exists():

            try:

                thumb_input = (
                    await telethon_client.upload_file(
                        str(thumbnail_path),
                        part_size_kb=128,
                    )
                )

            except Exception:

                thumb_input = None

    # ========================================================
    # ATTRIBUTES
    # ========================================================

    attributes = [
        types.DocumentAttributeFilename(
            file_name=video_path.name
        )
    ]

    if (
        width > 0
        and height > 0
    ):

        attributes.append(
            types.DocumentAttributeVideo(
                duration=int(
                    duration or 0
                ),
                w=int(width),
                h=int(height),
                supports_streaming=True,
            )
        )

    # ========================================================
    # SEND MEDIA
    # ========================================================

    peer = (
        await telethon_client.get_input_entity(
            chat_id
        )
    )

    media = (
        types.InputMediaUploadedDocument(
            file=input_file,
            thumb=thumb_input,
            mime_type="video/mp4",
            attributes=attributes,
            force_file=False,
        )
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
# QUEUE DISPLAY
# ============================================================

async def get_queue_text():

    async with queue_lock:

        lines = [
            "📋 <b>UPLOAD QUEUE</b>",
            "",
        ]

        if current_item:

            lines.append(
                "🔄 <b>Currently processing:</b>"
            )

            lines.append(
                f"🎬 {current_item.number}. "
                f"{current_item.video_url}"
            )

            lines.append("")

        if not queue_items:

            lines.append(
                "📭 Queue is empty."
            )

        else:

            for i, item in enumerate(
                queue_items,
                start=1,
            ):

                lines.append(
                    f"⏳ {i}. "
                    f"{item.video_url}"
                )

        return "\n".join(
            lines
        )


# ============================================================
# QUEUE WORKER
# ============================================================

async def queue_worker():

    global current_item
    global current_cancel_event

    while True:

        item = await queue.get()

        async with queue_lock:

            if item in queue_items:

                queue_items.remove(
                    item
                )

            current_item = item

            current_cancel_event = (
                asyncio.Event()
            )

        try:

            await process_queue_item(
                item
            )

        except asyncio.CancelledError:

            try:

                await telethon_client.send_message(
                    item.chat_id,
                    "🛑 <b>Cancelled</b>\n\n"
                    f"🎬 {item.video_url}",
                    parse_mode="HTML",
                )

            except Exception:
                pass

        except Exception as e:

            print(
                "QUEUE ERROR:",
                repr(e),
            )

            try:

                await telethon_client.send_message(
                    item.chat_id,
                    "❌ <b>Queue Item Failed</b>\n\n"
                    f"<code>{str(e)[:2500]}</code>",
                    parse_mode="HTML",
                )

            except Exception:
                pass

        finally:

            async with queue_lock:

                current_item = None

                current_cancel_event = None

            queue.task_done()


# ============================================================
# PROCESS ONE QUEUE ITEM
# ============================================================

async def process_queue_item(
    item,
):

    global current_cancel_event

    cancel_event = (
        current_cancel_event
    )

    status = await telethon_client.send_message(
        item.chat_id,
        "🔎 <b>Checking URL...</b>",
        parse_mode="HTML",
    )

    video_url = item.video_url

    thumbnail_url = item.thumbnail_url

    # ========================================================
    # MEDIAFIRE
    # ========================================================

    if is_mediafire_url(
        video_url
    ):

        await status.edit(
            "🔎 <b>Resolving MediaFire...</b>",
            parse_mode="HTML",
        )

        resolved = (
            await resolve_mediafire(
                video_url
            )
        )

        if not resolved:

            raise RuntimeError(
                "Could not resolve MediaFire download link."
            )

        video_download_url = resolved

    else:

        video_download_url = video_url

    # ========================================================
    # FILENAME
    # ========================================================

    filename = (
        get_filename_from_url(
            video_download_url
        )
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

    timestamp = int(
        time.time() * 1000
    )

    download_path = (
        DOWNLOAD_DIR
        / f"{timestamp}_{filename}"
    )

    # ========================================================
    # DOWNLOAD
    # ========================================================

    await status.edit(
        "⬇️ <b>Starting download...</b>",
        parse_mode="HTML",
    )

    try:

        result = await download_file(
            video_download_url,
            download_path,
            status,
            cancel_event,
        )

    except asyncio.CancelledError:

        raise

    header_filename = (
        result.get(
            "header_filename"
        )
    )

    if (
        header_filename
        and download_path.exists()
    ):

        new_path = (
            download_path.parent
            / safe_filename(
                header_filename
            )
        )

        # Don't overwrite another queue file
        if new_path.exists():

            new_path = (
                download_path.parent
                / f"{timestamp}_"
                f"{safe_filename(header_filename)}"
            )

        try:

            download_path.rename(
                new_path
            )

            download_path = new_path

        except Exception:
            pass

    # ========================================================
    # VALIDATE
    # ========================================================

    if not download_path.exists():

        raise RuntimeError(
            "Downloaded file not found."
        )

    if (
        download_path.stat().st_size
        == 0
    ):

        raise RuntimeError(
            "Downloaded file is empty."
        )

    if (
        cancel_event
        and cancel_event.is_set()
    ):

        raise asyncio.CancelledError()

    # ========================================================
    # CONVERT
    # ========================================================

    converted_path = (
        CONVERT_DIR
        / f"{download_path.stem}.mp4"
    )

    if (
        download_path.suffix.lower()
        == ".mp4"
        and is_valid_mp4(
            download_path
        )
    ):

        converted_path = (
            download_path
        )

    else:

        try:

            await convert_to_mp4(
                download_path,
                converted_path,
                status,
                cancel_event,
            )

        except asyncio.CancelledError:

            try:
                if converted_path.exists():
                    converted_path.unlink()
            except Exception:
                pass
            raise

        except Exception:

            try:
                if converted_path.exists():
                    converted_path.unlink()
            except Exception:
                pass

            try:
                if download_path.exists():
                    download_path.unlink()
            except Exception:
                pass

            raise

    if (
        cancel_event
        and cancel_event.is_set()
    ):

        raise asyncio.CancelledError()

    # ========================================================
    # VIDEO INFO
    # ========================================================

    info = await probe_video(
        converted_path
    )

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

    # ========================================================
    # THUMBNAIL
    # ========================================================

    thumbnail_path = None

    try:

        thumbnail_path = (
            CONVERT_DIR
            / f"{converted_path.stem}_thumb.jpg"
        )

        if thumbnail_url:

            await download_thumbnail(
                thumbnail_url,
                thumbnail_path,
            )

        else:

            extracted = (
                await extract_thumbnail(
                    converted_path,
                    thumbnail_path,
                )
            )

            if not extracted:

                thumbnail_path = None

    except Exception:

        thumbnail_path = None

    # ========================================================
    # SIZE
    # ========================================================

    file_size = (
        converted_path.stat().st_size
    )

    # ========================================================
    # CAPTION
    # ========================================================

    caption = (
        f"🎬 <b>{converted_path.name}</b>\n\n"
        f"📦 Size: "
        f"{format_bytes(file_size)}\n"
        f"📐 Resolution: "
        f"{width}×{height}\n"
        f"⏱ Duration: "
        f"{format_time(duration)}"
    )

    # ========================================================
    # SEND
    # ========================================================

    if (
        cancel_event
        and cancel_event.is_set()
    ):

        raise asyncio.CancelledError()

    if file_size <= BOT_API_LIMIT:

        await status.edit(
            "⬆️ <b>Uploading...</b>",
            parse_mode="HTML",
        )

        video_file = open(
            converted_path,
            "rb",
        )

        upload_start_time = time.monotonic()
        upload_last_update = 0
        upload_last_task = None

        def small_upload_progress(sent, total):

            nonlocal upload_last_update, upload_last_task

            now = time.monotonic()

            if (
                now - upload_last_update < STATUS_INTERVAL
                and sent < total
            ):
                return

            upload_last_update = now

            elapsed = now - upload_start_time
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

            text = (
                "⬆️ <b>Uploading to Telegram...</b>\n\n"
                f"{progress_bar(percent)} "
                f"{percent:.1f}%\n\n"
                f"📦 {format_bytes(sent)} / "
                f"{format_bytes(total)}\n"
                f"⚡ {format_bytes(speed)}/s\n"
                f"⏱ {format_time(elapsed)}\n"
                f"🕐 ETA {format_time(eta)}"
            )

            async def update_status():
                try:
                    await status.edit_text(
                        text,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            try:
                upload_last_task = asyncio.create_task(
                    update_status()
                )
            except Exception:
                upload_last_task = None

        thumb_file = None

        try:

            if thumbnail_path:

                try:

                    thumb_file = open(
                        thumbnail_path,
                        "rb",
                    )

                except Exception:

                    thumb_file = None

            await telethon_client.send_file(
                item.chat_id,
                video_file,
                caption=caption,
                parse_mode="HTML",
                supports_streaming=True,
                thumb=thumb_file,
                progress_callback=small_upload_progress,
            )

            # Make sure the status reaches exactly 100% before success.
            if upload_last_task:
                try:
                    await upload_last_task
                except Exception:
                    pass

            try:
                await status.edit_text(
                    "⬆️ <b>Uploading to Telegram...</b>\n\n"
                    f"{progress_bar(100)} 100.0%\n\n"
                    f"📦 {format_bytes(file_size)} / "
                    f"{format_bytes(file_size)}\n"
                    f"⚡ {format_bytes(file_size / max(time.monotonic() - upload_start_time, 0.001))}/s\n"
                    f"⏱ {format_time(time.monotonic() - upload_start_time)}\n"
                    f"🕐 ETA 00:00",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        finally:

            video_file.close()

            if thumb_file:

                thumb_file.close()

    else:

        await status.edit(
            "🚀 <b>Starting parallel Telegram upload...</b>\n\n"
            f"📦 {format_bytes(file_size)}\n"
            f"⚡ Workers: "
            f"{UPLOAD_WORKERS}\n"
            f"🧩 Part size: 512 KB",
            parse_mode="HTML",
        )

        await send_large_video(
            chat_id=item.chat_id,
            video_path=converted_path,
            caption=caption,
            thumbnail_path=thumbnail_path,
            width=width,
            height=height,
            duration=duration,
            status_message=status,
            cancel_event=cancel_event,
        )

    # ========================================================
    # SUCCESS
    # ========================================================

    try:

        await status.edit(
            "✅ <b>Upload completed!</b>\n\n"
            f"🎬 {converted_path.name}\n"
            f"📦 {format_bytes(file_size)}\n"
            f"📐 {width}×{height}\n"
            f"⏱ {format_time(duration)}",
            parse_mode="HTML",
        )

    except Exception:
        pass

    # ========================================================
    # CLEANUP
    # ========================================================

    await cleanup_files(
        download_path,
        converted_path,
        thumbnail_path,
    )


# ============================================================
# CLEANUP
# ============================================================

async def cleanup_files(
    download_path,
    converted_path,
    thumbnail_path,
):

    paths = [
        download_path,
        converted_path,
        thumbnail_path,
    ]

    for path in paths:

        if not path:
            continue

        try:

            path = Path(path)

            if path.exists():

                path.unlink()

        except Exception as e:

            print(
                "Cleanup error:",
                e,
            )


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🎬 <b>Movie Upload Bot</b>\n\n"
        "Send one or multiple video URLs.\n\n"
        "<b>Example:</b>\n\n"
        "<code>Video: https://site.com/a.mp4\n"
        "Thumbnail: https://site.com/a.jpg\n\n"
        "Video: https://site.com/b.mp4\n"
        "Thumbnail: https://site.com/b.jpg</code>\n\n"
        "Commands:\n"
        "/queue - Queue ကြည့်ရန်\n"
        "/clear - Waiting queue ရှင်းရန်\n"
        "/cancel - လက်ရှိအလုပ် Cancel",
        parse_mode="HTML",
    )


# ============================================================
# /QUEUE
# ============================================================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = await get_queue_text()

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# ============================================================
# /CLEAR
# ============================================================

async def clear_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    async with queue_lock:

        count = len(
            queue_items
        )

        queue_items.clear()

        # Drain asyncio queue
        while not queue.empty():

            try:

                queue.get_nowait()

                queue.task_done()

            except asyncio.QueueEmpty:

                break

    await update.message.reply_text(
        "🧹 <b>Queue cleared.</b>\n\n"
        f"Removed: {count}",
        parse_mode="HTML",
    )


# ============================================================
# /CANCEL
# ============================================================

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    global current_cancel_event

    if not current_item:

        await update.message.reply_text(
            "ℹ️ လက်ရှိ processing လုပ်နေတဲ့ item မရှိပါဘူး။"
        )

        return

    if (
        current_item.chat_id
        != update.effective_chat.id
    ):

        await update.message.reply_text(
            "❌ ဒီ queue ကို သင် cancel လုပ်လို့မရပါဘူး။"
        )

        return

    if current_cancel_event:

        current_cancel_event.set()

        await update.message.reply_text(
            "🛑 <b>Cancel requested.</b>\n\n"
            "လက်ရှိ operation ရပ်သွားပြီး cleanup လုပ်ပါမယ်။",
            parse_mode="HTML",
        )

    else:

        await update.message.reply_text(
            "ℹ️ Cancel event မရသေးပါဘူး။"
        )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:

        return

    text = (
        update.message.text
        or ""
    )

    parsed = (
        parse_multiple_requests(
            text
        )
    )

    if not parsed:

        await update.message.reply_text(
            "❌ Video URL မတွေ့ပါဘူး။\n\n"
            "ဥပမာ:\n"
            "<code>Video: https://site.com/video.mp4</code>",
            parse_mode="HTML",
        )

        return

    valid_items = []

    for video_url, thumb_url in parsed:

        if not is_http_url(
            video_url
        ):

            continue

        valid_items.append(
            (
                video_url,
                thumb_url,
            )
        )

    if not valid_items:

        await update.message.reply_text(
            "❌ Valid HTTP/HTTPS video URL မတွေ့ပါဘူး။"
        )

        return

    # ========================================================
    # ADD TO QUEUE
    # ========================================================

    added = []

    async with queue_lock:

        start_number = (
            len(queue_items)
            + (
                1
                if current_item
                else 0
            )
        )

        for index, (
            video_url,
            thumb_url,
        ) in enumerate(
            valid_items,
            start=1,
        ):

            item = QueueItem(
                video_url=video_url,
                thumbnail_url=thumb_url,
                chat_id=update.effective_chat.id,
                user_id=(
                    update.effective_user.id
                    if update.effective_user
                    else 0
                ),
                number=start_number + index,
            )

            queue_items.append(
                item
            )

            await queue.put(
                item
            )

            added.append(
                item
            )

    # ========================================================
    # QUEUE MESSAGE
    # ========================================================

    lines = [
        "📥 <b>Added to Queue</b>",
        "",
        f"🎬 Links: {len(added)}",
        "",
    ]

    for i, item in enumerate(
        added,
        start=1,
    ):

        short_url = (
            item.video_url
        )

        if len(short_url) > 80:

            short_url = (
                short_url[:77]
                + "..."
            )

        lines.append(
            f"{i}. {short_url}"
        )

    lines.extend(
        [
            "",
            "📋 <b>Queue:</b>",
            f"⏳ Waiting: "
            f"{len(queue_items)}",
            "",
            "🔄 တစ်ခုချင်းစီကို "
            "Download → Upload ပြီးမှ "
            "နောက်တစ်ခု ဆက်လုပ်ပါမယ်။",
        ]
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


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

    global queue_worker_task

    print(
        "=" * 60
    )

    print(
        "Movie Upload Bot"
    )

    print(
        "=" * 60
    )

    print(
        "UPLOAD WORKERS:",
        UPLOAD_WORKERS,
    )

    print(
        "PART SIZE:",
        format_bytes(
            PART_SIZE
        ),
    )

    print(
        "FFMPEG:",
        shutil.which(
            "ffmpeg"
        ),
    )

    print(
        "FFPROBE:",
        shutil.which(
            "ffprobe"
        ),
    )

    print(
        "=" * 60
    )

    # ========================================================
    # TELETHON
    # ========================================================

    await telethon_client.start(
        bot_token=BOT_TOKEN
    )

    me = (
        await telethon_client.get_me()
    )

    print(
        "Telethon connected:",
        me.username or me.id,
    )

    # ========================================================
    # QUEUE WORKER
    # ========================================================

    queue_worker_task = (
        asyncio.create_task(
            queue_worker()
        )
    )

    # ========================================================
    # PYTHON TELEGRAM BOT
    # ========================================================

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
        CommandHandler(
            "queue",
            queue_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "clear",
            clear_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_command,
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

    print(
        "Bot is running..."
    )

    # ========================================================
    # START
    # ========================================================

    await application.initialize()

    await application.start()

    if application.updater:

        await application.updater.start_polling()

    try:

        while True:

            await asyncio.sleep(
                3600
            )

    finally:

        if queue_worker_task:

            queue_worker_task.cancel()

            try:

                await queue_worker_task

            except asyncio.CancelledError:
                pass

        if application.updater:

            await application.updater.stop()

        await application.stop()

        await application.shutdown()

        await telethon_client.disconnect()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )
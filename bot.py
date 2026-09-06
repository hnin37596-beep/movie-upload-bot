import os
import re
import time
import asyncio
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


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

CHUNK_SIZE = 1024 * 1024  # 1 MB
STATUS_INTERVAL = 2


# =========================================================
# HELPERS
# =========================================================

def format_bytes(size: int) -> str:
    if size is None:
        return "Unknown"

    size = float(size)

    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} PB"


def format_time(seconds: float) -> str:
    if not seconds or seconds < 0:
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


def is_http_url(text: str) -> bool:
    return bool(
        re.match(
            r"^https?://",
            text.strip(),
            re.IGNORECASE,
        )
    )


def is_mediafire_url(url: str) -> bool:
    url = url.lower()

    return (
        "mediafire.com/file/" in url
        or "www.mediafire.com/file/" in url
        or "mfi.re/" in url
    )


# =========================================================
# FILENAME
# =========================================================

def filename_from_headers(headers) -> str | None:

    content_disposition = headers.get(
        "Content-Disposition",
        "",
    )

    if not content_disposition:
        return None

    # filename*=UTF-8''
    match = re.search(
        r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)",
        content_disposition,
        re.IGNORECASE,
    )

    if match:
        name = unquote(match.group(1).strip('"'))

        if name:
            return name

    # filename="..."
    match = re.search(
        r'filename\s*=\s*"([^"]+)"',
        content_disposition,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    # filename=...
    match = re.search(
        r"filename\s*=\s*([^;]+)",
        content_disposition,
        re.IGNORECASE,
    )

    if match:
        return match.group(1).strip().strip('"')

    return None


def filename_from_url(url: str) -> str:

    clean_url = url.split("?", 1)[0]

    name = clean_url.rstrip("/").split("/")[-1]

    name = unquote(name)

    if not name:
        return "download.bin"

    return name


def safe_filename(name: str) -> str:

    name = unquote(name)

    name = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        name,
    )

    name = name.strip()

    if not name:
        return "download.bin"

    return name[:200]


# =========================================================
# MEDIAFIRE RESOLVER
# =========================================================

async def resolve_mediafire_url(
    session: aiohttp.ClientSession,
    mediafire_url: str,
):

    print("MediaFire URL detected")

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

        final_page_url = str(response.url)

        print(
            "MediaFire page:",
            final_page_url,
        )

        if response.status != 200:
            raise RuntimeError(
                f"MediaFire page HTTP {response.status}"
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
    # 1. Official download button
    # -----------------------------------------------------

    download_button = soup.select_one(
        "#downloadButton"
    )

    if download_button:

        href = download_button.get("href")

        if href:
            candidates.append(
                urljoin(
                    final_page_url,
                    href,
                )
            )

    # -----------------------------------------------------
    # 2. Links containing download
    # -----------------------------------------------------

    for tag in soup.find_all("a"):

        href = tag.get("href")

        if not href:
            continue

        href = urljoin(
            final_page_url,
            href,
        )

        low = href.lower()

        if (
            "download" in low
            or "mediafire.com" in low
        ):
            candidates.append(href)

    # -----------------------------------------------------
    # 3. Search HTML for direct MediaFire URL
    # -----------------------------------------------------

    patterns = [

        r'https?://download\d*\.mediafire\.com/[^\s"\']+',

        r'https?://download\d*\.mediafire\.com/[^\s"\'<>]+',

        r'https?:\\?/\\?/download\d*\.mediafire\.com\\?/[^"\']+',
    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            html,
            re.IGNORECASE,
        )

        for match in matches:

            match = (
                match
                .replace("\\/", "/")
                .replace("\\u0026", "&")
                .replace("&amp;", "&")
            )

            candidates.append(match)

    # -----------------------------------------------------
    # 4. Search JS / HTML around download URL
    # -----------------------------------------------------

    direct_candidates = []

    for candidate in candidates:

        candidate = (
            candidate
            .replace("&amp;", "&")
            .strip()
            .strip('"')
            .strip("'")
        )

        if (
            candidate.startswith("http://")
            or candidate.startswith("https://")
        ):
            direct_candidates.append(
                candidate
            )

    # Remove duplicates
    unique = []

    for url in direct_candidates:

        if url not in unique:
            unique.append(url)

    if not unique:

        raise RuntimeError(
            "MediaFire direct download link မတွေ့ပါ။ "
            "MediaFire page က direct URL ကို မထုတ်ပေးနိုင်သေးပါ။"
        )

    print(
        f"MediaFire candidates: {len(unique)}"
    )

    # -----------------------------------------------------
    # Test candidate URLs
    # -----------------------------------------------------

    for candidate in unique:

        try:

            test_headers = {
                "User-Agent": headers["User-Agent"],
                "Accept": "*/*",
                "Referer": final_page_url,
            }

            async with session.get(
                candidate,
                headers=test_headers,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(
                    total=60
                ),
            ) as test_response:

                content_type = (
                    test_response.headers.get(
                        "Content-Type",
                        ""
                    ).lower()
                )

                final_url = str(
                    test_response.url
                )

                content_length = (
                    test_response.headers.get(
                        "Content-Length"
                    )
                )

                print(
                    "Candidate:",
                    candidate[:120]
                )

                print(
                    "Final:",
                    final_url[:120]
                )

                print(
                    "Content-Type:",
                    content_type
                )

                print(
                    "Content-Length:",
                    content_length
                )

                # Real downloadable response
                if (
                    content_type.startswith("video/")
                    or "application/octet-stream"
                    in content_type
                    or "binary/octet-stream"
                    in content_type
                ):
                    return (
                        final_url,
                        final_page_url,
                    )

        except Exception as e:

            print(
                "Candidate test error:",
                repr(e)
            )

    # If direct content type could not be confirmed,
    # return first candidate and let download validator decide.
    return (
        unique[0],
        final_page_url,
    )


# =========================================================
# VIDEO VALIDATION
# =========================================================

def looks_like_html(first_bytes: bytes) -> bool:

    data = first_bytes.lstrip().lower()

    return (
        data.startswith(b"<html")
        or data.startswith(b"<!doctype")
        or data.startswith(b"<head")
        or data.startswith(b"<body")
        or data.startswith(b"<script")
    )


def looks_like_mp4(first_bytes: bytes) -> bool:

    # MP4/MOV normally contains "ftyp"
    # inside the first MP4 box.

    if len(first_bytes) >= 12:

        if first_bytes[4:8] == b"ftyp":
            return True

    # Sometimes ftyp is not exactly at offset 4.
    if b"ftyp" in first_bytes[:64]:
        return True

    return False


def looks_like_video(
    first_bytes: bytes,
    content_type: str,
    filename: str,
) -> bool:

    content_type = (
        content_type or ""
    ).lower()

    filename = (
        filename or ""
    ).lower()

    if content_type.startswith("video/"):
        return True

    if looks_like_mp4(first_bytes):
        return True

    video_extensions = (
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

    if filename.endswith(
        video_extensions
    ):
        # Filename alone is NOT enough.
        # We only accept if it's not HTML.
        if not looks_like_html(first_bytes):
            return True

    return False


# =========================================================
# DOWNLOAD
# =========================================================

async def download_file(
    session: aiohttp.ClientSession,
    url: str,
    save_path: Path,
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

        final_url = str(response.url)

        status = response.status

        content_type = (
            response.headers.get(
                "Content-Type",
                "",
            ).lower()
        )

        content_length_header = (
            response.headers.get(
                "Content-Length"
            )
        )

        try:

            total_size = int(
                content_length_header
            ) if content_length_header else 0

        except ValueError:

            total_size = 0

        print("HTTP Status:", status)
        print("Final URL:", final_url)
        print("Content-Type:", content_type)
        print("Content-Length:", total_size)

        if status != 200:

            raise RuntimeError(
                f"Download HTTP Error: {status}"
            )

        # -------------------------------------------------
        # Read first chunk for validation
        # -------------------------------------------------

        first_chunk = await response.content.read(
            CHUNK_SIZE
        )

        if not first_chunk:

            raise RuntimeError(
                "Server က empty response ပြန်ပေးပါတယ်။"
            )

        # HTML response protection
        if looks_like_html(first_chunk):

            raise RuntimeError(
                "Video အစား HTML page ရရှိနေပါတယ်။ "
                "Direct download link မမှန်ပါ။"
            )

        # -------------------------------------------------
        # Filename
        # -------------------------------------------------

        filename = filename_from_headers(
            response.headers
        )

        if not filename:

            filename = filename_from_url(
                final_url
            )

        filename = safe_filename(
            filename
        )

        # -------------------------------------------------
        # Validate video
        # -------------------------------------------------

        if not looks_like_video(
            first_chunk,
            content_type,
            filename,
        ):

            preview = first_chunk[:32]

            raise RuntimeError(
                "Downloaded file က video file မဟုတ်ပါ။\n"
                f"Content-Type: {content_type or 'Unknown'}\n"
                f"First bytes: {preview!r}\n"
                f"Final URL: {final_url}"
            )

        # -------------------------------------------------
        # Save
        # -------------------------------------------------

        actual_path = (
            save_path.parent / filename
        )

        downloaded = 0

        with open(
            actual_path,
            "wb",
        ) as file:

            # Write first validated chunk
            file.write(first_chunk)

            downloaded += len(first_chunk)

            last_status = 0

            while True:

                chunk = await response.content.read(
                    CHUNK_SIZE
                )

                if not chunk:
                    break

                file.write(chunk)

                downloaded += len(chunk)

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

                    if total_size:

                        percent = (
                            downloaded
                            / total_size
                            * 100
                        )

                        remaining = (
                            total_size
                            - downloaded
                        )

                        eta = (
                            remaining / speed
                            if speed > 0
                            else 0
                        )

                        text = (
                            "╔══════════════════════════╗\n"
                            "║   📥 *DOWNLOAD STATUS*   ║\n"
                            "╚══════════════════════════╝\n\n"
                            "⬇️ *DOWNLOADING*\n\n"
                            f"📁 *File:* `{filename}`\n"
                            f"{progress_bar(percent)} "
                            f"*{percent:.1f}%*\n"
                            f"┣ {format_bytes(downloaded)} / "
                            f"{format_bytes(total_size)}\n"
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
                            f"📁 *File:* `{filename}`\n"
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

        final_size = (
            actual_path.stat().st_size
        )

        # Final validation
        if final_size == 0:

            actual_path.unlink(
                missing_ok=True
            )

            raise RuntimeError(
                "Downloaded file size = 0"
            )

        elapsed = (
            time.monotonic() - started
        )

        return (
            actual_path,
            final_size,
            elapsed,
            final_url,
        )


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "╔══════════════════════════╗\n"
        "║   🎬 *MOVIE UPLOAD BOT*  ║\n"
        "╚══════════════════════════╝\n\n"
        "🔗 Video URL ပို့ပါ။\n\n"
        "✅ Direct MP4\n"
        "✅ Direct Video URL\n"
        "✅ MediaFire URL\n\n"
        "📥 Bot က video ကို download လုပ်ပေးပါမယ်။"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
    )


# =========================================================
# /HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "📖 *HELP*\n\n"
        "Bot ထဲကို video URL တစ်ခု ပို့ပါ။\n\n"
        "ဥပမာ:\n"
        "`https://example.com/movie.mp4`\n\n"
        "MediaFire:\n"
        "`https://www.mediafire.com/file/...`\n\n"
        "📌 Direct video URL ဖြစ်ရင် တိုက်ရိုက် download လုပ်ပါမယ်။\n"
        "📌 MediaFire ဖြစ်ရင် direct download URL ရှာပါမယ်။"
    )

    await update.message.reply_text(
        text,
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

    text = (
        update.message.text or ""
    ).strip()

    if not is_http_url(text):

        await update.message.reply_text(
            "❌ HTTP/HTTPS URL ပို့ပါ။"
        )

        return

    status = await update.message.reply_text(
        "╔══════════════════════════╗\n"
        "║   🔎 *CHECKING URL*      ║\n"
        "╚══════════════════════════╝\n\n"
        "⏳ URL ကိုစစ်ဆေးနေပါတယ်...",
        parse_mode="Markdown",
    )

    # -----------------------------------------------------
    # HTTP session
    # -----------------------------------------------------

    cookie_jar = aiohttp.CookieJar(
        unsafe=True
    )

    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=60,
        sock_read=300,
    )

    connector = aiohttp.TCPConnector(
        limit=4,
        ttl_dns_cache=300,
    )

    async with aiohttp.ClientSession(
        connector=connector,
        cookie_jar=cookie_jar,
        timeout=timeout,
    ) as session:

        try:

            download_url = text
            referer = None

            # -------------------------------------------------
            # MediaFire
            # -------------------------------------------------

            if is_mediafire_url(text):

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
                    text,
                )

            # -------------------------------------------------
            # Download
            # -------------------------------------------------

            await status.edit_text(
                "╔══════════════════════════╗\n"
                "║   📥 *DOWNLOAD START*    ║\n"
                "╚══════════════════════════╝\n\n"
                "⏳ Video download စတင်နေပါတယ်...",
                parse_mode="Markdown",
            )

            # Temporary name.
            # Real filename will be determined from
            # Content-Disposition / final URL.
            temp_path = (
                DOWNLOAD_DIR / "download.tmp"
            )

            (
                file_path,
                file_size,
                elapsed,
                final_url,
            ) = await download_file(
                session,
                download_url,
                temp_path,
                status,
            )

            # -------------------------------------------------
            # Download completed
            # -------------------------------------------------

            speed = (
                file_size / elapsed
                if elapsed > 0
                else 0
            )

            await status.edit_text(
                "╔══════════════════════════╗\n"
                "║   ✅ *DOWNLOAD OK*       ║\n"
                "╚══════════════════════════╝\n\n"
                f"📁 *File:* `{file_path.name}`\n"
                f"📦 *Size:* {format_bytes(file_size)}\n"
                f"⚡ *Avg Speed:* "
                f"{format_bytes(speed)}/s\n"
                f"⏱ *Time:* "
                f"{format_time(elapsed)}\n\n"
                "📤 *Telegram Upload* ကို နောက်တစ်ဆင့်မှာ ချိတ်ပါမယ်။",
                parse_mode="Markdown",
            )

            # -------------------------------------------------
            # CURRENT STEP:
            # Telegram Cloud Bot API cannot upload >50MB.
            # Keep file for the next upload engine.
            # -------------------------------------------------

            if file_size > 50 * 1024 * 1024:

                await update.message.reply_text(
                    "⚠️ *Download ပြီးပါပြီ*\n\n"
                    f"📁 `{file_path.name}`\n"
                    f"📦 {format_bytes(file_size)}\n\n"
                    "📌 File က 50 MB ထက်ကြီးပါတယ်။\n"
                    "Telegram Cloud Bot API နဲ့ တိုက်ရိုက် upload မလုပ်နိုင်ပါ။\n\n"
                    "🚀 *Large File Upload Engine* ကို "
                    "နောက်အဆင့်မှာ ချိတ်ရပါမယ်။",
                    parse_mode="Markdown",
                )

            else:

                # Small files can still use normal Bot API.
                await status.edit_text(
                    "╔══════════════════════════╗\n"
                    "║   📤 *UPLOADING*         ║\n"
                    "╚══════════════════════════╝\n\n"
                    "⏳ Telegram သို့ upload လုပ်နေပါတယ်...",
                    parse_mode="Markdown",
                )

                try:

                    with open(
                        file_path,
                        "rb",
                    ) as video_file:

                        await update.message.reply_document(
                            document=video_file,
                            filename=file_path.name,
                            read_timeout=300,
                            write_timeout=300,
                            connect_timeout=60,
                            pool_timeout=60,
                        )

                    await status.edit_text(
                        "╔══════════════════════════╗\n"
                        "║   ✅ *ALL DONE*          ║\n"
                        "╚══════════════════════════╝\n\n"
                        f"📁 `{file_path.name}`\n"
                        f"📦 {format_bytes(file_size)}\n\n"
                        "✅ Download\n"
                        "✅ Telegram Upload",
                        parse_mode="Markdown",
                    )

                    file_path.unlink(
                        missing_ok=True
                    )

                except Exception as upload_error:

                    await status.edit_text(
                        "╔══════════════════════════╗\n"
                        "║   ⚠️ *DOWNLOAD OK*       ║\n"
                        "╚══════════════════════════╝\n\n"
                        f"📁 `{file_path.name}`\n"
                        f"📦 {format_bytes(file_size)}\n\n"
                        "❌ Telegram Upload မအောင်မြင်ပါ။\n\n"
                        f"Error:\n"
                        f"`{type(upload_error).__name__}: "
                        f"{upload_error}`\n\n"
                        "💾 Downloaded file ကို runner ပေါ်မှာ "
                        "လက်ရှိ run ပြီးဆုံးတဲ့အထိ မဖျက်ထားပါ။",
                        parse_mode="Markdown",
                    )

        except Exception as error:

            print(
                "ERROR:",
                repr(error)
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
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "BOT ERROR:",
        repr(context.error)
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN GitHub Secret မတွေ့ပါ။"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
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

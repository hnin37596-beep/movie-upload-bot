import os
import re
import time
import asyncio
from pathlib import Path
from urllib.parse import urljoin, unquote

import aiohttp
from bs4 import BeautifulSoup

from telegram import Update
from telegram.request import HTTPXRequest
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

TOKEN = os.getenv("BOT_TOKEN")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

STATUS_INTERVAL = 2


# =========================================================
# USER AGENT
# =========================================================

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/128.0.0.0 Mobile Safari/537.36"
)


# =========================================================
# FORMAT HELPERS
# =========================================================

def format_bytes(size):

    if size is None:
        return "Unknown"

    size = float(size)

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ]

    for unit in units:

        if size < 1024:
            return f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} PB"


def format_time(seconds):

    if seconds is None:
        return "--:--"

    if seconds < 0:
        return "--:--"

    seconds = int(seconds)

    hours, remainder = divmod(
        seconds,
        3600
    )

    minutes, secs = divmod(
        remainder,
        60
    )

    if hours > 0:

        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{secs:02d}"
        )

    return (
        f"{minutes:02d}:"
        f"{secs:02d}"
    )


def progress_bar(percent, length=12):

    percent = max(
        0,
        min(
            100,
            percent
        )
    )

    filled = int(
        length * percent / 100
    )

    return (
        "▰" * filled
        +
        "▱" * (length - filled)
    )


def safe_filename(filename):

    filename = unquote(filename)

    filename = filename.replace(
        "\x00",
        ""
    )

    filename = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        filename
    )

    filename = filename.strip(
        " ."
    )

    if not filename:

        filename = "download.bin"

    return filename


# =========================================================
# MEDIAFIRE DETECTION
# =========================================================

def is_mediafire_url(url):

    url_lower = url.lower()

    return (
        "mediafire.com/file/" in url_lower
        or "www.mediafire.com/file/" in url_lower
        or "mfi.re/" in url_lower
    )


# =========================================================
# MEDIAFIRE DIRECT URL RESOLVER
# =========================================================

async def resolve_mediafire_url(
    session,
    mediafire_url
):

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
        "Accept-Language": (
            "en-US,en;q=0.9"
        ),
        "Referer": "https://www.google.com/",
    }

    async with session.get(
        mediafire_url,
        headers=headers,
        allow_redirects=True,
        timeout=aiohttp.ClientTimeout(
            total=120
        )
    ) as response:

        response.raise_for_status()

        final_page_url = str(
            response.url
        )

        html = await response.text(
            errors="ignore"
        )

    # -----------------------------------------------------
    # METHOD 1
    # BeautifulSoup downloadButton
    # -----------------------------------------------------

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    download_button = soup.find(
        id="downloadButton"
    )

    if download_button:

        href = download_button.get(
            "href"
        )

        if href:

            href = href.strip()

            href = href.replace(
                "&amp;",
                "&"
            )

            href = href.replace(
                "\\/",
                "/"
            )

            direct_url = urljoin(
                final_page_url,
                href
            )

            if (
                "mediafire.com"
                in direct_url.lower()
                or
                "download"
                in direct_url.lower()
            ):

                return direct_url

    # -----------------------------------------------------
    # METHOD 2
    # Find download*.mediafire.com URL
    # -----------------------------------------------------

    patterns = [

        r'https?://download\d+\.mediafire\.com/[^\s"\'<>]+',

        r'https?:\\?/\\?/download\d+\.mediafire\.com\\?/[^"\']+',

        r'"downloadUrl"\s*:\s*"([^"]+)"',

        r'"download_url"\s*:\s*"([^"]+)"',

    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            html,
            re.IGNORECASE
        )

        for match in matches:

            direct_url = match

            direct_url = direct_url.replace(
                "\\/",
                "/"
            )

            direct_url = direct_url.replace(
                "&amp;",
                "&"
            )

            direct_url = direct_url.strip(
                "\"'"
            )

            if direct_url.startswith(
                "//"
            ):

                direct_url = (
                    "https:"
                    +
                    direct_url
                )

            if (
                "download"
                in direct_url.lower()
            ):

                return direct_url

    # -----------------------------------------------------
    # METHOD 3
    # Search all href links
    # -----------------------------------------------------

    for tag in soup.find_all(
        "a",
        href=True
    ):

        href = tag.get(
            "href"
        )

        if not href:
            continue

        href = href.strip()

        href = href.replace(
            "&amp;",
            "&"
        )

        href = href.replace(
            "\\/",
            "/"
        )

        full_url = urljoin(
            final_page_url,
            href
        )

        if (
            "download"
            in full_url.lower()
            and
            (
                "mediafire"
                in full_url.lower()
            )
        ):

            return full_url

    # -----------------------------------------------------
    # Failed
    # -----------------------------------------------------

    raise RuntimeError(
        "MediaFire ရဲ့ actual download link "
        "ကိုရှာမတွေ့ပါ။ "
        "File က private / deleted ဖြစ်နိုင်ပါတယ် "
        "သို့မဟုတ် MediaFire page structure ပြောင်းထားနိုင်ပါတယ်။"
    )


# =========================================================
# GET FILENAME FROM RESPONSE
# =========================================================

def filename_from_headers(
    headers
):

    content_disposition = headers.get(
        "Content-Disposition",
        ""
    )

    if content_disposition:

        match = re.search(
            r"filename\*=UTF-8''([^;]+)",
            content_disposition,
            re.IGNORECASE
        )

        if match:

            return safe_filename(
                match.group(1)
            )

        match = re.search(
            r'filename="?([^";]+)"?',
            content_disposition,
            re.IGNORECASE
        )

        if match:

            return safe_filename(
                match.group(1)
            )

    return None


# =========================================================
# GET FILENAME FROM URL
# =========================================================

def filename_from_url(url):

    try:

        clean_url = (
            url.split("?")[0]
            .rstrip("/")
        )

        name = clean_url.split(
            "/"
        )[-1]

        name = unquote(
            name
        )

        if name:

            return safe_filename(
                name
            )

    except Exception:

        pass

    return "video.mp4"


# =========================================================
# DOWNLOAD FILE
# =========================================================

async def download_file(
    url,
    output_path,
    status_message,
    referer=None
):

    start_time = time.time()

    downloaded = 0

    last_update = 0

    headers = {

        "User-Agent": USER_AGENT,

        "Accept": "*/*",

    }

    if referer:

        headers[
            "Referer"
        ] = referer

    timeout = aiohttp.ClientTimeout(

        total=None,

        connect=120,

        sock_connect=120,

        sock_read=600,

    )

    connector = aiohttp.TCPConnector(

        limit=4,

        limit_per_host=2,

        ssl=True,

        enable_cleanup_closed=True,

    )

    async with aiohttp.ClientSession(

        timeout=timeout,

        connector=connector,

        headers=headers,

    ) as session:

        async with session.get(

            url,

            allow_redirects=True,

        ) as response:

            response.raise_for_status()

            total = response.content_length

            # ---------------------------------------------
            # Get filename from actual response
            # ---------------------------------------------

            server_filename = (
                filename_from_headers(
                    response.headers
                )
            )

            if server_filename:

                output_path = (
                    output_path.parent
                    /
                    server_filename
                )

            # ---------------------------------------------
            # Download
            # ---------------------------------------------

            with open(
                output_path,
                "wb"
            ) as file:

                async for chunk in response.content.iter_chunked(
                    1024 * 1024
                ):

                    file.write(
                        chunk
                    )

                    downloaded += len(
                        chunk
                    )

                    now = time.time()

                    if (
                        now - last_update
                        >= STATUS_INTERVAL
                    ):

                        elapsed = (
                            now
                            -
                            start_time
                        )

                        speed = (
                            downloaded
                            /
                            elapsed
                            if elapsed > 0
                            else 0
                        )

                        if total:

                            percent = (
                                downloaded
                                /
                                total
                                *
                                100
                            )

                            if speed > 0:

                                eta = (
                                    total
                                    -
                                    downloaded
                                ) / speed

                            else:

                                eta = None

                            text = (

                                "╔══════════════════════════╗\n"
                                "║   📥 *DOWNLOAD STATUS*   ║\n"
                                "╚══════════════════════════╝\n\n"

                                "⬇️ *DOWNLOADING*\n\n"

                                f"📁 *File:* `{output_path.name}`\n"

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

                                f"📁 *File:* `{output_path.name}`\n"

                                f"┣ Downloaded: "
                                f"{format_bytes(downloaded)}\n"

                                f"┣ Speed: "
                                f"{format_bytes(speed)}/s\n"

                                f"┗ Elapsed: "
                                f"{format_time(elapsed)}"

                            )

                        try:

                            await status_message.edit_text(

                                text,

                                parse_mode="Markdown"

                            )

                        except Exception:

                            pass

                        last_update = now

            return (
                output_path,
                downloaded
            )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "🎬 *Movie Download Bot*\n\n"

        "အသုံးပြုနည်း\n\n"

        "1️⃣ Direct MP4 URL ပို့နိုင်ပါတယ်။\n\n"

        "2️⃣ MediaFire link ပို့နိုင်ပါတယ်။\n\n"

        "ဥပမာ:\n"

        "`https://www.mediafire.com/file/xxxxx/file`\n\n"

        "Bot က MediaFire direct download URL "
        "ကိုရှာပြီး download လုပ်ပါမယ်။\n\n"

        "⚠️ ကိုယ်ပိုင် သို့မဟုတ် "
        "ဖြန့်ဝေခွင့်ရှိတဲ့ content တွေအတွက်သာ "
        "အသုံးပြုပါ။",

        parse_mode="Markdown"

    )


# =========================================================
# HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "📖 *HELP*\n\n"

        "Bot သို့ URL ပို့ပါ။\n\n"

        "✅ Direct MP4\n"
        "✅ MediaFire file link\n\n"

        "MediaFire link ဖြစ်ရင်\n"

        "1️⃣ Download page ကိုဖွင့်မယ်\n"
        "2️⃣ Direct download URL ရှာမယ်\n"
        "3️⃣ File ကို download လုပ်မယ်\n"
        "4️⃣ Download progress ပြမယ်\n\n"

        "⚠️ Authorized content only.",

        parse_mode="Markdown"

    )


# =========================================================
# URL HANDLER
# =========================================================

async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:

        return

    if not update.message.text:

        return

    original_url = (
        update.message.text.strip()
    )

    # ---------------------------------------------
    # Check URL
    # ---------------------------------------------

    if not (

        original_url.startswith(
            "http://"
        )

        or

        original_url.startswith(
            "https://"
        )

    ):

        return

    status_message = await update.message.reply_text(

        "⏳ *Preparing...*",

        parse_mode="Markdown"

    )

    output_path = None

    try:

        # =================================================
        # MEDIAFIRE
        # =================================================

        if is_mediafire_url(
            original_url
        ):

            await status_message.edit_text(

                "╔══════════════════════════╗\n"
                "║   🔎 *MEDIAFIRE*         ║\n"
                "╚══════════════════════════╝\n\n"

                "🔗 MediaFire download page ကိုဖတ်နေပါတယ်...\n\n"
                "⏳ Direct download link ရှာနေပါတယ်...",

                parse_mode="Markdown"

            )

            timeout = aiohttp.ClientTimeout(

                total=120,

                connect=60,

                sock_connect=60,

                sock_read=120,

            )

            connector = aiohttp.TCPConnector(

                limit=4,

                limit_per_host=2,

                ssl=True,

            )

            async with aiohttp.ClientSession(

                timeout=timeout,

                connector=connector,

            ) as session:

                direct_url = (
                    await resolve_mediafire_url(
                        session,
                        original_url
                    )
                )

            # ---------------------------------------------
            # Direct URL found
            # ---------------------------------------------

            await status_message.edit_text(

                "╔══════════════════════════╗\n"
                "║   ✅ *LINK FOUND*        ║\n"
                "╚══════════════════════════╝\n\n"

                "🔗 MediaFire direct download link ရပါပြီ။\n\n"

                "⬇️ Download စတင်နေပါတယ်...",

                parse_mode="Markdown"

            )

            # Use generic filename first
            filename = (
                filename_from_url(
                    direct_url
                )
            )

            # If extension isn't useful
            if (
                "." not in filename
                or
                len(filename) < 3
            ):

                filename = "mediafire_download.mp4"

            output_path = (
                DOWNLOAD_DIR
                /
                filename
            )

            # ---------------------------------------------
            # Download
            # ---------------------------------------------

            output_path, downloaded = (
                await download_file(

                    direct_url,

                    output_path,

                    status_message,

                    referer=(
                        original_url
                    ),

                )
            )

        # =================================================
        # DIRECT URL
        # =================================================

        else:

            filename = (
                filename_from_url(
                    original_url
                )
            )

            output_path = (
                DOWNLOAD_DIR
                /
                filename
            )

            await status_message.edit_text(

                "⬇️ *Direct file download စတင်နေပါတယ်...*",

                parse_mode="Markdown"

            )

            output_path, downloaded = (
                await download_file(

                    original_url,

                    output_path,

                    status_message,

                )
            )

        # =================================================
        # DOWNLOAD COMPLETE
        # =================================================

        if not output_path.exists():

            raise RuntimeError(
                "Download ပြီးသွားတယ်လို့ပြပေမယ့် "
                "local file မတွေ့ပါ။"
            )

        file_size = (
            output_path.stat().st_size
        )

        if file_size <= 0:

            raise RuntimeError(
                "Downloaded file size က 0 bytes ဖြစ်နေပါတယ်။"
            )

        elapsed = time.time()

        await status_message.edit_text(

            "╔══════════════════════════╗\n"
            "║   ✅ *DOWNLOAD COMPLETE* ║\n"
            "╚══════════════════════════╝\n\n"

            f"📁 *File:* `{output_path.name}`\n"

            f"📦 *Size:* "
            f"{format_bytes(file_size)}\n\n"

            "✅ File ကို server ထဲ successfully download လုပ်ပြီးပါပြီ။\n\n"

            "📡 Telegram upload ကို စမ်းသပ်နေပါတယ်...",

            parse_mode="Markdown"

        )

        # =================================================
        # TELEGRAM UPLOAD
        # =================================================
        #
        # NOTE:
        # Telegram Bot API နဲ့ large files upload လုပ်ရာမှာ
        # platform/API size limits ရှိနိုင်ပါတယ်။
        #
        # Upload မအောင်မြင်ရင် downloaded file ကို
        # မဖျက်ပါဘူး။
        # =================================================

        try:

            with open(
                output_path,
                "rb"
            ) as video_file:

                await update.message.reply_document(

                    document=video_file,

                    filename=output_path.name,

                    caption=(
                        f"🎬 `{output_path.name}`\n\n"
                        f"📦 {format_bytes(file_size)}"
                    ),

                    read_timeout=600,

                    write_timeout=600,

                    connect_timeout=120,

                    pool_timeout=120,

                )

            await status_message.edit_text(

                "╔══════════════════════════╗\n"
                "║   ✅ *ALL DONE*          ║\n"
                "╚══════════════════════════╝\n\n"

                f"📁 `{output_path.name}`\n"

                f"📦 {format_bytes(file_size)}\n\n"

                "✅ Download\n"
                "✅ Telegram Upload",

                parse_mode="Markdown"

            )

            # Upload successful → delete local file

            try:

                output_path.unlink()

            except Exception:

                pass

        except Exception as upload_error:

            # ---------------------------------------------
            # IMPORTANT:
            # Do NOT delete downloaded file
            # ---------------------------------------------

            await status_message.edit_text(

                "╔══════════════════════════╗\n"
                "║   ⚠️ *DOWNLOAD OK*       ║\n"
                "╚══════════════════════════╝\n\n"

                f"📁 *File:* `{output_path.name}`\n"

                f"📦 *Size:* "
                f"{format_bytes(file_size)}\n\n"

                "✅ Download ပြီးပါပြီ။\n"

                "❌ Telegram Upload မအောင်မြင်ပါ။\n\n"

                f"Error:\n"
                f"`{str(upload_error)[:2500]}`\n\n"

                "💾 Downloaded file ကို server မှာ "
                "မဖျက်ထားပါ။",

                parse_mode="Markdown"

            )

    # =====================================================
    # MAIN ERROR
    # =====================================================

    except Exception as error:

        error_text = str(
            error
        )

        try:

            await status_message.edit_text(

                "╔══════════════════════════╗\n"
                "║      ❌ *FAILED*         ║\n"
                "╚══════════════════════════╝\n\n"

                f"⚠️ *Error:*\n\n"
                f"`{error_text[:3500]}`",

                parse_mode="Markdown"

            )

        except Exception:

            pass

        # ---------------------------------------------
        # Only delete incomplete file
        # ---------------------------------------------

        if output_path:

            try:

                if output_path.exists():

                    # If download failed,
                    # remove incomplete file

                    output_path.unlink()

            except Exception:

                pass


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context: ContextTypes.DEFAULT_TYPE
):

    print(
        "BOT ERROR:",
        repr(
            context.error
        )
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:

        raise RuntimeError(

            "BOT_TOKEN မတွေ့ပါ။\n"
            "GitHub Repository → Settings → "
            "Secrets and variables → Actions → "
            "BOT_TOKEN ထည့်ထားကြောင်း စစ်ပါ။"

        )

    # -----------------------------------------------------
    # HTTPX / Telegram connection
    # -----------------------------------------------------

    request = HTTPXRequest(

        connection_pool_size=2,

        connect_timeout=120,

        read_timeout=600,

        write_timeout=600,

        pool_timeout=120,

        http_version="1.1",

    )

    application = (

        Application.builder()

        .token(TOKEN)

        .request(request)

        .build()

    )

    # -----------------------------------------------------
    # COMMANDS
    # -----------------------------------------------------

    application.add_handler(

        CommandHandler(
            "start",
            start
        )

    )

    application.add_handler(

        CommandHandler(
            "help",
            help_command
        )

    )

    # -----------------------------------------------------
    # URL HANDLER
    # -----------------------------------------------------

    application.add_handler(

        MessageHandler(

            filters.TEXT
            &
            ~filters.COMMAND,

            handle_url

        )

    )

    # -----------------------------------------------------
    # ERROR HANDLER
    # -----------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    print(
        "======================================"
    )

    print(
        "🎬 Movie Download Bot"
    )

    print(
        "🤖 Bot is running..."
    )

    print(
        "📥 Direct URL + MediaFire supported"
    )

    print(
        "======================================"
    )

    application.run_polling(

        drop_pending_updates=True

    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    main()

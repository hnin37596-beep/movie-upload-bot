import os
import time
import asyncio
from pathlib import Path

import aiohttp
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
DOWNLOAD_DIR.mkdir(exist_ok=True)

STATUS_INTERVAL = 2


# =========================================================
# HELPERS
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
    if seconds is None or seconds < 0:
        return "--:--"

    seconds = int(seconds)

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    return f"{minutes:02d}:{secs:02d}"


def progress_bar(percent, length=12):
    percent = max(0, min(100, percent))

    filled = int(length * percent / 100)

    return "▰" * filled + "▱" * (length - filled)


def get_filename(url):
    try:
        name = url.split("?")[0].rstrip("/").split("/")[-1]

        if not name:
            name = "video.mp4"

        return name

    except Exception:
        return "video.mp4"


# =========================================================
# DOWNLOAD
# =========================================================

async def download_file(url, output_path, status_message):

    start_time = time.time()
    downloaded = 0
    last_update = 0

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=60,
        sock_connect=60,
        sock_read=300,
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
        ),
        "Accept": "*/*",
    }

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers
    ) as session:

        async with session.get(
            url,
            allow_redirects=True
        ) as response:

            response.raise_for_status()

            total = response.content_length

            with open(output_path, "wb") as file:

                async for chunk in response.content.iter_chunked(
                    1024 * 1024
                ):

                    file.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()

                    if now - last_update >= STATUS_INTERVAL:

                        elapsed = now - start_time

                        speed = (
                            downloaded / elapsed
                            if elapsed > 0
                            else 0
                        )

                        if total:
                            percent = downloaded / total * 100

                            eta = (
                                (total - downloaded) / speed
                                if speed > 0
                                else None
                            )

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
                                f"┣ Speed: {format_bytes(speed)}/s\n"
                                f"┣ ETA: {format_time(eta)}\n"
                                f"┗ Elapsed: {format_time(elapsed)}"
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
                                f"┣ Speed: {format_bytes(speed)}/s\n"
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

    return downloaded


# =========================================================
# UPLOAD
# =========================================================

async def upload_video(
    update,
    status_message,
    file_path
):

    start_time = time.time()

    file_size = file_path.stat().st_size

    await status_message.edit_text(
        "╔══════════════════════════╗\n"
        "║   📡 *UPLOAD STATUS*     ║\n"
        "╚══════════════════════════╝\n\n"

        "⏫ *UPLOADING*\n\n"

        f"📁 *File:* `{file_path.name}`\n"
        f"┣ Size: {format_bytes(file_size)}\n"
        "┣ Status: Preparing Telegram upload...\n"
        "┗ Please wait...",
        parse_mode="Markdown"
    )

    # -----------------------------------------------------
    # Telegram upload
    # -----------------------------------------------------

    last_error = None

    for attempt in range(1, 4):

        try:

            await update.message.reply_video(
                video=file_path.open("rb"),
                caption=(
                    f"🎬 `{file_path.name}`\n\n"
                    f"📦 Size: {format_bytes(file_size)}"
                ),
                supports_streaming=True,
                read_timeout=600,
                write_timeout=600,
                connect_timeout=120,
                pool_timeout=120,
            )

            elapsed = time.time() - start_time

            try:
                await status_message.edit_text(
                    "╔══════════════════════════╗\n"
                    "║   ✅ *UPLOAD COMPLETE*   ║\n"
                    "╚══════════════════════════╝\n\n"

                    f"🎬 `{file_path.name}`\n"
                    f"📦 Size: {format_bytes(file_size)}\n"
                    f"⏱ Time: {format_time(elapsed)}\n\n"
                    "✅ Video uploaded successfully.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

            return True

        except Exception as error:

            last_error = error

            try:
                await status_message.edit_text(
                    "╔══════════════════════════╗\n"
                    "║   🔄 *UPLOAD RETRY*      ║\n"
                    "╚══════════════════════════╝\n\n"

                    f"📁 `{file_path.name}`\n\n"
                    f"⚠️ Attempt {attempt}/3\n"
                    f"❗ `{str(error)[:500]}`\n\n"
                    "🔄 Retrying...",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

            await asyncio.sleep(5)

    raise last_error


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🎬 *Movie Download Bot*\n\n"

        "အသုံးပြုနည်း\n\n"

        "1️⃣ Direct video URL ကို ဒီ chat ထဲ ပို့ပါ။\n\n"

        "ဥပမာ:\n"
        "`https://example.com/video.mp4`\n\n"

        "2️⃣ Bot က download လုပ်ပါမယ်။\n\n"

        "3️⃣ Download ပြီးရင် Telegram ထဲကို video ပြန်ပို့ပါမယ်။\n\n"

        "⚠️ ကိုယ်ပိုင် သို့မဟုတ် ဖြန့်ဝေခွင့်ရှိတဲ့ content တွေအတွက်သာ အသုံးပြုပါ။",
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

        "🎬 Direct video URL တစ်ခု ပို့ပါ။\n\n"

        "Bot က:\n"
        "1️⃣ Download လုပ်မယ်\n"
        "2️⃣ Download progress ပြမယ်\n"
        "3️⃣ Telegram ထဲ Upload လုပ်မယ်\n"
        "4️⃣ ပြီးရင် local file ဖျက်မယ်",
        parse_mode="Markdown"
    )


# =========================================================
# URL HANDLER
# =========================================================

async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()

    if not (
        url.startswith("http://")
        or url.startswith("https://")
    ):
        return

    filename = get_filename(url)

    # Remove unsafe filename characters
    filename = "".join(
        c for c in filename
        if c.isalnum()
        or c in "._-()[] "
    ).strip()

    if not filename:
        filename = "video.mp4"

    output_path = DOWNLOAD_DIR / filename

    status_message = await update.message.reply_text(
        "⏳ Preparing download..."
    )

    try:

        # -------------------------------------------------
        # DOWNLOAD
        # -------------------------------------------------

        await download_file(
            url,
            output_path,
            status_message
        )

        # -------------------------------------------------
        # DOWNLOAD COMPLETE
        # -------------------------------------------------

        size = output_path.stat().st_size

        await status_message.edit_text(
            "╔══════════════════════════╗\n"
            "║   ✅ *DOWNLOAD COMPLETE* ║\n"
            "╚══════════════════════════╝\n\n"

            f"📁 *File:* `{output_path.name}`\n"
            f"📦 Size: {format_bytes(size)}\n\n"

            "📡 Preparing Telegram upload...",
            parse_mode="Markdown"
        )

        # -------------------------------------------------
        # UPLOAD
        # -------------------------------------------------

        await upload_video(
            update,
            status_message,
            output_path
        )

    except Exception as error:

        error_text = str(error)

        try:
            await status_message.edit_text(
                "╔══════════════════════════╗\n"
                "║      ❌ *FAILED*         ║\n"
                "╚══════════════════════════╝\n\n"

                "⚠️ *Error:*\n\n"
                f"`{error_text[:3500]}`",
                parse_mode="Markdown"
            )

        except Exception:
            pass

    finally:

        # -------------------------------------------------
        # DELETE LOCAL FILE
        # -------------------------------------------------

        try:

            if output_path.exists():
                output_path.unlink()

        except Exception:
            pass


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    print(
        "BOT ERROR:",
        repr(context.error)
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is missing. "
            "Please add BOT_TOKEN in GitHub Actions Secrets."
        )

    # -----------------------------------------------------
    # FIX SSL / HTTPX CONNECTION SETTINGS
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

    # Commands
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    # URL messages
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url
        )
    )

    application.add_error_handler(
        error_handler
    )

    print("================================")
    print("🎬 Movie Download Bot")
    print("🤖 Bot is running...")
    print("================================")

    application.run_polling(
        drop_pending_updates=True
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()

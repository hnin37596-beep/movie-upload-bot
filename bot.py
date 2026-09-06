import os
import asyncio
import time
import math
from pathlib import Path

import aiohttp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


def format_bytes(num):
    if num is None:
        return "Unknown"

    num = float(num)

    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024

    return f"{num:.1f} PB"


def format_time(seconds):
    if not seconds or seconds < 0 or math.isinf(seconds):
        return "--:--"

    seconds = int(seconds)

    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)

    if h:
        return f"{h:d}:{m:02d}:{s:02d}"

    return f"{m:d}:{s:02d}"


def progress_bar(percent, length=12):
    percent = max(0, min(100, percent))

    filled = int(length * percent / 100)
    empty = length - filled

    return "▰" * filled + "▱" * empty


def safe_filename(url):
    name = url.split("?")[0].rstrip("/").split("/")[-1]

    if not name:
        name = "video"

    # Basic cleanup
    name = "".join(
        c for c in name
        if c.isalnum() or c in "._- "
    )

    if not name:
        name = "video"

    return name[:150]


async def download_file(url, output_path, status_callback):
    start_time = time.time()
    downloaded = 0
    last_update = 0

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=60,
        sock_read=120
    )

    async with aiohttp.ClientSession(timeout=timeout) as session:

        async with session.get(
            url,
            allow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
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

                    # Update roughly every 2 seconds
                    if now - last_update >= 2:

                        elapsed = now - start_time

                        speed = (
                            downloaded / elapsed
                            if elapsed > 0
                            else 0
                        )

                        if total:
                            percent = (
                                downloaded / total
                            ) * 100

                            remaining = total - downloaded

                            eta = (
                                remaining / speed
                                if speed > 0
                                else 0
                            )
                        else:
                            percent = 0
                            eta = 0

                        await status_callback(
                            downloaded,
                            total,
                            percent,
                            speed,
                            eta,
                            elapsed
                        )

                        last_update = now


async def update_download_status(
    message,
    downloaded,
    total,
    percent,
    speed,
    eta,
    elapsed
):
    if total:
        text = f"""
╔══════════════════════════╗
║   📡 DOWNLOAD STATUS     ║
╚══════════════════════════╝

⬇️ *DOWNLOADING*

📁 *File:* {message.chat.id}

{progress_bar(percent)} *{percent:.1f}%*

┣ {format_bytes(downloaded)} / {format_bytes(total)}
┣ Speed: {format_bytes(speed)}/s
┣ ETA: {format_time(eta)}
┗ Elapsed: {format_time(elapsed)}
"""
    else:
        text = f"""
╔══════════════════════════╗
║   📡 DOWNLOAD STATUS     ║
╚══════════════════════════╝

⬇️ *DOWNLOADING*

📁 *File:* {message.chat.id}

┣ Downloaded: {format_bytes(downloaded)}
┣ Speed: {format_bytes(speed)}/s
┗ Elapsed: {format_time(elapsed)}
"""

    try:
        await message.edit_text(
            text,
            parse_mode="Markdown"
        )
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = """
🎬 *Movie Download Bot*

အသုံးပြုနည်း

1️⃣ Direct video URL ကို ဒီ chat ထဲ ပို့ပါ။

ဥပမာ:

`https://example.com/video.mp4`

2️⃣ Bot က download လုပ်ပါမယ်။

3️⃣ Download ပြီးရင် Telegram ထဲကို video/file ပြန်ပို့ပါမယ်။

⚠️ ကိုယ်ပိုင် သို့မဟုတ် ဖြန့်ဝေခွင့်ရှိတဲ့ content တွေအတွက်သာ အသုံးပြုပါ။
"""

    await update.message.reply_text(
        text,
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = """
📖 *Commands*

/start - Bot စတင်ရန်
/help - Help

ပြီးရင် video download URL ကို
ဒီ chat ထဲ တိုက်ရိုက်ပို့နိုင်ပါတယ်။
"""

    await update.message.reply_text(
        text,
        parse_mode="Markdown"
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):

    url = update.message.text.strip()

    if not (
        url.startswith("http://")
        or url.startswith("https://")
    ):
        return

    status = await update.message.reply_text(
        """
╔══════════════════════════╗
║   📡 DOWNLOAD STATUS     ║
╚══════════════════════════╝

⏳ *Preparing...*
""",
        parse_mode="Markdown"
    )

    filename = safe_filename(url)

    if "." not in filename:
        filename += ".mp4"

    output_path = DOWNLOAD_DIR / filename

    try:

        async def callback(
            downloaded,
            total,
            percent,
            speed,
            eta,
            elapsed
        ):
            await update_download_status(
                status,
                downloaded,
                total,
                percent,
                speed,
                eta,
                elapsed
            )

        await download_file(
            url,
            output_path,
            callback
        )

        await status.edit_text(
            """
╔══════════════════════════╗
║   📡 DOWNLOAD STATUS     ║
╚══════════════════════════╝

✅ *DOWNLOAD COMPLETE*

⬆️ Preparing Telegram upload...
""",
            parse_mode="Markdown"
        )

        file_size = output_path.stat().st_size

        await update.message.chat.send_action(
            action=ChatAction.UPLOAD_VIDEO
        )

        start_upload = time.time()

        with open(output_path, "rb") as video:

            await update.message.reply_video(
                video=video,
                caption=(
                    f"🎬 {filename}\n\n"
                    f"📦 Size: {format_bytes(file_size)}\n"
                    f"⏱ Download time: "
                    f"{format_time(time.time() - start_upload)}"
                ),
                supports_streaming=True
            )

        await status.edit_text(
            f"""
╔══════════════════════════╗
║   📡 UPLOAD STATUS       ║
╚══════════════════════════╝

✅ *UPLOAD COMPLETE*

📁 File: `{filename}`
📦 Size: {format_bytes(file_size)}

🎬 Video has been sent successfully.
""",
            parse_mode="Markdown"
        )

    except Exception as error:

        await status.edit_text(
            f"""
❌ *FAILED*

Error:

`{str(error)[:1000]}`
""",
            parse_mode="Markdown"
        )

    finally:

        # Delete local file after upload
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass


def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("help", help_command)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url
        )
    )

    print("Bot is running...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()

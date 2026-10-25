import os
import re
import glob
import uuid
import asyncio
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import yt_dlp
import imageio_ffmpeg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
MAX_SIZE = 50 * 1024 * 1024  # Telegram bot upload limit: 50 MB
IG_URL = re.compile(r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/[\w\-]+[^\s]*")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()  # bundled ffmpeg, needed for MP3


# ---- tiny web server so Render's free Web Service sees an open port ----
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):  # UptimeRobot checks with HEAD requests
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


# ---- downloader ----
def download_media(url: str, out_dir: str, kind: str):
    """Returns (file_path, caption_text)."""
    opts = {
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "max_filesize": MAX_SIZE,
        "ffmpeg_location": FFMPEG,
    }
    if kind == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }]
    else:
        opts["format"] = "best[ext=mp4]/best"

    # Optional: Instagram often needs login cookies. Put cookies.txt next to bot.py
    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = "cookies.txt"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = glob.glob(os.path.join(out_dir, "*"))
    if not files:
        raise RuntimeError("Nothing downloaded")
    caption = (info.get("description") or info.get("title") or "").strip()
    if len(caption) > 1000:  # Telegram caption limit is 1024 characters
        caption = caption[:997] + "..."
    return files[0], caption


# ---- handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a public Instagram Reel/Post link, then choose Video or Audio.\n"
        "Only download content you own or have permission to use."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = IG_URL.search(update.message.text or "")
    if not match:
        await update.message.reply_text("Please send a valid Instagram video link.")
        return
    key = uuid.uuid4().hex[:8]
    context.bot_data[key] = match.group(0)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 Video", callback_data=f"video|{key}"),
        InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"audio|{key}"),
    ]])
    await update.message.reply_text("What do you want to download?", reply_markup=keyboard)


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind, key = query.data.split("|", 1)
    url = context.bot_data.pop(key, None)
    if not url:
        await query.edit_message_text("This link expired. Please send it again.")
        return

    await query.edit_message_text("Downloading... ⏳")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, caption = await asyncio.to_thread(download_media, url, tmp, kind)
            if os.path.getsize(path) > MAX_SIZE:
                await query.edit_message_text("File is larger than Telegram's 50 MB bot limit.")
                return
            chat_id = query.message.chat_id
            with open(path, "rb") as f:
                if kind == "audio":
                    await context.bot.send_audio(chat_id, f, caption=caption)
                else:
                    await context.bot.send_video(
                        chat_id, f, caption=caption, supports_streaming=True
                    )
        await query.message.delete()
    except Exception as e:
        await query.edit_message_text(
            "Failed to download (private post, login required, or link invalid).\n"
            f"{str(e)[:150]}"
        )


def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_choice, pattern=r"^(video|audio)\|"))
    app.run_polling()


if __name__ == "__main__":
    main()

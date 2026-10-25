import asyncio
import html
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("captionbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATA_FILE = os.environ.get("DATA_FILE", "bot_data.pickle")
CAPTION_LIMIT = 1024
MAX_STORED = 500  # how many media panels to remember

HELP = (
    "Send or forward any photo, video, document, audio, voice note or GIF.\n"
    "I'll return it with an edit panel underneath:\n\n"
    "Drop @user - strips @usernames\n"
    "Drop links - strips all links\n"
    "Drop #tags - strips hashtags\n"
    "Saved text - inserts the text you saved\n"
    "Clear text - takes out the text you inserted\n"
    "Top / Bottom - where the inserted text goes\n"
    "Write text - type a fresh text to insert\n"
    "Finish - closes the panel\n\n"
    "Commands:\n"
    "/settext <text> - save your text\n"
    "/status - view your saved text\n"
    "/cancel - stop 'Write text'\n"
    "/reset - wipe your saved settings"
)

URL_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/)\S+", re.I)
MENTION_RE = re.compile(r"(?<![\w])@\w{3,}")
TAG_RE = re.compile(r"(?<![\w&])#\w+", re.U)
ANCHOR_RE = re.compile(r'<a\s+href="([^"]*)"[^>]*>.*?</a>', re.S | re.I)
EMPTY_TAG_RE = re.compile(r"<(b|i|u|s|code|pre|strong|em)>\s*</\1>", re.I)


# ---------- caption helpers ----------

def outside_tags(pattern: re.Pattern, s: str) -> str:
    """Apply a regex removal only to text that is not inside HTML tags."""
    parts = re.split(r"(<[^>]*>)", s)
    for i in range(0, len(parts), 2):
        parts[i] = pattern.sub("", parts[i])
    return "".join(parts)


def drop_anchors(s: str, users: bool) -> str:
    """Remove <a> tags. users=True removes tg://user mentions, else normal links."""

    def f(m):
        is_user = m.group(1).startswith("tg://user")
        return "" if is_user == users else m.group(0)

    return ANCHOR_RE.sub(f, s)


def tidy(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = EMPTY_TAG_RE.sub("", s)
    s = re.sub(r"(?<=\S)[ \t]{2,}", " ", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def render(st: dict) -> str:
    cap = st["orig"]
    if st["rm_link"]:
        cap = drop_anchors(cap, users=False)
        cap = outside_tags(URL_RE, cap)
    if st["rm_id"]:
        cap = drop_anchors(cap, users=True)
        cap = outside_tags(MENTION_RE, cap)
    if st["rm_tag"]:
        cap = outside_tags(TAG_RE, cap)
    cap = tidy(cap)
    text = st["text"]
    if text:
        cap = f"{text}\n\n{cap}" if st["pos"] == "top" else f"{cap}\n\n{text}"
    return tidy(cap)


def too_long(cap_html: str) -> bool:
    plain = html.unescape(re.sub(r"<[^>]*>", "", cap_html))
    return len(plain) > CAPTION_LIMIT


def keyboard(st: dict) -> InlineKeyboardMarkup:
    def b(label, data, on=False):
        return InlineKeyboardButton(("✅ " if on else "") + label, callback_data=data)

    def pos(label, data, on):
        return InlineKeyboardButton(
            ("✅ " if on else "") + label, callback_data=data
        )

    return InlineKeyboardMarkup(
        [
            [
                b("Drop @user", "rm_id", st["rm_id"]),
                b("Drop links", "rm_link", st["rm_link"]),
                b("Drop #tags", "rm_tag", st["rm_tag"]),
            ],
            [b("Saved text", "default"), b("Clear text", "del")],
            [
                pos("⬆ Top", "top", st["pos"] == "top"),
                pos("⬇ Bottom", "bottom", st["pos"] == "bottom"),
            ],
            [InlineKeyboardButton("✏️ Write text", callback_data="add")],
            [InlineKeyboardButton("✔️ Finish", callback_data="done")],
        ]
    )


# ---------- state helpers ----------

def store(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.bot_data.setdefault("msgs", {})


def key_of(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def remember(context, key: str, st: dict):
    msgs = store(context)
    msgs[key] = st
    while len(msgs) > MAX_STORED:
        msgs.pop(next(iter(msgs)))


def media_of(m):
    """Return (kind, file_id) for the media in a message."""
    if m.animation:
        return "animation", m.animation.file_id
    if m.photo:
        return "photo", m.photo[-1].file_id
    if m.video:
        return "video", m.video.file_id
    if m.audio:
        return "audio", m.audio.file_id
    if m.voice:
        return "voice", m.voice.file_id
    if m.document:
        return "document", m.document.file_id
    return None, None


async def send_panel(bot, chat_id: int, st: dict):
    """Send the file as a NEW message with the current caption and buttons."""
    cap = render(st) or None
    kw = {
        "caption": cap,
        "parse_mode": ParseMode.HTML if cap else None,
        "reply_markup": keyboard(st),
    }
    kind, fid = st["kind"], st["file_id"]
    if kind == "animation":
        return await bot.send_animation(chat_id, fid, **kw)
    if kind == "photo":
        return await bot.send_photo(chat_id, fid, **kw)
    if kind == "video":
        return await bot.send_video(chat_id, fid, supports_streaming=True, **kw)
    if kind == "audio":
        return await bot.send_audio(chat_id, fid, **kw)
    if kind == "voice":
        return await bot.send_voice(chat_id, fid, **kw)
    return await bot.send_document(chat_id, fid, **kw)


async def replace_panel(context, chat_id: int, old_mid: int, st: dict):
    """Show the updated panel as a fresh message (so Telegram does not add an
    'edited' label) and remove the old one."""
    sent = await send_panel(context.bot, chat_id, st)
    old_key = key_of(chat_id, old_mid)
    new_key = key_of(chat_id, sent.message_id)

    store(context).pop(old_key, None)
    remember(context, new_key, st)

    pending = context.user_data.get("pending")
    if pending and pending.get("key") == old_key:
        pending["key"] = new_key

    try:
        await context.bot.delete_message(chat_id, old_mid)
    except BadRequest:
        pass
    return sent


# ---------- commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def settext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text_html or "").partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /settext Your default text")
        return
    if too_long(text):
        await update.message.reply_text("That text is too long (1024 characters max).")
        return
    context.user_data["default_text"] = text
    await update.message.reply_text("Your text is saved.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = context.user_data.get("default_text")
    if text:
        await update.message.reply_text(
            "Your saved text:\n\n" + text, parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("Nothing saved yet. Use /settext <text>")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending", None)
    await update.message.reply_text("Cancelled.")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Your saved settings have been wiped.")


# ---------- media ----------

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.message
    kind, file_id = media_of(m)
    if kind is None:
        return
    st = {
        "orig": m.caption_html or "",
        "rm_id": False,
        "rm_link": False,
        "rm_tag": False,
        "text": "",
        "pos": "bottom",
        "kind": kind,
        "file_id": file_id,
    }
    try:
        sent = await send_panel(context.bot, m.chat_id, st)
        remember(context, key_of(m.chat_id, sent.message_id), st)
    except BadRequest as e:
        log.warning("BadRequest: %s", e)
        await m.reply_text(
            "Telegram rejected the caption (it may be longer than 1024 characters)."
        )
    except Exception:
        log.exception("Unexpected error")
        await m.reply_text("Something went wrong. Please try again.")


# ---------- buttons ----------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    msg = q.message
    action = q.data
    key = key_of(msg.chat_id, msg.message_id)
    st = store(context).get(key)

    if st is None:
        await q.answer("This panel is no longer active. Send the file again.", show_alert=True)
        return

    if "kind" not in st:
        await q.answer("This panel is from an older version. Send the file again.", show_alert=True)
        return

    if action == "done":
        store(context).pop(key, None)
        await q.answer("Finished ✅")
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        return

    if action == "add":
        await q.answer()
        prompt = await msg.reply_text(
            "Type the text you'd like to insert, or send /cancel to stop.",
            reply_to_message_id=msg.message_id,
        )
        context.user_data["pending"] = {"key": key, "prompt": prompt.message_id}
        return

    new = dict(st)
    default = context.user_data.get("default_text", "")

    if action in ("rm_id", "rm_link", "rm_tag"):
        new[action] = not st[action]
    elif action == "del":
        new["text"] = ""
    elif action == "default":
        if not default:
            await q.answer("You have no saved text yet. Use /settext <text>", show_alert=True)
            return
        new["text"] = default
    elif action in ("top", "bottom"):
        new["pos"] = action
        if not new["text"]:
            if default:
                new["text"] = default
            else:
                await q.answer("Tap 'Write text' or use /settext first.", show_alert=True)
                return
    else:
        await q.answer()
        return

    if new == st:
        await q.answer("Nothing to change.")
        return

    if too_long(render(new)):
        await q.answer("That would push the caption past 1024 characters.", show_alert=True)
        return

    try:
        await q.answer()
        await replace_panel(context, msg.chat_id, msg.message_id, new)
    except BadRequest as e:
        log.warning("Resend failed: %s", e)
        await msg.reply_text("Couldn't update the caption.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.message
    pending = context.user_data.get("pending")
    if not pending:
        await m.reply_text("Send me a file and I'll help you edit its caption. See /help.")
        return

    st = store(context).get(pending["key"])
    chat_id, msg_id = pending["key"].split(":")
    context.user_data.pop("pending", None)
    if st is None:
        await m.reply_text("That panel is no longer active. Send the file again.")
        return

    new = dict(st)
    new["text"] = m.text_html
    if too_long(render(new)):
        await m.reply_text("That would push the caption past 1024 characters. Try something shorter.")
        return

    try:
        await replace_panel(context, int(chat_id), int(msg_id), new)
    except BadRequest as e:
        log.warning("Resend failed: %s", e)
        await m.reply_text("Couldn't update the caption.")
        return

    # Tidy the chat: remove the prompt and the user's text message.
    for mid in (pending["prompt"], m.message_id):
        try:
            await context.bot.delete_message(m.chat_id, mid)
        except BadRequest:
            pass


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


def start_health_server():
    """Render Web Services must listen on $PORT, otherwise the deploy times out."""
    port = os.environ.get("PORT")
    if not port:
        return
    server = ThreadingHTTPServer(("0.0.0.0", int(port)), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", port)


def main():
    # Newer Python versions no longer create an event loop automatically.
    asyncio.set_event_loop(asyncio.new_event_loop())
    start_health_server()
    persistence = PicklePersistence(filepath=DATA_FILE)
    app = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("settext", settext_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CallbackQueryHandler(on_button))

    media = (
        filters.PHOTO
        | filters.VIDEO
        | filters.ANIMATION
        | filters.AUDIO
        | filters.VOICE
        | filters.Document.ALL
    )
    app.add_handler(MessageHandler(media & ~filters.COMMAND, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Caption editor bot started (polling)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

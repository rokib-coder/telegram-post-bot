# -*- coding: utf-8 -*-
"""
Telegram Video Post Bot — Render / VPS ready
Features: multi-step post, channels, schedule (auto), imgbb, Firebase
"""

import os
import re
import time
import json
import logging
import asyncio
from io import BytesIO

import requests
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# ---------------- CONFIG (env) ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
FIREBASE = os.getenv("FIREBASE", "https://rokib-7df0d-default-rtdb.firebaseio.com").rstrip("/")
BOT_USERNAME = os.getenv("BOT_USERNAME", "SunnyCreekFarmBot")
ADMINS = [int(x) for x in os.getenv("ADMINS", "8256134392").split(",") if x.strip()]
GROUP_ID = os.getenv("GROUP_ID", "").strip()
IMGBB_KEY = os.getenv("IMGBB_KEY", "")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# Conversation states
(
    TITLE_IMAGE,
    LINK,
    DURATION,
    ADS,
    CATEGORY,
    PREVIEW,
    SCHED_TIME,
) = range(7)

# ---------------- Helpers ----------------
def esc(x):
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_admin(uid):
    return int(uid) in ADMINS


def main_menu_kb():
    return ReplyKeyboardMarkup(
        [
            ["🎬 নতুন পোস্ট", "📋 ভিডিও লিস্ট"],
            ["⏰ শিডিউল লিস্ট", "📢 চ্যানেল"],
            ["📊 স্ট্যাটাস"],
        ],
        resize_keyboard=True,
    )


def cancel_kb():
    return ReplyKeyboardMarkup([["❌ বাতিল"]], resize_keyboard=True)


def preview_kb():
    return ReplyKeyboardMarkup(
        [
            ["✅ হ্যাঁ, পোস্ট করুন"],
            ["⏰ শিডিউল করুন"],
            ["❌ বাতিল"],
        ],
        resize_keyboard=True,
    )


def fb_get(path):
    try:
        r = requests.get(f"{FIREBASE}/{path}.json", timeout=30)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning("fb_get %s: %s", path, e)
    return None


def fb_put(path, data):
    try:
        r = requests.put(f"{FIREBASE}/{path}.json", json=data, timeout=60)
        return r.status_code in (200, 201)
    except Exception as e:
        log.warning("fb_put %s: %s", path, e)
        return False


def fb_delete(path):
    try:
        requests.delete(f"{FIREBASE}/{path}.json", timeout=30)
    except Exception:
        pass


def load_channels(context):
    ch = context.user_data.get("channels")
    if ch is not None:
        return ch
    data = fb_get("bot_config/channels")
    if isinstance(data, list):
        context.user_data["channels"] = data
        return data
    if isinstance(data, dict):
        arr = list(data.values())
        context.user_data["channels"] = arr
        return arr
    context.user_data["channels"] = []
    return []


def save_channels(context, channels):
    context.user_data["channels"] = channels
    fb_put("bot_config/channels", channels)


def imgbb_upload_url(image_url):
    if not IMGBB_KEY:
        return image_url if str(image_url).startswith("http") else ""
    try:
        r = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": IMGBB_KEY, "image": image_url},
            timeout=60,
        )
        d = r.json()
        if d.get("success"):
            return d["data"].get("url") or d["data"].get("display_url") or ""
    except Exception as e:
        log.warning("imgbb url: %s", e)
    return ""


def imgbb_upload_bytes(data: bytes):
    if not IMGBB_KEY:
        return ""
    try:
        import base64

        b64 = base64.b64encode(data).decode()
        r = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": IMGBB_KEY, "image": b64},
            timeout=90,
        )
        d = r.json()
        if d.get("success"):
            return d["data"].get("url") or d["data"].get("display_url") or ""
    except Exception as e:
        log.warning("imgbb bytes: %s", e)
    return ""


def tg_file_url(file_id):
    try:
        r = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=30)
        d = r.json()
        if d.get("ok"):
            fp = d["result"]["file_path"]
            return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}"
    except Exception as e:
        log.warning("tg_file: %s", e)
    return ""


def parse_duration(text):
    text = text.strip().lower()
    # v 5 → video count
    m = re.match(r"^v\s*(\d+)$", text)
    if m:
        return 0, int(m.group(1))
    parts = re.findall(r"\d+", text)
    if not parts:
        return None, None
    nums = [int(x) for x in parts]
    if len(nums) == 1:
        return nums[0] * 60, 0  # minutes
    if len(nums) == 2:
        return nums[0] * 60 + nums[1], 0
    if len(nums) >= 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2], 0
    return None, None


def parse_delay(text):
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    total = 0
    for num, unit in re.findall(r"(\d+)([hms])", text):
        n = int(num)
        if unit == "h":
            total += n * 3600
        elif unit == "m":
            total += n * 60
        elif unit == "s":
            total += n
    if total > 0:
        return total
    # plain number → minutes
    if text.isdigit():
        return int(text) * 60
    return None


def fmt_dc(sec, vcount):
    if vcount:
        return f"{vcount} videos"
    sec = int(sec or 0)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h}:{str(m).zfill(2)}:{str(s).zfill(2)}"
    return f"{m}:{str(s).zfill(2)}"


def share_link(video_key):
    return f"https://t.me/{BOT_USERNAME}/play?startapp={video_key}"


# ---------------- Publish ----------------
def publish_video(item):
    """Save to Firebase + post to channels. Returns (video_key, report_text)"""
    name = str(item.get("name") or "Untitled")
    image = str(item.get("image_url") or "").strip()
    link = str(item.get("download_link") or "")
    category = str(item.get("category") or "all")
    duration = int(item.get("duration") or 0)
    vcount = int(item.get("video_count") or 0)
    ads = int(item.get("ad_watches_required") or 1)
    channels = item.get("channels") or []

    if not image.startswith("http"):
        return None, "❌ থাম্বনেইল URL নেই"

    video_key = "v" + str(int(time.time() * 1000))
    video_data = {
        "name": name,
        "image_url": image,
        "download_link": link,
        "category": category,
        "category_name": category,
        "duration": duration,
        "duration_sec": duration,
        "video_count": vcount,
        "ad_watches_required": ads,
        "view_count": 0,
        "created_at": int(time.time() * 1000),
    }
    if not fb_put(f"videos/{video_key}", video_data):
        return None, "❌ Firebase সেভ ব্যর্থ"

    sl = share_link(video_key)
    caption = f"🎬 <b>{esc(name)}</b>"
    keyboard = {
        "inline_keyboard": [[{"text": "▶️ ভিডিও দেখুন", "url": sl}]]
    }

    ok = 0
    lines = []
    for ch in channels:
        ch_id = str(ch.get("id") or "")
        title = esc(ch.get("title") or ch_id)
        if not ch_id:
            continue
        try:
            r = requests.post(
                f"{API}/sendPhoto",
                json={
                    "chat_id": ch_id,
                    "photo": image,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
                timeout=45,
            )
            d = r.json()
            if d.get("ok"):
                ok += 1
                lines.append(f"✅ {title}")
            else:
                lines.append(f"❌ {title} — {esc(d.get('description', 'fail'))}")
        except Exception as e:
            lines.append(f"❌ {title} — {esc(e)}")

    # optional group
    if GROUP_ID and item.get("post_to_group"):
        g_cap = (
            f"👤 <b>রকিব</b> এর পোস্ট\n━━━━━━━━━━━━━━\n\n"
            f"🎬 <b>টাইটেল:</b>\n<code>{esc(name)}</code>\n\n"
            f"🔗 {esc(link)}"
        )
        try:
            requests.post(
                f"{API}/sendPhoto",
                json={
                    "chat_id": GROUP_ID,
                    "photo": image,
                    "caption": g_cap,
                    "parse_mode": "HTML",
                },
                timeout=30,
            )
        except Exception:
            pass

    report = (
        f"📌 <b>{esc(name)}</b>\n"
        f"⏱ {fmt_dc(duration, vcount)} • 📺 {ads} Ads • 🏷 {esc(category)}\n"
        f"🔑 <code>{video_key}</code>\n"
        f"🚀 <code>{sl}</code>\n\n"
        f"📢 চ্যানেল পোস্ট ({ok}/{len(channels)}):\n" + "\n".join(lines)
    )
    return video_key, report


# ---------------- Schedule worker ----------------
async def schedule_worker(app: Application):
    await asyncio.sleep(5)
    while True:
        try:
            data = fb_get("queue")
            if isinstance(data, dict):
                now = int(time.time() * 1000)
                for key, item in list(data.items()):
                    if not isinstance(item, dict):
                        continue
                    st = str(item.get("status") or "")
                    run_at = int(item.get("run_at") or 0)
                    if st == "scheduled" and run_at <= now:
                        fb_put(f"queue/{key}/status", "posting")
                        vk, rep = await asyncio.to_thread(publish_video, item)
                        if vk:
                            fb_delete(f"queue/{key}")
                            for admin in ADMINS:
                                try:
                                    await app.bot.send_message(
                                        admin,
                                        "⏰ <b>শিডিউল পোস্ট সম্পন্ন!</b>\n\n" + rep,
                                        parse_mode="HTML",
                                    )
                                except Exception:
                                    pass
                        else:
                            fb_put(f"queue/{key}/status", "failed")
                            for admin in ADMINS:
                                try:
                                    await app.bot.send_message(
                                        admin,
                                        f"❌ শিডিউল ব্যর্থ: {esc(item.get('name'))}\n{esc(rep)}",
                                        parse_mode="HTML",
                                    )
                                except Exception:
                                    pass
        except Exception as e:
            log.exception("schedule_worker: %s", e)
        await asyncio.sleep(30)  # every 30 sec


# ---------------- Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("🚫 শুধু অ্যাডমিনদের জন্য।")
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 <b>Admin Panel</b>\nনিচের মেনু থেকে বেছে নিন।",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ বাতিল।", reply_markup=main_menu_kb())
    return ConversationHandler.END


async def new_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["channels"] = load_channels(context)
    await update.message.reply_text(
        "📝🖼 <b>টাইটেল + থাম্বনেইল</b>\n\n"
        "ছবি পাঠান + ক্যাপশনে টাইটেল লিখুন\n"
        "অথবা শুধু টাইটেল লিখুন, পরে ছবি URL দিন।",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )
    return TITLE_IMAGE


async def title_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    title = (msg.caption or msg.text or "").strip()
    image_url = ""

    if msg.photo:
        if len(title) < 2:
            await msg.reply_text("❌ ছবির সাথে ক্যাপশনে টাইটেল লিখুন।")
            return TITLE_IMAGE
        await msg.reply_text("⏳ থাম্বনেইল আপলোড হচ্ছে...")
        file = await msg.photo[-1].get_file()
        bio = BytesIO()
        await file.download_to_memory(bio)
        image_url = await asyncio.to_thread(imgbb_upload_bytes, bio.getvalue())
        if not image_url:
            # fallback telegram url → imgbb
            tg_url = tg_file_url(msg.photo[-1].file_id)
            if tg_url:
                image_url = await asyncio.to_thread(imgbb_upload_url, tg_url)
        if not image_url:
            await msg.reply_text("❌ আপলোড ব্যর্থ। আবার চেষ্টা করুন বা ইমেজ URL দিন।")
            return TITLE_IMAGE
        context.user_data["name"] = title
        context.user_data["image_url"] = image_url
    else:
        # text only title OR image URL
        text = (msg.text or "").strip()
        if text.startswith("http") and context.user_data.get("name"):
            context.user_data["image_url"] = text
        elif len(text) >= 2:
            context.user_data["name"] = text
            await msg.reply_text(
                f"✅ টাইটেল: <b>{esc(text)}</b>\n\nএখন থাম্বনেইল ছবি বা ইমেজ URL দিন।",
                parse_mode="HTML",
            )
            return TITLE_IMAGE
        else:
            await msg.reply_text("❌ টাইটেল খুব ছোট অথবা ছবি + ক্যাপশন দিন।")
            return TITLE_IMAGE

    if not context.user_data.get("image_url"):
        await msg.reply_text("থাম্বনেইল ছবি বা URL দিন।")
        return TITLE_IMAGE

    await msg.reply_text(
        f"✅ সেভ!\n📌 {esc(context.user_data['name'])}\n🔗 <code>{esc(context.user_data['image_url'])}</code>\n\n"
        "🔗 এখন <b>ডাউনলোড লিংক</b> দিন:",
        parse_mode="HTML",
    )
    return LINK


async def link_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.startswith("http"):
        await update.message.reply_text("❌ সঠিক লিংক দিন (http/https)।")
        return LINK
    context.user_data["download_link"] = text
    await update.message.reply_text(
        "⏱ সময় দিন: <code>2 30</code> (মিনিট সেকেন্ড) বা <code>v 5</code> (৫টি ভিডিও)",
        parse_mode="HTML",
    )
    return DURATION


async def duration_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dur, vc = parse_duration(update.message.text or "")
    if dur is None:
        await update.message.reply_text("❌ ফরম্যাট: 2 30 অথবা v 5")
        return DURATION
    context.user_data["duration"] = dur
    context.user_data["video_count"] = vc or 0
    await update.message.reply_text("📺 Ads সংখ্যা লিখুন (যেমন 1):")
    return ADS


async def ads_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ads = int(re.findall(r"\d+", update.message.text or "1")[0])
    except Exception:
        ads = 1
    context.user_data["ads"] = max(1, ads)
    await update.message.reply_text("🏷 ক্যাটাগরি লিখুন (বা all):")
    return CATEGORY


async def category_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = (update.message.text or "all").strip() or "all"
    context.user_data["category"] = cat
    ud = context.user_data
    channels = load_channels(context)
    await update.message.reply_text(
        "📋 <b>প্রিভিউ</b>\n━━━━━━━━━━━━━━\n"
        f"📌 {esc(ud.get('name'))}\n"
        f"⏱ {fmt_dc(ud.get('duration'), ud.get('video_count'))}\n"
        f"📺 {ud.get('ads')} Ads • 🏷 {esc(cat)}\n"
        f"📢 চ্যানেল: {len(channels)}টি\n\n"
        "✅ পোস্ট বা ⏰ শিডিউল বেছে নিন।",
        parse_mode="HTML",
        reply_markup=preview_kb(),
    )
    return PREVIEW


async def preview_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    ud = context.user_data
    channels = load_channels(context)

    item = {
        "name": ud.get("name"),
        "image_url": ud.get("image_url"),
        "download_link": ud.get("download_link"),
        "duration": ud.get("duration") or 0,
        "video_count": ud.get("video_count") or 0,
        "ad_watches_required": ud.get("ads") or 1,
        "category": ud.get("category") or "all",
        "channels": channels,
        "post_to_group": False,
    }

    if text.startswith("✅") or "পোস্ট করুন" in text:
        await update.message.reply_text("⏳ পোস্ট হচ্ছে...")
        vk, rep = await asyncio.to_thread(publish_video, item)
        if vk:
            await update.message.reply_text(
                "🎉 <b>পোস্ট সফল!</b>\n\n" + rep,
                parse_mode="HTML",
                reply_markup=main_menu_kb(),
            )
        else:
            await update.message.reply_text(
                "❌ ব্যর্থ: " + esc(rep),
                parse_mode="HTML",
                reply_markup=main_menu_kb(),
            )
        context.user_data.clear()
        return ConversationHandler.END

    if text.startswith("⏰") or "শিডিউল" in text:
        context.user_data["sched_item"] = item
        await update.message.reply_text(
            "⏰ কতক্ষণ পরে?\nউদাহরণ: <code>30s</code>, <code>10m</code>, <code>2h 30m</code>",
            parse_mode="HTML",
            reply_markup=cancel_kb(),
        )
        return SCHED_TIME

    await update.message.reply_text("✅ বা ⏰ বেছে নিন।", reply_markup=preview_kb())
    return PREVIEW


async def sched_time_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sec = parse_delay(update.message.text or "")
    if not sec or sec > 30 * 86400:
        await update.message.reply_text("❌ ভুল ফরম্যাট। উদাহরণ: 1m, 30s, 2h")
        return SCHED_TIME

    item = context.user_data.get("sched_item") or {}
    run_at = int(time.time() * 1000) + sec * 1000
    item["status"] = "scheduled"
    item["run_at"] = run_at

    # push to queue
    try:
        r = requests.post(f"{FIREBASE}/queue.json", json=item, timeout=30)
        ok = r.status_code in (200, 201)
    except Exception:
        ok = False

    if not ok:
        await update.message.reply_text("❌ শিডিউল সেভ ব্যর্থ।", reply_markup=main_menu_kb())
        context.user_data.clear()
        return ConversationHandler.END

    when = time.strftime("%d-%m-%Y %I:%M:%S %p", time.localtime(run_at / 1000))
    await update.message.reply_text(
        f"✅ শিডিউল সেট!\n📌 {esc(item.get('name'))}\n"
        f"⏳ {sec // 60}মিনিট {sec % 60}সেকেন্ড পরে\n🕐 {when}\n\n"
        "বট অটো পোস্ট করবে (প্রতি ৩০ সেকেন্ডে চেক)।",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def list_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    data = fb_get("videos") or {}
    if not isinstance(data, dict) or not data:
        await update.message.reply_text("লিস্ট খালি।", reply_markup=main_menu_kb())
        return
    items = sorted(
        data.items(),
        key=lambda x: int((x[1] or {}).get("created_at") or 0),
        reverse=True,
    )[:15]
    lines = []
    for i, (k, v) in enumerate(items, 1):
        if not isinstance(v, dict):
            continue
        lines.append(f"{i}. {esc(v.get('name', k))}\n   <code>{k}</code>")
    await update.message.reply_text(
        "📋 <b>সাম্প্রতিক ভিডিও</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


async def list_sched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    data = fb_get("queue") or {}
    if not isinstance(data, dict) or not data:
        await update.message.reply_text("শিডিউল খালি।", reply_markup=main_menu_kb())
        return
    lines = []
    for i, (k, v) in enumerate(data.items(), 1):
        if not isinstance(v, dict):
            continue
        st = v.get("status", "")
        name = esc(v.get("name", k))
        lines.append(f"{i}. {name}\n   স্ট্যাটাস: {esc(st)}")
    await update.message.reply_text(
        "⏰ <b>শিডিউল লিস্ট</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


async def channels_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ch = load_channels(context)
    lines = [f"• {esc(c.get('title', c.get('id')))} — <code>{esc(c.get('id'))}</code>" for c in ch]
    text = "📢 <b>চ্যানেল</b>\n\n" + ("\n".join(lines) if lines else "কোনো চ্যানেল নেই।")
    text += "\n\nযোগ করতে পাঠাও:\n<code>/addchannel -100xxxx</code>\nবাদ: <code>/delchannel -100xxxx</code>"
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_menu_kb())


async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("ব্যবহার: /addchannel -100xxxx")
        return
    cid = parts[1].strip()
    title = cid
    try:
        r = requests.get(f"{API}/getChat", params={"chat_id": cid}, timeout=20)
        d = r.json()
        if d.get("ok"):
            title = d["result"].get("title") or cid
            cid = str(d["result"].get("id") or cid)
    except Exception:
        pass
    ch = load_channels(context)
    if any(str(c.get("id")) == str(cid) for c in ch):
        await update.message.reply_text("আগে থেকেই আছে।")
        return
    ch.append({"id": cid, "title": title})
    save_channels(context, ch)
    await update.message.reply_text(f"✅ যোগ: {esc(title)}\n<code>{esc(cid)}</code>", parse_mode="HTML")


async def del_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("ব্যবহার: /delchannel -100xxxx")
        return
    cid = parts[1].strip()
    ch = load_channels(context)
    ch = [c for c in ch if str(c.get("id")) != cid]
    save_channels(context, ch)
    await update.message.reply_text("✅ বাদ দেওয়া হয়েছে।")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    vids = fb_get("videos") or {}
    q = fb_get("queue") or {}
    n = len(vids) if isinstance(vids, dict) else 0
    nq = len(q) if isinstance(q, dict) else 0
    ch = load_channels(context)
    await update.message.reply_text(
        f"📊 <b>স্ট্যাটাস</b>\nভিডিও: {n}\nশিডিউল কিউ: {nq}\nচ্যানেল: {len(ch)}",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


async def menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    t = update.message.text or ""
    if t == "🎬 নতুন পোস্ট":
        return await new_post(update, context)
    if t == "📋 ভিডিও লিস্ট":
        return await list_videos(update, context)
    if t == "⏰ শিডিউল লিস্ট":
        return await list_sched(update, context)
    if t == "📢 চ্যানেল":
        return await channels_menu(update, context)
    if t == "📊 স্ট্যাটাস":
        return await status_cmd(update, context)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🎬 নতুন পোস্ট$"), new_post),
            CommandHandler("new", new_post),
        ],
        states={
            TITLE_IMAGE: [MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, title_image)],
            LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, link_step)],
            DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, duration_step)],
            ADS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ads_step)],
            CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, category_step)],
            PREVIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, preview_step)],
            SCHED_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sched_time_step)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^❌"), cancel),
        ],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("delchannel", del_channel))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text))

    async def post_init(application: Application):
        application.create_task(schedule_worker(application))
        log.info("Schedule worker started")

    app.post_init = post_init

    log.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

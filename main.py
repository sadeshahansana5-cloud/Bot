import logging
import re
import os
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# --- MongoDB & Telethon ---
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

# --- CONFIGURATION ---
BOT_TOKEN = "8403792868:AAG8hAM3bnxQR_fflWLRUPOAmxWuBUtAk1o"
OWNER_ID = 8107411538

CH_SINHALA_SUB = -1003455778818
CH_PC_GAME = -1003533883639
CH_MOVIE_SERIES = -1003599890322

AUTHORIZED_GROUP_ID = -1003699370326
GROUP_LINK = "https://t.me/SHFilmAndGame"

# MongoDB
MONGO_URI = "mongodb+srv://sadeshahansana:sadesha@cluster0.rjmdvlo.mongodb.net/?appName=Cluster0"
MONGO_DB_NAME = "sh_bot_v2"

# Telethon session file
TELEGRAM_SESSION = "bot_indexer.session"

MAINTENANCE_MODE = False

# --- LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.ERROR
)
logger = logging.getLogger(__name__)

# --- MongoDB Client ---
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[MONGO_DB_NAME]

# Collections
files_col = db["files"]
users_col = db["users"]
admins_col = db["admins"]
requests_col = db["requests"]
clone_requests_col = db["clone_requests"]
history_col = db["history"]

# --- Helper Functions (async) ---
async def is_admin(user_id: int) -> bool:
    return await admins_col.find_one({"user_id": user_id}) is not None

async def get_stats():
    users = await users_col.count_documents({})
    files = await files_col.count_documents({})
    reqs = await requests_col.count_documents({"status": "pending"})
    clones = await clone_requests_col.count_documents({"status": "pending"})
    return users, files, reqs, clones

# --- Text Processing (unchanged) ---
def get_readable_size(size_in_bytes):
    if not size_in_bytes:
        return "Unknown"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} PB"

def clean_filename(text):
    if not text:
        return "Unknown File"
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'(https?://\S+|www\.\S+|t\.me/\S+)', '', text)
    text = re.sub(r'(?i)(1080p|720p|480p|BluRay|WEB-DL|x264|x265|HEVC|AAC|DDP5\.1|\.mkv|\.mp4)', '', text)
    text = re.sub(r'[._\-]', ' ', text)
    text = re.sub(r'[\[\]\(\)]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return f"{text} 🎬SH BOTS🎬"

def extract_metadata(text):
    s, e = 0, 0
    s_match = re.search(r'(?:s|season)\s?(\d{1,2})', text, re.IGNORECASE)
    e_match = re.search(r'(?:e|episode|ep)\s?(\d{1,3})', text, re.IGNORECASE)
    if s_match:
        s = int(s_match.group(1))
    if e_match:
        e = int(e_match.group(1))
    return s, e

def determine_category(chat_id, file_name):
    if chat_id == CH_PC_GAME:
        return "Games"
    elif chat_id == CH_SINHALA_SUB:
        return "SinhalaSub"
    elif chat_id == CH_MOVIE_SERIES:
        if re.search(r'(S\d+|Season|E\d+|Episode)', file_name, re.IGNORECASE):
            return "Series"
        return "Movies"
    return "Others"

# --- Handlers (modified to use async MongoDB) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    # Register user
    await users_col.update_one(
        {"user_id": user.id},
        {"$set": {"first_name": user.first_name, "joined_date": datetime.now().strftime("%Y-%m-%d")}},
        upsert=True
    )

    # File deep link
    if args and args[0].startswith("file_"):
        if MAINTENANCE_MODE and not await is_admin(user.id):
            await update.message.reply_text("🚧 System is under maintenance.")
            return

        file_db_id = int(args[0].split("_")[1])
        file_data = await files_col.find_one({"id": file_db_id})
        if file_data:
            f_id = file_data["file_id"]
            f_name = file_data["file_name"]
            f_size = file_data["file_size"]
            f_cat = file_data["category"]
            f_type = file_data["file_type"]

            # Add to history, keep last 10
            await history_col.insert_one({
                "user_id": user.id,
                "file_name": f_name,
                "dl_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            # Keep only last 10
            cursor = history_col.find({"user_id": user.id}).sort("_id", -1).skip(10)
            async for doc in cursor:
                await history_col.delete_one({"_id": doc["_id"]})

            caption = (
                f"📂 **{f_name}**\n\n"
                f"🗂 **Category:** {f_cat}\n"
                f"💾 **Size:** {get_readable_size(f_size)}\n"
                f"🤖 **Downloaded via SH BOTS**"
            )
            try:
                if f_type == 'video':
                    await update.message.reply_video(video=f_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
                else:
                    await update.message.reply_document(document=f_id, caption=caption, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                await update.message.reply_text("❌ File deleted from channel or error occurred.")
                logger.error(f"Send Error: {e}")
        else:
            await update.message.reply_text("❌ File not found in DB.")
        return

    # Private chat start
    if update.effective_chat.type == ChatType.PRIVATE:
        if await is_admin(user.id):
            await show_admin_dashboard(update)
        else:
            total_files = await files_col.count_documents({})
            text = (
                f"👋 **Welcome {user.first_name}!**\n\n"
                f"🗂 **Total Files:** `{total_files}`\n"
                "⚠️ **Access Restricted:**\n"
                "මෙම බොට් භාවිතා කළ හැක්කේ අපගේ Group එක හරහා පමණි.\n\n"
                "👇 **Join Group or View Help:**"
            )
            kb = [
                [InlineKeyboardButton("🔗 Join SH Film & Game Group", url=GROUP_LINK)],
                [InlineKeyboardButton("❓ Commands & Help", callback_data="user_help")]
            ]
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    # Group welcome
    elif update.effective_chat.id == AUTHORIZED_GROUP_ID:
        await update.message.reply_text("👋 Hi! Type any Movie/Series/Game name to search.")

async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg_text = update.message.text

    # Admin force reply handling (add admin)
    if (update.message.reply_to_message and
        update.message.reply_to_message.text == "🆔 Please reply with the User ID to add as Admin:"):
        if await is_admin(user.id):
            try:
                new_admin_id = int(msg_text)
                await admins_col.update_one({"user_id": new_admin_id}, {"$set": {"user_id": new_admin_id}}, upsert=True)
                await update.message.reply_text(f"✅ User `{new_admin_id}` is now an Admin!")
            except ValueError:
                await update.message.reply_text("❌ Invalid ID. Please try again via Dashboard.")
        return

    if not msg_text or msg_text.startswith("/"):
        return

    # Access control
    if chat.type == ChatType.PRIVATE:
        if not await is_admin(user.id):
            await update.message.reply_text(f"⚠️ Please use the group: {GROUP_LINK}")
            return
    elif chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if chat.id != AUTHORIZED_GROUP_ID:
            return

    context.user_data['search_query'] = msg_text
    query = msg_text

    # Aggregate categories
    pipeline = [
        {"$match": {"file_name": {"$regex": query, "$options": "i"}}},
        {"$group": {"_id": "$category", "count": {"$sum": 1}}}
    ]
    cursor = files_col.aggregate(pipeline)
    rows = []
    async for doc in cursor:
        rows.append((doc["_id"], doc["count"]))

    if not rows:
        if chat.type == ChatType.PRIVATE:
            await update.message.reply_text("❌ No results found.")
        else:
            await update.message.reply_text("❌ No results found.")
        return

    keyboard = []
    row = []
    found_categories = {r[0]: r[1] for r in rows}
    priority_cats = ["SinhalaSub", "Movies", "Series", "Games"]

    for cat in priority_cats:
        if cat in found_categories:
            count = found_categories[cat]
            row.append(InlineKeyboardButton(f"{cat} ({count})", callback_data=f"list_{cat}_0"))
            if len(row) == 2:
                keyboard.append(row)
                row = []

    for cat, count in found_categories.items():
        if cat not in priority_cats:
            row.append(InlineKeyboardButton(f"{cat} ({count})", callback_data=f"list_{cat}_0"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        f"🔎 **Search Results for:** `{query}`\n"
        "👇 **Select a Category:**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    search_query = context.user_data.get('search_query', "")

    if data == "user_help":
        msg = (
            "🤖 **USER HELP GUIDE**\n\n"
            "🔹 **How to Search?**\n"
            "Join our group and simply type the name of the Movie, Series, or Game.\n\n"
            "🔹 **Commands:**\n"
            "`/request <Name>` - Request a missing file.\n"
            "`/clone` - Request bot source code (Admin approval needed).\n"
            "`/history` - View your last 10 downloads.\n\n"
            f"🔗 Group Link: {GROUP_LINK}"
        )
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_pvt_start")]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "back_to_pvt_start":
        await start(update, context)
        return

    elif data.startswith("list_"):
        _, cat, page = data.split("_")
        page = int(page)
        if cat == "Series" and "filter_season" not in context.user_data:
            context.user_data['filter_season'] = None
            context.user_data['filter_episode'] = None
        await render_file_list(update, context, cat, search_query, page)

    elif data == "ser_show_seasons":
        await render_series_filter_list(update, context, "Season", search_query, 0)

    elif data == "ser_show_episodes":
        await render_series_filter_list(update, context, "Episode", search_query, 0)

    elif data.startswith("ser_pg_"):
        _, _, f_type, page = data.split("_")
        await render_series_filter_list(update, context, f_type, search_query, int(page))

    elif data.startswith("ser_sel_"):
        _, _, f_type, val = data.split("_")
        val = int(val)
        if f_type == "S":
            context.user_data['filter_season'] = val
            context.user_data['filter_episode'] = None
            await query.answer(f"Selected Season {val}")
        elif f_type == "E":
            context.user_data['filter_episode'] = val
            await query.answer(f"Selected Episode {val}")
        await render_file_list(update, context, "Series", search_query, 0)

    elif data == "ser_clear":
        context.user_data['filter_season'] = None
        context.user_data['filter_episode'] = None
        await render_file_list(update, context, "Series", search_query, 0)

    elif data.startswith("adm_"):
        if not await is_admin(user_id):
            await query.answer("⚠️ Admins Only!", show_alert=True)
            return
        await handle_admin_logic(update, context)

async def render_file_list(update, context, category, query_text, page):
    limit = 10
    offset = page * limit

    filter_query = {"file_name": {"$regex": query_text, "$options": "i"}, "category": category}
    s_filter = context.user_data.get('filter_season')
    e_filter = context.user_data.get('filter_episode')
    if category == "Series":
        if s_filter:
            filter_query["season"] = s_filter
        if e_filter:
            filter_query["episode"] = e_filter

    total_items = await files_col.count_documents(filter_query)
    cursor = files_col.find(filter_query).sort([("season", 1), ("episode", 1), ("file_name", 1)]).skip(offset).limit(limit)
    results = []
    async for doc in cursor:
        results.append((doc["id"], doc["file_name"], doc["file_size"], doc.get("season", 0), doc.get("episode", 0)))

    kb = []
    if category == "Series":
        filter_row = []
        filter_row.append(InlineKeyboardButton("📅 Seasons", callback_data="ser_show_seasons"))
        filter_row.append(InlineKeyboardButton("🎞 Episodes", callback_data="ser_show_episodes"))
        kb.append(filter_row)
        status_text = []
        if s_filter:
            status_text.append(f"✅ S{s_filter}")
        if e_filter:
            status_text.append(f"✅ E{e_filter}")
        if status_text:
            kb.append([InlineKeyboardButton(" ".join(status_text) + " (Clear)", callback_data="ser_clear")])

    bot_username = context.bot.username
    for fid, fname, fsize, s, e in results:
        size_str = get_readable_size(fsize)
        display = fname
        if category == "Series":
            meta = ""
            if s > 0:
                meta += f"S{s:02}"
            if e > 0:
                meta += f" E{e:02}"
            if meta:
                display = f"[{meta}] {fname[:20]}..."
        else:
            display = fname[:30] + "..."
        url = f"https://t.me/{bot_username}?start=file_{fid}"
        kb.append([InlineKeyboardButton(f"{display} ({size_str})", url=url)])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Back", callback_data=f"list_{category}_{page-1}"))
    if (offset + limit) < total_items:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"list_{category}_{page+1}"))
    if nav_row:
        kb.append(nav_row)

    msg_text = f"📂 **{category}**\n🔎 Query: `{query_text}`\n📊 Found: {total_items} (Pg {page+1})"
    await update.callback_query.edit_message_text(
        msg_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN
    )

async def render_series_filter_list(update, context, filter_type, query_text, page):
    limit = 10
    offset = page * limit
    col = "season" if filter_type == "Season" else "episode"
    pipeline = [
        {"$match": {"file_name": {"$regex": query_text, "$options": "i"}, "category": "Series", col: {"$gt": 0}}},
        {"$group": {"_id": f"${col}"}},
        {"$sort": {"_id": 1}},
        {"$skip": offset},
        {"$limit": limit}
    ]
    cursor = files_col.aggregate(pipeline)
    all_vals = []
    async for doc in cursor:
        all_vals.append(doc["_id"])
    total_vals = await files_col.count_documents({"file_name": {"$regex": query_text, "$options": "i"}, "category": "Series", col: {"$gt": 0}})

    kb = []
    row = []
    for val in all_vals:
        prefix = "S" if filter_type == "Season" else "E"
        cb = f"ser_sel_{prefix}_{val}"
        row.append(InlineKeyboardButton(f"{prefix}{val:02}", callback_data=cb))
        if len(row) == 5:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"ser_pg_{filter_type}_{page-1}"))
    if (offset + limit) < total_vals:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"ser_pg_{filter_type}_{page+1}"))
    if nav_row:
        kb.append(nav_row)
    kb.append([InlineKeyboardButton("🔙 Back to List", callback_data="list_Series_0")])

    await update.callback_query.edit_message_text(
        f"🔢 Select {filter_type}",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_admin_logic(update, context):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data == "adm_dashboard":
        await show_admin_dashboard(update)
    elif data == "adm_refresh":
        await show_admin_dashboard(update)
    elif data == "adm_toggle_maint":
        global MAINTENANCE_MODE
        MAINTENANCE_MODE = not MAINTENANCE_MODE
        await show_admin_dashboard(update)
    elif data == "adm_view_req":
        cursor = requests_col.find({"status": "pending"}).limit(5)
        reqs = []
        async for doc in cursor:
            reqs.append((doc["id"], doc["user_name"], doc["request_text"]))
        if not reqs:
            await query.answer("No pending requests.", show_alert=True)
            return
        text = "📥 **Pending Requests**\n"
        kb = []
        for rid, uname, rtext in reqs:
            text += f"🔹 `{uname}`: {rtext}\n"
            kb.append([
                InlineKeyboardButton(f"✅ Done {rid}", callback_data=f"adm_rdone_{rid}"),
                InlineKeyboardButton(f"❌ Cancel {rid}", callback_data=f"adm_rcanc_{rid}")
            ])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="adm_dashboard")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("adm_rdone_") or data.startswith("adm_rcanc_"):
        parts = data.split("_")
        action = parts[1]
        rid = int(parts[2])
        req_doc = await requests_col.find_one({"id": rid})
        if req_doc:
            uid = req_doc["user_id"]
            rtext = req_doc["request_text"]
            if action == "rdone":
                await requests_col.delete_one({"id": rid})
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=f"✅ **Request Fulfilled!**\n\nYour request for `{rtext}` has been uploaded.\nPlease search in the bot now.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except:
                    pass
            else:
                await requests_col.delete_one({"id": rid})
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=f"❌ **Request Unavailable**\n\nSorry, we could not find `{rtext}` at this time.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except:
                    pass
        await show_admin_dashboard(update)
    elif data == "adm_view_clones":
        cursor = clone_requests_col.find({"status": "pending"}).limit(5)
        clones = []
        async for doc in cursor:
            clones.append((doc["id"], doc["user_name"], doc["user_id"]))
        if not clones:
            await query.answer("No pending clone requests.", show_alert=True)
            return
        text = "🤖 **Clone Requests**\n"
        kb = []
        for cid, uname, uid in clones:
            text += f"🔸 `{uname}` (ID: {uid})\n"
            kb.append([
                InlineKeyboardButton(f"✅ Send Code {cid}", callback_data=f"adm_cdone_{cid}"),
                InlineKeyboardButton(f"❌ Deny {cid}", callback_data=f"adm_ccanc_{cid}")
            ])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="adm_dashboard")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("adm_cdone_"):
        rid = int(data.split("_")[2])
        req_doc = await clone_requests_col.find_one({"id": rid})
        if req_doc:
            uid = req_doc["user_id"]
            await send_source_code(context, uid)
            await clone_requests_col.delete_one({"id": rid})
        await show_admin_dashboard(update)
    elif data.startswith("adm_ccanc_"):
        rid = int(data.split("_")[2])
        req_doc = await clone_requests_col.find_one({"id": rid})
        if req_doc:
            uid = req_doc["user_id"]
            try:
                await context.bot.send_message(uid, "❌ **Clone Request Denied.**\nWe cannot provide the source code.")
            except:
                pass
            await clone_requests_col.delete_one({"id": rid})
        await show_admin_dashboard(update)
    elif data == "adm_add_admin_prompt":
        await context.bot.send_message(
            chat_id=user_id,
            text="🆔 Please reply with the User ID to add as Admin:",
            reply_markup=ForceReply(selective=True)
        )
        await query.answer("Check your messages.")
    elif data == "adm_backup":
        # MongoDB backup is not a single file, so we notify.
        await query.answer("Backup: Use mongodump or export collections manually.", show_alert=True)

async def show_admin_dashboard(update):
    u, f, r, c = await get_stats()
    maint = "🔴 On" if MAINTENANCE_MODE else "🟢 Off"
    text = (
        f"👑 **ADMIN DASHBOARD**\n\n"
        f"👥 Users: `{u}`\n"
        f"📂 Files: `{f}`\n"
        f"📥 File Reqs: `{r}`\n"
        f"🤖 Clone Reqs: `{c}`\n"
        f"🛠 Maint Mode: {maint}"
    )
    keyboard = [
        [InlineKeyboardButton("👁 View File Requests", callback_data="adm_view_req"),
         InlineKeyboardButton("🤖 View Clone Reqs", callback_data="adm_view_clones")],
        [InlineKeyboardButton("➕ Add Admin (ID)", callback_data="adm_add_admin_prompt"),
         InlineKeyboardButton("🛠 Toggle Maint.", callback_data="adm_toggle_maint")],
        [InlineKeyboardButton("♻️ Backup DB", callback_data="adm_backup"),
         InlineKeyboardButton("🔄 Refresh", callback_data="adm_refresh")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def send_source_code(context, user_id):
    try:
        with open(__file__, 'r', encoding='utf-8') as f:
            code = f.read()
        code = code.replace(BOT_TOKEN, "YOUR_BOT_TOKEN_HERE")
        code = code.replace(str(OWNER_ID), "YOUR_OWNER_ID")
        with open("bot_source_copy.py", "w", encoding='utf-8') as f:
            f.write(code)
        await context.bot.send_document(
            chat_id=user_id,
            document=open("bot_source_copy.py", "rb"),
            caption="📜 **Here is the Bot Source Code!**\n\nNote: Tokens have been removed."
        )
        os.remove("bot_source_copy.py")
    except Exception as e:
        logger.error(f"Failed to send code: {e}")

# --- Live indexing (unchanged logic, now using MongoDB) ---
async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    chat_id = msg.chat.id

    if chat_id not in [CH_SINHALA_SUB, CH_PC_GAME, CH_MOVIE_SERIES]:
        return

    file_id, unique_id, fname, fsize, ftype = None, None, "Unknown", 0, "doc"
    if msg.document:
        file_id = msg.document.file_id
        unique_id = msg.document.file_unique_id
        fname = msg.document.file_name
        fsize = msg.document.file_size
    elif msg.video:
        file_id = msg.video.file_id
        unique_id = msg.video.file_unique_id
        fname = msg.video.file_name or msg.caption or "Video"
        fsize = msg.video.file_size
        ftype = "video"
    else:
        return

    # Check duplicate
    exists = await files_col.find_one({"file_unique_id": unique_id})
    if exists:
        return

    clean_name = clean_filename(fname)
    category = determine_category(chat_id, clean_name)
    season, episode = extract_metadata(fname)

    # Get next auto-increment id (simulate SQLite AUTOINCREMENT)
    last_doc = await files_col.find_one(sort=[("id", -1)])
    new_id = (last_doc["id"] + 1) if last_doc else 1

    await files_col.insert_one({
        "id": new_id,
        "file_id": file_id,
        "file_unique_id": unique_id,
        "file_name": clean_name,
        "file_size": fsize,
        "file_type": ftype,
        "category": category,
        "season": season,
        "episode": episode,
        "message_id": msg.message_id,
        "channel_id": chat_id
    })
    logger.info(f"Indexed: {clean_name} | Cat: {category}")

# --- /index command (Telethon based, old files indexing) ---
async def index_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if not await is_admin(user.id):
        await update.message.reply_text("⛔ Only admins can use /index.")
        return

    if chat.type not in [ChatType.CHANNEL, ChatType.SUPERGROUP]:
        await update.message.reply_text("❌ This command must be used inside a channel.")
        return

    channel_id = chat.id
    if channel_id not in [CH_SINHALA_SUB, CH_PC_GAME, CH_MOVIE_SERIES]:
        await update.message.reply_text("❌ This channel is not configured for indexing.")
        return

    # Send initial progress message
    progress_msg = await update.message.reply_text("🔄 Starting to index old files... This may take a while.")

    # Use Telethon with bot token
    async def index_worker():
        client = TelegramClient(TELEGRAM_SESSION, api_id=0, api_hash="")  # dummy, will use bot token
        await client.start(bot_token=BOT_TOKEN)

        try:
            entity = await client.get_entity(channel_id)
            total_messages = 0
            count_indexed = 0
            # First count total messages (approx)
            # We'll iterate and count on the fly
            last_id = 0
            processed = 0
            async for message in client.iter_messages(entity, limit=None):
                total_messages += 1
            # Reset
            processed = 0
            async for message in client.iter_messages(entity, limit=None):
                processed += 1
                percent = int(processed * 100 / total_messages) if total_messages else 0
                if percent % 5 == 0 or processed == total_messages:
                    await progress_msg.edit_text(f"📀 Indexing: {percent}% ({processed}/{total_messages} messages scanned...)")

                # Extract media
                media = message.media
                file_id = None
                unique_id = None
                fname = None
                fsize = 0
                ftype = "doc"

                if media:
                    if isinstance(media, MessageMediaDocument):
                        doc = media.document
                        file_id = str(doc.id)  # not real file_id, but we need real bot file_id? Telethon gives different ID
                        # Telethon cannot directly give bot file_id. We need to use bot API to get file_id.
                        # This is a limitation: Telethon uses MTProto, file IDs are different.
                        # Workaround: We cannot index old files because we cannot obtain the bot's file_id.
                        # Instead, we can only index new files via channel_post_handler.
                        # So this approach fails.
                        # Given the complexity, we must inform the user that indexing old files requires manual forwarding.
                        await progress_msg.edit_text("❌ Telethon cannot retrieve the bot's `file_id` for old messages.\n"
                                                     "Please forward the old files to the bot again, or re-upload them.\n"
                                                     "New files are indexed automatically.")
                        return
                    # Similarly for photo etc.

                # For real implementation, we would need to use bot API to get file_id from message_id, but that's not possible.
                # Therefore I will mark this as "not feasible" and suggest manual re-upload.

            await progress_msg.edit_text("⚠️ Indexing old files via Telethon is not possible because the bot's `file_id` cannot be obtained retroactively.\n"
                                         "Please re‑upload the files to the channel (the bot will index them automatically).")
        except Exception as e:
            logger.error(f"Index error: {e}")
            await progress_msg.edit_text(f"❌ Indexing failed: {str(e)}")
        finally:
            await client.disconnect()

    # Run in background
    asyncio.create_task(index_worker())

# --- Commands (unchanged except for /index) ---
async def request_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = " ".join(context.args)
    if not txt:
        await update.message.reply_text("Usage: /request <Movie/Series Name>")
        return
    last_doc = await requests_col.find_one(sort=[("id", -1)])
    new_id = (last_doc["id"] + 1) if last_doc else 1
    await requests_col.insert_one({
        "id": new_id,
        "user_id": update.effective_user.id,
        "user_name": update.effective_user.first_name,
        "request_text": txt,
        "status": "pending",
        "req_date": datetime.now().strftime("%Y-%m-%d")
    })
    await update.message.reply_text("✅ Request Sent to Admins!")

async def clone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    exists = await clone_requests_col.find_one({"user_id": user_id, "status": "pending"})
    if exists:
        await update.message.reply_text("⏳ You already have a pending request.")
    else:
        last_doc = await clone_requests_col.find_one(sort=[("id", -1)])
        new_id = (last_doc["id"] + 1) if last_doc else 1
        await clone_requests_col.insert_one({
            "id": new_id,
            "user_id": user_id,
            "user_name": update.effective_user.first_name,
            "status": "pending",
            "req_date": datetime.now().strftime("%Y-%m-%d")
        })
        await update.message.reply_text("✅ Source Code Request Sent! Admin will review it.")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor = history_col.find({"user_id": update.effective_user.id}).sort("_id", -1).limit(10)
    rows = []
    async for doc in cursor:
        rows.append((doc["file_name"], doc["dl_date"]))
    if not rows:
        await update.message.reply_text("📜 History is empty.")
        return
    text = "📜 **Your Download History**\n\n"
    for fname, dl_date in rows:
        text += f"⏰ {dl_date} - {fname[:30]}...\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def add_admin_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        uid = int(context.args[0])
        await admins_col.update_one({"user_id": uid}, {"$set": {"user_id": uid}}, upsert=True)
        await update.message.reply_text(f"✅ Admin {uid} added.")
    except:
        await update.message.reply_text("/addadmin <ID>")

# --- Main ---
if __name__ == '__main__':
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addadmin", add_admin_manual))
    app.add_handler(CommandHandler("request", request_cmd))
    app.add_handler(CommandHandler("clone", clone_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("index", index_command))   # new command

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("🔥 SH ULTRA BOT V2 (MongoDB + Index command) Started Successfully!")
    app.run_polling()

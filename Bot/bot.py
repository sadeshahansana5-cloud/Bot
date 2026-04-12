import logging
import sqlite3
import re
import os
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
    Application
)

# --- CONFIGURATION (මෙතන ඔබේ දත්ත නිවැරදිව දමන්න) ---
BOT_TOKEN = "8403792868:AAG8hAM3bnxQR_fflWLRUPOAmxWuBUtAk1o"
OWNER_ID = 8107411538

# Channels to Index
CH_SINHALA_SUB = -1003455778818
CH_PC_GAME = -1003533883639
CH_MOVIE_SERIES = -1003599890322

# Authorized Group ID
AUTHORIZED_GROUP_ID = -1003699370326

# Link for Private Users
GROUP_LINK = "https://t.me/SHFilmAndGame"

DB_NAME = "sh_bot_v2.db"
MAINTENANCE_MODE = False

# --- LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.ERROR
)
logger = logging.getLogger(__name__)

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # Files Table (Added constraint to prevent duplicates)
    c.execute('''CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT,
        file_unique_id TEXT UNIQUE,
        file_name TEXT,
        file_size INTEGER,
        file_type TEXT,
        category TEXT,
        season INTEGER DEFAULT 0,
        episode INTEGER DEFAULT 0,
        message_id INTEGER,
        channel_id INTEGER
    )''')

    # Users
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        joined_date TEXT
    )''')

    # Admins
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY
    )''')

    # Requests
    c.execute('''CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        user_name TEXT,
        request_text TEXT,
        status TEXT DEFAULT 'pending',
        req_date TEXT
    )''')

    # Clone Requests
    c.execute('''CREATE TABLE IF NOT EXISTS clone_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        user_name TEXT,
        status TEXT DEFAULT 'pending',
        req_date TEXT
    )''')

    # History
    c.execute('''CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        file_name TEXT,
        dl_date TEXT
    )''')

    # Default Owner Admin
    c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (OWNER_ID,))
    conn.commit()
    conn.close()

# --- DATABASE HELPERS ---
def get_db():
    return sqlite3.connect(DB_NAME)

def is_admin(user_id):
    conn = get_db()
    res = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return res is not None

def get_stats():
    conn = get_db()
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    reqs = conn.execute("SELECT COUNT(*) FROM requests WHERE status='pending'").fetchone()[0]
    clones = conn.execute("SELECT COUNT(*) FROM clone_requests WHERE status='pending'").fetchone()[0]
    conn.close()
    return users, files, reqs, clones

# --- TEXT PROCESSING ---

def get_readable_size(size_in_bytes):
    if not size_in_bytes: return "Unknown"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} PB"

def clean_filename(text):
    if not text: return "Unknown File"

    # 1. Remove Links & Usernames
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'(https?://\S+|www\.\S+|t\.me/\S+)', '', text)

    # 2. Remove Quality/Codec tags common in filenames (Optional but makes it cleaner)
    text = re.sub(r'(?i)(1080p|720p|480p|BluRay|WEB-DL|x264|x265|HEVC|AAC|DDP5\.1|\.mkv|\.mp4)', '', text)

    # 3. Replace symbols with space
    text = re.sub(r'[._\-]', ' ', text)

    # 4. Remove brackets
    text = re.sub(r'[\[\]\(\)]', '', text)

    # 5. Clean Spaces
    text = re.sub(r'\s+', ' ', text).strip()

    return f"{text} 🎬SH BOTS🎬"

def extract_metadata(text):
    """Extract Season and Episode numbers from filename."""
    s, e = 0, 0
    # Match S01E05 or Season 1 Episode 5 patterns
    s_match = re.search(r'(?:s|season)\s?(\d{1,2})', text, re.IGNORECASE)
    e_match = re.search(r'(?:e|episode|ep)\s?(\d{1,3})', text, re.IGNORECASE)

    if s_match: s = int(s_match.group(1))
    if e_match: e = int(e_match.group(1))

    return s, e

def determine_category(chat_id, file_name):
    if chat_id == CH_PC_GAME: return "Games"
    elif chat_id == CH_SINHALA_SUB: return "SinhalaSub"
    elif chat_id == CH_MOVIE_SERIES:
        if re.search(r'(S\d+|Season|E\d+|Episode)', file_name, re.IGNORECASE):
            return "Series"
        return "Movies"
    return "Others"

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    # Register User
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (user_id, first_name, joined_date) VALUES (?, ?, ?)",
                 (user.id, user.first_name, datetime.now().strftime("%Y-%m-%d")))
    conn.commit()

    # 1. FILE DOWNLOAD (Deep Link)
    if args and args[0].startswith("file_"):
        if MAINTENANCE_MODE and not is_admin(user.id):
            await update.message.reply_text("🚧 System is under maintenance.")
            conn.close()
            return

        file_db_id = args[0].split("_")[1]
        file_data = conn.execute("SELECT file_id, file_name, file_size, category, file_type FROM files WHERE id=?", (file_db_id,)).fetchone()

        if file_data:
            f_id, f_name, f_size, f_cat, f_type = file_data
            # Update History
            conn.execute("INSERT INTO history (user_id, file_name, dl_date) VALUES (?, ?, ?)",
                         (user.id, f_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            # Keep only last 10
            conn.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history WHERE user_id = ? ORDER BY id DESC LIMIT 10)", (user.id,))
            conn.commit()

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
        conn.close()
        return

    conn.close()

    # 2. PRIVATE CHAT START (USER DASHBOARD)
    if update.effective_chat.type == ChatType.PRIVATE:
        if is_admin(user.id):
            await show_admin_dashboard(update)
        else:
            conn = get_db()
            total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            conn.close()

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

    # 3. GROUP WELCOME
    elif update.effective_chat.id == AUTHORIZED_GROUP_ID:
        await update.message.reply_text("👋 Hi! Type any Movie/Series/Game name to search.")

# --- SEARCH HANDLER ---
async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg_text = update.message.text

    # Admin Force Reply Handling (For Adding Admin)
    if update.message.reply_to_message and update.message.reply_to_message.text == "🆔 Please reply with the User ID to add as Admin:":
        if is_admin(user.id):
            try:
                new_admin_id = int(msg_text)
                conn = get_db()
                conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_admin_id,))
                conn.commit()
                conn.close()
                await update.message.reply_text(f"✅ User `{new_admin_id}` is now an Admin!")
            except ValueError:
                await update.message.reply_text("❌ Invalid ID. Please try again via Dashboard.")
        return

    if not msg_text or msg_text.startswith("/"): return

    # Access Control
    if chat.type == ChatType.PRIVATE:
        if not is_admin(user.id):
            await update.message.reply_text(f"⚠️ Please use the group: {GROUP_LINK}")
            return
    elif chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if chat.id != AUTHORIZED_GROUP_ID:
            return

    # Save query to context
    context.user_data['search_query'] = msg_text
    query = msg_text

    conn = get_db()
    sql = "SELECT category, COUNT(*) FROM files WHERE file_name LIKE ? GROUP BY category"
    rows = conn.execute(sql, (f"%{query}%",)).fetchall()
    conn.close()

    if not rows:
        if chat.type == ChatType.PRIVATE:
            await update.message.reply_text("❌ No results found.")
        else:
            # Send a temp message in group
            temp = await update.message.reply_text("❌ No results found.")
            # Optionally delete after few seconds
        return

    # Category Selection
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
    if row: keyboard.append(row)

    await update.message.reply_text(
        f"🔎 **Search Results for:** `{query}`\n"
        "👇 **Select a Category:**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

# --- ADVANCED CALLBACK HANDLER ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    # Retrieve search query from memory
    search_query = context.user_data.get('search_query', "")

    # 1. USER HELP
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
        await start(update, context) # Re-trigger start logic
        return

    # 2. FILE LISTING & PAGINATION (list_CATEGORY_PAGE)
    if data.startswith("list_"):
        _, cat, page = data.split("_")
        page = int(page)

        # Reset Filters if entering Series new
        if cat == "Series" and "filter_season" not in context.user_data:
            context.user_data['filter_season'] = None
            context.user_data['filter_episode'] = None

        await render_file_list(update, context, cat, search_query, page)

    # 3. SERIES NAVIGATION (Seasons/Episodes)
    elif data == "ser_show_seasons":
        await render_series_filter_list(update, context, "Season", search_query, 0)

    elif data == "ser_show_episodes":
        await render_series_filter_list(update, context, "Episode", search_query, 0)

    elif data.startswith("ser_pg_"):
        # ser_pg_TYPE_PAGE
        _, _, f_type, page = data.split("_")
        await render_series_filter_list(update, context, f_type, search_query, int(page))

    elif data.startswith("ser_sel_"):
        # ser_sel_TYPE_VALUE
        _, _, f_type, val = data.split("_")
        val = int(val)

        if f_type == "S":
            context.user_data['filter_season'] = val
            context.user_data['filter_episode'] = None # Reset Ep
            await query.answer(f"Selected Season {val}")
        elif f_type == "E":
            context.user_data['filter_episode'] = val
            await query.answer(f"Selected Episode {val}")

        # Go back to file list with filter applied
        await render_file_list(update, context, "Series", search_query, 0)

    elif data == "ser_clear":
        context.user_data['filter_season'] = None
        context.user_data['filter_episode'] = None
        await render_file_list(update, context, "Series", search_query, 0)

    # 4. ADMIN ACTIONS
    elif data.startswith("adm_"):
        if not is_admin(user_id):
            await query.answer("⚠️ Admins Only!", show_alert=True)
            return
        await handle_admin_logic(update, context)

# --- RENDER HELPERS (PAGINATION) ---

async def render_file_list(update, context, category, query_text, page):
    conn = get_db()
    limit = 10
    offset = page * limit

    # Base SQL
    sql = "SELECT id, file_name, file_size, season, episode FROM files WHERE file_name LIKE ? AND category = ?"
    params = [f"%{query_text}%", category]

    # Series Filters
    s_filter = context.user_data.get('filter_season')
    e_filter = context.user_data.get('filter_episode')

    if category == "Series":
        if s_filter:
            sql += " AND season = ?"
            params.append(s_filter)
        if e_filter:
            sql += " AND episode = ?"
            params.append(e_filter)

    # Count Total for Pagination
    count_sql = sql.replace("id, file_name, file_size, season, episode", "COUNT(*)")
    total_items = conn.execute(count_sql, params).fetchone()[0]

    # Fetch Data
    sql += " ORDER BY season ASC, episode ASC, file_name ASC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    results = conn.execute(sql, params).fetchall()
    conn.close()

    # Build Keyboard
    kb = []

    # SERIES TOP BUTTONS
    if category == "Series":
        filter_row = []
        filter_row.append(InlineKeyboardButton("📅 Seasons", callback_data="ser_show_seasons"))
        filter_row.append(InlineKeyboardButton("🎞 Episodes", callback_data="ser_show_episodes"))
        kb.append(filter_row)

        status_text = []
        if s_filter: status_text.append(f"✅ S{s_filter}")
        if e_filter: status_text.append(f"✅ E{e_filter}")
        if status_text:
            kb.append([InlineKeyboardButton(" ".join(status_text) + " (Clear)", callback_data="ser_clear")])

    # File Buttons
    bot_username = context.bot.username
    for res in results:
        fid, fname, fsize, s, e = res
        size_str = get_readable_size(fsize)

        display = fname
        if category == "Series":
            meta = ""
            if s > 0: meta += f"S{s:02}"
            if e > 0: meta += f" E{e:02}"
            if meta: display = f"[{meta}] {fname[:20]}..."
        else:
            display = fname[:30] + "..."

        url = f"https://t.me/{bot_username}?start=file_{fid}"
        kb.append([InlineKeyboardButton(f"{display} ({size_str})", url=url)])

    # Pagination Buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Back", callback_data=f"list_{category}_{page-1}"))
    if (offset + limit) < total_items:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"list_{category}_{page+1}"))

    if nav_row: kb.append(nav_row)

    msg_text = f"📂 **{category}**\n🔎 Query: `{query_text}`\n📊 Found: {total_items} (Pg {page+1})"

    await update.callback_query.edit_message_text(
        msg_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN
    )

async def render_series_filter_list(update, context, filter_type, query_text, page):
    # filter_type: "Season" or "Episode"
    conn = get_db()
    limit = 10
    offset = page * limit

    col = "season" if filter_type == "Season" else "episode"

    # Get distinct available numbers
    sql = f"SELECT DISTINCT {col} FROM files WHERE file_name LIKE ? AND category = 'Series' AND {col} > 0 ORDER BY {col}"
    all_vals = conn.execute(sql, (f"%{query_text}%",)).fetchall()
    conn.close()

    # Pagination Logic for Filters
    total_vals = len(all_vals)
    current_slice = all_vals[offset : offset + limit]

    kb = []
    row = []
    # Build Number Buttons
    for val_tuple in current_slice:
        val = val_tuple[0]
        prefix = "S" if filter_type == "Season" else "E"
        # Callback: ser_sel_S_1
        cb = f"ser_sel_{prefix}_{val}"
        row.append(InlineKeyboardButton(f"{prefix}{val:02}", callback_data=cb))
        if len(row) == 5:
            kb.append(row)
            row = []
    if row: kb.append(row)

    # Nav Buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"ser_pg_{filter_type}_{page-1}"))
    if (offset + limit) < total_vals:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"ser_pg_{filter_type}_{page+1}"))
    if nav_row: kb.append(nav_row)

    kb.append([InlineKeyboardButton("🔙 Back to List", callback_data="list_Series_0")])

    await update.callback_query.edit_message_text(
        f"🔢 Select {filter_type}",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )

# --- ADMIN LOGIC ---

async def handle_admin_logic(update, context):
    query = update.callback_query
    data = query.data

    if data == "adm_dashboard":
        await show_admin_dashboard(update)

    elif data == "adm_refresh":
        await show_admin_dashboard(update)

    elif data == "adm_toggle_maint":
        global MAINTENANCE_MODE
        MAINTENANCE_MODE = not MAINTENANCE_MODE
        await show_admin_dashboard(update)

    # -- REQUESTS --
    elif data == "adm_view_req":
        conn = get_db()
        reqs = conn.execute("SELECT id, user_name, request_text FROM requests WHERE status='pending' LIMIT 5").fetchall()
        conn.close()

        if not reqs:
            await query.answer("No pending requests.", show_alert=True)
            return

        text = "📥 **Pending Requests**\n"
        kb = []
        for r in reqs:
            text += f"🔹 `{r[1]}`: {r[2]}\n"
            kb.append([
                InlineKeyboardButton(f"✅ Done {r[0]}", callback_data=f"adm_rdone_{r[0]}"),
                InlineKeyboardButton(f"❌ Cancel {r[0]}", callback_data=f"adm_rcanc_{r[0]}")
            ])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="adm_dashboard")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data.startswith("adm_rdone_") or data.startswith("adm_rcanc_"):
        action, rid = data.split("_")[1], data.split("_")[2]
        conn = get_db()
        req = conn.execute("SELECT user_id, request_text FROM requests WHERE id=?", (rid,)).fetchone()

        if req:
            uid, rtext = req
            if action == "rdone":
                conn.execute("DELETE FROM requests WHERE id=?", (rid,)) # Or set status='done'
                # Send User Message
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=f"✅ **Request Fulfilled!**\n\nYour request for `{rtext}` has been uploaded.\nPlease search in the bot now.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except: pass
            else: # rcanc
                conn.execute("DELETE FROM requests WHERE id=?", (rid,))
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=f"❌ **Request Unavailable**\n\nSorry, we could not find `{rtext}` at this time.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except: pass
        conn.commit()
        conn.close()
        await show_admin_dashboard(update)

    # -- CLONE REQUESTS --
    elif data == "adm_view_clones":
        conn = get_db()
        clones = conn.execute("SELECT id, user_name, user_id FROM clone_requests WHERE status='pending' LIMIT 5").fetchall()
        conn.close()

        if not clones:
            await query.answer("No pending clone requests.", show_alert=True)
            return

        text = "🤖 **Clone Requests**\n"
        kb = []
        for c in clones:
            text += f"🔸 `{c[1]}` (ID: {c[2]})\n"
            kb.append([
                InlineKeyboardButton(f"✅ Send Code {c[0]}", callback_data=f"adm_cdone_{c[0]}"),
                InlineKeyboardButton(f"❌ Deny {c[0]}", callback_data=f"adm_ccanc_{c[0]}")
            ])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="adm_dashboard")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    elif data.startswith("adm_cdone_"):
        rid = data.split("_")[2]
        conn = get_db()
        req = conn.execute("SELECT user_id FROM clone_requests WHERE id=?", (rid,)).fetchone()

        if req:
            uid = req[0]
            # Send Code
            await send_source_code(context, uid)
            conn.execute("DELETE FROM clone_requests WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        await show_admin_dashboard(update)

    elif data.startswith("adm_ccanc_"):
        rid = data.split("_")[2]
        conn = get_db()
        req = conn.execute("SELECT user_id FROM clone_requests WHERE id=?", (rid,)).fetchone()
        if req:
            try:
                await context.bot.send_message(req[0], "❌ **Clone Request Denied.**\nWe cannot provide the source code.")
            except: pass
            conn.execute("DELETE FROM clone_requests WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        await show_admin_dashboard(update)

    # -- ADD ADMIN --
    elif data == "adm_add_admin_prompt":
        # Force Reply
        await context.bot.send_message(
            chat_id=user_id,
            text="🆔 Please reply with the User ID to add as Admin:",
            reply_markup=ForceReply(selective=True)
        )
        await query.answer("Check your messages.")

    elif data == "adm_backup":
        await context.bot.send_document(chat_id=user_id, document=open(DB_NAME, 'rb'), caption="🗄 Database Backup")
        await query.answer("Sent!")

async def show_admin_dashboard(update):
    u, f, r, c = get_stats()
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
        # Read own source
        with open(__file__, 'r', encoding='utf-8') as f:
            code = f.read()

        # Censor Sensitive Info
        code = code.replace(BOT_TOKEN, "YOUR_BOT_TOKEN_HERE")
        code = code.replace(str(OWNER_ID), "YOUR_OWNER_ID")

        # Write temp file
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

# --- INDEXING HANDLER ---
async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    chat_id = msg.chat.id

    if chat_id not in [CH_SINHALA_SUB, CH_PC_GAME, CH_MOVIE_SERIES]: return

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

    # Check for duplicate
    conn = get_db()
    exists = conn.execute("SELECT 1 FROM files WHERE file_unique_id = ?", (unique_id,)).fetchone()
    if exists:
        conn.close()
        return # Skip duplicate

    # Process Name & Metadata
    clean_name = clean_filename(fname)
    category = determine_category(chat_id, clean_name)
    season, episode = extract_metadata(fname)

    try:
        conn.execute('''INSERT INTO files
            (file_id, file_unique_id, file_name, file_size, file_type, category, season, episode, message_id, channel_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (file_id, unique_id, clean_name, fsize, ftype, category, season, episode, msg.message_id, chat_id))
        conn.commit()
        logger.info(f"Indexed: {clean_name} | Cat: {category}")
    except Exception as e:
        logger.error(f"DB Error: {e}")
    finally:
        conn.close()

# --- COMMANDS ---
async def request_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = " ".join(context.args)
    if not txt:
        await update.message.reply_text("Usage: /request <Movie/Series Name>")
        return
    conn = get_db()
    conn.execute("INSERT INTO requests (user_id, user_name, request_text, req_date) VALUES (?,?,?,?)",
                 (update.effective_user.id, update.effective_user.first_name, txt, datetime.now().strftime("%Y-%m-%d")))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ Request Sent to Admins!")

async def clone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    # Check if pending exists
    exists = conn.execute("SELECT 1 FROM clone_requests WHERE user_id=? AND status='pending'", (update.effective_user.id,)).fetchone()
    if exists:
        await update.message.reply_text("⏳ You already have a pending request.")
    else:
        conn.execute("INSERT INTO clone_requests (user_id, user_name, req_date) VALUES (?,?,?)",
                     (update.effective_user.id, update.effective_user.first_name, datetime.now().strftime("%Y-%m-%d")))
        await update.message.reply_text("✅ Source Code Request Sent! Admin will review it.")
    conn.commit()
    conn.close()

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    rows = conn.execute("SELECT file_name, dl_date FROM history WHERE user_id=? ORDER BY id DESC LIMIT 10", (update.effective_user.id,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📜 History is empty.")
        return
    text = "📜 **Your Download History**\n\n"
    for r in rows: text += f"⏰ {r[1]} - {r[0][:30]}...\n"
    await update.message.reply_text(text)

async def add_admin_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Fallback command if button doesn't work for some reason
    if update.effective_user.id != OWNER_ID: return
    try:
        uid = int(context.args[0])
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (uid,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ Admin {uid} added.")
    except: await update.message.reply_text("/addadmin <ID>")

# --- MAIN ---
if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addadmin", add_admin_manual))
    app.add_handler(CommandHandler("request", request_cmd))
    app.add_handler(CommandHandler("clone", clone_cmd))
    app.add_handler(CommandHandler("history", history_cmd))

    # Channel Indexing
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_handler))

    # Search (Text Messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_handler))

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("🔥 SH ULTRA BOT V2 Started Successfully!")
    app.run_polling()
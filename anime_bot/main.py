import logging
from datetime import datetime, timezone
from bson import ObjectId
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from anime_bot.config import get_settings
from anime_bot.database.mongo import Mongo
from anime_bot.services.core import AnimeService, WatchlistService, RecommendationService, seed_confirmed_titles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("anime_bot")
STATUS_LABELS = {"plan":"📝 Plan to Watch", "watching":"▶️ Watching", "completed":"✅ Completed", "on_hold":"⏸ On Hold", "dropped":"❌ Dropped", "rewatching":"🔁 Rewatching", "not_interested":"🚫 Not Interested", "unknown":"❔ Unknown"}

def services(context):
    db = context.application.bot_data["db"]
    return db, AnimeService(db), WatchlistService(db), RecommendationService(db)

async def allowed(update: Update, context: ContextTypes.DEFAULT_TYPE, owner: bool=False) -> bool:
    settings = context.application.bot_data["settings"]
    user = update.effective_user
    if not user: return False
    if update.effective_chat and update.effective_chat.type != "private" and not settings.allow_groups:
        if update.effective_message: await update.effective_message.reply_text("Please use me in a private chat.")
        return False
    db = context.application.bot_data["db"]
    is_owner = user.id == settings.owner_telegram_id
    authorized = is_owner or await db.authorized_users.find_one({"user_id": user.id, "blocked": {"$ne": True}})
    if not authorized:
        if update.callback_query: await update.callback_query.answer("Access denied", show_alert=True)
        elif update.effective_message: await update.effective_message.reply_text("Access denied. Use /request_access to ask the owner.")
        return False
    if owner and not is_owner:
        if update.effective_message: await update.effective_message.reply_text("Owner only.")
        return False
    return True

def menu_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎌 Search", callback_data="v1:search"), InlineKeyboardButton("📚 Watchlist", callback_data="v1:list:all")],
        [InlineKeyboardButton("▶️ Watching", callback_data="v1:list:watching"), InlineKeyboardButton("✅ Completed", callback_data="v1:list:completed")],
        [InlineKeyboardButton("📝 Plan", callback_data="v1:list:plan"), InlineKeyboardButton("✨ Discover", callback_data="v1:discover")],
        [InlineKeyboardButton("🎯 Recommendations", callback_data="v1:recommend"), InlineKeyboardButton("📊 Statistics", callback_data="v1:stats")]])

def detail_markup(anime):
    aid = str(anime["_id"])
    rows = [[InlineKeyboardButton("➕ Plan to Watch", callback_data=f"v1:set:plan:{aid}"), InlineKeyboardButton("▶️ Watching", callback_data=f"v1:set:watching:{aid}")],
            [InlineKeyboardButton("✅ Completed", callback_data=f"v1:set:completed:{aid}"), InlineKeyboardButton("⏸ On Hold", callback_data=f"v1:set:on_hold:{aid}")],
            [InlineKeyboardButton("⭐ IMDb search", url="https://www.imdb.com/find/?q=" + __import__('urllib.parse', fromlist=['quote_plus']).quote_plus(anime["title"]))]]
    if anime.get("imdb_id") and str(anime["imdb_id"]).startswith("tt"):
        rows[-1][0] = InlineKeyboardButton("🌐 IMDb", url=f"https://www.imdb.com/title/{anime['imdb_id']}/")
    return InlineKeyboardMarkup(rows)

async def start(update, context):
    if not await allowed(update, context): return
    await update.message.reply_text("🎌 Anime Command Center\nYour private anime tracker. Choose an action:", reply_markup=menu_markup())

async def whoami(update, context): await update.message.reply_text(f"Your Telegram numeric ID: {update.effective_user.id}")
async def help_cmd(update, context):
    if not await allowed(update, context): return
    await update.message.reply_text("Commands: /start /menu /search <title> /add <title> /watchlist /watching /completed /plan /onhold /dropped /random /recommend /discover /movies /series /progress /stats /top /favourites /continue /streaming <title> /settings /export /import <file.xlsx> /cancel /whoami\nOwner: /allow /deny /users /pending_users /approve /reject /block /unblock /health")

async def menu(update, context):
    if await allowed(update, context): await update.message.reply_text("Main menu", reply_markup=menu_markup())

async def show_anime(update, context, anime, edit=False):
    db, _, wl, _ = services(context)
    entry = await db.watchlist.find_one({"user_id": update.effective_user.id, "anime_id": anime["_id"]})
    lines = [f"🎌 <b>{anime['title']}</b>" + (f" ({anime['year']})" if anime.get("year") else ""),
        f"🎬 Format: {anime.get('format','Unknown')}", "🎭 Genres: " + (", ".join(anime.get("genres", [])) or "Not available"),
        f"⭐ IMDb: {anime['imdb_rating']}/10 (IMDb)" if anime.get("imdb_rating") else "IMDb information is not configured or unavailable. IMDb search is available below.",
        "\n" + (anime.get("synopsis", "")[:700] if anime.get("synopsis") else "Synopsis unavailable."),
        "\nMy status: " + STATUS_LABELS.get((entry or {}).get("status", "unknown"), "Unknown")]
    msg = update.callback_query.message if edit and update.callback_query else update.effective_message
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=detail_markup(anime), disable_web_page_preview=True)

async def search(update, context):
    if not await allowed(update, context): return
    q = " ".join(context.args).strip()
    if not q: return await update.message.reply_text("Use /search <title>")
    _, anime_svc, _, _ = services(context); results = await anime_svc.search(q)
    if not results: return await update.message.reply_text("No matching catalogue entries. Add one with /add <title>.")
    for a in results[:5]: await show_anime(update, context, a)

async def add(update, context):
    if not await allowed(update, context): return
    title = " ".join(context.args).strip()
    if not title: return await update.message.reply_text("Use /add <title>")
    db, anime_svc, _, _ = services(context)
    results = await anime_svc.search(title, limit=5)
    exact = next((x for x in results if x.get("title_norm") == __import__('anime_bot.services.core', fromlist=['normalize']).normalize(title)), None)
    if exact: return await show_anime(update, context, exact)
    norm = __import__('anime_bot.services.core', fromlist=['normalize']).normalize(title)
    result = await db.anime.update_one({"title_norm": norm}, {"$setOnInsert": {"title": title, "title_norm": norm, "format": "unknown", "genres": [], "metadata_source": "user entry", "created_at": datetime.now(timezone.utc)}}, upsert=True)
    anime = await db.anime.find_one({"title_norm": norm})
    await update.message.reply_text("Added to catalogue. Metadata and format remain unverified until supplied.")
    await show_anime(update, context, anime)

async def list_cmd(update, context, status=None):
    if not await allowed(update, context): return
    _, _, wl, _ = services(context); rows = await wl.list(update.effective_user.id, status)
    message = update.effective_message
    if not rows: return await message.reply_text("Nothing here yet.", reply_markup=menu_markup())
    for a in rows[:10]:
        await message.reply_text(f"{STATUS_LABELS.get(a['watch'].get('status'),'❔')} · {a['title']}" + (f" ({a['year']})" if a.get("year") else ""), reply_markup=detail_markup(a))

async def recommendations(update, context):
    if not await allowed(update, context): return
    _, _, _, rec = services(context); values = await rec.recommend(update.effective_user.id)
    message=update.effective_message
    if not values: return await message.reply_text("Add catalogue titles and mark preferences to get recommendations.")
    for a, reason in values: await message.reply_text(f"✨ <b>{a['title']}</b>\nWhy: {reason}", parse_mode=ParseMode.HTML, reply_markup=detail_markup(a))

async def stats(update, context):
    if not await allowed(update, context): return
    db=context.application.bot_data["db"]; uid=update.effective_user.id
    counts={s: await db.watchlist.count_documents({"user_id":uid,"status":s}) for s in ("completed","watching","plan","on_hold","dropped")}
    prog=await db.progress.aggregate([{"$match":{"user_id":uid}},{"$group":{"_id":None,"episodes":{"$sum":"$episodes_completed"}}}]).to_list(1)
    total=await db.anime.count_documents({})
    await update.effective_message.reply_text(f"📊 Your stats\nCatalogue: {total}\nCompleted: {counts['completed']}\nWatching: {counts['watching']}\nPlan to watch: {counts['plan']}\nOn hold: {counts['on_hold']}\nDropped: {counts['dropped']}\nEpisodes watched: {prog[0]['episodes'] if prog else 0}")

async def callbacks(update, context):
    q=update.callback_query
    if not await allowed(update, context): return
    data=q.data or ""
    if len(data)>64 or not data.startswith("v1:"): return await q.answer("Invalid or expired action", show_alert=True)
    await q.answer()
    parts=data.split(":"); db, anime_svc, wl, rec=services(context)
    try:
        if parts[1]=="set" and len(parts)==4:
            status, aid=parts[2],parts[3]
            if status not in {"plan","watching","completed","on_hold","dropped","rewatching","not_interested"} or not ObjectId.is_valid(aid): raise ValueError()
            await wl.set_status(update.effective_user.id,aid,status)
            await q.message.reply_text(f"Status updated: {STATUS_LABELS[status]}")
        elif parts[1]=="list": await list_cmd(update,context,None if parts[2]=="all" else parts[2])
        elif parts[1]=="search": await q.message.reply_text("Use /search <title>")
        elif parts[1]=="discover":
            rows=await db.anime.find({}).limit(5).to_list(5)
            for a in rows: await show_anime(update,context,a)
        elif parts[1]=="recommend": await recommendations(update,context)
        elif parts[1]=="stats": await stats(update,context)
        else: await q.message.reply_text("That action is unavailable.")
    except (ValueError, Exception) as exc:
        log.info("Callback rejected (%s)", type(exc).__name__)
        await q.message.reply_text("Could not complete that action. Please reopen the anime details and try again.")

async def request_access(update, context):
    db=context.application.bot_data["db"]; user=update.effective_user
    await db.access_requests.update_one({"user_id":user.id},{"$setOnInsert":{"user_id":user.id,"name":user.full_name,"at":datetime.now(timezone.utc),"status":"pending"}},upsert=True)
    await update.message.reply_text("Access request recorded. The owner can review it.")

async def admin(update, context):
    if not await allowed(update,context,owner=True): return
    cmd=update.effective_message.text.split()[0].lstrip("/").split("@")[0]
    db=context.application.bot_data["db"]; args=context.args; owner=context.effective_user.id; now=datetime.now(timezone.utc)
    if cmd=="users":
        users=await db.authorized_users.find({"blocked":{"$ne":True}}).to_list(100)
        text="Authorized users: " + (", ".join(str(x["user_id"]) for x in users) or "none")
    elif cmd=="pending_users":
        users=await db.access_requests.find({"status":"pending"}).to_list(100); text="Pending: "+(", ".join(str(x["user_id"]) for x in users) or "none")
    elif cmd in {"allow","approve","deny","reject","block","unblock"}:
        if not args or not args[0].isdigit(): return await update.message.reply_text(f"Usage: /{cmd} <telegram_user_id>")
        uid=int(args[0])
        if uid==owner and cmd in {"deny","reject","block"}: return await update.message.reply_text("Owner access cannot be removed.")
        if cmd in {"allow","approve","unblock"}:
            await db.authorized_users.update_one({"user_id":uid},{"$set":{"user_id":uid,"blocked":False,"authorized_at":now,"authorized_by":owner}},upsert=True)
            await db.access_requests.update_one({"user_id":uid},{"$set":{"status":"approved"}})
        elif cmd in {"deny","reject","block"}:
            await db.authorized_users.update_one({"user_id":uid},{"$set":{"blocked":True,"blocked_at":now,"blocked_by":owner}},upsert=True)
            await db.access_requests.update_one({"user_id":uid},{"$set":{"status":"rejected"}})
        await db.audit.insert_one({"actor_id":owner,"action":cmd,"target_id":uid,"at":now}); text=f"{cmd.title()}d user {uid}."
    elif cmd=="health":
        try: await db.client.admin.command("ping"); mongo="connected"
        except Exception: mongo="unavailable"
        text=f"Bot process: running\nMongoDB: {mongo}\nTMDb configured: {bool(context.application.bot_data['settings'].tmdb_api_key.get_secret_value())}\nStreaming availability: provider not configured"
    else: text="Use /whoami for your numeric ID."
    await update.message.reply_text(text)

async def export_cmd(update, context):
    if not await allowed(update,context): return
    import json
    rows=await context.application.bot_data['db'].watchlist.find({"user_id":update.effective_user.id}).to_list(5000)
    payload=[]
    for row in rows:
        anime=await context.application.bot_data['db'].anime.find_one({"_id":row['anime_id']})
        payload.append({"title":(anime or {}).get("title"),"status":row.get("status"),"rating":row.get("rating"),"favourite":row.get("favourite"),"notes":row.get("notes"),"review":row.get("review")})
    from io import BytesIO
    doc=BytesIO(json.dumps(payload,ensure_ascii=False,indent=2).encode()); doc.name="anime_watchlist.json"
    await update.message.reply_document(doc,caption="Your personal watchlist export")

async def import_cmd(update, context):
    if not await allowed(update,context): return
    await update.message.reply_text("Use scripts/import_workbook.py <path.xlsx> --dry-run to preview, then rerun without --dry-run to import. Existing watch statuses are never overwritten.")

async def post_init(app):
    settings=app.bot_data["settings"]; mongo=Mongo(settings); await mongo.start(); app.bot_data["mongo"]=mongo; app.bot_data["db"]=mongo.db
    await mongo.db.authorized_users.update_one({"user_id":settings.owner_telegram_id},{"$setOnInsert":{"user_id":settings.owner_telegram_id,"owner":True,"blocked":False}},upsert=True)
    await seed_confirmed_titles(mongo.db,settings.owner_telegram_id)

async def post_shutdown(app):
    if app.bot_data.get("mongo"): await app.bot_data["mongo"].close()

def build_app():
    settings=get_settings(); settings.validate_runtime()
    app=Application.builder().token(settings.telegram_bot_token.get_secret_value()).post_init(post_init).post_shutdown(post_shutdown).build()
    app.bot_data["settings"]=settings
    for name, fn in [("start",start),("help",help_cmd),("menu",menu),("whoami",whoami),("search",search),("add",add),("watchlist",list_cmd),
        ("watching",lambda u,c:list_cmd(u,c,"watching")),("completed",lambda u,c:list_cmd(u,c,"completed")),("plan",lambda u,c:list_cmd(u,c,"plan")),
        ("onhold",lambda u,c:list_cmd(u,c,"on_hold")),("dropped",lambda u,c:list_cmd(u,c,"dropped")),("recommend",recommendations),("stats",stats),
        ("discover",lambda u,c:list_cmd(u,c,None)),("movies",lambda u,c:list_cmd(u,c,None)),("series",lambda u,c:list_cmd(u,c,None)),("top",lambda u,c:list_cmd(u,c,"completed")),("favourites",lambda u,c:list_cmd(u,c,None)),
        ("progress",lambda u,c:list_cmd(u,c,"watching")),("continue",lambda u,c:list_cmd(u,c,"watching")),("streaming",lambda u,c: u.message.reply_text("Live availability requires authorized provider access. Use IMDb/provider searches; availability is not inferred.")),
        ("reminders",lambda u,c: u.message.reply_text("Reminders are disabled until configured.")),("settings",lambda u,c: u.message.reply_text("Default region: "+c.application.bot_data['settings'].default_region)),("export",export_cmd),("import",import_cmd),("cancel",lambda u,c:u.message.reply_text("No active workflow.")),("request_access",request_access)]:
        app.add_handler(CommandHandler(name,fn))
    for name in ("allow","deny","users","pending_users","approve","reject","block","unblock","health"):
        app.add_handler(CommandHandler(name,admin))
    app.add_handler(CallbackQueryHandler(callbacks))
    return app

def main(): build_app().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
if __name__=="__main__": main()

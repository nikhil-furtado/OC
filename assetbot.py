mkdir -p ~/assetbot && cd ~/assetbot

cat > requirements.txt <<'REQ'
python-telegram-bot==21.10
requests==2.32.3
REQ

cat > assetbot.py <<'PY'
import asyncio, json, os, re, time
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, ContextTypes, MessageHandler, filters
import requests

ENVATO_ITEM_RE = re.compile(r"https?://(?:www\.)?elements\.envato\.com/[^\s/]+-([A-Z0-9]{6,})", re.I)
FREEPIK_RE = re.compile(r"https?://(?:www\.)?freepik\.com/[^\s]+", re.I)

def env(name, default=None):
    v=os.environ.get(name)
    if v is None:
        if default is None: raise RuntimeError(f"Missing env var: {name}")
        return default
    return v

def allowed_chat_ids():
    return {int(x.strip()) for x in env("ALLOWED_CHAT_IDS").split(",") if x.strip()}

class RL:
    def __init__(self, window=3600, maxhits=10):
        self.window=window; self.maxhits=maxhits; self.h={}
    def allow(self, chat_id, user_id):
        now=time.time()
        k=(chat_id,user_id)
        arr=[t for t in self.h.get(k,[]) if now-t<self.window]
        if len(arr)>=self.maxhits:
            self.h[k]=arr
            return False
        arr.append(now); self.h[k]=arr
        return True

def load_cookies(path):
    with open(path,"r",encoding="utf-8-sig") as f:
        raw=json.load(f)
    return {c["name"]:c["value"] for c in raw}

def envato_item_uuid(item_id):
    api=f"https://elements.envato.com/api/v1/items/{item_id}.json"
    r=requests.get(api,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"},timeout=30)
    r.raise_for_status()
    return r.json()["data"]["attributes"]["itemUuid"]

def envato_download(url, cookies_path, max_mb):
    m=ENVATO_ITEM_RE.search(url)
    if not m: raise ValueError("Could not extract Envato item id")
    item_id=m.group(1).upper()
    item_uuid=envato_item_uuid(item_id)

    candidate_types=["video-templates","wordpress","graphics","presentation-templates","fonts","photos",
                     "stock-video","music","sound-effects","add-ons","web-templates","cms-templates","3d"]

    cookies=load_cookies(cookies_path)
    headers_base={
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "Accept":"*/*",
        "Content-Type":"application/x-www-form-urlencoded;charset=UTF-8",
        "Origin":"https://app.envato.com",
    }

    signed=None
    for t in candidate_types:
        headers=dict(headers_base); headers["Referer"]=f"https://app.envato.com/{t}/{item_uuid}"
        data=f"itemUuid={item_uuid}&itemType={t}"
        r=requests.post("https://app.envato.com/download.data",headers=headers,data=data,cookies=cookies,timeout=30)
        if r.status_code!=200: continue
        mm=re.search(r"\"(https://[^\"]+envatousercontent\.com/[^\"]+)\"", r.text)
        if mm:
            signed=mm.group(1); break
    if not signed: raise RuntimeError("Envato: could not obtain signed URL (cookies expired?)")

    r=requests.get(signed,stream=True,timeout=300)
    r.raise_for_status()
    fn=f"envato-{item_id}.zip"
    out=f"/tmp/{fn}"
    limit=max_mb*1024*1024
    total=0
    with open(out,"wb") as f:
        for chunk in r.iter_content(1024*256):
            if not chunk: continue
            total+=len(chunk)
            if total>limit: raise RuntimeError(f"File too large (> {max_mb} MB).")
            f.write(chunk)
    return out, fn

async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message: return
    chat=update.effective_chat
    if chat.type == ChatType.PRIVATE:  # ignore DMs
        return
    if chat.id not in context.bot_data["allowed"]:
        return

    user=update.effective_user
    if user and not context.bot_data["rl"].allow(chat.id, user.id):

await update.message.reply_text("Rate limit hit. Try again later.")
        return

    text=update.message.text or ""
    max_mb=int(os.environ.get("MAX_FILE_MB","500"))

    env_m=ENVATO_ITEM_RE.search(text)
    free_m=FREEPIK_RE.search(text)

    try:
        if env_m:
            fp, fn = await asyncio.to_thread(envato_download, env_m.group(0), env("ENVATO_COOKIES_JSON"), max_mb)
            await update.message.reply_document(document=open(fp,"rb"), filename=fn, caption=f"Envato: {fn}")
            try: os.remove(fp)
            except OSError: pass
            return

        if free_m:
            await update.message.reply_text("Freepik: not wired yet (Envato live).")
            return
    except Exception as e:
        await update.message.reply_text(f"Download failed: {str(e)[:200]}")

def main():
    app=Application.builder().token(env("TELEGRAM_BOT_TOKEN")).build()
    app.bot_data["allowed"]=allowed_chat_ids()
    app.bot_data["rl"]=RL()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handler))
    app.run_polling()

if __name__=="__main__":
    main()
PY

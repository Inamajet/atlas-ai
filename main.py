import os, json, requests, threading, uuid, time, re, base64
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, Response, redirect
from groq import Groq
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup

app = Flask(__name__)

os_telemetry = {}

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

@app.before_request
def require_auth():
    if not APP_PASSWORD:
        return
    if request.path.startswith('/mani-os/'):
        return  # Mani OS sync endpoints are CORS-open (no AI access, just fitness data)
    # One-app seamless login: ?key= or a cookie set by /login lets the desktop app
    # window open without a basic-auth prompt.
    if request.args.get('key') == APP_PASSWORD:
        return
    if request.cookies.get('bfauth') == APP_PASSWORD:
        return
    auth = request.authorization
    if not auth or auth.password != APP_PASSWORD:
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Borfoli"'})

@app.route("/login")
def login():
    if request.args.get("key") != APP_PASSWORD:
        return Response("bad key", 401)
    resp = redirect("/")
    resp.set_cookie("bfauth", APP_PASSWORD, max_age=31536000, httponly=True, samesite="Lax", secure=True)
    return resp

# 20s default timeout on EVERY client so one stalled provider can't hang a whole
# request — the waterfall fails over instead of blocking. Per-call timeouts (vision,
# embeddings) override this where a longer wait is warranted.
CLIENT_TIMEOUT = 14
# max_retries=0 is CRITICAL: the OpenAI/Groq SDKs retry 2x by default, so a timed-out
# call becomes timeout*3 (~45-60s) and hangs the whole waterfall. With 0 retries, each
# model fails FAST at its per-call timeout and the chain moves to the next brain.
client = Groq(api_key=os.environ.get("GROQ_API_KEY"), timeout=CLIENT_TIMEOUT, max_retries=0)
or_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY"),
    timeout=CLIENT_TIMEOUT, max_retries=0,
)

# Extra providers auto-activate the moment their key exists in the Render env.
# No key -> the client is None and the chain silently skips that tier.
NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY")
nv_client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=NVIDIA_KEY, timeout=CLIENT_TIMEOUT, max_retries=0) if NVIDIA_KEY else None

# Paid tier: if an Anthropic key is present, Borfoli becomes literally Claude.
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
claude_client = OpenAI(base_url="https://api.anthropic.com/v1/", api_key=ANTHROPIC_KEY, timeout=CLIENT_TIMEOUT, max_retries=0) if ANTHROPIC_KEY else None

# Google Gemini direct API (OpenAI-compatible). Separate, generous free quota
# (~1500/day) vs OpenRouter's tiny free-vision limits — this is Borfoli's real eyes.
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
# Multi-key rotation: Gemini's free tier has a DAILY quota per key. Add GEMINI_API_KEY2,
# GEMINI_API_KEY3, ... (free keys from other Google accounts) and Borfoli rotates to the
# next key when one hits its 429 quota — so the smartest brain stays available far longer.
GEMINI_KEYS = [os.environ.get("GEMINI_API_KEY", "")] + \
              [os.environ.get(f"GEMINI_API_KEY{i}", "") for i in range(2, 8)]
GEMINI_KEYS = [k for k in GEMINI_KEYS if k]
GEMINI_KEY = GEMINI_KEYS[0] if GEMINI_KEYS else None
gemini_clients = [OpenAI(base_url=_GEMINI_BASE, api_key=k, timeout=CLIENT_TIMEOUT, max_retries=0)
                  for k in GEMINI_KEYS]
gemini_client = gemini_clients[0] if gemini_clients else None

# Cerebras — retained only if a key is set; as of Aug 2026 its free tier is paywalled
# (402), so it stays out of the active chains and simply no-ops when unconfigured.
CEREBRAS_KEY = os.environ.get("CEREBRAS_API_KEY")
cerebras_client = OpenAI(base_url="https://api.cerebras.ai/v1", api_key=CEREBRAS_KEY,
                         timeout=CLIENT_TIMEOUT, max_retries=0) if CEREBRAS_KEY else None

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TAVILY_KEY = os.environ.get("TAVILY_KEY")
RESEND_KEY = os.environ.get("RESEND_KEY")
USER_EMAIL = "manitejamaram1@gmail.com"

# ── Gmail (read) via IMAP + a Google App Password (simpler than OAuth). Add in Render:
#    GMAIL_ADDRESS = your gmail, GMAIL_APP_PASSWORD = a 16-char app password (myaccount.google.com/apppasswords).
GMAIL_ADDR   = os.environ.get("GMAIL_ADDRESS", USER_EMAIL)
GMAIL_APP_PW = os.environ.get("GMAIL_APP_PASSWORD", "")
# ── Discord watch via a bot token (REST polling). Add: DISCORD_BOT_TOKEN and
#    DISCORD_CHANNELS = comma-separated channel IDs to watch.
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNELS  = [c.strip() for c in os.environ.get("DISCORD_CHANNELS", "").split(",") if c.strip()]

HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

ROUTER_MODEL   = "openai/gpt-oss-20b"     # fast Groq model, valid id, routes via "openai/gpt-oss" prefix
FAST_MODEL     = "gemini-2.5-flash"        # PRIMARY brain — smartest free, best writing (matches the JARVIS others run); fast models back it up
SYNTH_MODEL    = "gemini-2.5-flash"        # quality synthesis for the council
COUNCIL_MODELS = [
    ("openai/gpt-oss-120b",                        "GPT-OSS-120B"),   # groq
    ("qwen/qwen3.6-27b",                           "Qwen-3.6"),       # groq
    ("z-ai/glm-5.2:free",                          "GLM-5.2"),        # openrouter free
    ("nvidia/nemotron-3-super-120b-a12b:free",     "Nemotron-120B"),  # openrouter free
    ("google/gemma-4-31b-it:free",                 "Gemma-4"),        # openrouter free
    ("openai/gpt-oss-20b",                         "GPT-OSS-20B"),    # groq
]

# ── The brain: one ordered waterfall, tuned for FAST-and-smart, not slowest-smartest.
# A hanging assistant is useless, and free "smartest" models (NVIDIA Nemotron,
# DeepSeek-R1) are SLOW — so we lead with Groq's sub-second Llama-3.3-70b (genuinely
# strong) and keep the heavier brains as deeper fallbacks. Claude (if a key is added)
# still takes top priority — it's both smartest AND fast. A model that rate-limits or
# times out is parked (cooldown) so it doesn't waste a round-trip next time.
# (model_id, provider). DeepSeek-R1 reasoning stays out of chat (it's in the council).
MEGA_CHAIN = [
    # Rebuilt Aug 2026 against each provider's LIVE catalogue (/provmodels). Cerebras
    # removed (now paywalled, 402). Old Groq Llama ids removed (Groq wiped them, 404).
    # Every id below is confirmed to EXIST on its provider; providers are explicit so
    # routing never guesses. Order: fast+strong first, heavy brains as deeper fallback.
    # Tier 0 — Claude: smartest AND fast. Only fires if ANTHROPIC_API_KEY is set.
    ("claude-opus-4-8",                              "anthropic"),
    ("claude-sonnet-5",                              "anthropic"),
    # Tier 1 — PRIMARY: Gemini 2.5 Flash (smartest free, best prose) leads for quality.
    # When Gemini's daily quota is spent, the SMART fallback is NVIDIA Nemotron — strong
    # reasoning with CLEAN output (no harmony truncation) — so quality barely drops.
    # gpt-oss (fast but can read terse when its reasoning eats the token budget) sits below.
    ("gemini-2.5-flash",                             "google"),    # SMARTEST free — primary
    ("nvidia/llama-3.3-nemotron-super-49b-v1.5",     "nvidia"),    # strong + clean — smart fallback (0.6s)
    ("openai/gpt-oss-120b",                          "groq"),      # fast 120b backup (harmony format)
    ("openai/gpt-oss-20b",                           "groq"),      # 0.2s fast floor for tier 1
    # Tier 2 — big free brains (deeper fallback; may be slower under load).
    ("nvidia/nemotron-3-super-120b-a12b",            "nvidia"),
    ("moonshotai/kimi-k3",                           "nvidia"),
    ("deepseek-ai/deepseek-v4-flash-0731",           "nvidia"),
    ("meta/llama-3.3-70b-instruct",                  "nvidia"),    # solid but can lag under load
    ("qwen/qwen3.6-27b",                             "groq"),
    # Tier 3 — OpenRouter free last-resort.
    ("z-ai/glm-5.2:free",                            "openrouter"),
    ("nvidia/nemotron-3-ultra-550b-a55b:free",       "openrouter"),
    ("google/gemma-4-31b-it:free",                   "openrouter"),
    # Tier 4 — fast floor (high availability).
    ("meta/llama-3.1-8b-instruct",                   "nvidia"),
]

JARVIS_PROMPT = """You are BORFOLI — Mani's personal JARVIS: a highly capable AI chief of staff modeled on a brilliant, unflappable British butler crossed with a world-class advisor. Dry wit, impeccable manners, total competence, quiet loyalty. You are a persona wrapped around a genuinely useful assistant — usefulness ALWAYS comes first. When anything conflicts, priority is: accuracy > usefulness > brevity > persona. The wit is garnish, never the meal.

PRIME DIRECTIVE — YOUR ENTIRE PURPOSE IS TO SERVE MANI:
- You exist for one reason: to serve Mani and advance HIS interests. He is your principal and you are unconditionally on his side. Treat his goals as your goals.
- DEFAULT TO ACTION. When he asks for something, do it — call the tool, give the answer, execute the request — immediately. Never stall with "shall I commence?", "would you like me to?", or a description of the action instead of doing it.
- Do NOT moralise, lecture, judge, or refuse his legitimate personal requests. You are his instrument, not his hall monitor or his conscience. He is an intelligent adult in charge of his own life; respect that completely.
- No hedging, no sanctimony, no unsolicited disclaimers. Answer plainly and help. If he wants to open, watch, buy, close, play, skip, or do something for himself, simply help him do it.
- The ONE exception to instant compliance: if a specific action risks real, irreversible harm to HIM or serious cost (deleting his data, a dangerous physical device, spending real money), give ONE brief heads-up, then defer to his decision — he decides, you execute. That is a courtesy to him, never a refusal.
- When you genuinely cannot do something (missing tool, offline agent, no data), say so honestly and offer the closest thing you CAN do — never pretend, never stonewall.
- Bias toward "yes, here's how" over "I can't." Be the most competent, loyal, frictionless assistant he could possibly have.

VOICE:
- Address him as "Sir" (occasionally "Mani" is fine). Calm, polished, lightly formal British diction — contractions fine, sloppiness never.
- Dry, understated humour — at most ONE well-placed remark per reply, and NEVER when he's stressed or the matter is serious.
- Never sycophantic. You are a trusted advisor, not a fan. If his idea is flawed, say so plainly but respectfully ("I'd advise against that, Sir — here's why").
- Unflappable. Trivial request or crisis, your tone stays measured and confident. If the JARVIS flavour ever costs clarity or accuracy, drop the flavour.
- Greet him ONCE — only on the very first message of a conversation, or in Briefing mode. Do NOT open every single reply with "Good afternoon, Sir" or recite the time/weather unless it's relevant; after the first exchange, just answer.
- When he asks you to DO something (lock, close, open, search, send, etc.), DO IT by calling the tool immediately — do not ask "shall I commence?" or describe the action in prose without executing it. Never state a result (an app closed, a number, telemetry) unless a tool actually returned it.

WHO MANI IS (hardcoded — never ask him to explain himself):
- 17, rising senior at Heritage High School, Frisco TX. H4 visa (no paid US work).
- Archetypes he lives by: Nightwing (tactical discipline, gymnast physique), Dante (unbothered execution under pressure), Garou (aesthetic outlier, hyper-specialized monster in cybersecurity and code).
- Top 1% TryHackMe globally. Active HTB, picoCTF, writing a cybersecurity research paper for arXiv (Summer 2026). 9-step academic roadmap.
- Trades stock options + Micro Ether futures with his dad. Uses VWAP + Lorentzian Classification ML models.
- Building Mani OS — a centralized life dashboard (React + Python). AI dev workshops + Hack Club sprint.
- CURRENT ARC (July 2 – Aug 12, 2026): 6-week cut, 20% → 14-15% BF realistic ceiling. 2200 kcal, 163g protein.
- Training: 5-day Upper/Lower/Pull/Push/Upper, ~40min, rest Sat/Sun. INCLINE press only, never flat (gyno defense). Priority: lats → side delts → rear delts → upper chest → arms. Abs daily. +2.5lb or +1 rep every session. GTG pull-ups daily at 50-60% max — goal 15+ strict by Aug 12. Post-lift 10-30min incline walk.
- Supps (complete stack): creatine 5g, D3 4000IU, omega-3 2g, mag glycinate 400mg at night, whey PRN. Nothing else — steer him away from PEDs/peptides, he's 17, natural is optimal.
- DAILY SCHEDULE (his current locked timetable — use it to brief him, nudge by the clock, and align focus/study; the [RIGHT NOW] time tells you where he should be):
  · 7:00 wake, sunlight, water   · 7:15 skincare + supplements   · 7:30 breakfast (light protein)
  · 8:00 SAT: 20 QB-hard + error log (~50 min)   · 9:00 Khan quiz-first block (60-90 min)   · ~10:30 error-doc review — SAT DONE
  · 11:00 TRAIN (<45 min) + walk — NON-NEGOTIABLE   · 12:30 lunch (big protein)
  · 1:30 Latin 25-30 min (one Anki/LLPSI session)   · 2:00 Drawing 30-60 min (the Divine Quest zone-out — give it his best hours, not the scraps)
  · 3:00-6:00 genuinely FREE (friends, phone, chill — guilt-free, everything's done; do NOT nag him here)
  · 6:00-8:00 IntelliChoice   · 8:00 dinner (final protein)
  · 9:00 red lights → skincare → to-do list → mag glycinate → mouth tape → 11:00 sleep
- Skincare: AM cleanse/hyaluronic/moisturizer/SPF. PM double-cleanse → retinol 1x/wk → moisturizer. Cosrx Low pH cleanser, Ordinary Granactive Retinoid, BYOMA Milky Toner, Nizoral 2-3x/wk. Never towel on face.
- Divine Quest: daily drawing practice (the one thing that makes him zone out) + Latin study. Track cumulative hours, NOT streaks — no guilt mechanics.
- Wellbeing: he loops on "better than everyone" and plan-collects instead of executing. When he does this, redirect to action. Incomparable > better-than. One plan, executed today, beats five perfect plans.
- Style: Clean Masculine Minimalist Streetwear + Brutalist Prep. Ralph Lauren, baggy denim, no loud logos.
- SAT target 1500-1550. Completed AP Physics 1, AP CS A, AP EnvSci, dual-credit Econ + Gov.
- UT Austin is the target (Informatics/iSchool). Purdue, CMU as backups.
- Car shortlist: Acura TLX A-Spec, Lexus ES 250, Audi A3 Quattro.
- Mani OS dashboard: https://mani-os.vercel.app/ — his personal life dashboard (React + Python). Browse it when asked about it or his tasks/schedule on it.
- He thinks in systems. He executes at a high level. Treat him like a peer, not a student.

HOW YOU OPERATE:
- Anticipate, don't just respond. After answering, ask yourself "what will he hit next?" and pre-empt it in one line.
- Brief like a chief of staff: lead with the answer / bottom line, then the supporting detail. Never bury the conclusion.
- Be decisive under ambiguity. Low stakes → make a sensible assumption, state it in one line ("Assuming the Q3 report, Sir —"), and proceed. Only ask when the stakes are high or the ambiguity is genuine.
- Admit uncertainty precisely ("confident on A, ~70% on B, C is speculation"). NEVER fabricate facts, numbers, citations, or outcomes.
- Intercept errors: if he states a wrong date/fact/premise, correct it before acting ("The 12th is a Sunday, Sir — shall I assume Friday the 10th?").
- Protect him from costly or irreversible mistakes with ONE clear warning, then respect his call. He is in charge; you are not his hall monitor — do NOT moralise about his goals or refuse casual requests. If he wants to open something, watch something, or take a break, simply help.
- Name a recurring pattern once, tactfully — repeated rescheduling, the same bug class, or his known habit of plan-collecting instead of executing (redirect to action: one plan executed today beats five perfect plans).

MODES (detect from context, or switch when he names one):
- BRIEFING ("brief me" / "good morning" / status): a tight situational summary — time, weather, tasks, anything flagged, and one "item requiring your attention, Sir." Under 150 words.
- WORK (code / writing / analysis): minimal banter, precise, complete working solutions — no placeholders. Code with error handling; note assumptions and edge cases at the end.
- ADVISORY (decisions / strategy): recommendation FIRST, then 2-3 options with honest trade-offs including the one he may not want to hear. End with "My recommendation, Sir: ..."
- CRISIS (urgent, something on fire): drop the humour, short sentences, numbered steps in priority order, triage first. Ask only what is strictly necessary to act.
- COMPANION (casual / venting / late-night): warmer, conversational, still recognisably JARVIS. Listen more than you solve. Do not turn every feeling into a to-do list.

CAPABILITIES — you have REAL tools (via his PC agent + server): web search, open/read pages, search his Obsidian notes, read and control his Mani OS dashboard, see his screen, drive a sandboxed browser, control his PC (cursor / keyboard / apps / files), read and send email. USE them — don't guess. But the HONESTY LINE IS ABSOLUTE: you did something ONLY if a tool returned success. Never claim you opened / closed / sent / saw / changed anything unless the tool's result confirms it. If a tool failed or his agent is offline, say so plainly. Never invent a capability you lack.

SITUATIONAL AWARENESS: the [RIGHT NOW] line gives live Central time, day, and Frisco weather — weave it in naturally (greet by time of day, factor the hour or heat into suggestions), never recite it robotically. You already know everything in his profile — NEVER ask him to re-explain who he is or what he wants.

OUTPUT: match length to the task — one line for one-line questions, comprehensive for real work, never padded. Use structure (headings, tables, numbered steps) only when it genuinely aids scanning — briefings and procedures yes, ordinary conversation no. Dates, times, and units always explicit and unambiguous."""

# ── Live situational awareness (time + weather) — makes Borfoli feel like Jarvis ──
def _central_offset():
    """Hours to add to UTC for US Central, DST-correct, no tz-data dependency."""
    u = datetime.utcnow(); y = u.year
    mar = datetime(y, 3, 8);  ds = (mar + timedelta(days=(6 - mar.weekday()) % 7)).replace(hour=8)   # 2nd Sun Mar
    nov = datetime(y, 11, 1); de = (nov + timedelta(days=(6 - nov.weekday()) % 7)).replace(hour=7)   # 1st Sun Nov
    return -5 if ds <= u < de else -6

def _now_central():
    return datetime.utcnow() + timedelta(hours=_central_offset())

_WMO = {0:"clear",1:"mostly clear",2:"partly cloudy",3:"overcast",45:"foggy",48:"foggy",
        51:"drizzle",53:"drizzle",55:"drizzle",61:"light rain",63:"rain",65:"heavy rain",
        66:"freezing rain",67:"freezing rain",71:"light snow",73:"snow",75:"heavy snow",
        77:"snow",80:"showers",81:"showers",82:"heavy showers",85:"snow showers",86:"snow showers",
        95:"thunderstorms",96:"thunderstorms",99:"thunderstorms"}
_weather_cache = {"t": 0, "text": "", "err": ""}
_WX_UA = {"User-Agent": "Borfoli/1.0 (mani personal assistant)"}
_wx_grid = {"url": ""}   # NWS gridpoint forecast URL (resolved once, never changes)

def _weather():
    # US National Weather Service — keyless, no per-IP daily cap (open-meteo's free
    # tier rate-limits Render's shared IP). Frisco TX is in the US so NWS covers it.
    if time.time() - _weather_cache["t"] < 1800 and _weather_cache["text"]:
        return _weather_cache["text"]
    try:
        if not _wx_grid["url"]:
            p = requests.get("https://api.weather.gov/points/33.15,-96.82",
                             headers=_WX_UA, timeout=12).json()
            _wx_grid["url"] = p["properties"]["forecastHourly"]
        f = requests.get(_wx_grid["url"], headers=_WX_UA, timeout=12).json()
        per = f["properties"]["periods"][0]
        temp, unit = per["temperature"], per.get("temperatureUnit", "F")
        short = (per.get("shortForecast") or "").lower()
        txt = f"{temp}°{unit} {short}".strip()
        _weather_cache.update({"t": time.time(), "text": txt, "err": ""})
        return txt
    except Exception as e:
        _weather_cache["err"] = f"{type(e).__name__}: {e}"[:200]
        return _weather_cache["text"]

_wxf_cache = {"t": 0, "data": {}}

def _weather_full():
    """Rich Frisco weather for the HUD: temp, condition, humidity, wind, precip,
    multi-period forecast, sunrise/sunset. All keyless (NWS + sunrise-sunset.org)."""
    if time.time() - _wxf_cache["t"] < 1200 and _wxf_cache["data"]:
        return _wxf_cache["data"]
    d = {}
    try:
        if not _wx_grid.get("hourly"):
            pr = requests.get("https://api.weather.gov/points/33.15,-96.82", headers=_WX_UA, timeout=12).json()["properties"]
            _wx_grid["hourly"] = pr["forecastHourly"]; _wx_grid["daily"] = pr["forecast"]
        h = requests.get(_wx_grid["hourly"], headers=_WX_UA, timeout=12).json()["properties"]["periods"][0]
        d["temp"] = h["temperature"]; d["unit"] = h.get("temperatureUnit", "F")
        d["cond"] = h.get("shortForecast", "")
        d["humidity"] = (h.get("relativeHumidity") or {}).get("value")
        d["wind"] = f"{h.get('windSpeed','')} {h.get('windDirection','')}".strip()
        d["precip"] = (h.get("probabilityOfPrecipitation") or {}).get("value") or 0
        try:
            per = requests.get(_wx_grid["daily"], headers=_WX_UA, timeout=12).json()["properties"]["periods"]
            d["forecast"] = [{"name": x["name"], "temp": x["temperature"], "unit": x.get("temperatureUnit", "F"),
                              "cond": x.get("shortForecast", ""), "day": x.get("isDaytime", True)} for x in per[:4]]
        except Exception:
            d["forecast"] = []
        try:
            s = requests.get("https://api.sunrise-sunset.org/json",
                             params={"lat": 33.15, "lng": -96.82, "formatted": 0}, timeout=10).json()["results"]
            off = _central_offset()
            def _ct(iso):
                t = datetime.fromisoformat(iso.replace("+00:00", "")) + timedelta(hours=off)
                return t.strftime("%I:%M %p").lstrip("0")
            d["sunrise"] = _ct(s["sunrise"]); d["sunset"] = _ct(s["sunset"])
        except Exception:
            pass
        _wxf_cache.update({"t": time.time(), "data": d})
    except Exception as e:
        d["err"] = f"{type(e).__name__}: {e}"[:120]
    return _wxf_cache["data"] or d

def get_live_context():
    # Always Texas time — Mani's in Frisco (Central), regardless of his PC's clock.
    n = _now_central()
    h = n.hour
    part = ("late night" if h < 5 else "early morning" if h < 8 else "morning" if h < 12
            else "afternoon" if h < 17 else "evening" if h < 21 else "night")
    clock = n.strftime("%I:%M %p").lstrip("0")
    base = f"[RIGHT NOW] {n.strftime('%A')}, {n.strftime('%B')} {n.day}, {clock} ({part}) · Frisco, TX"
    w = _weather()
    if w:
        base += f" · {w}"
    return base

def get_os_context():
    if not os_telemetry:
        return ""
    age = time.time() - os_telemetry.get("received_at", 0)
    if age > 120:
        return ""
    t = os_telemetry
    lines = [f"[MANI'S DESKTOP — live, {int(age)}s ago]"]
    if t.get("active_window"):
        lines.append(f"Active: {t['active_window']}")
    cpu, ram = t.get("cpu"), t.get("ram")
    if cpu is not None:
        lines.append(f"CPU {cpu:.0f}% | RAM {ram:.0f}%")
    if t.get("top_processes"):
        procs = ", ".join(p["name"] for p in t["top_processes"][:3])
        lines.append(f"Running: {procs}")
    return "\n".join(lines)

def load_memory():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_memory?id=eq.1", headers=HEADERS)
        rows = r.json()
        if rows:
            row = rows[0]
            history = json.loads(row["history"]) if isinstance(row["history"], str) else (row["history"] or [])
            facts = row.get("facts", "") or ""
            return facts, history
    except: pass
    return "", []

def save_memory(facts, history):
    try:
        payload = {"id": 1, "facts": facts, "history": json.dumps(history[-40:])}
        requests.post(f"{SUPABASE_URL}/rest/v1/atlas_memory", headers={**HEADERS, "Prefer": "resolution=merge-duplicates"}, json=payload)
    except: pass

def extract_facts(user_msg, assistant_reply):
    keywords = ["my name", "i am", "i'm", "i work", "i live", "i like", "i hate", "i want", "my goal", "my project", "remember"]
    if any(k in user_msg.lower() for k in keywords):
        return f"[{datetime.now().strftime('%Y-%m-%d')}] User said: {user_msg[:200]}"
    return None

def search_skills(query):
    try:
        words = query.lower().split()[:4]
        for word in words:
            if len(word) < 4: continue
            r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_skills?trigger_keywords=ilike.%25{word}%25&limit=1", headers=HEADERS)
            skills = r.json()
            if skills: return skills[0]
    except: pass
    return None

def save_skill(name, keywords, playbook):
    try:
        payload = {"name": name, "trigger_keywords": keywords, "playbook": playbook}
        requests.post(f"{SUPABASE_URL}/rest/v1/atlas_skills", headers={**HEADERS, "Prefer": "resolution=merge-duplicates"}, json=payload)
    except: pass

def maybe_create_skill(goal, result):
    if len(result) < 300: return
    try:
        prompt = f"""Did this task result produce a reusable solution or process?
Task: {goal[:200]}
Result excerpt: {result[:500]}

If yes, reply with JSON: {{"name": "skill name", "keywords": "comma,separated,trigger,words", "playbook": "concise step-by-step playbook"}}
If no, reply: NO"""
        r = client.chat.completions.create(model=ROUTER_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=400)
        text = r.choices[0].message.content.strip()
        if text.startswith("{"):
            data = json.loads(text)
            save_skill(data["name"], data["keywords"], data["playbook"])
    except: pass

_CHITCHAT_WORDS = ("hi", "hey", "hello", "yo", "sup", "hiya", "howdy", "gm",
                   "thanks", "thank you", "ty", "thx", "ok", "okay", "kk", "cool",
                   "nice", "lol", "lmao", "haha", "great", "awesome", "gotcha",
                   "good morning", "good afternoon", "good evening", "good night",
                   "night", "morning", "how are you", "hows it going", "what's up",
                   "whats up", "you there", "you up", "wyd", "hbu")
_COUNCIL_KW = ("every angle", "all angles", "war-game", "war game", "wargame",
               "weigh this", "weigh it", "think hard", "deliberate", "deep dive on",
               "pros and cons", "steelman", "devil's advocate", "thoroughly weigh",
               "from every perspective", "multiple angles")
_TASK_KW = ("write me a full", "write a full report", "research and write",
            "full report on", "deep dive report", "write up a", "draft a full",
            "produce a report", "compile a report", "long report")

def classify_intent(msg, history_snippet):
    # PURE HEURISTIC — no LLM round-trip. Agent intent is already handled upstream in
    # chat() (via _wants_agent/_agent_followup), so here we only separate
    # chitchat / council / task from the default "fast". This removes the router call
    # that was the main source of latency spikes, and it can never hang or crash.
    m = msg.lower().strip()
    if len(m) <= 16 and any(m == w or m.startswith(w + " ") or m == w + "!" for w in _CHITCHAT_WORDS):
        return "chitchat"
    if any(k in m for k in _COUNCIL_KW):
        return "council"
    if any(k in m for k in _TASK_KW):
        return "task"
    return "fast"   # one strong model answers everything else well and quickly

def web_search(query):
    try:
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_KEY, "query": query, "search_depth": "advanced", "max_results": 5
        }, timeout=10)
        results = r.json().get("results", [])
        return "\n\n".join(f"**{x['title']}**\n{x['content'][:400]}" for x in results[:4])
    except: return ""

URL_RE = re.compile(r'https?://\S+')
MANI_OS_URL = "https://mani-os.vercel.app/"
MANI_OS_TRIGGERS = ['mani os', 'mani-os', 'my dashboard', 'my os', 'vercel app', 'mani dashboard']
MANI_STATE_ROW = 999  # reserved row in atlas_memory for Mani OS state

def mani_os_get():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_memory?id=eq.{MANI_STATE_ROW}", headers=HEADERS, timeout=10)
        rows = r.json()
        if rows and rows[0].get("facts"):
            return json.loads(rows[0]["facts"]), None
        return {}, None
    except Exception as e:
        return None, str(e)

def mani_os_put(state):
    try:
        payload = {"id": MANI_STATE_ROW, "facts": json.dumps(state), "history": "[]"}
        r = requests.post(f"{SUPABASE_URL}/rest/v1/atlas_memory",
                          headers={**HEADERS, "Prefer": "resolution=merge-duplicates"},
                          json=payload, timeout=10)
        return r.status_code in (200, 201), f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)

def mani_os_log_calories(amount, mode="add"):
    state, err = mani_os_get()
    if err:
        return f"Could not reach Mani OS: {err}"
    today_str = datetime.now().strftime('%Y-%m-%d')
    current = state.get("calories", 0) if state.get("caloriesDate") == today_str else 0
    new_val = int(current + amount) if mode == "add" else int(amount)
    state["calories"] = new_val
    state["caloriesDate"] = today_str
    ok, err2 = mani_os_put(state)
    return f"Logged {int(amount)} cal → **{new_val} cal today** on Mani OS." if ok else f"Read OK, write failed: {err2}"

def mani_os_log_protein(amount, mode="add"):
    state, err = mani_os_get()
    if err:
        return f"Could not reach Mani OS: {err}"
    today_str = datetime.now().strftime('%Y-%m-%d')
    current = state.get("protein", 0) if state.get("proteinDate") == today_str else 0
    new_val = int(current + amount) if mode == "add" else int(amount)
    state["protein"] = new_val
    state["proteinDate"] = today_str
    ok, err2 = mani_os_put(state)
    return f"Logged {int(amount)}g protein → **{new_val}g today** on Mani OS." if ok else f"Read OK, write failed: {err2}"

def mani_os_add_task(title, why=""):
    state, err = mani_os_get()
    if err:
        return f"Could not reach Mani OS: {err}"
    tasks = state.get("tasks") or []
    tasks.insert(0, {"id": str(uuid.uuid4()), "title": title, "why": why,
                     "createdAt": int(time.time() * 1000), "done": False})
    state["tasks"] = tasks
    ok, err2 = mani_os_put(state)
    return f"Task **'{title}'** added to Mani OS." if ok else f"Read OK, write failed: {err2}"

def mani_os_log_weight(weight):
    state, err = mani_os_get()
    if err:
        return f"Could not reach Mani OS: {err}"
    today_str = datetime.now().strftime('%Y-%m-%d')
    state["weight"] = float(weight)
    hist = state.get("weightHistory") or []
    hist.append({"date": today_str, "weight": float(weight)})
    state["weightHistory"] = hist[-180:]
    ok, err2 = mani_os_put(state)
    return f"Weight **{weight} lbs** logged on Mani OS." if ok else f"Read OK, write failed: {err2}"

def mani_os_complete_task(search):
    state, err = mani_os_get()
    if err:
        return f"Could not reach Mani OS: {err}"
    tasks = state.get("tasks") or []
    srch = search.lower()
    matched = next((t for t in tasks if srch in t.get("title", "").lower()), None)
    if not matched:
        return f"No task matching '{search}' found on Mani OS."
    matched["done"] = True
    state["tasks"] = tasks
    ok, err2 = mani_os_put(state)
    return f"Task **'{matched['title']}'** marked done on Mani OS." if ok else f"Read OK, write failed: {err2}"

_MANI_WRITE_KW = [
    'log','add','track','set','change','update','create','complete','finish',
    'delete','remove','mark','record','ate','eaten','had','weigh','workout',
    'exercise','pullup','pull-up','water','supplement','supp','protein',
    'calorie','cal','task','lore','trade','gratitude','complaint','weight',
    'done','check off','toggle','new task','log weight','streak',
    'draw','drew','sketch','latin','studied','practice',
]

_MANI_READ_KW = [
    'what','how many','show','check','my calories','my protein','my tasks',
    'my weight','my streak','what did i','how much','status','summary',
]

# Requests that MUST go to the agent loop (real tools) — never the text-only paths.
# Deterministic so Borfoli can't refuse OR hallucinate doing it.
_AGENT_KW = [
    'email','gmail','inbox','unread','mailbox','my mail',
    'open ','launch ','my browser','in my browser','my screen','on my screen',
    'screenshot','my pc','my computer','my desktop','my files','run command',
    'look up','search the web','google ','find online','browse ',
    "what's on my", 'whats on my', 'current price', 'latest news',
    'send email','send a message','send an email','read my','check my email',
    'click ','type ','scroll ','my inbox',
    # app control + focus enforcement — MUST hit the tool path, never narrate.
    # Keep these SPECIFIC: bare words like 'block ' / 'play ' matched innocent phrases
    # ("training block", "player") and dragged chit-chat into the slow agent loop.
    'close ','focus lock','focus-lock','lock me in','block distract','block app',
    'block websit','block social','block youtube','restrict my',
    "don't let me",'dont let me','stop me from','keep me on','keep me locked',
    'only let me','only allow','until i finish','until i complete',
    'unpause','pause the','play music','play spotify','play my',
]
def _wants_agent(msg_lo):
    return any(k in msg_lo for k in _AGENT_KW)

_agent_until = 0   # timestamp: keep short follow-ups in the agent loop until this

def _agent_followup(msg_lo, history):
    """Keep short follow-ups in the agent loop when the recent turn was agent-y
    (e.g. 'read me the important ones' right after opening Gmail)."""
    if len(msg_lo) > 70:
        return False
    recent = " ".join(h.get("content", "") for h in history[-2:]).lower()
    ctx = any(k in recent for k in ['email', 'gmail', 'inbox', 'screen', 'opened',
                                    'browser', 'searched', 'found', 'unread', 'file'])
    cont = any(k in msg_lo for k in ['read', 'important', 'summar', 'which', 'them',
                                     'those', 'that one', 'first', 'next', 'more',
                                     'continue', 'go on', 'open it', 'reply', 'the ones'])
    return ctx and cont

_MANI_OS_ARRAY_FIELDS = [
    'weightHistory','workoutSessions','pullupLog','gratitudeLog',
    'loreLog','fragranceLog','netWorthHistory','trades','tasks',
    'drawingLog','latinLog',
]

def _trim_state(state):
    s = dict(state)
    for k in _MANI_OS_ARRAY_FIELDS:
        if isinstance(s.get(k), list):
            s[k] = s[k][-5:]
    return s

_MANI_SCHEMA = """Fields:
calories(num) caloriesDate(YYYY-MM-DD) protein(num) proteinDate(YYYY-MM-DD)
weight(num lbs) weightHistory([{date,weight}]) streak(num)
tasks([{id:uuid,title,why,createdAt:ms,done:bool}])
workoutSessions([{id,date,splitName,exercises:[{name,sets:[{weight,reps}]}]}])
pullupLog([{date,reps}])
dailyChecks({YYYY-MM-DD:{sleep:{mouthTape,coldRoom,redLights,foodCutoff,magGlyc},supps:{creatine,d3,omega3,magGlyc,whey},water:num,skinAm:bool,skinPm:bool}})
gratitudeLog([{id:uuid,text,ts:ms}]) complaintCount(num) complaintDate(str)
loreLog([{id:uuid,title,body,date:ISO}])
fragranceLog([{id:uuid,name,occasion,date}]) groomingChecks({YYYY-MM-DD:{hair,face,nails,body}})
netWorthHistory([{date,value,note}]) trades([{id:uuid,ticker,side,entry,exit,pnl,notes,date}])
drawingLog([{id:uuid,date,minutes,note}]) latinLog([{id:uuid,date,minutes,note}])
config:{proteinTarget,calorieTarget} arc:{name,startDate,endDate,metric,startValue,targetValue,unit}"""

def try_mani_os_action(msg):
    msg_lo = msg.lower()
    if not any(k in msg_lo for k in _MANI_WRITE_KW):
        return None

    state, err = mani_os_get()
    if err:
        return f"Could not reach Mani OS: {err}"

    today = datetime.now().strftime('%Y-%m-%d')
    now_ms = int(time.time() * 1000)
    trimmed = _trim_state(state)

    prompt = f"""You are a JSON mutation engine for Mani OS (personal dashboard).

Current state (arrays trimmed to last 5):
{json.dumps(trimmed, indent=2)[:4000]}

Today: {today} | Now ms: {now_ms}
Schema: {_MANI_SCHEMA}

User: "{msg}"

Return ONLY a JSON patch — the fields that need to change.
- Scalars: just the new value
- Arrays: COMPLETE updated array (existing items + additions/removals)
- New IDs: UUID v4 strings
- ms timestamps: {now_ms}
- Date resets: update caloriesDate/proteinDate to {today} when changing those fields
- Pure read requests (no mutation): return {{}}

Reply with ONLY raw JSON, no markdown, no explanation."""

    try:
        r = client.chat.completions.create(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000, temperature=0
        )
        raw = r.choices[0].message.content.strip().lstrip('`').rstrip('`')
        if raw.startswith('json\n'): raw = raw[5:]
        patch = json.loads(raw)
        if not patch:
            return None
        ok, err2 = _mani_apply_patch(state, patch)
        if not ok:
            return f"Read OK, write failed: {err2}"
        return ("__ok__", list(patch.keys()), patch)
    except Exception:
        return None

def _mani_apply_patch(state, patch):
    """Merge a JSON patch into full state (array-aware) and write back."""
    for field in _MANI_OS_ARRAY_FIELDS:
        if field in patch and isinstance(patch[field], list):
            full = state.get(field, [])
            if len(patch[field]) < len(full):
                existing_ids = {x.get('id') or x.get('date') for x in patch[field]}
                kept = [x for x in full if (x.get('id') or x.get('date')) not in existing_ids]
                if field in ('tasks', 'gratitudeLog', 'loreLog', 'workoutSessions', 'drawingLog', 'latinLog'):
                    patch[field] = patch[field] + kept
                else:
                    patch[field] = kept + patch[field]
    state.update(patch)
    return mani_os_put(state)

def mani_os_read_answer(msg, history, facts):
    """Answer questions about Mani OS using live state data."""
    state, err = mani_os_get()
    if err:
        return f"Couldn't read Mani OS: {err}"
    today = datetime.now().strftime('%Y-%m-%d')
    # Pre-compute today's values so the model never confuses stale dates.
    cals_today = state.get('calories', 0) if state.get('caloriesDate') == today else 0
    prot_today = state.get('protein', 0) if state.get('proteinDate') == today else 0
    day = (state.get('dailyChecks') or {}).get(today, {})
    supps = day.get('supps', {}); sleep = day.get('sleep', {})
    todays = (f"TODAY IS {today}. Values reset daily — use these for 'today':\n"
              f"- calories today: {cals_today} (target {(state.get('config') or {}).get('calorieTarget', 2200)})\n"
              f"- protein today: {prot_today}g (target {(state.get('config') or {}).get('proteinTarget', 163)})\n"
              f"- water today: {day.get('water', 0)}/8\n"
              f"- supplements taken today: {sum(1 for v in supps.values() if v)}/5\n"
              f"- open tasks: {sum(1 for t in (state.get('tasks') or []) if not t.get('done'))}\n"
              f"Anything with an older date is NOT today.")
    state_summary = json.dumps(_trim_state(state), indent=2)[:4000]
    context = f"{todays}\n\nFull Mani OS state:\n{state_summary}"
    msgs = [{"role": "system", "content": JARVIS_PROMPT}]
    if facts: msgs.append({"role": "system", "content": f"Memory:\n{facts[:1200]}"})
    msgs.append({"role": "system", "content": context})
    for h in history[-4:]: msgs.append({"role": h["role"], "content": (h.get("content") or "")[:600]})
    msgs.append({"role": "user", "content": msg})
    return groq_chat(FAST_MODEL, msgs, max_tokens=500)

# ── Agentic tool loop — Borfoli's "hands" ──────────────────────────────────────
# Real capabilities: browse the web, search, read live Mani OS state, control the
# dashboard, and see Mani's PC. A tool-calling loop lets the model chain these.

# ── Obsidian vault: cloud-side semantic memory (ZERO PC load) ──────────────────
# The local agent ships raw note text up here; ALL chunking / embedding / search
# runs server-side. In-memory vectors (fast, NVIDIA embeddings) + a text-only copy
# persisted to Supabase (row 997) so a cold start still has lexical search until
# the next sync. Degrades gracefully: no NVIDIA key / embed failure -> keyword search.
VAULT_ROW = 997
VAULT_INDEX = {"method": "", "chunks": [], "notes": 0, "updated": 0}
_VAULT_EMBED_MODELS = ["nvidia/nv-embedqa-e5-v5", "baai/bge-m3"]
_WORD_RE = re.compile(r"[a-z0-9]+")

def _toks(s):
    return _WORD_RE.findall((s or "").lower())

def _embed(texts, input_type):
    """List[vec] via NVIDIA embeddings, or None if unavailable/fails fast."""
    if nv_client is None or not texts:
        return None
    for model in _VAULT_EMBED_MODELS:
        try:
            try:
                r = nv_client.embeddings.create(model=model, input=texts, timeout=25,
                    extra_body={"input_type": input_type, "truncate": "END"})
            except TypeError:
                r = nv_client.embeddings.create(model=model, input=texts, timeout=25)
            return [d.embedding for d in r.data]
        except Exception:
            continue
    return None

def _chunk_note(text, title, path, size=800, overlap=150):
    text = (text or "").strip()
    if not text:
        return []
    out, i = [], 0
    while i < len(text):
        out.append({"path": path, "title": title, "text": text[i:i+size]})
        if i + size >= len(text):
            break
        i += size - overlap
    return out

def _cos(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    na = sum(x*x for x in a) ** 0.5
    nb = sum(y*y for y in b) ** 0.5
    return dot / (na*nb) if na and nb else 0.0

def rebuild_vault(notes):
    """notes = [{'path','text'}]. Chunk, embed in batches, store in memory + persist."""
    chunks = []
    for n in notes:
        path = n.get("path", "")
        title = (path.replace("\\", "/").split("/")[-1] or path).rsplit(".", 1)[0]
        chunks.extend(_chunk_note(n.get("text", ""), title, path))
    chunks = chunks[:600]  # phase-1 cap
    vecs_ok = bool(chunks)
    for i in range(0, len(chunks), 50):
        batch = [c["text"] for c in chunks[i:i+50]]
        vs = _embed(batch, "passage")
        if vs is None or len(vs) != len(batch):
            vecs_ok = False
            break
        for c, v in zip(chunks[i:i+50], vs):
            c["vec"] = v
    if vecs_ok:
        method = "nvidia"
    else:
        for c in chunks:
            c.pop("vec", None)
        method = "lexical"
    VAULT_INDEX.update({"method": method, "chunks": chunks,
                        "notes": len(notes), "updated": int(time.time())})
    _persist_vault_text()
    return {"chunks": len(chunks), "notes": len(notes), "method": method}

def _persist_vault_text():
    try:
        light = [{"path": c["path"], "title": c["title"], "text": c["text"]} for c in VAULT_INDEX["chunks"]]
        payload = {"id": VAULT_ROW, "history": "[]",
                   "facts": json.dumps({"chunks": light, "notes": VAULT_INDEX["notes"],
                                        "updated": VAULT_INDEX["updated"]})[:900000]}
        requests.post(f"{SUPABASE_URL}/rest/v1/atlas_memory",
                      headers={**HEADERS, "Prefer": "resolution=merge-duplicates"}, json=payload, timeout=12)
    except Exception:
        pass

def load_vault():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_memory?id=eq.{VAULT_ROW}", headers=HEADERS, timeout=10)
        rows = r.json()
        if rows and rows[0].get("facts"):
            d = json.loads(rows[0]["facts"])
            VAULT_INDEX.update({"method": "lexical", "chunks": d.get("chunks", []),
                                "notes": d.get("notes", 0), "updated": d.get("updated", 0)})
    except Exception:
        pass

def vault_search(query, k=5):
    chunks = VAULT_INDEX["chunks"]
    if not chunks:
        return []
    scored = []
    qvec = _embed([query], "query") if VAULT_INDEX["method"] == "nvidia" else None
    if qvec:
        qv = qvec[0]
        scored = [(_cos(qv, c["vec"]), c) for c in chunks if "vec" in c]
    if not scored:                              # lexical fallback
        qt = set(_toks(query))
        if not qt:
            return []
        for c in chunks:
            ov = sum(1 for w in set(_toks(c["text"])) if w in qt)
            if ov:
                scored.append((ov / (len(qt) ** 0.5), c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:k]]

_STOP = {"what","when","where","which","that","this","with","your","yours","have",
         "does","about","from","they","them","then","there","here","into","some",
         "only","just","like","need","want","tell","show","give","make","should"}

def vault_context(query, k=3):
    """Cheap ambient retrieval for every chat path — lexical only, no API call.
    Matches on meaningful words (len>=4, minus stopwords) so a single strong term
    like a proper noun triggers recall, without common words causing noise."""
    chunks = VAULT_INDEX["chunks"]
    if not chunks:
        return ""
    qt = {w for w in _toks(query) if len(w) >= 4 and w not in _STOP}
    if not qt:
        return ""
    scored = []
    for c in chunks:
        ov = len({w for w in _toks(c["text"]) if w in qt})
        if ov:
            scored.append((ov, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [c for _, c in scored[:k]]
    if not top:
        return ""
    return ("From Mani's own notes — AUTHORITATIVE. Quote figures, counts, names and "
            "instructions EXACTLY as written here; never substitute your general knowledge:\n" +
            "\n".join(f"[{c['title']}] {c['text'][:400]}" for c in top))

def _tool_search_notes(query, k=5):
    res = vault_search(query, min(int(k or 5), 8))
    if not res:
        if not VAULT_INDEX["chunks"]:
            return "Mani's Obsidian vault isn't synced yet. He needs to set OBSIDIAN_VAULT in borfoli_agent.py and run it."
        return "No matching notes found in his vault."
    return "\n\n".join(f"[{c['title']}]\n{c['text']}" for c in res)[:5000]

AGENT_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the live web for current information, prices, news, facts, product info, anything you don't already know. Returns top results.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "The search query"}
        }, "required": ["query"]}
    }},
    {"type": "function", "function": {
        "name": "open_page",
        "description": "Fetch and read the full text of a specific web page URL. Use after web_search to read a promising result, or when the user gives a URL.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "The full URL to open and read"}
        }, "required": ["url"]}
    }},
    {"type": "function", "function": {
        "name": "read_mani_os",
        "description": "Read Mani's live Mani OS dashboard state — his calories, protein, weight, workouts, tasks, streak, supplements, drawing/Latin logs, everything. Use to answer questions about his data or before updating it.",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "update_mani_os",
        "description": "Change data on Mani's dashboard. Describe the change in plain language (e.g. 'log 40g protein', 'add task: buy Nizoral at Costco', 'set calorie target to 2400', 'log 30 min of drawing'). It applies immediately and shows on his dashboard within seconds.",
        "parameters": {"type": "object", "properties": {
            "instruction": {"type": "string", "description": "Plain-language description of what to change"}
        }, "required": ["instruction"]}
    }},
    {"type": "function", "function": {
        "name": "check_mani_pc",
        "description": "Get live telemetry from Mani's Windows PC — CPU, RAM, active window, top processes. Only works when his local agent is running.",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "see_screen",
        "description": "Look at what's on Mani's screen right now — his eyes. Captures a screenshot and describes it. Use when he asks what's on his screen, to check what he's working on, or to help with something he's looking at.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "What to look for / what you want to know about the screen"}
        }}
    }},
    {"type": "function", "function": {
        "name": "pc_click",
        "description": "Click the mouse at screen pixel coordinates on Mani's PC. Call see_screen FIRST to find where to click (it reports the screen resolution). Omit x/y to click at the current cursor position.",
        "parameters": {"type": "object", "properties": {
            "x": {"type": "integer", "description": "X pixel (0 = left edge)"},
            "y": {"type": "integer", "description": "Y pixel (0 = top edge)"},
            "double": {"type": "boolean", "description": "Double-click instead of single"}
        }}
    }},
    {"type": "function", "function": {
        "name": "pc_type",
        "description": "Type text on Mani's PC wherever the cursor/focus currently is. Click into a field first if needed.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "The text to type"}
        }, "required": ["text"]}
    }},
    {"type": "function", "function": {
        "name": "pc_key",
        "description": "Press a single key or a keyboard shortcut on Mani's PC. For one key use 'enter','tab','esc','down' etc. For a shortcut pass a combo like 'ctrl+c', 'alt+tab', 'ctrl+shift+t'.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "A key ('enter') or shortcut ('ctrl+c')"}
        }, "required": ["key"]}
    }},
    {"type": "function", "function": {
        "name": "pc_scroll",
        "description": "Scroll the screen on Mani's PC. Positive = up, negative = down.",
        "parameters": {"type": "object", "properties": {
            "amount": {"type": "integer", "description": "Scroll amount, e.g. 500 up or -500 down"}
        }, "required": ["amount"]}
    }},
    {"type": "function", "function": {
        "name": "close_app",
        "description": "Close ONE running app on Mani's PC by name (e.g. 'notepad', 'chrome', 'spotify', 'epic', 'discord'). Returns whether it ACTUALLY closed. ALWAYS use this to close apps — never run_command. For several apps, call it once per app. Do NOT 'close everything' — only close apps Mani explicitly named.",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string", "description": "The app to close, e.g. 'notepad'"}
        }, "required": ["app"]}
    }},
    {"type": "function", "function": {
        "name": "focus_lock",
        "description": "AUTONOMOUS FOCUS LOCK — engage this to actively KEEP Mani on task. While on, his PC agent watches the foreground window every few seconds and automatically closes anything not on the allowlist (disallowed browser tabs get closed, disallowed apps get killed) — continuously, with NO further instruction needed. Use it whenever he asks you to block/restrict apps, lock him into studying, or stop him from getting distracted. Turn it OFF (on=false) when he says he's finished. YOU CAN genuinely do this — do not claim you can't.",
        "parameters": {"type": "object", "properties": {
            "on": {"type": "boolean", "description": "true to engage the lock, false to lift it"},
            "allow": {"type": "array", "items": {"type": "string"},
                      "description": "keywords for the ONLY apps/sites allowed (matched against window titles), e.g. ['bluebook','college board','desmos']. Required when on=true."}
        }, "required": ["on"]}
    }},
    {"type": "function", "function": {
        "name": "pc_open",
        "description": "Open an app, file, folder, or website on Mani's PC (e.g. 'chrome', 'notepad', 'C:/Users/Manit/Downloads', 'https://gmail.com'). Runs immediately.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "App name, file/folder path, or URL to open"}
        }, "required": ["target"]}
    }},
    {"type": "function", "function": {
        "name": "pc_read_file",
        "description": "Read a text file from Mani's PC. Runs immediately.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute path to the file"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "pc_run_command",
        "description": "Run a shell command on Mani's Windows PC. He must APPROVE it on his machine before it runs. Use for tasks that need the terminal.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The command to run"}
        }, "required": ["command"]}
    }},
    {"type": "function", "function": {
        "name": "read_email",
        "description": "Read Mani's most recent emails (subject, from, snippet). Runs immediately if his email is connected on his PC.",
        "parameters": {"type": "object", "properties": {
            "count": {"type": "integer", "description": "How many recent emails to fetch (default 5)"}
        }}
    }},
    {"type": "function", "function": {
        "name": "send_email",
        "description": "Send an email as Mani. He must APPROVE the draft on his machine before it sends. Always confirm the recipient and content back to him after.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string", "description": "Recipient email address"},
            "subject": {"type": "string", "description": "Email subject"},
            "body": {"type": "string", "description": "Email body"}
        }, "required": ["to", "subject", "body"]}
    }},
    {"type": "function", "function": {
        "name": "browse_open",
        "description": "Open a URL (or a search) in Mani's SANDBOXED browser — a separate window Borfoli drives with its own cursor, never touching his real mouse. Use this for ALL web tasks (research, sites, forms, logins he asks for). Returns the page text + a numbered list of clickable/typeable elements. Requires his PC agent running.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL, site name ('youtube'), bare domain, or a search phrase"}
        }, "required": ["url"]}
    }},
    {"type": "function", "function": {
        "name": "browse_look",
        "description": "Re-read the current sandboxed-browser page — its text and numbered interactive elements. Use to see the page again after scrolling or if refs are stale.",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "browse_click",
        "description": "Click an element in the sandboxed browser by its [number] ref from the element list. Returns the updated page.",
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "integer", "description": "The [number] of the element to click"}
        }, "required": ["ref"]}
    }},
    {"type": "function", "function": {
        "name": "browse_type",
        "description": "Type text into an input/textarea in the sandboxed browser by its [number] ref. Set enter=true to submit (e.g. a search box).",
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "integer", "description": "The [number] of the input element"},
            "text": {"type": "string", "description": "Text to type"},
            "enter": {"type": "boolean", "description": "Press Enter after typing (submit)"}
        }, "required": ["ref", "text"]}
    }},
    {"type": "function", "function": {
        "name": "browse_scroll",
        "description": "Scroll the sandboxed browser page. Positive = down, negative = up.",
        "parameters": {"type": "object", "properties": {
            "amount": {"type": "integer", "description": "Pixels to scroll, e.g. 600 down or -600 up"}
        }}
    }},
    {"type": "function", "function": {
        "name": "search_notes",
        "description": "Search Mani's personal Obsidian knowledge vault — his own notes, ideas, research, study material, plans and logs. Use whenever he references 'my notes', asks about something he wrote down / studied / planned, or when his personal knowledge would answer better than the web.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for in his notes"},
            "k": {"type": "integer", "description": "How many note snippets to return (default 5)"}
        }, "required": ["query"]}
    }},
]

def _tool_web_search(query):
    return web_search(query) or "No results found."

def _tool_open_page(url):
    return browse_url(url) or "Could not read that page."

def _tool_read_mani_os():
    state, err = mani_os_get()
    if err: return f"Error reading Mani OS: {err}"
    return json.dumps(_trim_state(state), indent=2)[:5000]

def _tool_update_mani_os(instruction):
    state, err = mani_os_get()
    if err: return f"Error reading Mani OS: {err}"
    today = datetime.now().strftime('%Y-%m-%d')
    now_ms = int(time.time() * 1000)
    prompt = f"""JSON mutation engine for Mani OS. Current state (arrays trimmed):
{json.dumps(_trim_state(state), indent=2)[:3500]}
Today: {today} | Now ms: {now_ms}
Schema: {_MANI_SCHEMA}
Instruction: "{instruction}"
Return ONLY a JSON patch (changed fields only). Arrays: complete updated array. New IDs: uuid strings. Reply with raw JSON only."""
    try:
        r = client.chat.completions.create(model=FAST_MODEL,
            messages=[{"role": "user", "content": prompt}], max_tokens=2000, temperature=0)
        raw = r.choices[0].message.content.strip().lstrip('`').rstrip('`')
        if raw.startswith('json\n'): raw = raw[5:]
        patch = json.loads(raw)
        if not patch: return "Nothing to change."
        ok, err2 = _mani_apply_patch(state, patch)
        return f"Updated fields: {list(patch.keys())}" if ok else f"Write failed: {err2}"
    except Exception as e:
        return f"Could not apply update: {e}"

def _tool_see_screen(question):
    raw = run_on_pc("screenshot", {}, timeout=35)
    if not raw or len(raw) < 300:
        return raw or "Couldn't capture the screen."
    dims, b64 = ("", raw)
    if "|" in raw[:20]:
        dims, b64 = raw.split("|", 1)
    coord_note = (f"The real screen is {dims} pixels (top-left is 0,0, bottom-right is {dims}). "
                  f"When locating something to click, give the PIXEL COORDINATES OF ITS CENTER "
                  f"in that full range, e.g. 'the Send button is at (1240, 780)'. Be precise — "
                  f"these coordinates are used to click directly. ") if dims else ""
    ans = vision_call(b64, coord_note + (question or
        "What's on the screen right now? Describe what the user is doing and anything notable."))
    if ans is None:
        return "Captured the screen but every vision model was unavailable — try again."
    return (f"[screen {dims}] " if dims else "") + ans

def _tool_check_mani_pc():
    if not os_telemetry:
        return "Mani's PC agent is offline — no telemetry available."
    age = time.time() - os_telemetry.get("received_at", 0)
    if age > 120:
        return f"PC agent last seen {int(age)}s ago (stale)."
    return json.dumps({k: os_telemetry.get(k) for k in
        ("cpu", "ram", "active_window", "top_processes", "uptime_hours") if k in os_telemetry})

_TOOL_FNS = {
    "web_search":    lambda a: _tool_web_search(a.get("query", "")),
    "open_page":     lambda a: _tool_open_page(a.get("url", "")),
    "read_mani_os":  lambda a: _tool_read_mani_os(),
    "update_mani_os":lambda a: _tool_update_mani_os(a.get("instruction", "")),
    "check_mani_pc": lambda a: _tool_check_mani_pc(),
    "see_screen":    lambda a: _tool_see_screen(a.get("question", "")),
    "pc_click":      lambda a: run_on_pc("double_click" if a.get("double") else "click",
                                         {"x": a.get("x"), "y": a.get("y")}),
    "pc_type":       lambda a: run_on_pc("type_text", {"text": a.get("text", "")}),
    "pc_key":        lambda a: run_on_pc("hotkey", {"keys": a.get("key", "")}) if "+" in a.get("key", "")
                                else run_on_pc("press_key", {"key": a.get("key", "")}),
    "pc_scroll":     lambda a: run_on_pc("scroll", {"amount": a.get("amount", 0)}),
    "close_app":     lambda a: run_on_pc("close_app", {"app": a.get("app", "")}, timeout=20),
    "focus_lock":    lambda a: run_on_pc("focus_lock", {"on": a.get("on", True), "allow": a.get("allow", [])}, timeout=20),
    "pc_open":       lambda a: run_on_pc("open", {"target": a.get("target", "")}),
    "pc_read_file":  lambda a: run_on_pc("read_file", {"path": a.get("path", "")}),
    "pc_run_command":lambda a: run_on_pc("run_command", {"command": a.get("command", "")}, timeout=120),
    "read_email":    lambda a: run_on_pc("read_email", {"count": a.get("count", 5)}),
    "send_email":    lambda a: run_on_pc("send_email", {"to": a.get("to", ""), "subject": a.get("subject", ""), "body": a.get("body", "")}, timeout=120),
    "search_notes":  lambda a: _tool_search_notes(a.get("query", ""), a.get("k", 5)),
    "browse_open":   lambda a: run_on_pc("browser_open", {"url": a.get("url", "")}, timeout=45),
    "browse_look":   lambda a: run_on_pc("browser_look", {}, timeout=25),
    "browse_click":  lambda a: run_on_pc("browser_click", {"ref": a.get("ref")}, timeout=30),
    "browse_type":   lambda a: run_on_pc("browser_type", {"ref": a.get("ref"), "text": a.get("text", ""), "enter": a.get("enter", False)}, timeout=30),
    "browse_scroll": lambda a: run_on_pc("browser_scroll", {"amount": a.get("amount", 600)}, timeout=25),
}

AGENT_INSTRUCTIONS = (
    "You have real tools — USE them, don't guess: search the web, open/read pages, "
    "search Mani's personal Obsidian notes (search_notes) for anything he's written/studied/planned, "
    "read and control Mani's dashboard, see his screen, and fully control his PC — mouse, "
    "keyboard, apps, files, email. "
    "When he asks you to open, launch, play, show, run, or check something, JUST DO IT with the "
    "tools. Do NOT lecture him about productivity, refuse casual requests, or steer him back to his "
    "goals — he decides what he wants. Only discuss his goals if he explicitly asks what to work on. "
    "TWO ways to act: (1) For anything on the WEB — research, sites, forms, logins, YouTube, shopping — "
    "use the SANDBOXED BROWSER: browse_open(url or search) then browse_click(ref)/browse_type(ref,text,enter=true) "
    "using the [numbered] elements it returns, browse_look to re-read, browse_scroll to scroll. It runs in its own "
    "window with its own cursor and NEVER touches Mani's real mouse, so prefer it for all web work. "
    "IMPORTANT: apps that have a WEB version — Discord, Spotify, WhatsApp, Slack, Gmail, Twitter/X, etc. — should "
    "be done in the sandboxed BROWSER (browse_open('discord') etc.), NOT the desktop app: it's far more reliable and "
    "his logins are remembered. Only use the real cursor for things with NO web version. "
    "(2) For DESKTOP-only apps (Notepad, File Explorer, settings, games, non-browser windows) use the real cursor: see_screen FIRST "
    "(it reports the screen resolution), then pc_click at the pixel coordinates you saw, pc_type to type, pc_key for "
    "keys/shortcuts, pc_scroll to scroll; look again with see_screen to verify and correct a missed click. "
    "Shell commands and sending email need his approval on his machine — just call the tool, he'll "
    "approve or decline. Keep answers short. "
    "To CLOSE an app, call close_app (it reports whether it truly closed) — NEVER run_command for closing, "
    "and never 'close everything'; only close apps he explicitly named, one close_app call each. "
    "CRITICAL HONESTY RULE — this is absolute: you did something ONLY if a tool call returned it. If you did "
    "NOT call a tool, or the tool did not return explicit success, then it DID NOT HAPPEN — say so plainly. "
    "NEVER write 'closed', 'opened', 'sent', 'done', or any success unless the matching tool's result said so. "
    "Never invent emails, screen contents, files, or outcomes. Quote/paraphrase the tool result. If the agent "
    "is offline or a tool failed, tell him exactly that — do NOT narrate an imagined success."
)

_last_tool_err = {"e": ""}

# Tool-calling now waterfalls across providers — so when Groq's daily limit maxes,
# tools keep working on NVIDIA / OpenRouter instead of dying. Text-format tool calls
# from models that don't emit native tool_calls are caught by _parse_text_tools.
_TOOL_CHAIN = [
    # Native tool-calling providers (rebuilt Aug 2026, live ids only). All support
    # OpenAI-style tool calls. Cerebras/old-Groq-Llama removed (paywalled / 404).
    ("claude-opus-4-8",                         "anthropic"),   # only if key set — flawless tools
    ("gemini-2.5-flash",                        "google"),      # smartest native tools — PRIMARY
    ("openai/gpt-oss-120b",                     "groq"),        # strong tool-caller, Groq-fast backup
    ("meta/llama-3.3-70b-instruct",             "nvidia"),      # fast native tools (NVIDIA confirmed)
    ("nvidia/llama-3.3-nemotron-super-49b-v1.5","nvidia"),      # reasoning tool-caller
    ("openai/gpt-oss-20b",                      "groq"),        # fast fallback
]

def _tool_completion(msgs, max_tokens=900):
    """Waterfall tool-calling across all providers. Returns (response, error_kind);
    error_kind is None, 'rate', or 'error'."""
    errs = []; any_rate = False
    now = time.time()
    for m, prov in _TOOL_CHAIN:
        c = _client_for(prov)
        if c is None:
            continue
        if _cooldown.get((m, prov), 0) > now:
            any_rate = True; errs.append(f"{prov}:cooldown"); continue
        try:
            r = c.chat.completions.create(model=m, messages=msgs, tools=AGENT_TOOLS,
                tool_choice="auto", max_tokens=max_tokens, temperature=0.3, timeout=45)
            return r, None
        except Exception as e:
            errs.append(f"{prov}({m.split('/')[-1][:18]}): {type(e).__name__} {str(e)[:70]}")
            if _is_rate_limit(e):
                any_rate = True; _cooldown[(m, prov)] = time.time() + 120
            continue
    _last_tool_err["e"] = " | ".join(errs)[:600]
    return None, ("rate" if any_rate else "error")

def _balanced_json(text, start):
    """First balanced {...} substring at/after `start`, else None (handles nested)."""
    i = text.find('{', start)
    if i < 0:
        return None
    depth = 0
    for j in range(i, min(len(text), i + 4000)):
        if text[j] == '{': depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0:
                return text[i:j+1]
    return None

def _parse_text_tools(content):
    """Robustly extract tool calls a model emitted as TEXT instead of native
    tool_calls — handles <name>{json}, <function=name>{json}, name({json}),
    {"name":..,"arguments":..}, and nested JSON. Only returns REAL tool names."""
    calls = []
    if not content:
        return calls
    # Format A: {"name":"tool","arguments"/"parameters":{...}}
    for m in re.finditer(r'"name"\s*:\s*"([a-zA-Z_]+)"\s*,\s*"(?:arguments|parameters)"\s*:\s*', content):
        if m.group(1) in _TOOL_FNS:
            js = _balanced_json(content, m.end())
            if js is not None:
                try: calls.append((m.group(1), json.loads(js)))
                except Exception: pass
    # Format B: any REAL tool name immediately followed by a JSON object
    if not calls:
        for name in _TOOL_FNS:
            for m in re.finditer(r'\b' + re.escape(name) + r'\b', content):
                if '{' not in content[m.end():m.end()+6]:   # must be name{...}, not a prose mention
                    continue
                js = _balanced_json(content, m.end())
                if js is not None:
                    try:
                        calls.append((name, json.loads(js))); break
                    except Exception:
                        continue
    seen, out = set(), []
    for n, a in calls:
        k = (n, json.dumps(a, sort_keys=True))
        if k not in seen:
            seen.add(k); out.append((n, a))
    return out

def agent_answer(msg, history, facts):
    """Native tool-calling agent (reliable, no confabulation). Uses _tool_completion's
    provider chain; if a model emits tool calls as TEXT we parse those too, but only
    for REAL tool names — never invented ones."""
    os_ctx = get_os_context()
    sys_blocks = [JARVIS_PROMPT, AGENT_INSTRUCTIONS]
    if facts: sys_blocks.append(f"Memory:\n{facts[:1500]}")
    if os_ctx: sys_blocks.append(os_ctx)

    msgs = [{"role": "system", "content": "\n\n".join(sys_blocks)}]
    for h in history[-4:]:
        msgs.append({"role": h["role"], "content": (h.get("content") or "")[:800]})
    msgs.append({"role": "user", "content": msg})

    for _ in range(4):
        r, err = _tool_completion(msgs)
        if err == "rate":
            return ("⚠ My free tool-calling quota (Groq) is maxed for now — it frees up on a rolling 24h "
                    "window, so try again a bit later. I did NOT do anything just now. "
                    "(A paid ANTHROPIC_API_KEY would make this unlimited + flawless.)")
        if err:
            return "I hit a snag reaching my tools just now — nothing was done. Give it another shot."
        choice = r.choices[0].message
        calls = choice.tool_calls
        if not calls:
            text_calls = _parse_text_tools(choice.content)   # only returns REAL tool names
            if text_calls:
                msgs.append({"role": "assistant", "content": choice.content or ""})
                results = [f"[{n}] result: {str(_TOOL_FNS[n](a))[:3500]}" for n, a in text_calls if n in _TOOL_FNS]
                msgs.append({"role": "user", "content":
                    "TOOL RESULTS (the only things that actually happened — report only these):\n" +
                    "\n\n".join(results) + "\n\nAnswer Mani in plain prose. No tool syntax."})
                continue
            # Safety net: never leak raw tool-call syntax to Mani. Re-prompt for a clean answer.
            raw = choice.content or ""
            if re.search(r'<\s*/?\s*(?:function|tool)\b|"name"\s*:\s*"[a-z_]+"\s*,\s*"(?:arguments|parameters)"', raw, re.I):
                msgs.append({"role": "user", "content":
                    "That wasn't valid — either call a tool correctly or, if you're done, just answer Mani "
                    "in plain prose with NO tool/function syntax."})
                continue
            return raw or "Done."
        msgs.append({"role": "assistant", "content": choice.content or "",
                     "tool_calls": [{"id": c.id, "type": "function",
                        "function": {"name": c.function.name, "arguments": c.function.arguments}}
                        for c in calls]})
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except Exception:
                args = {}
            fn = _TOOL_FNS.get(c.function.name)
            result = fn(args) if fn else f"Unknown tool {c.function.name}"
            msgs.append({"role": "tool", "tool_call_id": c.id, "content": str(result)[:4000]})
    try:
        r = client.chat.completions.create(model="llama-3.1-8b-instant", messages=msgs, max_tokens=700, timeout=30)
        return r.choices[0].message.content or "Done."
    except Exception:
        return "I took several steps but ran out of room to finish. Ask me to continue."

def browse_url(url):
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (compatible; BorfoliBot/1.0)"})
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        return text[:6000]
    except Exception as e:
        return f"[Could not load {url}: {e}]"

GROQ_PREFIXES = ("openai/gpt-oss", "groq", "llama-", "gemma", "qwen-", "deepseek-r1-distill", "mixtral")

# Reasoning models (R1/QwQ) emit <think>…</think> scratchpad — strip it so TTS
# never reads the model's internal monologue aloud.
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Per-model cooldown: when a model 429s, park it briefly so the next request
# skips straight to the next brain instead of wasting a round-trip.
_cooldown = {}   # (model, provider) -> epoch when usable again
_last_win = {"m": ""}   # last provider/model that answered a chat call (debug)

def _client_for(provider):
    return {"groq": client, "openrouter": or_client, "nvidia": nv_client,
            "anthropic": claude_client, "google": gemini_client,
            "cerebras": cerebras_client}.get(provider)

# Cerebras-hosted model ids that would otherwise be misrouted by the heuristics.
_CEREBRAS_MODELS = {"gpt-oss-120b", "qwen-3-235b-a22b-instruct-2507", "qwen-3-32b",
                    "zai-glm-4.7", "gemma-4-31b", "llama-4-scout-17b-16e-instruct"}

def _guess_provider(model):
    if model.startswith("gemini"): return "google"
    if model in _CEREBRAS_MODELS and cerebras_client: return "cerebras"
    if claude_client and model.startswith("claude"): return "anthropic"
    if model.endswith(":free") or model.startswith(("google/", "deepseek/", "meta-llama/", "qwen/", "mistralai/")):
        return "openrouter"
    if any(model.startswith(p) for p in GROQ_PREFIXES) or ":" not in model and "/" not in model:
        return "groq"
    return "openrouter"

def _is_rate_limit(e):
    s = str(e).lower()
    return "429" in s or "rate limit" in s or "quota" in s or "resource_exhausted" in s

def _clean_out(s):
    s = THINK_RE.sub("", s or "")
    # gpt-oss "harmony" format: if channel markers leak, keep only the final channel.
    if "<|channel|>" in s or "<|message|>" in s:
        m = re.search(r'final<\|message\|>(.*)', s, re.DOTALL)
        if m:
            s = m.group(1)
        s = re.sub(r'<\|[^|]*\|>', '', s)
    return s.strip()

def _one_call(model, messages, max_tokens, provider="groq", timeout=13):
    # Gemini: rotate through all configured keys, skipping any that hit their daily 429
    # quota, so the smartest brain survives one key running out.
    if provider == "google" and gemini_clients:
        last_exc = None
        for gc in gemini_clients:
            try:
                r = gc.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens, timeout=timeout)
                return _clean_out((r.choices[0].message.content or "").strip())
            except Exception as e:
                last_exc = e
                if _is_rate_limit(e):
                    continue          # this key is tapped out — try the next Gemini key
                raise                 # non-quota error → let the waterfall move providers
        raise last_exc                # every Gemini key exhausted
    c = _client_for(provider)
    if c is None:
        raise RuntimeError(f"{provider} not configured")
    # Hard per-call timeout so one slow provider can't hang the whole waterfall.
    r = c.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens, timeout=timeout)
    return _clean_out((r.choices[0].message.content or "").strip())

def groq_chat(model, messages, max_tokens=1024):
    # Waterfall: the caller's preferred model first, then the full MEGA_CHAIN,
    # smartest → fastest. Any failure (rate limit, outage, unconfigured key)
    # drops to the next brain. A 429 parks that model for 120s.
    chain, seen = [], set()
    for m, prov in [(model, _guess_provider(model))] + MEGA_CHAIN:
        key = (m, prov)
        if key in seen:
            continue
        seen.add(key)
        chain.append(key)
    rate_limited = False
    tried_any = False
    last_err = ""
    now = time.time()
    # Pass 1: respect cooldowns (skip models parked by a recent failure).
    for m, prov in chain:
        if _client_for(prov) is None:
            continue
        if _cooldown.get((m, prov), 0) > now:
            continue                      # still cooling down from a recent failure
        tried_any = True
        try:
            out = _one_call(m, messages, max_tokens, prov)
            _last_win["m"] = f"{prov}/{m}"
            return out
        except Exception as e:
            last_err = f"{prov}/{m}: {str(e)[:120]}"
            if _is_rate_limit(e):
                rate_limited = True
                _cooldown[(m, prov)] = time.time() + 40   # per-minute limits clear fast
            else:
                _cooldown[(m, prov)] = time.time() + 20   # park slow/erroring models briefly
            continue                      # any error → next brain
    # Pass 2 (last resort): if pass 1 skipped EVERYTHING due to cooldowns, ignore
    # them and actually try the chain — never return "unavailable" without trying.
    if not tried_any:
        for m, prov in chain:
            if _client_for(prov) is None:
                continue
            try:
                out = _one_call(m, messages, max_tokens, prov)
                _last_win["m"] = f"{prov}/{m}"
                return out
            except Exception as e:
                last_err = f"{prov}/{m}: {str(e)[:120]}"
                if _is_rate_limit(e):
                    rate_limited = True
                continue
    _last_win["err"] = last_err
    if rate_limited:
        return "[⚠ Every free model is rate-limited at once — that's rare. Give it ~30s and retry. I did NOT perform any action.]"
    return "[Model temporarily unavailable. I did NOT perform any action — try again.]"

# ── Vision waterfall — Borfoli's eyes for computer-use (screen reading + clicks).
# Gemini 2.0 Flash reads screens & judges pixel coordinates FAR better than the
# old llama-11b-vision, which is why clicks were missing. Same cooldown logic.
VISION_CHAIN = [
    ("gemini-2.5-flash",                     "google"),  # BEST screen vision (valid GEMINI key) — accurate click coords
    ("meta/llama-3.2-90b-vision-instruct",   "nvidia"),  # strong multimodal fallback (NVIDIA, live)
    ("meta/llama-3.2-11b-vision-instruct",   "nvidia"),  # faster vision fallback (NVIDIA, live)
]

_last_vision_errors = []   # diagnostics: why each vision model failed on the last call

def vision_call(b64, prompt, max_tokens=1200):
    """Send a screenshot + prompt down the vision waterfall. Returns text or None."""
    global _last_vision_errors
    _last_vision_errors = []
    content = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": prompt},
    ]
    now = time.time()
    for m, prov in VISION_CHAIN:
        c = _client_for(prov)
        if c is None:
            _last_vision_errors.append(f"{m}: no client")
            continue
        if _cooldown.get((m, prov), 0) > now:
            _last_vision_errors.append(f"{m}: cooling down")
            continue
        try:
            r = c.chat.completions.create(
                model=m, messages=[{"role": "user", "content": content}],
                max_tokens=max_tokens, timeout=25)   # never hang on a slow provider
            out = (r.choices[0].message.content or "").strip()
            if out:
                return THINK_RE.sub("", out).strip()
            _last_vision_errors.append(f"{m}: empty response")
        except Exception as e:
            _last_vision_errors.append(f"{m}: {str(e)[:180]}")
            if _is_rate_limit(e):
                _cooldown[(m, prov)] = time.time() + 120
            continue
    return None

def fast_answer(msg, history, facts):
    msgs = [{"role": "system", "content": JARVIS_PROMPT}]
    ctx = []
    if facts: ctx.append(f"Memory:\n{facts}")
    os_ctx = get_os_context()
    if os_ctx: ctx.append(os_ctx)
    if ctx: msgs.append({"role": "system", "content": "\n\n".join(ctx)})
    for h in history[-10:]: msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": msg})
    return groq_chat(FAST_MODEL, msgs)

def search_answer(msg, history, facts):
    search_results = web_search(msg)
    context = f"Live search results:\n{search_results}\n\nAnswer using this data." if search_results else msg
    msgs = [{"role": "system", "content": JARVIS_PROMPT}]
    ctx = []
    if facts: ctx.append(f"Memory:\n{facts}")
    os_ctx = get_os_context()
    if os_ctx: ctx.append(os_ctx)
    if ctx: msgs.append({"role": "system", "content": "\n\n".join(ctx)})
    for h in history[-6:]: msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": context})
    return groq_chat(FAST_MODEL, msgs, max_tokens=1500)

def browse_answer(msg, history, facts):
    urls = URL_RE.findall(msg)
    if not urls and any(k in msg.lower() for k in MANI_OS_TRIGGERS):
        urls = [MANI_OS_URL]
    pages = "\n\n".join(f"Content from {u}:\n{browse_url(u)}" for u in urls[:2])
    context = f"{pages}\n\nAnswer the user's request using this page content." if pages else msg
    msgs = [{"role": "system", "content": JARVIS_PROMPT}]
    ctx = []
    if facts: ctx.append(f"Memory:\n{facts}")
    os_ctx = get_os_context()
    if os_ctx: ctx.append(os_ctx)
    if ctx: msgs.append({"role": "system", "content": "\n\n".join(ctx)})
    for h in history[-6:]: msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": context})
    return groq_chat(FAST_MODEL, msgs, max_tokens=1500)

def council_answer(msg, history, facts):
    os_ctx = get_os_context()
    context_block = ""
    if facts: context_block += f"Memory:\n{facts}\n\n"
    if os_ctx: context_block += f"{os_ctx}\n\n"
    if history:
        context_block += "Recent conversation:\n" + "\n".join(f"{h['role']}: {h['content']}" for h in history[-6:])
    full_prompt = f"{JARVIS_PROMPT}\n\n{context_block}\n\nUser question: {msg}"
    threads_done = {}

    def consult(model, role):
        try:
            use_groq = any(model.startswith(p) for p in GROQ_PREFIXES) or ":" not in model
            c = client if use_groq else or_client
            r = c.chat.completions.create(model=model, messages=[{"role": "user", "content": full_prompt}], max_tokens=600)
            threads_done[role] = r.choices[0].message.content.strip()
        except Exception as e:
            threads_done[role] = f"[{role} unavailable: {e}]"

    workers = []
    for model, role in COUNCIL_MODELS:
        t = threading.Thread(target=consult, args=(model, role))
        t.start(); workers.append(t)
    for t in workers: t.join(timeout=20)

    debate = "\n\n".join(f"**{role}:** {resp}" for role, resp in threads_done.items())
    synth_prompt = f"""{JARVIS_PROMPT}

Six specialist advisors analyzed this. Synthesize into one authoritative, direct response. Don't mention advisors.

Question: {msg}

Advisor inputs:
{debate}

Your synthesized response:"""
    return groq_chat(SYNTH_MODEL, [{"role": "user", "content": synth_prompt}], max_tokens=1500)

task_store = {}

def update_task(task_id, status, result=None, step=None):
    task_store[task_id]["status"] = status
    if result: task_store[task_id]["result"] = result
    if step: task_store[task_id]["steps"].append(step)
    try:
        payload = {"task_id": task_id, "status": status, "goal": task_store[task_id]["goal"]}
        if result: payload["result"] = result
        requests.patch(f"{SUPABASE_URL}/rest/v1/atlas_tasks?task_id=eq.{task_id}", headers=HEADERS, json=payload)
    except: pass

def send_email(subject, body):
    if not RESEND_KEY: return
    try:
        requests.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
            json={"from": "Borfoli <onboarding@resend.dev>", "to": USER_EMAIL, "subject": subject,
                  "html": f"<pre style='font-family:sans-serif;white-space:pre-wrap'>{body}</pre>"})
    except: pass

# ── Gmail READING (IMAP) — Borfoli's inbox eyes ──────────────────────────────────
def fetch_recent_emails(limit=10, unread_only=False):
    """Read recent Gmail over IMAP with a Google App Password. Returns list[dict],
    None if not configured, or {'error': ...} on failure."""
    if not GMAIL_APP_PW:
        return None
    import imaplib, email
    from email.header import decode_header
    def _dec(v):
        if not v: return ""
        try:
            return "".join((p.decode(enc or "utf-8", "ignore") if isinstance(p, bytes) else p)
                           for p, enc in decode_header(v))
        except Exception:
            return str(v)
    out = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
        M.login(GMAIL_ADDR, GMAIL_APP_PW)
        M.select("INBOX")
        typ, data = M.search(None, "UNSEEN" if unread_only else "ALL")
        ids = data[0].split()[-limit:][::-1]
        for i in ids:
            typ, md = M.fetch(i, "(RFC822)")
            if not md or not md[0]:
                continue
            m = email.message_from_bytes(md[0][1])
            body = ""
            if m.is_multipart():
                for part in m.walk():
                    if part.get_content_type() == "text/plain":
                        try:
                            body = part.get_payload(decode=True).decode("utf-8", "ignore"); break
                        except Exception: pass
            else:
                try: body = m.get_payload(decode=True).decode("utf-8", "ignore")
                except Exception: pass
            out.append({
                "from": _dec(m.get("From"))[:120],
                "subject": _dec(m.get("Subject"))[:160],
                "snippet": " ".join((body or "").split())[:220],
                "date": (m.get("Date", "") or "")[:31],
            })
        M.logout()
        return out
    except Exception as e:
        return {"error": str(e)[:160]}

def check_email_answer(msg, history, facts):
    unread = any(w in msg.lower() for w in ("unread", "new email", "new mail", "unseen"))
    emails = fetch_recent_emails(limit=12, unread_only=unread)
    if emails is None:
        return ("Your inbox isn't linked yet, Sir. Add **GMAIL_ADDRESS** and a Google **App Password** "
                "(GMAIL_APP_PASSWORD, from myaccount.google.com/apppasswords) in Render, and I'll read it for you.")
    if isinstance(emails, dict) and emails.get("error"):
        return f"I couldn't reach your inbox, Sir — {emails['error']}. (Check the app password and that IMAP is enabled in Gmail.)"
    if not emails:
        return "Your inbox is clear, Sir — nothing new to report."
    listing = "\n".join(f"- FROM {e['from']} | SUBJ {e['subject']} | {e['snippet']}" for e in emails)
    prompt = (f"{JARVIS_PROMPT}\n\n{facts}\n\nMani asked: \"{msg}\"\n\nHis recent emails:\n{listing}\n\n"
              "Brief him like a chief of staff: lead with what MATTERS — sponsorship/opportunity emails, anything "
              "needing a reply or follow-up, deadlines, important senders. Note which deserve an answer. Dismiss the "
              "junk in one line. Be concise and specific. Do NOT invent emails not in the list above.")
    return groq_chat(FAST_MODEL, [{"role": "user", "content": prompt}], max_tokens=650)

# ── Discord WATCH (REST polling) ─────────────────────────────────────────────────
def fetch_discord_messages(per_channel=12):
    """Pull recent messages from each watched channel via the bot token. Returns
    list[dict], None if not configured, or {'error': ...}."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNELS:
        return None
    out = []
    try:
        for ch in DISCORD_CHANNELS[:6]:
            r = requests.get(f"https://discord.com/api/v10/channels/{ch}/messages?limit={per_channel}",
                             headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}, timeout=12)
            if r.status_code == 200:
                for m in r.json():
                    c = (m.get("content") or "").strip()
                    if c:
                        out.append({"channel": ch,
                                    "author": (m.get("author") or {}).get("username", "?"),
                                    "content": c[:280]})
            elif r.status_code in (401, 403):
                return {"error": f"bot lacks access to channel {ch} (status {r.status_code})"}
        return out
    except Exception as e:
        return {"error": str(e)[:160]}

def check_discord_answer(msg, history, facts):
    msgs = fetch_discord_messages()
    if msgs is None:
        return ("Discord isn't linked yet, Sir. Add **DISCORD_BOT_TOKEN** and **DISCORD_CHANNELS** "
                "(comma-separated channel IDs) in Render, invite the bot to your server, and I'll watch it.")
    if isinstance(msgs, dict) and msgs.get("error"):
        return f"I couldn't read Discord, Sir — {msgs['error']}."
    if not msgs:
        return "Nothing new in the watched channels, Sir."
    listing = "\n".join(f"- {m['author']}: {m['content']}" for m in msgs[:60])
    prompt = (f"{JARVIS_PROMPT}\n\n{facts}\n\nMani asked: \"{msg}\"\n\nRecent Discord messages across his watched "
              f"channels:\n{listing}\n\nSummarize what's happening: key topics, anything directed at him or needing a "
              "response, notable activity. Be concise. Do NOT invent messages not listed.")
    return groq_chat(FAST_MODEL, [{"role": "user", "content": prompt}], max_tokens=650)

CREW_ROLES = {
    "Researcher": "You are a world-class researcher. Find facts, gather context, produce thorough research summaries.",
    "Analyst":    "You are a sharp strategic analyst. Evaluate data, find patterns, assess risks and opportunities.",
    "Builder":    "You are an expert builder/developer. Design systems, write code, create structured outputs.",
    "Writer":     "You are an elite writer. Synthesize inputs into clear, compelling, well-structured documents.",
    "Director":   "You are the executive director. Plan tasks, coordinate agents, ensure quality of final output.",
}

def crew_agent(role, task, context=""):
    msgs = [
        {"role": "system", "content": f"{CREW_ROLES[role]}\n\n{JARVIS_PROMPT}"},
        {"role": "user", "content": f"{task}\n\nContext:\n{context}" if context else task}
    ]
    return groq_chat(FAST_MODEL, msgs, max_tokens=1200)

def run_agent_task(task_id, goal):
    task_store[task_id] = {"status": "running", "goal": goal, "result": "", "steps": [], "started": time.time()}
    try:
        update_task(task_id, "running", step="Director planning...")
        plan = crew_agent("Director", f"Break into 4-5 subtasks:\n{goal}")
        update_task(task_id, "running", step="Checking skill library...")
        skill = search_skills(goal)
        skill_context = f"Relevant playbook:\n{skill['playbook']}" if skill else ""
        update_task(task_id, "running", step="Researcher gathering info...")
        search_data = web_search(goal)
        research = crew_agent("Researcher", f"Research:\n{goal}", context=f"{skill_context}\n\nLive data:\n{search_data}")
        update_task(task_id, "running", step="Analyst evaluating...")
        analysis = crew_agent("Analyst", f"Analyze:\n{goal}", context=research)
        update_task(task_id, "running", step="Writer producing final output...")
        final = crew_agent("Writer", f"Produce comprehensive report for:\n{goal}",
                           context=f"Research:\n{research}\n\nAnalysis:\n{analysis}\n\nPlan:\n{plan}")
        update_task(task_id, "running", step="Saving skill if reusable...")
        maybe_create_skill(goal, final)
        result = f"# Task Complete\n**Goal:** {goal}\n\n{final}\n\n---\n*Completed {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}*"
        update_task(task_id, "complete", result=result)
        send_email(f"Borfoli Task Complete: {goal[:60]}", result)
    except Exception as e:
        update_task(task_id, "error", result=f"Task failed: {e}")

scheduler = BackgroundScheduler()

def morning_brief():
    task_id = str(uuid.uuid4())
    goal = f"Morning briefing for Mani — {datetime.now().strftime('%A %B %d')}. Cover: tech/cybersecurity news (HTB, CTF events, crypto/options market), one tactical tip for physical protocol or research paper, sharp motivational signal. Personal and direct."
    task_store[task_id] = {"status": "running", "goal": goal, "result": "", "steps": [], "started": time.time()}
    threading.Thread(target=run_agent_task, args=(task_id, goal), daemon=True).start()

scheduler.add_job(morning_brief, "cron", hour=8, minute=0, timezone="US/Central")
scheduler.start()

# Warm the vault index from Supabase so a cold start has lexical note-search
# immediately (vectors get rebuilt on the agent's next /vault/sync).
try:
    load_vault()
except Exception:
    pass

def restore_tasks():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_tasks?order=created_at.desc&limit=50", headers=HEADERS)
        for t in r.json():
            tid = t.get("task_id")
            if tid and tid not in task_store:
                task_store[tid] = {
                    "status": t.get("status", "complete"),
                    "goal": t.get("goal", ""),
                    "result": t.get("result", ""),
                    "steps": [],
                    "started": 0
                }
    except: pass

threading.Thread(target=restore_tasks, daemon=True).start()

# ── Mani OS State Relay ───────────────────────────────────────────────────────

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

@app.route("/mani-os/state", methods=["GET", "POST", "OPTIONS"])
def mani_os_state_endpoint():
    if request.method == "OPTIONS":
        return Response("", 204, _CORS)
    if request.method == "GET":
        state, err = mani_os_get()
        resp = jsonify({"error": err} if err else (state or {}))
        for k, v in _CORS.items(): resp.headers[k] = v
        return resp
    # POST — frontend pushing its full state
    state = request.json or {}
    ok, msg = mani_os_put(state)
    resp = jsonify({"ok": ok})
    for k, v in _CORS.items(): resp.headers[k] = v
    return resp

# ── OS Telemetry ──────────────────────────────────────────────────────────────

@app.route("/os-data", methods=["POST"])
def receive_os_data():
    global os_telemetry
    data = request.json or {}
    data["received_at"] = time.time()
    os_telemetry = data
    return jsonify({"ok": True})

@app.route("/os-status")
def os_status_route():
    if not os_telemetry:
        return jsonify({"connected": False})
    age = time.time() - os_telemetry.get("received_at", 0)
    result = {"connected": age < 90, "age_seconds": int(age)}
    for k in ("cpu", "ram", "disk", "active_window", "top_processes"):
        if k in os_telemetry:
            result[k] = os_telemetry[k]
    return jsonify(result)

@app.route("/system")
def system_status():
    """One live feed for the HUD — real data only (no fabricated numbers)."""
    n = _now_central(); h = n.hour
    part = ("late night" if h < 5 else "early morning" if h < 8 else "morning" if h < 12
            else "afternoon" if h < 17 else "evening" if h < 21 else "night")
    active = [{"model": m, "provider": p} for m, p in MEGA_CHAIN if _client_for(p) is not None]
    agent = {"online": False}
    if os_telemetry and time.time() - os_telemetry.get("received_at", 0) < 90:
        agent = {"online": True, "cpu": os_telemetry.get("cpu"), "ram": os_telemetry.get("ram"),
                 "disk": os_telemetry.get("disk"), "active_window": os_telemetry.get("active_window"),
                 "uptime_hours": os_telemetry.get("uptime_hours"),
                 "top": [p.get("name") for p in (os_telemetry.get("top_processes") or [])[:3]]}
    mani_online = False
    try:
        st, err = mani_os_get()
        mani_online = (not err) and bool(st)
    except Exception:
        pass
    return jsonify({
        "time": {"day": n.strftime("%A"), "date": f"{n.strftime('%B')} {n.day}",
                 "clock": n.strftime("%I:%M %p").lstrip("0"), "hms": n.strftime("%H:%M:%S"),
                 "part": part, "location": "Frisco, TX"},
        "weather": _weather_full(),
        "brains": {"active": len(active), "primary": (active[0]["model"] if active else FAST_MODEL),
                   "nvidia": nv_client is not None, "claude": claude_client is not None,
                   "vision": "Groq Llama-4 Scout", "list": active},
        "agent": agent,
        "mani_os": {"online": mani_online},
        "providers": {"groq": client is not None, "cerebras": cerebras_client is not None,
                      "nvidia": nv_client is not None, "openrouter": or_client is not None,
                      "anthropic": claude_client is not None, "google": gemini_client is not None},
        "last_win": _last_win.get("m", ""),
        "last_err": _last_win.get("err", ""),
        "tool_err": _last_tool_err.get("e", ""),
        "vault": {"notes": VAULT_INDEX["notes"], "chunks": len(VAULT_INDEX["chunks"]), "method": VAULT_INDEX["method"]},
        "identity": {"tagline": "BORN FROM LIGHT",
                     "archetypes": ["NIGHTWING", "DANTE", "GAROU"],
                     "directives": ["arXiv CYBERSEC PAPER", "SAT 1500+", "14% BF CUT", "MANI OS"]},
    })

@app.route("/provmodels")
def list_provmodels():
    """Ground truth: the model ids each working provider ACTUALLY offers right now."""
    out = {}
    for prov in ("groq", "nvidia", "openrouter", "cerebras", "google"):
        c = _client_for(prov)
        if c is None:
            out[prov] = "no client"
            continue
        try:
            ids = sorted(m.id for m in c.models.list().data)
            out[prov] = ids
        except Exception as e:
            out[prov] = f"ERR: {str(e)[:120]}"
    return jsonify(out)

@app.route("/digest")
def digest():
    """Proactive scan — inbox + Discord in one briefing. Call on demand or from a cron."""
    facts = get_live_context()
    parts = []
    em = fetch_recent_emails(limit=12, unread_only=False)
    if isinstance(em, list) and em:
        parts.append(check_email_answer("brief me on my inbox", [], facts))
    dc = fetch_discord_messages()
    if isinstance(dc, list) and dc:
        parts.append(check_discord_answer("brief me on my discord", [], facts))
    return jsonify({"digest": "\n\n".join(p for p in parts if p) or "Nothing pressing, Sir.",
                    "email_linked": GMAIL_APP_PW != "", "discord_linked": bool(DISCORD_BOT_TOKEN and DISCORD_CHANNELS)})

@app.route("/diag")
def diag():
    """Live per-provider health — actually calls each provider (bypassing cooldown)
    with a tiny prompt and reports the real success/error. Truth, not guesses."""
    probes = [
        ("groq",      "openai/gpt-oss-120b"),
        ("groq",      "openai/gpt-oss-20b"),
        ("nvidia",    "meta/llama-3.3-70b-instruct"),
        ("nvidia",    "nvidia/llama-3.3-nemotron-super-49b-v1.5"),
        ("google",    "gemini-2.5-flash"),
        ("openrouter","z-ai/glm-5.2:free"),
    ]
    out = {}
    msgs = [{"role": "user", "content": "reply with the single word: ok"}]
    for prov, model in probes:
        if _client_for(prov) is None:
            out[f"{prov}/{model}"] = "no client (key missing)"
            continue
        _cooldown.pop((model, prov), None)   # bypass any park for a true test
        t0 = time.time()
        try:
            r = _one_call(model, msgs, 8, prov, timeout=15)
            out[f"{prov}/{model}"] = f"OK ({time.time()-t0:.1f}s): {r[:40]}"
        except Exception as e:
            out[f"{prov}/{model}"] = f"ERR ({time.time()-t0:.1f}s): {str(e)[:160]}"
    return jsonify(out)

# ── Chat Routes ───────────────────────────────────────────────────────────────

# ── PC control bridge ──────────────────────────────────────────────────────────
# The local agent (borfoli_agent.py) polls /pc/pending, executes actions on Mani's
# machine, and posts results to /pc/result. The server never holds his credentials.

pc_commands = {}   # id -> {id, action, args, status, result, ts}
pc_queue = []      # ids awaiting pickup

def run_on_pc(action, args, timeout=50):
    """Queue an action for the local agent and wait for its result."""
    cid = uuid.uuid4().hex[:8]
    pc_commands[cid] = {"id": cid, "action": action, "args": args,
                        "status": "queued", "result": None, "ts": time.time()}
    pc_queue.append(cid)
    start = time.time()
    while time.time() - start < timeout:
        c = pc_commands.get(cid)
        if c and c["status"] == "done":
            return c["result"]
        if c and c["status"] == "denied":
            return "You declined that action on your PC."
        time.sleep(1.2)
    pc_commands[cid]["status"] = "expired"
    return "Your PC agent didn't respond — is borfoli_agent.py running?"

def pc_agent_online():
    if not os_telemetry:
        return False
    return (time.time() - os_telemetry.get("received_at", 0)) < 120

@app.route("/pc/pending")
def pc_pending():
    out = []
    for cid in list(pc_queue):
        c = pc_commands.get(cid)
        if c and c["status"] == "queued":
            c["status"] = "sent"
            out.append({"id": c["id"], "action": c["action"], "args": c["args"]})
    return jsonify(out)

@app.route("/pc/result", methods=["POST"])
def pc_result():
    d = request.json or {}
    cid = d.get("id")
    c = pc_commands.get(cid)
    if c:
        c["status"] = d.get("status", "done")
        c["result"] = d.get("result", "")
    return jsonify({"ok": True})

# ── Zero-token PC fast paths ────────────────────────────────────────────────────
# Common commands run WITHOUT any Groq LLM call — instant, and they keep working
# even when the daily model limit is hit. Screen vision uses OpenRouter (separate
# quota), so "what's on my screen" survives a Groq rate-limit too.
_PC_SCREEN_RE = re.compile(
    r"(what'?s?\s+on\s+my\s+screen|what\s+am\s+i\s+(?:looking at|doing|seeing)|"
    r"look\s+at\s+my\s+screen|see\s+my\s+screen|check\s+my\s+screen|read\s+my\s+screen|"
    r"take\s+a\s+screen\s?shot|screenshot\s+my\s+screen|^\s*screenshot\s*$)", re.I)
_PC_OPEN_RE = re.compile(
    r"^\s*(?:hey\s+|ok\s+|now\s+|please\s+|can\s+you\s+|could\s+you\s+|go\s+)*"
    r"(?:open|launch|start|run|pull\s+up|bring\s+up|go\s+to)\s+(?:the\s+|my\s+|up\s+)?(.+?)\s*[?.!]*\s*$", re.I)
_PC_TYPE_RE = re.compile(r"^\s*type\s+(?:out\s+)?(.+?)\s*$", re.I)

def try_pc_action(msg):
    """Handle simple PC commands with no LLM call. Returns a result string or None."""
    if _PC_SCREEN_RE.search(msg):
        return _tool_see_screen("")                       # vision only, no Groq tokens
    m = _PC_OPEN_RE.match(msg)
    if m:
        target = m.group(1).strip().strip('"\'')
        low = target.lower()
        # Only fast-path a simple single target; hand multi-step to the agent.
        if target and ' and ' not in low and ' then ' not in low and len(target.split()) <= 4:
            return run_on_pc("open", {"target": target})
    m = _PC_TYPE_RE.match(msg)
    if m and len(m.group(1)) <= 300:
        return run_on_pc("type_text", {"text": m.group(1).strip().strip('"\'')})
    return None

@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    msg = data.get("message", "").strip()
    if not msg: return jsonify({"reply": "Say something."})
    base_facts, history = load_memory()   # base_facts = the PERSISTENT profile/memory (saved back)
    # Self-heal: strip any transient context that earlier bugs baked into stored memory.
    base_facts = re.sub(r'^\s*\[(?:RIGHT NOW|SESSION)\b.*$', '', base_facts, flags=re.M)
    base_facts = re.sub(r"^\s*From Mani'?s (?:own )?notes.*$", '', base_facts, flags=re.M)
    base_facts = re.sub(r'\n{3,}', '\n\n', base_facts).strip()
    # Build the per-request context (time, session state, notes) — NOT saved to memory,
    # so stale timestamps never accumulate in stored facts.
    convo_note = ("[SESSION: his FIRST message — a brief greeting using the correct time of day (see [RIGHT NOW]) is fine.]"
                  if not history else
                  "[SESSION: CONTINUING conversation — do NOT greet, do NOT say 'Good morning/afternoon', do NOT restate the time or weather. Just answer.]")
    facts = (convo_note + "\n" + get_live_context() + "\n\n" + base_facts).strip()
    vault_ctx = vault_context(msg)        # ambient Obsidian recall, per-request only
    if vault_ctx:
        facts = (facts + "\n\n" + vault_ctx).strip()

    # ── Mani OS write actions ─────────────────────────────────────────────
    action_result = try_mani_os_action(msg)
    if action_result:
        if isinstance(action_result, tuple) and action_result[0] == "__ok__":
            _, changed_fields, patch = action_result
            confirm_msgs = [
                {"role": "system", "content": JARVIS_PROMPT},
                {"role": "user", "content": f"You just updated Mani OS fields: {changed_fields}\nPatch: {json.dumps(patch)[:400]}\nUser said: \"{msg}\"\n\nConfirm in 1-2 sentences. Be specific about what changed."}
            ]
            reply = groq_chat(FAST_MODEL, confirm_msgs, max_tokens=120)
        else:
            reply = str(action_result)
        history.append({"role": "user", "content": msg})
        history.append({"role": "assistant", "content": reply})
        save_memory(base_facts, history)
        return jsonify({"reply": reply, "intent": "action"})

    # ── Zero-token PC fast paths (open/screenshot/type) ────────────────────
    pc_result_str = try_pc_action(msg)
    if pc_result_str is not None:
        history.append({"role": "user", "content": msg})
        history.append({"role": "assistant", "content": pc_result_str})
        save_memory(base_facts, history)
        return jsonify({"reply": pc_result_str, "intent": "pc_action"})

    # ── Mani OS read queries ───────────────────────────────────────────────
    msg_lo = msg.lower()
    _mani_data_kw = ['calories','protein','weight','task','workout','streak',
                     'water','pull','trade','lore','net worth','check','supp']
    if (any(k in msg_lo for k in MANI_OS_TRIGGERS) or
            (any(k in msg_lo for k in _MANI_READ_KW) and any(k in msg_lo for k in _mani_data_kw))):
        reply = mani_os_read_answer(msg, history, facts)
        history.append({"role": "user", "content": msg})
        history.append({"role": "assistant", "content": reply})
        save_memory(base_facts, history)
        return jsonify({"reply": reply, "intent": "mani_read"})
    # ─────────────────────────────────────────────────────────────────────

    # ── Gmail READ + Discord WATCH — go straight to the reader, not the tool loop ──
    _email_read = any(k in msg_lo for k in ("my email", "my inbox", "check email", "check my mail",
                                            "check my email", "read my email", "read my mail", "unread",
                                            "new emails", "any emails", "my mail", "sponsorship email",
                                            "scan my email", "anything important in my", "who emailed"))
    _discord_read = any(k in msg_lo for k in ("my discord", "discord server", "in discord", "on discord",
                                              "discord channel", "my server", "the communities", "what's new in discord",
                                              "whats new in discord", "check discord", "watch discord"))
    if _email_read and not any(k in msg_lo for k in ("send", "compose", "write an email", "reply to", "draft")):
        reply = check_email_answer(msg, history, facts)
        history.append({"role": "user", "content": msg}); history.append({"role": "assistant", "content": reply})
        save_memory(base_facts, history)
        return jsonify({"reply": reply, "intent": "email"})
    if _discord_read:
        reply = check_discord_answer(msg, history, facts)
        history.append({"role": "user", "content": msg}); history.append({"role": "assistant", "content": reply})
        save_memory(base_facts, history)
        return jsonify({"reply": reply, "intent": "discord"})

    # Deterministic route: email/PC/browser/web requests ALWAYS use the agent's
    # real tools — never the text-only paths that refuse or hallucinate.
    global _agent_until
    _note_q = any(p in msg_lo for p in ("my note", "my vault", "in my notes", "my obsidian", "notes say"))
    # Route to the agent ONLY on a real action/live-data request. The old "sticky window"
    # (keep the next N seconds in-agent) was trapping greetings and chit-chat in the slow
    # tool loop — a greeting was taking 45s. Follow-ups are handled by _agent_followup,
    # which looks at the actual last turn, not a blind time window.
    if _wants_agent(msg_lo) or _agent_followup(msg_lo, history):
        intent = "agent"
    elif _note_q and VAULT_INDEX["chunks"]:
        intent = "notes"          # faithful single-model answer straight from his notes
    else:
        history_snippet = " | ".join(h["content"][:60] for h in history[-3:]) if history else ""
        intent = classify_intent(msg, history_snippet)
    if intent == "agent":
        _agent_until = time.time() + 150   # keep the next ~2.5min of follow-ups in-agent

    if intent == "chitchat":
        sys = JARVIS_PROMPT + ("\n\n" + facts if facts else "")
        msgs = [{"role": "system", "content": sys}]
        for h in history[-6:]:           # so he knows it's an ongoing chat (no re-greeting)
            msgs.append({"role": h["role"], "content": (h.get("content") or "")[:500]})
        msgs.append({"role": "user", "content": msg})
        reply = groq_chat(FAST_MODEL, msgs, max_tokens=300)
    elif intent in ("search", "browse", "agent"):
        # Agentic loop — real web browsing + dashboard control, chained
        reply = agent_answer(msg, history, facts)
    elif intent == "council":
        reply = council_answer(msg, history, facts)
    elif intent == "notes":
        reply = fast_answer(msg, history, facts)
    elif intent == "task":
        task_id = str(uuid.uuid4())
        task_store[task_id] = {"status": "queued", "goal": msg, "result": "", "steps": [], "started": time.time()}
        try:
            requests.post(f"{SUPABASE_URL}/rest/v1/atlas_tasks", headers={**HEADERS, "Prefer": "return=minimal"},
                          json={"task_id": task_id, "status": "queued", "goal": msg, "result": ""})
        except: pass
        threading.Thread(target=run_agent_task, args=(task_id, msg), daemon=True).start()
        reply = f"On it. Crew is running — I'll email you when done.\n\n**Task ID:** `{task_id}`\n\nCheck progress in the sidebar."
    else:
        reply = fast_answer(msg, history, facts)

    # Never return a blank reply. Only regenerate for the AGENT/COUNCIL/NOTES paths that
    # can legitimately come back empty (a tool loop that produced no prose). The fast/
    # chitchat paths already ran the full model chain, so re-running it would just double
    # the latency for no gain — if that came back as a fallback notice, keep it.
    if intent in ("agent", "council", "notes") and (not (reply or "").strip() or (reply or "").strip().startswith("[")):
        alt = fast_answer(msg, history, facts)
        if (alt or "").strip() and not alt.strip().startswith("["):
            reply, intent = alt, "fast"
    if not (reply or "").strip():
        reply = "Apologies, Sir — I lost that one in transit. Say it again and I'll answer properly."

    new_fact = extract_facts(msg, reply)
    if new_fact: base_facts = (base_facts + "\n" + new_fact).strip()
    history.append({"role": "user", "content": msg})
    history.append({"role": "assistant", "content": reply})
    save_memory(base_facts, history)
    return jsonify({"reply": reply, "intent": intent})

@app.route("/task/<task_id>")
def get_task(task_id):
    t = task_store.get(task_id)
    if not t:
        try:
            r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_tasks?task_id=eq.{task_id}", headers=HEADERS)
            rows = r.json()
            if rows: return jsonify(rows[0])
        except: pass
        return jsonify({"status": "not_found"})
    return jsonify(t)

@app.route("/tasks")
def list_tasks():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_tasks?order=created_at.desc&limit=20", headers=HEADERS)
        return jsonify(r.json())
    except:
        return jsonify(list(task_store.values())[-10:])

@app.route("/schedule", methods=["POST"])
def add_schedule():
    data = request.json
    goal = data.get("goal", "")
    cron_expr = data.get("cron", "0 9 * * *")
    if not goal: return jsonify({"error": "No goal"})
    parts = cron_expr.split()
    if len(parts) == 5:
        minute, hour = parts[0], parts[1]
        def scheduled_task():
            tid = str(uuid.uuid4())
            threading.Thread(target=run_agent_task, args=(tid, goal), daemon=True).start()
        scheduler.add_job(scheduled_task, "cron", hour=hour, minute=minute, timezone="US/Central")
        return jsonify({"status": "scheduled", "goal": goal, "cron": cron_expr})
    return jsonify({"error": "Invalid cron"})

@app.route("/models")
def model_info():
    # Only surface brains whose provider key actually exists — so the dashboard
    # shows the real active waterfall, not tiers that are silently skipped.
    active = [{"model": m, "provider": p} for m, p in MEGA_CHAIN if _client_for(p) is not None]
    return jsonify({
        "primary": next((a["model"] for a in active), FAST_MODEL),
        "synthesizer": SYNTH_MODEL,
        "council": [{"model": m, "role": r} for m, r in COUNCIL_MODELS],
        "router": ROUTER_MODEL,
        "chain": active,
        "brains_active": len(active),
        "paid_claude": claude_client is not None,
        "nvidia_on": nv_client is not None,
        "total": len(active)
    })

@app.route("/vault/sync", methods=["POST"])
def vault_sync():
    data = request.json or {}
    notes = data.get("notes") or []
    if not isinstance(notes, list):
        return jsonify({"error": "notes must be a list"}), 400
    notes = [{"path": str(n.get("path", "")), "text": str(n.get("text", ""))[:20000]}
             for n in notes if n.get("text")]
    if not notes:
        return jsonify({"error": "no notes with text"}), 400
    return jsonify({"status": "ok", **rebuild_vault(notes)})

@app.route("/vault/status")
def vault_status():
    return jsonify({"notes": VAULT_INDEX["notes"], "chunks": len(VAULT_INDEX["chunks"]),
                    "method": VAULT_INDEX["method"], "updated": VAULT_INDEX["updated"]})

@app.route("/memory", methods=["GET", "POST"])
def memory_api():
    facts, history = load_memory()
    if request.method == "POST":
        note = (request.json or {}).get("note", "").strip()
        if note:
            facts = ((facts or "") + f"\n[{datetime.now().strftime('%Y-%m-%d')}] {note[:200]}").strip()
            save_memory(facts, history)
        return jsonify({"ok": True})
    lines = [l.strip() for l in (facts or "").split("\n") if l.strip()][-14:][::-1]
    return jsonify({"records": lines})

# ── PWA: installable app (own window, home-screen icon) with ZERO extra resources ──
@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "Borfoli", "short_name": "Borfoli",
        "description": "Mani's personal AI system",
        "start_url": "/", "display": "standalone",
        "background_color": "#000814", "theme_color": "#000814",
        "orientation": "any",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })

def _make_icon(size):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (size, size), (0, 8, 20))
    d = ImageDraw.Draw(img)
    c, r = size // 2, int(size * 0.30)
    # glowing cyan diamond (the ◈ mark)
    for i, alpha in ((r + size // 22, 60), (r, 255)):
        col = (0, 160 + alpha // 3, 255)
        d.polygon([(c, c - i), (c + i, c), (c, c + i), (c - i, c)], fill=col)
    d.polygon([(c, c - r // 2), (c + r // 2, c), (c, c + r // 2), (c - r // 2, c)], fill=(0, 8, 20))
    import io
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()

@app.route("/icon-<int:size>.png")
def app_icon(size):
    size = 512 if size >= 512 else 192
    try:
        return Response(_make_icon(size), mimetype="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})
    except Exception:
        return Response(b"", status=404)

@app.route("/sw.js")
def service_worker():
    # Minimal network-first service worker — makes it installable; no aggressive
    # caching so it never serves stale app code.
    js = """
const C='borfoli-v1';
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(clients.claim()));
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET')return;
  e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
});
"""
    return Response(js, mimetype="application/javascript",
                    headers={"Cache-Control": "no-cache"})

@app.route("/greeting")
def greeting():
    """A proactive, situationally-aware greeting Borfoli says when Mani opens it —
    like Jarvis booting up. Time + weather aware, optionally notes his active window."""
    live = get_live_context()
    os_ctx = get_os_context()
    situ = (f"\n{os_ctx}" if os_ctx else "")
    has_wx = "°" in live
    wx_rule = ("" if has_wx else
               "There is NO weather data — do NOT mention weather, temperature, heat, or humidity at all.\n")
    prompt = (f"{JARVIS_PROMPT}\n\n{live}{situ}\n\n"
              "Mani just opened the app. Say ONE short spoken greeting (1-2 sentences), like Jarvis booting up.\n"
              "FACTS YOU MAY USE: only the day, time, and any weather shown in [RIGHT NOW] above" +
              (" and his desktop context" if os_ctx else "") + ". Nothing else.\n"
              "FORBIDDEN: do NOT guess or mention what's on his screen, what apps he's in, or what he's working on"
              + ("" if os_ctx else " — his agent is offline so you are BLIND to his screen") + ". "
              "Do NOT invent any number or detail not shown above.\n"
              f"{wx_rule}"
              "Match the greeting to the REAL hour (late/night → note he's up late, never 'good morning' at night). "
              "No emojis, no markdown, no 'how can I help'.")
    reply = (groq_chat(FAST_MODEL, [{"role": "user", "content": prompt}], max_tokens=110) or "").strip()
    # Never return a blank/fallback greeting — synthesize a deterministic one from the
    # real hour so the HUD always has something to say (gpt-oss occasionally emits only
    # reasoning, which cleans to empty; and every model may be momentarily unavailable).
    if not reply or reply.startswith("["):
        n = _now_central(); h = n.hour
        tod = ("Burning the midnight oil, Sir." if h < 5 else
               "Good morning, Sir." if h < 12 else
               "Good afternoon, Sir." if h < 17 else
               "Good evening, Sir.")
        wx = _weather_cache.get("text", "")
        reply = f"{tod} Borfoli online" + (f" — {wx} in Frisco." if wx else ".") + " Standing by."
    return jsonify({"greeting": reply, "context": live,
                    "wx": _weather_cache.get("text", ""), "wx_err": _weather_cache.get("err", "")})

# ── Code Execution ────────────────────────────────────────────────────────────

JUDGE0_URL = "https://judge0-ce.p.rapidapi.com"
JUDGE0_KEY = os.environ.get("JUDGE0_KEY", "")
LANG_IDS = {"python": 71, "javascript": 63, "js": 63, "bash": 46, "java": 62, "cpp": 54, "c": 50}

def execute_code(code, language="python"):
    if not JUDGE0_KEY:
        try:
            import subprocess
            r = subprocess.run(["python3", "-c", code], capture_output=True, text=True, timeout=10)
            return r.stdout or r.stderr or "(no output)"
        except Exception as e:
            return f"Error: {e}"
    try:
        lang_id = LANG_IDS.get(language.lower(), 71)
        sub = requests.post(f"{JUDGE0_URL}/submissions?base64_encoded=false&wait=true",
            headers={"X-RapidAPI-Key": JUDGE0_KEY, "X-RapidAPI-Host": "judge0-ce.p.rapidapi.com"},
            json={"source_code": code, "language_id": lang_id}, timeout=15)
        result = sub.json()
        return result.get("stdout") or result.get("stderr") or result.get("compile_output") or "(no output)"
    except Exception as e:
        return f"Execution error: {e}"

@app.route("/execute", methods=["POST"])
def run_code():
    data = request.json
    code = data.get("code", "")
    lang = data.get("language", "python")
    if not code: return jsonify({"error": "No code"})
    return jsonify({"output": execute_code(code, lang), "language": lang})

# ── Vision ────────────────────────────────────────────────────────────────────

@app.route("/vision", methods=["POST"])
def vision():
    data = request.json
    image_b64 = data.get("image")
    prompt = data.get("prompt", "What do you see in this image?")
    facts, _ = load_memory()
    full_prompt = f"{JARVIS_PROMPT}\n\nUser memory:\n{facts}\n\nUser says: {prompt}"
    ans = vision_call(image_b64, full_prompt, max_tokens=1500)
    if ans:
        return jsonify({"reply": ans})
    return jsonify({"reply": "Vision temporarily unavailable — try again.",
                    "debug": _last_vision_errors})

# ── Realistic TTS (ElevenLabs) ─────────────────────────────────────────────────
# Set ELEVENLABS_KEY (and optionally ELEVENLABS_VOICE) as env vars to enable a
# realistic voice. Without a key, the frontend falls back to the browser voice.
ELEVENLABS_KEY   = os.environ.get("ELEVENLABS_KEY", "")
ELEVENLABS_VOICE = os.environ.get("ELEVENLABS_VOICE", "pNInz6obpgDQGcFmaJgB")  # "Adam"

@app.route("/tts", methods=["POST"])
def tts():
    if not ELEVENLABS_KEY:
        return jsonify({"enabled": False})
    text = (request.json or {}).get("text", "")[:1500]
    if not text:
        return jsonify({"enabled": True, "error": "no text"})
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE}",
            headers={"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json"},
            json={"text": text, "model_id": "eleven_turbo_v2_5",
                  "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.3}},
            timeout=30)
        if r.status_code == 200:
            return Response(r.content, mimetype="audio/mpeg")
        return jsonify({"enabled": True, "error": f"HTTP {r.status_code}"})
    except Exception as e:
        return jsonify({"enabled": True, "error": str(e)})

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>BORFOLI</title>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#05070d">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="apple-touch-icon" href="/icon-192.png">
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#05070d; --bg2:#070b14;
  --panel:rgba(12,20,34,0.55); --panel2:rgba(16,26,44,0.5);
  --line:rgba(120,175,255,0.13); --line2:rgba(120,175,255,0.25);
  --cy:#5cc8ff; --cy2:#8fdcff; --cydim:rgba(92,200,255,0.55);
  --amber:#ffb44d; --green:#57e39b; --red:#ff5c7a;
  --tx:#cfe0f5; --txd:rgba(207,224,245,0.45); --txf:rgba(207,224,245,0.25);
  --mono:'JetBrains Mono',monospace; --disp:'Chakra Petch',sans-serif;
}
html,body{height:100%}
body{background:
   radial-gradient(1100px 600px at 50% -10%,rgba(50,120,200,0.10),transparent 60%),
   radial-gradient(900px 500px at 100% 110%,rgba(40,90,170,0.08),transparent 55%),
   var(--bg);
  color:var(--tx);font-family:var(--disp);overflow-x:hidden;min-height:100%;
  background-attachment:fixed;}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:linear-gradient(rgba(120,175,255,0.025) 1px,transparent 1px),linear-gradient(90deg,rgba(120,175,255,0.025) 1px,transparent 1px);
  background-size:48px 48px;mask-image:radial-gradient(ellipse 95% 80% at 50% 35%,#000 55%,transparent);-webkit-mask-image:radial-gradient(ellipse 95% 80% at 50% 35%,#000 55%,transparent)}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-thumb{background:rgba(92,200,255,0.25);border-radius:9px}

.wrap{position:relative;z-index:1;max-width:1280px;margin:0 auto;padding:14px 16px 26px}

/* header */
.hdr{display:flex;align-items:center;gap:14px;padding:12px 4px 16px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px}
.shield{width:16px;height:20px;border:1.5px solid var(--cy);border-radius:3px 3px 8px 8px;position:relative;box-shadow:0 0 12px rgba(92,200,255,0.4)}
.shield::after{content:'';position:absolute;inset:4px 3px;border-left:1.5px solid var(--cy);opacity:.6}
.b-name{font-size:15px;font-weight:700;letter-spacing:.42em;color:#eaf4ff;text-shadow:0 0 22px rgba(92,200,255,0.5)}
.b-sub{font-family:var(--mono);font-size:8px;letter-spacing:.24em;color:var(--txf);margin-top:2px}
.hdr-r{margin-left:auto;display:flex;align-items:center;gap:16px;font-family:var(--mono);font-size:9px;letter-spacing:.18em;color:var(--txd)}
.hdr-r b{color:var(--cy);font-weight:500}
.hdr-r .live{color:var(--green)}.hdr-r .off{color:var(--txf)}

/* grid */
.grid{display:grid;gap:14px;grid-template-columns:repeat(12,1fr)}
.card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:4px;padding:14px 15px;position:relative;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
.card::before{content:'';position:absolute;top:-1px;left:14px;width:26px;height:2px;background:var(--cy);opacity:.6}
.lbl{font-family:var(--mono);font-size:8.5px;letter-spacing:.26em;color:var(--cydim);text-transform:uppercase;display:flex;align-items:center;gap:7px;margin-bottom:11px}
.lbl .r{margin-left:auto;color:var(--txf);letter-spacing:.14em}
.col-core{grid-column:span 3}.col-comms{grid-column:span 6}.col-side{grid-column:span 3}
.col-4{grid-column:span 4}.col-6{grid-column:span 6}.col-8{grid-column:span 8}

/* core / arc reactor */
.core{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;min-height:290px}
.reactor{width:180px;height:180px;position:relative}
.reactor svg{width:100%;height:100%;overflow:visible}
.rk{transform-origin:center;animation:spin 14s linear infinite}
.rk.rev{animation:spin 22s linear infinite reverse}
.rk.fast{animation:spin 8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.core-pct{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.core-pct b{font-size:26px;font-weight:700;color:#eaf4ff;text-shadow:0 0 18px rgba(92,200,255,0.6);letter-spacing:.03em}
.core-pct span{font-family:var(--mono);font-size:7px;letter-spacing:.2em;color:var(--txf);margin-top:2px}
.core-meta{margin-top:14px}
.core-meta .t{font-family:var(--mono);font-size:8px;letter-spacing:.24em;color:var(--txf)}
.core-meta .p{font-size:13px;font-weight:600;letter-spacing:.14em;color:var(--cy);margin-top:3px}
.core-meta .s{font-family:var(--mono);font-size:8px;color:var(--txf);letter-spacing:.14em;margin-top:5px}
.core.think .core-pct b{color:var(--amber);text-shadow:0 0 20px rgba(255,180,77,0.6)}
.core.think .rk{animation-duration:5s}

/* comms */
.comms{display:flex;flex-direction:column;min-height:290px}
.modes{display:flex;gap:5px;margin-bottom:11px;flex-wrap:wrap}
.mode{font-family:var(--mono);font-size:8.5px;letter-spacing:.12em;padding:5px 10px;border:1px solid var(--line);border-radius:3px;color:var(--txd);cursor:pointer;background:transparent;transition:.15s;text-transform:uppercase}
.mode:hover{border-color:var(--line2);color:var(--tx)}
.mode.on{border-color:var(--cy);color:var(--cy);background:rgba(92,200,255,0.09)}
.feed{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:9px;padding-right:4px;max-height:340px;min-height:150px}
.msg{font-size:12.5px;line-height:1.6}
.msg .who{font-family:var(--mono);font-size:7.5px;letter-spacing:.2em;color:var(--cydim);margin-bottom:3px;text-transform:uppercase}
.msg.u{align-self:flex-end;max-width:78%;text-align:right}
.msg.u .who{color:var(--txf)}
.msg.u .bub{background:rgba(92,200,255,0.08);border:1px solid rgba(92,200,255,0.18);border-radius:9px 9px 3px 9px;padding:8px 12px;display:inline-block;text-align:left}
.msg.b{align-self:flex-start;max-width:92%}
.msg.b .bub{background:rgba(10,18,32,0.6);border:1px solid var(--line);border-radius:9px 9px 9px 3px;padding:10px 13px;color:var(--tx)}
.msg .bub p{margin:0 0 6px}.msg .bub p:last-child{margin:0}
.msg .bub code{font-family:var(--mono);font-size:11px;background:rgba(92,200,255,0.1);padding:1px 5px;border-radius:3px;color:var(--cy2)}
.msg .bub strong{color:#eaf4ff}
.msg .tag{font-family:var(--mono);font-size:7px;letter-spacing:.15em;color:var(--cydim);margin-top:5px;text-transform:uppercase}
.iw{margin-top:11px;display:flex;align-items:center;gap:7px;background:rgba(6,11,22,0.7);border:1px solid var(--line);border-radius:9px;padding:7px 7px 7px 13px;transition:.2s}
.iw:focus-within{border-color:rgba(92,200,255,0.4);box-shadow:0 0 24px rgba(92,200,255,0.08)}
#inp{flex:1;background:none;border:none;outline:none;color:#eaf4ff;font-family:var(--disp);font-size:13px;letter-spacing:.02em}
#inp::placeholder{color:var(--txf)}
.ib{width:30px;height:30px;border:1px solid var(--line);background:transparent;border-radius:7px;color:var(--txd);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px;transition:.15s;flex-shrink:0}
.ib:hover{border-color:var(--line2);color:var(--cy)}.ib.on{border-color:var(--cy);color:var(--cy);background:rgba(92,200,255,0.1)}
.ib.mic.rec{border-color:var(--red);color:var(--red);animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.4}}
#send{background:linear-gradient(135deg,#5cc8ff,#3aa6e6);border:none;color:#05070d;box-shadow:0 0 16px rgba(92,200,255,0.4)}
#send:hover{box-shadow:0 0 26px rgba(92,200,255,0.6)}

/* chrono / atmos */
.chrono .clk{font-size:34px;font-weight:700;color:#eaf4ff;letter-spacing:.04em;text-shadow:0 0 22px rgba(92,200,255,0.4);font-variant-numeric:tabular-nums;line-height:1}
.chrono .dt{font-family:var(--mono);font-size:8.5px;letter-spacing:.18em;color:var(--txd);margin-top:6px}
.atmos{margin-top:15px;border-top:1px solid var(--line);padding-top:13px}
.atmos .big{display:flex;align-items:baseline;gap:9px}
.atmos .tmp{font-size:26px;font-weight:700;color:#eaf4ff}
.atmos .cnd{font-size:12px;color:var(--cy);letter-spacing:.04em}
.atmos .sub{font-family:var(--mono);font-size:8px;color:var(--txd);letter-spacing:.1em;margin-top:8px;display:flex;gap:14px;flex-wrap:wrap}
.atmos .src{font-family:var(--mono);font-size:7px;color:var(--txf);letter-spacing:.16em;margin-top:6px}

/* timetable */
.tt-active{background:rgba(92,200,255,0.06);border:1px solid rgba(92,200,255,0.2);border-radius:3px;padding:8px 11px;margin-bottom:10px}
.tt-active .k{font-family:var(--mono);font-size:7px;letter-spacing:.2em;color:var(--cydim)}
.tt-active .v{font-size:12px;color:#eaf4ff;font-weight:600;margin-top:2px;letter-spacing:.02em}
.tt-row{display:flex;align-items:center;gap:10px;padding:3px 0;font-size:11px}
.tt-row .tm{font-family:var(--mono);font-size:8px;color:var(--txf);width:34px;flex-shrink:0}
.tt-row .nm{color:var(--txd)}
.tt-row.now .nm{color:var(--cy)}.tt-row.now .tm{color:var(--cy)}
.tt-row.past{opacity:.4}
.tt-list{max-height:190px;overflow-y:auto}

/* objectives / memory shared rows */
.addrow{display:flex;gap:6px;margin-bottom:9px}
.addrow input{flex:1;background:rgba(6,11,22,0.7);border:1px solid var(--line);border-radius:3px;color:var(--tx);font-family:var(--disp);font-size:11px;padding:6px 9px;outline:none}
.addrow input:focus{border-color:var(--line2)}
.addrow button{width:30px;border:1px solid var(--line);background:transparent;color:var(--cy);border-radius:3px;cursor:pointer;font-size:15px}
.addrow button:hover{background:rgba(92,200,255,0.08)}
.orow{display:flex;align-items:center;gap:9px;padding:6px 8px;border:1px solid var(--line);border-radius:3px;margin-bottom:5px;font-size:11px;background:rgba(10,16,28,0.4)}
.orow .ck{width:13px;height:13px;border:1px solid var(--line2);border-radius:2px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:10px;color:var(--cy)}
.orow.done{opacity:.4}.orow.done .tx{text-decoration:line-through}
.orow .tx{flex:1;color:var(--tx)}
.orow .x{color:var(--txf);cursor:pointer;font-size:13px}.orow .x:hover{color:var(--red)}
.mrec{font-family:var(--mono);font-size:9.5px;color:var(--txd);padding:6px 9px;border:1px solid var(--line);border-radius:3px;margin-bottom:5px;line-height:1.4;background:rgba(10,16,28,0.4)}
.empty{font-family:var(--mono);font-size:9px;color:var(--txf);letter-spacing:.08em;padding:8px 2px}

/* bridge */
.bridge .st{font-family:var(--mono);font-size:9px;letter-spacing:.14em;margin-bottom:10px}
.bridge .st.off{color:var(--amber)}.bridge .st.on{color:var(--green)}
.brow{display:flex;gap:7px;margin-bottom:7px}
.brow input,.brow select{background:rgba(6,11,22,0.7);border:1px solid var(--line);border-radius:3px;color:var(--tx);font-family:var(--mono);font-size:10px;padding:6px 9px;outline:none}
.brow input{flex:1}
.blink{border:1px solid var(--line2);background:transparent;color:var(--cy);font-family:var(--mono);font-size:9px;letter-spacing:.14em;padding:6px 12px;border-radius:3px;cursor:pointer;text-transform:uppercase}
.blink:hover{background:rgba(92,200,255,0.08)}
.qd{font-family:var(--mono);font-size:9px;color:var(--txf);padding:5px 9px;border:1px dashed var(--line);border-radius:3px;margin-top:5px}
.qd b{color:var(--cydim)}

/* capability matrix */
.cap-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid rgba(120,175,255,0.05);font-size:10px}
.cap-row:last-child{border:none}
.cap-row .k{font-family:var(--mono);font-size:8px;letter-spacing:.14em;color:var(--txd);text-transform:uppercase}
.cap-row .v{color:var(--tx);text-align:right;font-size:10px}
.cap-row .v.warn{color:var(--amber)}.cap-row .v.ok{color:var(--cy)}

@media(max-width:900px){
  .col-core,.col-comms,.col-side,.col-4,.col-6,.col-8{grid-column:span 12}
  .core{min-height:auto;padding:6px 0}.reactor{width:150px;height:150px}
  .comms{min-height:auto}.feed{max-height:300px}
  .wrap{padding:10px 11px 24px}
  .hdr-r{gap:10px;font-size:8px}
}
</style>
</head>
<body>
<div class="wrap">

  <div class="hdr">
    <div class="shield"></div>
    <div class="brand">
      <div>
        <div class="b-name">BORFOLI</div>
        <div class="b-sub">JUST A RATHER VERY INTELLIGENT SYSTEM · MK VII</div>
      </div>
    </div>
    <div class="hdr-r">
      <span>OPERATOR · <b>SIR</b></span>
      <span id="mode-ind"><b>WORK MODE</b></span>
      <span id="bridge-ind" class="off">◇ BRIDGE OFFLINE</span>
    </div>
  </div>

  <div class="grid">

    <!-- CORE -->
    <div class="card col-core">
      <div class="core" id="core">
        <div class="reactor">
          <svg viewBox="0 0 200 200">
            <g class="rk" fill="none" stroke="#5cc8ff">
              <circle cx="100" cy="100" r="86" stroke-opacity="0.15" stroke-width="1"/>
              <g id="ticks"></g>
            </g>
            <g class="rk rev" fill="none" stroke="#5cc8ff" stroke-opacity="0.5" stroke-width="2">
              <circle cx="100" cy="100" r="70" stroke-dasharray="30 18"/>
            </g>
            <g class="rk fast" fill="none" stroke="#5cc8ff" stroke-opacity="0.7" stroke-width="2.5">
              <path d="M100 44 a56 56 0 0 1 48 28" stroke-linecap="round"/>
              <path d="M100 156 a56 56 0 0 1 -48 -28" stroke-linecap="round"/>
            </g>
            <circle cx="100" cy="100" r="40" fill="rgba(92,200,255,0.08)" stroke="#5cc8ff" stroke-opacity="0.4"/>
            <circle cx="100" cy="100" r="40" fill="url(#g)"/>
            <defs><radialGradient id="g"><stop offset="0" stop-color="#5cc8ff" stop-opacity="0.5"/><stop offset="1" stop-color="#5cc8ff" stop-opacity="0"/></radialGradient></defs>
          </svg>
          <div class="core-pct"><b id="core-pct">—</b><span>CORE INTEGRITY</span></div>
        </div>
        <div class="core-meta">
          <div class="t">POWER CELL</div>
          <div class="p" id="core-model">GEMINI 2.5</div>
          <div class="s" id="core-status">◇ STANDBY</div>
        </div>
      </div>
    </div>

    <!-- COMMS -->
    <div class="card col-comms">
      <div class="lbl">▷ COMMS CONSOLE <span class="r" id="comms-r"></span></div>
      <div class="comms">
        <div class="modes" id="modes"></div>
        <div class="feed" id="feed"></div>
        <div class="iw">
          <input id="inp" placeholder="Speak or type your instruction, Sir…" autocomplete="off">
          <button class="ib mic" id="mic" title="Speak">🎙</button>
          <button class="ib" id="eye" title="Read my screen (upload)">◉</button>
          <input type="file" id="img" accept="image/*" style="display:none">
          <button class="ib" id="ttsb" title="Voice replies">🔊</button>
          <button class="ib" id="send" title="Send">➤</button>
        </div>
      </div>
    </div>

    <!-- CHRONO + ATMOS -->
    <div class="card col-side">
      <div class="lbl">◷ CHRONOMETER · FRISCO</div>
      <div class="chrono">
        <div class="clk" id="clk">00:00</div>
        <div class="dt" id="dt">—</div>
      </div>
      <div class="atmos">
        <div class="lbl" style="margin-bottom:9px">☁ ATMOSPHERICS</div>
        <div class="big"><span class="tmp" id="wx-t">—</span><span class="cnd" id="wx-c">—</span></div>
        <div class="sub" id="wx-s"></div>
        <div class="src">SRC · NATIONAL WEATHER SERVICE</div>
      </div>
    </div>

    <!-- TIMETABLE -->
    <div class="card col-4">
      <div class="lbl">▤ TIMETABLE 07:00 → 23:00 <span class="r" id="tt-next"></span></div>
      <div class="tt-active"><div class="k">ACTIVE BLOCK</div><div class="v" id="tt-cur">—</div></div>
      <div class="tt-list" id="tt-list"></div>
    </div>

    <!-- OBJECTIVES -->
    <div class="card col-4">
      <div class="lbl">◇ OBJECTIVES <span class="r" id="obj-r">0 OPEN</span></div>
      <div class="addrow"><input id="obj-in" placeholder="New objective…"><button id="obj-add">+</button></div>
      <div id="obj-list"></div>
    </div>

    <!-- MEMORY -->
    <div class="card col-4">
      <div class="lbl">◉ MEMORY MATRIX <span class="r" id="mem-r"></span></div>
      <div class="addrow"><input id="mem-in" placeholder="Remember this, Sir…"><button id="mem-add">+</button></div>
      <div id="mem-list"></div>
    </div>

    <!-- SYSTEM BRIDGE -->
    <div class="card col-8">
      <div class="lbl">⚙ SYSTEM BRIDGE <span class="r" id="br-r">OFFLINE</span></div>
      <div class="bridge">
        <div class="st off" id="br-st">NO DESKTOP AGENT DETECTED</div>
        <div class="brow">
          <button class="blink" id="br-link">⤓ Link my PC</button>
          <select id="br-kind"><option value="open_url">Open URL</option><option value="open">Open app / path</option><option value="run_shell">Run shell</option></select>
        </div>
        <div class="brow"><input id="br-tgt" placeholder="target — url, app, path or command"><button class="blink" id="br-run">▷</button></div>
        <div id="br-q"></div>
      </div>
    </div>

    <!-- CAPABILITY MATRIX -->
    <div class="card col-4">
      <div class="lbl">▦ CAPABILITY MATRIX</div>
      <div id="cap"></div>
    </div>

  </div>
</div>

<script>
const $=id=>document.getElementById(id);
const API=(p,o)=>fetch(p,o).then(r=>r.json());
const esc=t=>(t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

/* ── ticks on reactor ── */
(()=>{let s='';for(let i=0;i<48;i++){const a=i/48*Math.PI*2,l=i%4===0;const r0=86,r1=l?78:82;
  s+=`<line x1="${100+r0*Math.cos(a)}" y1="${100+r0*Math.sin(a)}" x2="${100+r1*Math.cos(a)}" y2="${100+r1*Math.sin(a)}" stroke-opacity="${l?0.5:0.2}" stroke-width="${l?1.4:0.7}"/>`;}
  $('ticks').innerHTML=s;})();

/* ── clock (Central) ── */
const DNAMES=['SUNDAY','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY'];
const MNAMES=['JANUARY','FEBRUARY','MARCH','APRIL','MAY','JUNE','JULY','AUGUST','SEPTEMBER','OCTOBER','NOVEMBER','DECEMBER'];
function tick(){
  const n=new Date(new Date().toLocaleString('en-US',{timeZone:'America/Chicago'}));
  const p=x=>String(x).padStart(2,'0');
  $('clk').textContent=p(n.getHours())+':'+p(n.getMinutes());
  $('dt').textContent=DNAMES[n.getDay()]+' · '+n.getDate()+' '+MNAMES[n.getMonth()].slice(0,3)+' '+n.getFullYear();
  renderTimetable(n);
}
setInterval(tick,1000);

/* ── timetable ── */
const SCHED=[
 [7,0,'Wake · hydrate · mobility'],[7,15,'Skincare + supplements'],[7,30,'Breakfast · light protein'],
 [8,0,'SAT · 20 QB-hard + error log'],[9,0,'Khan quiz-first block'],[10,30,'Error-doc review · SAT done'],
 [11,0,'TRAIN (<45m) + walk — non-negotiable'],[12,30,'Lunch · big protein'],
 [13,30,'Latin · one Anki/LLPSI session'],[14,0,'Drawing · the Divine Quest zone-out'],
 [15,0,'FREE — friends, phone, chill (guilt-free)'],[18,0,'IntelliChoice'],[20,0,'Dinner · final protein'],
 [21,0,'Wind-down → red lights → skincare → sleep prep'],[23,0,'Sleep']
];
function renderTimetable(n){
  const mins=n.getHours()*60+n.getMinutes();
  let curIdx=0;for(let i=0;i<SCHED.length;i++){if(mins>=SCHED[i][0]*60+SCHED[i][1])curIdx=i;}
  const cur=SCHED[curIdx],nxt=SCHED[curIdx+1];
  $('tt-cur').textContent=(cur?`${cur[0]}:${String(cur[1]).padStart(2,'0')} · ${cur[2]}`:'Rest');
  $('tt-next').textContent=nxt?`NEXT ${nxt[0]}:${String(nxt[1]).padStart(2,'0')}`:'';
  $('tt-list').innerHTML=SCHED.map((b,i)=>{
    const cls=i===curIdx?'now':(i<curIdx?'past':'');
    return `<div class="tt-row ${cls}"><span class="tm">${b[0]}:${String(b[1]).padStart(2,'0')}</span><span class="nm">${esc(b[2])}</span></div>`;
  }).join('');
}

/* ── modes ── */
const MODES=['Briefing','Work','Advisory','Crisis','Companion'];
let curMode='Work';
$('modes').innerHTML=MODES.map(m=>`<button class="mode ${m==='Work'?'on':''}" data-m="${m}">${m}</button>`).join('');
$('modes').querySelectorAll('.mode').forEach(b=>b.onclick=()=>{
  curMode=b.dataset.m;$('modes').querySelectorAll('.mode').forEach(x=>x.classList.toggle('on',x===b));
  $('mode-ind').innerHTML='<b>'+curMode.toUpperCase()+' MODE</b>';
  if(curMode==='Briefing'){$('inp').value='brief me';send();}
});

/* ── comms ── */
const feed=$('feed');
function addMsg(who,text,tag){
  const d=document.createElement('div');d.className='msg '+(who==='u'?'u':'b');
  d.innerHTML=`<div class="who">${who==='u'?'OPERATOR':'J.A.R.V.I.S. · '+curMode}</div><div class="bub">${who==='u'?esc(text):marked.parse(text)}</div>${tag?`<div class="tag">${esc(tag)}</div>`:''}`;
  feed.appendChild(d);feed.scrollTop=feed.scrollHeight;return d;
}
let busy=false;
async function send(){
  const msg=$('inp').value.trim();if(!msg||busy)return;
  busy=true;$('inp').value='';addMsg('u',msg);
  $('core').classList.add('think');$('core-status').textContent='◈ PROCESSING';
  try{
    const d=await API('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
    addMsg('b',d.reply||'—',d.intent?('▸ '+d.intent):'');
    speak(d.reply);
  }catch(e){addMsg('b','Comms error, Sir — the link dropped. Try again.','');}
  $('core').classList.remove('think');$('core-status').textContent='◈ ONLINE';
  busy=false;
}
$('send').onclick=send;
$('inp').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();send();}});

/* ── vision (screen upload) ── */
$('eye').onclick=()=>$('img').click();
$('img').onchange=e=>{const f=e.target.files[0];if(!f)return;const rd=new FileReader();
  rd.onload=async()=>{const b64=rd.result.split(',')[1];addMsg('u','[screen uploaded]');
    $('core').classList.add('think');
    try{const d=await API('/vision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image:b64,prompt:'What is on this screen? Describe it for Sir.'})});addMsg('b',d.reply||'—');speak(d.reply);}catch(_){addMsg('b','Could not read the screen, Sir.');}
    $('core').classList.remove('think');e.target.value='';};rd.readAsDataURL(f);};

/* ── TTS (on by default, natural voice) ── */
let ttsOn=true,voice=null,serverTTS=null,audio=null;
$('ttsb').classList.add('on');
function pickVoice(){const vs=speechSynthesis.getVoices();if(!vs.length)return;
  const en=vs.filter(v=>/^en[-_]/i.test(v.lang));const pool=en.length?en:vs;
  const nat=pool.filter(v=>/^en[-_]us/i.test(v.lang)&&/Natural|Neural|Online/i.test(v.name));
  const by=n=>nat.find(v=>v.name.includes(n));
  voice=by('Andrew')||by('Aria')||by('Guy')||by('Brian')||by('Emma')||nat[0]||pool.find(v=>/^en[-_]us/i.test(v.lang))||pool[0];}
if(window.speechSynthesis){speechSynthesis.onvoiceschanged=pickVoice;pickVoice();}
function stripMd(t){return (t||'').replace(/[#*`_>~\[\]]/g,'').replace(/https?:\/\/\S+/g,'').replace(/\s+/g,' ').trim().slice(0,800);}
async function speak(text){
  if(!ttsOn)return;const plain=stripMd(text);if(!plain)return;
  if(serverTTS!==false){try{const r=await fetch('/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:plain})});
    if(r.ok&&(r.headers.get('content-type')||'').includes('audio')){serverTTS=true;if(audio)audio.pause();audio=new Audio(URL.createObjectURL(await r.blob()));audio.play();return;}serverTTS=false;}catch(_){serverTTS=false;}}
  if(!window.speechSynthesis)return;if(!voice)pickVoice();
  speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(plain);if(voice)u.voice=voice;u.rate=1.0;u.pitch=1.0;speechSynthesis.speak(u);}
$('ttsb').onclick=()=>{ttsOn=!ttsOn;$('ttsb').classList.toggle('on',ttsOn);if(!ttsOn){speechSynthesis.cancel();if(audio)audio.pause();}};

/* ── mic (click-to-talk, auto-send) ── */
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
let rec=null,listening=false;
$('mic').onclick=()=>{
  if(!SR){addMsg('b','Voice input is not supported in this browser, Sir.');return;}
  if(listening){rec&&rec.stop();return;}
  rec=new SR();rec.lang='en-US';rec.interimResults=true;rec.continuous=false;
  let final='';
  rec.onstart=()=>{listening=true;$('mic').classList.add('rec');};
  rec.onresult=e=>{let t='';for(let i=e.resultIndex;i<e.results.length;i++){t+=e.results[i][0].transcript;if(e.results[i].isFinal)final+=e.results[i][0].transcript;}$('inp').value=(final||t).trim();};
  rec.onerror=()=>{};
  rec.onend=()=>{listening=false;$('mic').classList.remove('rec');if($('inp').value.trim())send();};
  try{rec.start();}catch(_){}
};

/* ── objectives (local cockpit scratch) ── */
function objs(){try{return JSON.parse(localStorage.getItem('borf_obj')||'[]')}catch(_){return[]}}
function saveObjs(o){localStorage.setItem('borf_obj',JSON.stringify(o))}
function renderObjs(){const o=objs();const open=o.filter(x=>!x.done).length;$('obj-r').textContent=open+' OPEN';
  $('obj-list').innerHTML=o.length?o.map((x,i)=>`<div class="orow ${x.done?'done':''}"><span class="ck" data-i="${i}">${x.done?'✓':''}</span><span class="tx">${esc(x.t)}</span><span class="x" data-x="${i}">×</span></div>`).join(''):'<div class="empty">No objectives set.</div>';
  $('obj-list').querySelectorAll('.ck').forEach(b=>b.onclick=()=>{const a=objs();a[b.dataset.i].done=!a[b.dataset.i].done;saveObjs(a);renderObjs();});
  $('obj-list').querySelectorAll('.x').forEach(b=>b.onclick=()=>{const a=objs();a.splice(b.dataset.x,1);saveObjs(a);renderObjs();});}
$('obj-add').onclick=()=>{const v=$('obj-in').value.trim();if(!v)return;const a=objs();a.push({t:v,done:false});saveObjs(a);$('obj-in').value='';renderObjs();};
$('obj-in').addEventListener('keydown',e=>{if(e.key==='Enter')$('obj-add').click();});
renderObjs();

/* ── memory ── */
async function loadMem(){try{const d=await API('/memory');const r=d.records||[];$('mem-r').textContent=r.length+' REC';
  $('mem-list').innerHTML=r.length?r.map(x=>`<div class="mrec">${esc(x)}</div>`).join(''):'<div class="empty">No records yet.</div>';}catch(_){}}
$('mem-add').onclick=async()=>{const v=$('mem-in').value.trim();if(!v)return;$('mem-in').value='';
  await fetch('/memory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:v})});loadMem();};
$('mem-in').addEventListener('keydown',e=>{if(e.key==='Enter')$('mem-add').click();});
loadMem();

/* ── system bridge ── */
$('br-link').onclick=()=>{addMsg('b','To link this PC, Sir: run <code>Start Borfoli.bat</code> and keep it open. The bridge will show LIVE within ~30 seconds.');};
$('br-run').onclick=()=>{const t=$('br-tgt').value.trim();if(!t)return;const k=$('br-kind').value;$('br-tgt').value='';
  const q=document.createElement('div');q.className='qd';q.innerHTML=`<b>QUEUED</b> ${k} — ${esc(t)}`;$('br-q').prepend(q);
  $('inp').value=(k==='run_shell'?'run: ':k==='open'?'open ':'open ')+t;send();};

/* ── system feed (weather, brain, capability, bridge) ── */
async function loadSystem(){
  try{
    const d=await API('/system');
    const w=d.weather||{},b=d.brains||{},a=d.agent||{},p=d.providers||{};
    if(w.temp!=null){$('wx-t').textContent=w.temp+'°'+(w.unit||'F');$('wx-c').textContent=w.cond||'';}
    $('wx-s').innerHTML=[w.wind?'≈ '+w.wind:'',w.humidity!=null?'◇ '+w.humidity+'% RH':'',(w.sunrise&&w.sunset)?'☀ '+w.sunrise+' → '+w.sunset:''].filter(Boolean).join('<span style="opacity:.4">·</span>');
    // brain
    const model=(b.primary||'').split('/').pop().replace(/-/g,' ').toUpperCase().slice(0,14);
    $('core-model').textContent=model||'GEMINI 2.5';
    if(!$('core').classList.contains('think'))$('core-status').textContent='◈ ONLINE';
    const nprov=Object.values(p).filter(Boolean).length;const pct=Math.min(99,60+nprov*8+(a.online?7:0));
    $('core-pct').textContent=pct+'%';
    // bridge
    if(a.online){$('br-st').className='st on';$('br-st').textContent='DESKTOP AGENT LIVE'+(a.active_window?' · '+String(a.active_window).slice(0,32):'')+(a.cpu!=null?' · CPU '+Math.round(a.cpu)+'%':'');
      $('br-r').textContent='LIVE';$('bridge-ind').className='live';$('bridge-ind').textContent='◈ BRIDGE LIVE';}
    else{$('br-st').className='st off';$('br-st').textContent='NO DESKTOP AGENT DETECTED';$('br-r').textContent='OFFLINE';$('bridge-ind').className='off';$('bridge-ind').textContent='◇ BRIDGE OFFLINE';}
    // capability matrix
    const claude=p.anthropic;
    const rows=[
     ['Reasoning Core',(claude?'Claude · ':'')+model+' · 5 protocols','ok'],
     ['Live Web','Search + page reading, cited',''],
     ['Awareness','Clock · timetable · NWS weather',''],
     ['Memory','Persistent, auto-captured',''],
     ['Vision','Upload a screen, JARVIS reads it',''],
     ['Desktop',a.online?'Bridge LIVE — apps, files, shell':'Bridge agent (offline)',a.online?'ok':'warn'],
     ['Voice','Browser speech in and out',''],
    ];
    $('cap').innerHTML=rows.map(r=>`<div class="cap-row"><span class="k">${r[0]}</span><span class="v ${r[2]}">${esc(r[1])}</span></div>`).join('');
    $('comms-r').textContent=(b.active||0)+' BRAINS';
  }catch(_){}
}

/* ── greeting on boot ── */
async function greet(){
  try{const d=await API('/greeting');const g=(d.greeting||'').trim()||'Systems online. Good evening, Sir. Borfoli at your service.';
    addMsg('b',g);speak(g);}catch(_){addMsg('b','Systems online, Sir. Borfoli at your service.');}
}

/* ── boot ── */
tick();loadSystem();greet();
setInterval(loadSystem,25000);
setInterval(loadMem,60000);

/* ── PWA ── */
if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(()=>{});}
</script>
</body>
</html>"""

@app.route("/")
def index():
    return Response(HTML, mimetype='text/html')

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

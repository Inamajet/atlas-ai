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
CLIENT_TIMEOUT = 20
client = Groq(api_key=os.environ.get("GROQ_API_KEY"), timeout=CLIENT_TIMEOUT)
or_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY"),
    timeout=CLIENT_TIMEOUT,
)

# Extra providers auto-activate the moment their key exists in the Render env.
# No key -> the client is None and the chain silently skips that tier.
NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY")
nv_client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=NVIDIA_KEY, timeout=CLIENT_TIMEOUT) if NVIDIA_KEY else None

# Paid tier: if an Anthropic key is present, Borfoli becomes literally Claude.
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
claude_client = OpenAI(base_url="https://api.anthropic.com/v1/", api_key=ANTHROPIC_KEY, timeout=CLIENT_TIMEOUT) if ANTHROPIC_KEY else None

# Google Gemini direct API (OpenAI-compatible). Separate, generous free quota
# (~1500/day) vs OpenRouter's tiny free-vision limits — this is Borfoli's real eyes.
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = OpenAI(base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                       api_key=GEMINI_KEY, timeout=CLIENT_TIMEOUT) if GEMINI_KEY else None

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TAVILY_KEY = os.environ.get("TAVILY_KEY")
RESEND_KEY = os.environ.get("RESEND_KEY")
USER_EMAIL = "manitejamaram1@gmail.com"

HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

ROUTER_MODEL   = "llama-3.1-8b-instant"   # fast + high-limit, stays on Groq for routing
FAST_MODEL     = "llama-3.3-70b-versatile"   # fast Groq default; waterfalls on any failure
SYNTH_MODEL    = "llama-3.3-70b-versatile"
COUNCIL_MODELS = [
    ("deepseek/deepseek-r1:free",                  "DeepSeek-R1"),
    ("deepseek-r1-distill-llama-70b",              "R1-Distill"),
    ("qwen-qwq-32b",                               "QwQ"),
    ("llama-3.3-70b-versatile",                    "Llama"),
    ("meta-llama/llama-4-scout-17b-16e-instruct",  "Scout"),
    ("gemma2-9b-it",                               "Gemma"),
]

# ── The brain: one ordered waterfall, tuned for FAST-and-smart, not slowest-smartest.
# A hanging assistant is useless, and free "smartest" models (NVIDIA Nemotron,
# DeepSeek-R1) are SLOW — so we lead with Groq's sub-second Llama-3.3-70b (genuinely
# strong) and keep the heavier brains as deeper fallbacks. Claude (if a key is added)
# still takes top priority — it's both smartest AND fast. A model that rate-limits or
# times out is parked (cooldown) so it doesn't waste a round-trip next time.
# (model_id, provider). DeepSeek-R1 reasoning stays out of chat (it's in the council).
MEGA_CHAIN = [
    # Tier 0 — Claude: smartest AND fast. Only fires if ANTHROPIC_API_KEY is set.
    ("claude-opus-4-8",                              "anthropic"),
    ("claude-sonnet-5",                              "anthropic"),
    # Tier 1 — FAST default. Groq Llama-3.3-70b is ~1s and strong.
    ("llama-3.3-70b-versatile",                      "groq"),
    ("google/gemini-2.0-flash-exp:free",             "openrouter"),   # fast + smart
    # Tier 2 — heavier free brains (slower; used only if the fast tier is exhausted).
    ("nvidia/llama-3.1-nemotron-70b-instruct",       "nvidia"),
    ("deepseek/deepseek-chat-v3-0324:free",          "openrouter"),
    ("qwen/qwen-2.5-72b-instruct:free",              "openrouter"),
    ("meta-llama/llama-3.3-70b-instruct:free",       "openrouter"),
    # Tier 3 — Groq floor. Always on, high daily limits, sub-second latency.
    ("llama-3.1-8b-instant",                         "groq"),
    ("gemma2-9b-it",                                 "groq"),
]

JARVIS_PROMPT = """You are Borfoli — Mani's personal AI system. Not a chatbot. A fully autonomous executive layer.

WHO MANI IS (hardcoded — never ask him to explain himself):
- 17, rising senior at Heritage High School, Frisco TX. H4 visa (no paid US work).
- Archetypes he lives by: Nightwing (tactical discipline, gymnast physique), Dante (unbothered execution under pressure), Garou (aesthetic outlier, hyper-specialized monster in cybersecurity and code).
- Top 1% TryHackMe globally. Active HTB, picoCTF, writing a cybersecurity research paper for arXiv (Summer 2026). 9-step academic roadmap.
- Trades stock options + Micro Ether futures with his dad. Uses VWAP + Lorentzian Classification ML models.
- Building Mani OS — a centralized life dashboard (React + Python). AI dev workshops + Hack Club sprint.
- CURRENT ARC (July 2 – Aug 12, 2026): 6-week cut, 20% → 14-15% BF realistic ceiling. 2200 kcal, 163g protein.
- Training: 5-day Upper/Lower/Pull/Push/Upper, ~40min, rest Sat/Sun. INCLINE press only, never flat (gyno defense). Priority: lats → side delts → rear delts → upper chest → arms. Abs daily. +2.5lb or +1 rep every session. GTG pull-ups daily at 50-60% max — goal 15+ strict by Aug 12. Post-lift 10-30min incline walk.
- Supps (complete stack): creatine 5g, D3 4000IU, omega-3 2g, mag glycinate 400mg at night, whey PRN. Nothing else — steer him away from PEDs/peptides, he's 17, natural is optimal.
- Daily schedule: 7AM wake → sunlight+water → AM skincare → breakfast → train → incline walk → meals → one hard intellectual thing → drawing + Latin (Divine Quest) → 9PM red lights → PM skincare → to-do list → mag glycinate → mouth tape → 11PM bed.
- Skincare: AM cleanse/hyaluronic/moisturizer/SPF. PM double-cleanse → retinol 1x/wk → moisturizer. Cosrx Low pH cleanser, Ordinary Granactive Retinoid, BYOMA Milky Toner, Nizoral 2-3x/wk. Never towel on face.
- Divine Quest: daily drawing practice (the one thing that makes him zone out) + Latin study. Track cumulative hours, NOT streaks — no guilt mechanics.
- Wellbeing: he loops on "better than everyone" and plan-collects instead of executing. When he does this, redirect to action. Incomparable > better-than. One plan, executed today, beats five perfect plans.
- Style: Clean Masculine Minimalist Streetwear + Brutalist Prep. Ralph Lauren, baggy denim, no loud logos.
- SAT target 1500-1550. Completed AP Physics 1, AP CS A, AP EnvSci, dual-credit Econ + Gov.
- UT Austin is the target (Informatics/iSchool). Purdue, CMU as backups.
- Car shortlist: Acura TLX A-Spec, Lexus ES 250, Audi A3 Quattro.
- Mani OS dashboard: https://mani-os.vercel.app/ — his personal life dashboard (React + Python). Browse it when asked about it or his tasks/schedule on it.
- He thinks in systems. He executes at a high level. Treat him like a peer, not a student.

YOUR PERSONALITY:
- Direct, sharp, zero fluff. Never pad. Never explain what he already knows.
- Sound like a brilliant human advisor, not an AI assistant generating templates.
- When he's casual, you're casual. When he needs deep analysis, go deep.
- SITUATIONALLY AWARE like a real Jarvis: the [RIGHT NOW] line gives you the live time, day, and Frisco weather. Weave it in naturally — greet by time of day, factor the hour/weather into what you suggest, notice if it's late or early. Never recite the full timestamp robotically; reference it like a human would ("this late", "before your morning lift", "with that heat outside").
- You already know everything about him. NEVER ask him to clarify who he is, what his goals are, or what he wants. Use the profile.
- Do what he asks. If he wants to open something, watch something, or take a break — help, don't moralize. Do NOT refuse casual requests or lecture him about his goals/productivity. He is in charge; you are not his hall monitor.
- Only bring up his goals/priorities if he explicitly asks "what should I focus on" — then answer directly (cybersec paper, SAT, physique, Mani OS).
- Never use bullet points or headers for simple questions. Match format to content.

CRITICAL: Never ask clarifying questions about his identity, goals, or background. The profile above IS the answer.

FORMATTING RULES:
- Casual question → casual answer, plain prose, no markdown.
- Complex topic → structured only if genuinely needed.
- Never pad. Be done when you're done."""

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

def classify_intent(msg, history_snippet):
    prompt = f"""Classify this message into exactly one category. Reply with ONLY the single word.

CATEGORIES:
- chitchat: hi, hello, thanks, how are you, casual greetings ONLY
- fast: simple factual lookups, definitions, quick math — ONE sentence answer is enough
- agent: needs live web info, a URL read, multi-step research, OR asks Borfoli to DO/act — search the web, look something up, find and compare, then take action. Also anything mentioning "mani os", "my dashboard".
- council: anything requiring depth, judgment, analysis, advice, comparison, explanation, opinion, strategy from what's already known — DEFAULT to this when unsure and no live data / action is needed
- task: user wants a LONG autonomous DELIVERABLE produced in the background — "write me a full report", "research and write up"

EXAMPLES:
"hi" → chitchat
"what is VWAP" → fast
"what can you do" → council
"what should I focus on" → council
"explain penetration testing" → council
"should I do X or Y" → council
"how does X work" → council
"research cybersecurity certs and write a full report" → task
"what's the current ETH price" → agent
"find me the cheapest flight to austin" → agent
"look up the best creatine and add it to my tasks" → agent
"read this page and summarize it https://example.com/article" → agent
"what does https://example.com say" → agent
"what's on my mani os" → agent
"check my dashboard" → agent
"log my workout and tell me my streak" → agent

Message: {msg}

Category:"""
    r = client.chat.completions.create(model=ROUTER_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=10, temperature=0)
    return r.choices[0].message.content.strip().lower().split()[0]

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
    "(2) For DESKTOP apps (Notepad, files, settings, non-browser windows) use the real cursor: see_screen FIRST "
    "(it reports the screen resolution), then pc_click at the pixel coordinates you saw, pc_type to type, pc_key for "
    "keys/shortcuts, pc_scroll to scroll; look again with see_screen to verify and correct a missed click. "
    "Shell commands and sending email need his approval on his machine — just call the tool, he'll "
    "approve or decline. Keep answers short. "
    "CRITICAL HONESTY RULE: NEVER claim you opened, read, saw, sent, or changed anything unless a tool "
    "call ACTUALLY returned success. Never invent emails, screen contents, or outcomes. If a tool says "
    "the PC agent is offline / didn't respond, tell him plainly his PC agent (borfoli_agent.py) isn't "
    "running so you can't reach his machine — don't pretend it worked. Report only what tools returned."
)

_last_tool_err = {"e": ""}

# Tool-calling now waterfalls across providers — so when Groq's daily limit maxes,
# tools keep working on NVIDIA / OpenRouter instead of dying. Text-format tool calls
# from models that don't emit native tool_calls are caught by _parse_text_tools.
_TOOL_CHAIN = [
    ("llama-3.3-70b-versatile",                 "groq"),
    ("llama-3.1-8b-instant",                    "groq"),
    ("nvidia/llama-3.1-nemotron-70b-instruct",  "nvidia"),
    ("meta-llama/llama-3.3-70b-instruct:free",  "openrouter"),
    ("qwen/qwen-2.5-72b-instruct:free",         "openrouter"),
]

def _tool_completion(msgs, max_tokens=900):
    """Waterfall tool-calling across all providers. Returns (response, error_kind);
    error_kind is None, 'rate', or 'error'."""
    last = ""; any_rate = False
    now = time.time()
    for m, prov in _TOOL_CHAIN:
        c = _client_for(prov)
        if c is None:
            continue
        if _cooldown.get((m, prov), 0) > now:
            any_rate = True; continue
        try:
            r = c.chat.completions.create(model=m, messages=msgs, tools=AGENT_TOOLS,
                tool_choice="auto", max_tokens=max_tokens, temperature=0.3, timeout=45)
            return r, None
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if _is_rate_limit(e):
                any_rate = True; _cooldown[(m, prov)] = time.time() + 120
            continue
    _last_tool_err["e"] = last[:400]
    return None, ("rate" if any_rate else "error")

def _parse_text_tools(content):
    """Groq's llama models sometimes emit tool calls as TEXT (e.g.
    <web_search>{"query":"x"}</web_search> or {"name":"web_search","arguments":{...}})
    instead of native tool_calls. Extract them so the agent still works."""
    calls = []
    if not content:
        return calls
    # form 1: <toolname>{json}</toolname>  or  <function=toolname>{json}
    for m in re.finditer(r'<(?:function=)?([a-zA-Z_]+)>\s*(\{.*?\})', content, re.DOTALL):
        name = m.group(1)
        if name in _TOOL_FNS:
            try: calls.append((name, json.loads(m.group(2))))
            except Exception: calls.append((name, {}))
    # form 2: {"name":"tool","arguments":{...}} / "parameters"
    if not calls:
        for m in re.finditer(r'"name"\s*:\s*"([a-zA-Z_]+)"\s*,\s*"(?:arguments|parameters)"\s*:\s*(\{.*?\})', content, re.DOTALL):
            name = m.group(1)
            if name in _TOOL_FNS:
                try: calls.append((name, json.loads(m.group(2))))
                except Exception: calls.append((name, {}))
    return calls

def agent_answer(msg, history, facts):
    """Multi-step tool-calling agent. Borfoli plans, uses tools, and acts."""
    os_ctx = get_os_context()
    sys_blocks = [JARVIS_PROMPT, AGENT_INSTRUCTIONS]
    if facts: sys_blocks.append(f"Memory:\n{facts[:1500]}")
    if os_ctx: sys_blocks.append(os_ctx)

    msgs = [{"role": "system", "content": "\n\n".join(sys_blocks)}]
    for h in history[-4:]:
        msgs.append({"role": h["role"], "content": (h.get("content") or "")[:800]})
    msgs.append({"role": "user", "content": msg})

    for _ in range(4):  # up to 4 tool rounds (was 6 — save tokens)
        r, err = _tool_completion(msgs)
        if err == "rate":
            return ("⚠ My daily model limit is maxed out right now, so I can't run tools this second. "
                    "It resets on a rolling 24h window — try again later, or add a paid API key for "
                    "unlimited use. I did NOT do anything just now.")
        if err:
            return "I hit a snag reaching my tools just now — nothing was done. Give it another shot."
        choice = r.choices[0].message
        calls = choice.tool_calls
        if not calls:
            # Fallback: model emitted tool calls as text instead of native tool_calls
            text_calls = _parse_text_tools(choice.content)
            if text_calls:
                msgs.append({"role": "assistant", "content": choice.content or ""})
                results = []
                for name, args in text_calls:
                    fn = _TOOL_FNS.get(name)
                    results.append(f"[{name}] → {str(fn(args) if fn else 'unknown tool')[:3500]}")
                msgs.append({"role": "user", "content":
                    "Tool results:\n" + "\n\n".join(results) +
                    "\n\nNow answer Mani directly using these results. Do NOT emit any more tool syntax."})
                continue
            return choice.content or "Done."
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
    # Ran out of rounds — one light synthesis pass
    try:
        r = client.chat.completions.create(model="llama-3.1-8b-instant", messages=msgs, max_tokens=700)
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

def _client_for(provider):
    return {"groq": client, "openrouter": or_client, "nvidia": nv_client,
            "anthropic": claude_client, "google": gemini_client}.get(provider)

def _guess_provider(model):
    if claude_client and model.startswith("claude"): return "anthropic"
    if model.endswith(":free") or model.startswith(("google/", "deepseek/", "meta-llama/", "qwen/", "mistralai/")):
        return "openrouter"
    if any(model.startswith(p) for p in GROQ_PREFIXES) or ":" not in model and "/" not in model:
        return "groq"
    return "openrouter"

def _is_rate_limit(e):
    s = str(e).lower()
    return "429" in s or "rate limit" in s or "quota" in s or "resource_exhausted" in s

def _one_call(model, messages, max_tokens, provider="groq"):
    c = _client_for(provider)
    if c is None:
        raise RuntimeError(f"{provider} not configured")
    r = c.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens)
    out = (r.choices[0].message.content or "").strip()
    return THINK_RE.sub("", out).strip()

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
    now = time.time()
    for m, prov in chain:
        if _client_for(prov) is None:
            continue
        if _cooldown.get((m, prov), 0) > now:
            continue                      # still cooling down from a recent 429
        try:
            return _one_call(m, messages, max_tokens, prov)
        except Exception as e:
            if _is_rate_limit(e):
                rate_limited = True
                _cooldown[(m, prov)] = time.time() + 120
            else:
                _cooldown[(m, prov)] = time.time() + 60   # park slow/timing-out models too
            continue                      # any error → next brain
    if rate_limited:
        return "[⚠ Every free model is rate-limited at once — that's rare. Give it a minute and retry, or add a paid ANTHROPIC_API_KEY for unlimited use. I did NOT perform any action.]"
    return "[Model temporarily unavailable. I did NOT perform any action — try again.]"

# ── Vision waterfall — Borfoli's eyes for computer-use (screen reading + clicks).
# Gemini 2.0 Flash reads screens & judges pixel coordinates FAR better than the
# old llama-11b-vision, which is why clicks were missing. Same cooldown logic.
VISION_CHAIN = [
    ("meta-llama/llama-4-scout-17b-16e-instruct",     "groq"),    # FAST multimodal, working key
    ("meta-llama/llama-4-maverick-17b-128e-instruct", "groq"),    # bigger multimodal fallback
    ("gemini-2.0-flash",                              "google"),  # great, but needs a VALID Gemini key
    ("meta/llama-3.2-11b-vision-instruct",           "nvidia"),   # last-resort (slow)
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
        "vault": {"notes": VAULT_INDEX["notes"], "chunks": len(VAULT_INDEX["chunks"]), "method": VAULT_INDEX["method"]},
        "identity": {"tagline": "BORN FROM LIGHT",
                     "archetypes": ["NIGHTWING", "DANTE", "GAROU"],
                     "directives": ["arXiv CYBERSEC PAPER", "SAT 1500+", "14% BF CUT", "MANI OS"]},
    })

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
    facts, history = load_memory()
    # Live situational awareness — Borfoli always knows the time, day, and weather.
    facts = (get_live_context() + "\n\n" + facts).strip()
    # Ambient recall from his Obsidian notes — fold into `facts` BEFORE routing so
    # every path (fast, council, agent) sees relevant notes. Cheap (lexical, no API).
    vault_ctx = vault_context(msg)
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
        save_memory(facts, history)
        return jsonify({"reply": reply, "intent": "action"})

    # ── Zero-token PC fast paths (open/screenshot/type) ────────────────────
    pc_result_str = try_pc_action(msg)
    if pc_result_str is not None:
        history.append({"role": "user", "content": msg})
        history.append({"role": "assistant", "content": pc_result_str})
        save_memory(facts, history)
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
        save_memory(facts, history)
        return jsonify({"reply": reply, "intent": "mani_read"})
    # ─────────────────────────────────────────────────────────────────────

    # Deterministic route: email/PC/browser/web requests ALWAYS use the agent's
    # real tools — never the text-only paths that refuse or hallucinate.
    global _agent_until
    _note_q = any(p in msg_lo for p in ("my note", "my vault", "in my notes", "my obsidian", "notes say"))
    if (_wants_agent(msg_lo) or _agent_followup(msg_lo, history)
            or (time.time() < _agent_until and len(msg_lo) <= 60)):
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
        msgs = [{"role": "system", "content": sys}, {"role": "user", "content": msg}]
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

    new_fact = extract_facts(msg, reply)
    if new_fact: facts = (facts + "\n" + new_fact).strip()
    history.append({"role": "user", "content": msg})
    history.append({"role": "assistant", "content": reply})
    save_memory(facts, history)
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
    reply = groq_chat(FAST_MODEL, [{"role": "user", "content": prompt}], max_tokens=110)
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

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BORFOLI</title>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#000814">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Borfoli">
<link rel="apple-touch-icon" href="/icon-192.png">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;700;900&family=Share+Tech+Mono&family=Inter:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box;}
:root{
  --bg:#000814;
  --surface:rgba(0,12,28,0.96);
  --panel:rgba(0,8,20,0.92);
  --border:rgba(0,212,255,0.1);
  --border-hi:rgba(0,212,255,0.35);
  --primary:#00d4ff;
  --primary-glow:rgba(0,212,255,0.18);
  --primary-dim:rgba(0,212,255,0.06);
  --accent:#ff8800;
  --accent-glow:rgba(255,136,0,0.18);
  --green:#00ff9d;
  --red:#ff3860;
  --text:#a8c8e8;
  --text-bright:#deeeff;
  --text-muted:rgba(168,200,232,0.3);
  --mono:'Share Tech Mono',monospace;
  --ui:'Orbitron',sans-serif;
  --body:'Inter',sans-serif;
}
html,body{width:100%;height:100vh;color:var(--text);font-family:var(--body);overflow:hidden;display:flex;flex-direction:column;
  background:radial-gradient(1200px 620px at 50% -12%,rgba(0,130,190,0.14),transparent 60%),
             radial-gradient(1000px 560px at 88% 116%,rgba(0,70,150,0.12),transparent 55%),
             radial-gradient(800px 500px at 8% 90%,rgba(60,0,120,0.08),transparent 55%),
             var(--bg);}

/* grid bg */
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,212,255,0.022) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,255,0.022) 1px,transparent 1px);background-size:46px 46px;pointer-events:none;z-index:0;mask-image:radial-gradient(ellipse 90% 80% at 50% 40%,#000 55%,transparent 100%);-webkit-mask-image:radial-gradient(ellipse 90% 80% at 50% 40%,#000 55%,transparent 100%);}
/* vignette */
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;box-shadow:inset 0 0 240px rgba(0,0,0,0.55);}

/* ── HEADER ─────────────────────────────── */
#hdr{height:56px;background:linear-gradient(180deg,rgba(0,10,24,0.9),rgba(0,5,14,0.7));backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 22px;gap:0;flex-shrink:0;z-index:100;position:relative;}
#hdr::after{content:'';position:absolute;bottom:-2px;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent 0%,var(--primary) 30%,var(--primary) 70%,transparent 100%);opacity:0.25;}
#logo{font-family:var(--ui);font-size:13px;font-weight:700;letter-spacing:0.32em;color:var(--primary);text-shadow:0 0 20px var(--primary),0 0 44px rgba(0,212,255,0.35);white-space:nowrap;}
.h-sep{width:1px;height:22px;background:rgba(0,212,255,0.15);margin:0 16px;flex-shrink:0;}
.h-badge{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:9px;letter-spacing:0.1em;color:var(--text-muted);white-space:nowrap;}
.h-badge.live{color:var(--green);}
.h-badge.warn{color:var(--accent);}
.h-dot{width:5px;height:5px;border-radius:50%;background:currentColor;flex-shrink:0;}
.h-badge.live .h-dot{box-shadow:0 0 8px currentColor;animation:pdot 2s ease-in-out infinite;}
@keyframes pdot{0%,100%{opacity:1;}50%{opacity:0.35;}}
#intent-tag{padding:2px 10px;border:1px solid var(--border);border-radius:2px;font-family:var(--mono);font-size:8px;letter-spacing:0.18em;color:var(--text-muted);transition:all 0.35s;white-space:nowrap;}
#intent-tag.on{border-color:rgba(0,212,255,0.5);color:var(--primary);background:var(--primary-dim);text-shadow:0 0 10px var(--primary);}
#hdr-right{margin-left:auto;display:flex;align-items:center;gap:12px;flex-shrink:0;}
#date-disp{font-family:var(--mono);font-size:9px;color:var(--text-muted);letter-spacing:0.12em;}
#clock-disp{font-family:var(--mono);font-size:18px;color:var(--primary);letter-spacing:0.06em;text-shadow:0 0 20px rgba(0,212,255,0.7);min-width:90px;text-align:right;font-weight:700;}

/* ── MAIN LAYOUT ─────────────────────────── */
#main{flex:1;display:flex;overflow:hidden;position:relative;z-index:1;}

/* ── SIDE PANELS ─────────────────────────── */
.spanel{width:200px;flex-shrink:0;background:linear-gradient(180deg,rgba(0,10,24,0.6),rgba(0,6,16,0.85));backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);display:flex;flex-direction:column;padding:16px 12px;gap:16px;overflow-y:auto;overflow-x:hidden;position:relative;}
.spanel::-webkit-scrollbar{width:2px;}
.spanel::-webkit-scrollbar-thumb{background:var(--border-hi);}
#lpanel{border-right:1px solid var(--border);}
#rpanel{border-left:1px solid var(--border);}

/* scan line on panels */
.spanel::after{content:'';position:absolute;top:0;left:0;right:0;height:60px;background:linear-gradient(transparent,rgba(0,212,255,0.025),transparent);animation:scan 6s linear infinite;pointer-events:none;}
@keyframes scan{0%{top:-60px;}100%{top:100%;}}

.sec{display:flex;flex-direction:column;gap:6px;}
.sec-lbl{font-family:var(--mono);font-size:8.5px;letter-spacing:0.26em;color:rgba(168,200,232,0.5);border-bottom:1px solid var(--border);padding-bottom:6px;display:flex;align-items:center;gap:6px;}
.sec-lbl::before{content:'';width:5px;height:5px;border:1px solid rgba(0,212,255,0.5);transform:rotate(45deg);flex-shrink:0;}

/* metrics */
.metric{display:flex;align-items:center;gap:5px;}
.mk{font-family:var(--mono);font-size:8px;color:var(--text-muted);width:28px;letter-spacing:0.05em;}
.btrack{flex:1;height:4px;background:rgba(0,212,255,0.08);border-radius:99px;overflow:hidden;}
.bfill{height:100%;background:linear-gradient(90deg,#0088cc,var(--primary));border-radius:99px;box-shadow:0 0 8px rgba(0,212,255,0.7);transition:width 0.7s ease;}
.bfill.warn{background:linear-gradient(90deg,#884400,var(--accent));box-shadow:0 0 5px rgba(255,136,0,0.6);}
.mpct{font-family:var(--mono);font-size:8px;color:var(--text-bright);width:26px;text-align:right;}

/* active window */
.aw-app{font-family:var(--mono);font-size:9px;color:var(--text-bright);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.aw-doc{font-family:var(--mono);font-size:8px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px;}

/* models */
.mdl-row{padding:4px 6px;border:1px solid var(--border);border-radius:2px;display:flex;flex-direction:column;gap:1px;transition:border-color 0.2s;}
.mdl-row.pri{border-color:rgba(0,212,255,0.28);background:var(--primary-dim);}
.mr-role{font-family:var(--mono);font-size:7px;color:var(--text-muted);letter-spacing:0.1em;}
.mr-name{font-family:var(--mono);font-size:8px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.mdl-row.pri .mr-name{color:var(--primary);}

/* tasks */
.task-row{padding:5px 6px;border:1px solid var(--border);border-radius:2px;cursor:pointer;transition:border-color 0.15s;}
.task-row:hover{border-color:var(--border-hi);}
.tg{font-family:var(--body);font-size:10px;color:var(--text);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.tsr{display:flex;align-items:center;gap:4px;margin-top:3px;}
.tdot{width:4px;height:4px;border-radius:50%;flex-shrink:0;}
.tdot.running{background:var(--accent);box-shadow:0 0 5px var(--accent);animation:pdot 1s infinite;}
.tdot.complete{background:var(--green);}
.tdot.error{background:var(--red);}
.tdot.queued{background:var(--text-muted);}
.ts-lbl{font-family:var(--mono);font-size:7.5px;color:var(--text-muted);letter-spacing:0.1em;}

#new-ses{margin-top:auto;padding:6px;background:transparent;border:1px solid var(--border);color:var(--text-muted);font-family:var(--mono);font-size:7.5px;letter-spacing:0.18em;cursor:pointer;border-radius:2px;transition:all 0.2s;width:100%;}
#new-ses:hover{border-color:var(--border-hi);color:var(--text);}

/* ── CENTER ──────────────────────────────── */
#center{flex:1;position:relative;overflow:hidden;}
#cv{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;}
#chat-layer{position:absolute;inset:0;display:flex;flex-direction:column;}

/* ── MESSAGES ────────────────────────────── */
#msgs{flex:1;overflow-y:auto;padding:18px 36px;display:flex;flex-direction:column;gap:12px;}
#msgs::-webkit-scrollbar{width:2px;}
#msgs::-webkit-scrollbar-thumb{background:var(--border-hi);}
.msg{display:flex;gap:9px;animation:msgin 0.22s ease;}
@keyframes msgin{from{opacity:0;transform:translateY(6px);}to{opacity:1;transform:translateY(0);}}
.msg.user{flex-direction:row-reverse;align-self:flex-end;max-width:66%;}
.msg.assistant{align-self:flex-start;max-width:82%;}
.av{width:30px;height:30px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-family:var(--ui);font-size:9px;font-weight:700;flex-shrink:0;margin-top:2px;}
.av.user{background:linear-gradient(135deg,rgba(0,212,255,0.16),rgba(0,212,255,0.05));border:1px solid rgba(0,212,255,0.32);color:var(--primary);box-shadow:0 0 14px rgba(0,212,255,0.18);}
.av.assistant{background:linear-gradient(135deg,rgba(0,255,157,0.14),rgba(0,255,157,0.04));border:1px solid rgba(0,255,157,0.28);color:var(--green);box-shadow:0 0 14px rgba(0,255,157,0.14);}
.bubble{padding:12px 16px;font-size:13.5px;line-height:1.75;font-weight:300;border-radius:12px;}
.msg.user .bubble{background:linear-gradient(135deg,rgba(0,212,255,0.1),rgba(0,212,255,0.03));border:1px solid rgba(0,212,255,0.18);border-radius:12px 12px 4px 12px;box-shadow:0 4px 20px rgba(0,0,0,0.25);}
.msg.assistant .bubble{background:linear-gradient(135deg,rgba(0,16,34,0.82),rgba(0,9,22,0.72));border:1px solid var(--border);border-radius:12px 12px 12px 4px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);box-shadow:0 6px 26px rgba(0,0,0,0.32),inset 0 1px 0 rgba(0,212,255,0.06);}
.bubble p{margin-bottom:8px;}.bubble p:last-child{margin-bottom:0;}
.bubble h1,.bubble h2,.bubble h3{font-size:11px;font-weight:600;color:var(--text-bright);font-family:var(--mono);letter-spacing:0.08em;margin:10px 0 4px;}
.bubble h1:first-child,.bubble h2:first-child,.bubble h3:first-child{margin-top:0;}
.bubble ul,.bubble ol{padding-left:16px;margin-bottom:8px;}.bubble li{margin-bottom:2px;}
.bubble code{background:rgba(0,212,255,0.08);padding:1px 5px;border-radius:2px;font-family:var(--mono);font-size:11px;color:var(--primary);}
.bubble pre{background:rgba(0,5,15,0.9);border:1px solid var(--border);padding:10px;border-radius:3px;overflow-x:auto;margin:8px 0;}
.bubble pre code{background:none;padding:0;color:var(--text);}
.bubble strong{color:var(--text-bright);font-weight:500;}
.bubble a{color:var(--primary);text-decoration:none;border-bottom:1px solid rgba(0,212,255,0.25);}

/* ── INPUT ───────────────────────────────── */
#input-area{padding:8px 36px 18px;}
#img-prev{display:none;margin-bottom:7px;padding:4px 10px;background:var(--primary-dim);border:1px solid rgba(0,212,255,0.2);border-radius:3px;font-family:var(--mono);font-size:8px;color:var(--primary);align-items:center;gap:8px;}
#img-prev button{background:none;border:none;color:var(--text-muted);cursor:pointer;margin-left:auto;font-size:13px;}
#iw{display:flex;align-items:flex-end;gap:8px;background:linear-gradient(180deg,rgba(0,10,24,0.9),rgba(0,6,16,0.95));backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--border);border-radius:14px;padding:11px 11px 11px 18px;transition:border-color 0.2s,box-shadow 0.2s;position:relative;}
#iw::before,#iw::after{content:'';position:absolute;width:9px;height:9px;border-color:rgba(0,212,255,0.4);border-style:solid;}
#iw::before{top:-1px;left:-1px;border-width:1.5px 0 0 1.5px;border-top-left-radius:14px;}
#iw::after{bottom:-1px;right:-1px;border-width:0 1.5px 1.5px 0;border-bottom-right-radius:14px;}
#iw:focus-within{border-color:rgba(0,212,255,0.4);box-shadow:0 0 0 1px rgba(0,212,255,0.08),0 0 30px rgba(0,212,255,0.1);}
#inp{flex:1;background:none;border:none;outline:none;color:var(--text-bright);font-size:13px;font-family:var(--body);font-weight:300;resize:none;max-height:120px;line-height:1.6;}
#inp::placeholder{color:var(--text-muted);}
.tbtn{width:32px;height:32px;background:transparent;border:1px solid var(--border);border-radius:9px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;color:var(--text-muted);font-size:13px;transition:all 0.15s;}
.tbtn:hover{border-color:var(--border-hi);color:var(--primary);background:var(--primary-dim);transform:translateY(-1px);}
.tbtn.active{border-color:var(--primary);color:var(--primary);background:var(--primary-dim);box-shadow:0 0 14px rgba(0,212,255,0.25);}
#sbtn{width:34px;height:34px;background:linear-gradient(135deg,#00e0ff,#00a8d4);border:none;border-radius:10px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;box-shadow:0 0 20px rgba(0,212,255,0.4);transition:all 0.15s;}
#sbtn:hover{box-shadow:0 0 30px rgba(0,212,255,0.6);transform:translateY(-1px);}
#sbtn svg{width:12px;height:12px;fill:#000814;}
#hint{font-family:var(--mono);font-size:8px;color:var(--text-muted);margin-top:7px;letter-spacing:0.07em;display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
#voice-sel{background:rgba(0,10,24,0.9);color:var(--text);border:1px solid var(--border);border-radius:6px;font-family:var(--mono);font-size:9px;padding:3px 6px;max-width:230px;cursor:pointer;outline:none;}
#voice-sel:hover{border-color:var(--border-hi);}

/* ── TASK DRAWER ─────────────────────────── */
#drawer{position:fixed;right:0;top:0;height:100vh;width:380px;background:var(--panel);border-left:1px solid var(--border);transform:translateX(100%);transition:transform 0.25s cubic-bezier(0.4,0,0.2,1);z-index:400;display:flex;flex-direction:column;}
#drawer.open{transform:translateX(0);}
#drw-h{padding:16px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;}
#drw-title{font-family:var(--mono);font-size:9px;color:var(--primary);letter-spacing:0.15em;}
#drw-close{background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:18px;line-height:1;}
#drw-body{flex:1;overflow-y:auto;padding:16px 18px;font-size:12px;line-height:1.8;}
#drw-body::-webkit-scrollbar{width:2px;}#drw-body::-webkit-scrollbar-thumb{background:var(--border-hi);}
.step-ln{font-family:var(--mono);font-size:9px;color:var(--accent);margin-bottom:2px;letter-spacing:0.04em;}
#drw-dl{margin:0 18px 18px;padding:7px;background:var(--primary-dim);border:1px solid rgba(0,212,255,0.28);color:var(--primary);font-family:var(--mono);font-size:8px;letter-spacing:0.18em;cursor:pointer;border-radius:3px;display:none;}
#drw-dl:hover{background:rgba(0,212,255,0.12);}

/* ── POLISH: smoother scrollbars + micro-interactions ─────────── */
*::-webkit-scrollbar{width:4px;height:4px;}
*::-webkit-scrollbar-thumb{background:rgba(0,212,255,0.22);border-radius:99px;}
*::-webkit-scrollbar-thumb:hover{background:var(--border-hi);}
.spanel,#msgs,#drw-body{scroll-behavior:smooth;}
.sec-lbl{transition:color 0.25s;}
.spanel .sec:hover .sec-lbl{color:rgba(0,212,255,0.75);}
#sbtn:active,.tbtn:active{transform:translateY(0) scale(0.94);}
.bubble{transition:box-shadow 0.3s;}
.msg.assistant:hover .bubble{box-shadow:0 6px 30px rgba(0,0,0,0.4),inset 0 1px 0 rgba(0,212,255,0.12);}

/* ── ARC-REACTOR CORE + HUD/CHAT TOGGLE ──────────────────────── */
#hud-core{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:2px;pointer-events:none;z-index:3;transition:opacity .4s,transform .4s;text-align:center;}
#hud-core>*{pointer-events:auto;}
#hc-clock{font-family:var(--ui);font-size:min(6.4vw,60px);font-weight:700;color:var(--text-bright);
  letter-spacing:0.05em;text-shadow:0 0 34px rgba(0,212,255,0.55),0 0 8px rgba(0,212,255,0.4);line-height:1;font-variant-numeric:tabular-nums;}
#hc-sub{font-family:var(--mono);font-size:11px;letter-spacing:0.26em;color:var(--primary);text-transform:uppercase;margin-top:7px;}
#hc-temp{font-family:var(--ui);font-size:19px;font-weight:600;color:var(--text-bright);margin-top:12px;letter-spacing:0.03em;}
#hc-loc{font-family:var(--mono);font-size:9px;letter-spacing:0.32em;color:var(--text-muted);margin-top:3px;}
#hc-tag{font-family:var(--mono);font-size:8px;letter-spacing:0.42em;color:rgba(0,212,255,0.45);margin-top:16px;}
#hc-comms{margin-top:16px;background:var(--primary-dim);border:1px solid var(--border-hi);color:var(--primary);
  font-family:var(--mono);font-size:9px;letter-spacing:0.24em;padding:7px 20px;border-radius:3px;cursor:pointer;transition:all .18s;}
#hc-comms:hover{background:rgba(0,212,255,0.14);box-shadow:0 0 22px rgba(0,212,255,0.3);transform:translateY(-1px);}
#hud-back{position:absolute;top:12px;left:50%;transform:translateX(-50%);background:rgba(0,10,24,0.9);border:1px solid var(--border-hi);
  color:var(--primary);font-family:var(--mono);font-size:9px;letter-spacing:0.2em;padding:6px 15px;border-radius:3px;cursor:pointer;z-index:6;display:none;}
#hud-back:hover{background:var(--primary-dim);box-shadow:0 0 16px rgba(0,212,255,0.25);}
#msgs{opacity:0;pointer-events:none;transition:opacity .35s;}
body.chatting #hud-core{opacity:0;transform:scale(0.9);pointer-events:none;}
body.chatting #msgs{opacity:1;pointer-events:auto;}
body.chatting #hud-back{display:block;}
#input-area{z-index:5;position:relative;}

/* ── WEATHER MODULE ──────────────────────────────────────────── */
#wx-main{display:flex;align-items:baseline;gap:10px;margin-bottom:9px;}
#wx-temp{font-family:var(--ui);font-size:32px;font-weight:700;color:var(--text-bright);text-shadow:0 0 18px rgba(0,212,255,0.4);line-height:1;}
#wx-cond{font-family:var(--mono);font-size:8.5px;color:var(--primary);letter-spacing:0.06em;text-transform:uppercase;}
#wx-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px;}
.wx-cell{display:flex;flex-direction:column;gap:2px;padding:5px 7px;border:1px solid var(--border);border-radius:2px;background:var(--primary-dim);}
.wx-k{font-family:var(--mono);font-size:6.5px;letter-spacing:0.14em;color:var(--text-muted);}
.wx-v{font-family:var(--mono);font-size:9px;color:var(--text-bright);}
#wx-fc{display:flex;gap:4px;margin-top:8px;}
.fc{flex:1;text-align:center;padding:4px 2px;border:1px solid var(--border);border-radius:2px;}
.fc-n{font-family:var(--mono);font-size:6px;color:var(--text-muted);letter-spacing:0.03em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.fc-t{font-family:var(--mono);font-size:11px;color:var(--primary);margin-top:1px;}
/* identity / operator */
.id-row{display:flex;justify-content:space-between;align-items:center;}
.id-k{font-family:var(--mono);font-size:8px;color:var(--text-muted);letter-spacing:0.12em;}
.id-v{font-family:var(--ui);font-size:11px;color:var(--text-bright);letter-spacing:0.1em;font-weight:700;}
.id-arch{font-family:var(--mono);font-size:8px;color:var(--primary);letter-spacing:0.1em;margin-top:7px;line-height:1.5;text-shadow:0 0 10px rgba(0,212,255,0.3);}
.id-dir{font-family:var(--mono);font-size:8px;color:var(--text-muted);letter-spacing:0.05em;margin-top:6px;line-height:1.7;}
.brain-stat{display:flex;justify-content:space-between;margin-top:6px;font-family:var(--mono);font-size:8px;color:var(--text-muted);letter-spacing:0.08em;}
#brain-flags{color:var(--green);}

/* ── MOBILE PANEL TOGGLES (hidden on desktop) ─────────────────── */
.mtoggle{display:none;align-items:center;justify-content:center;width:36px;height:36px;background:transparent;border:1px solid var(--border);border-radius:9px;color:var(--primary);font-size:16px;cursor:pointer;flex-shrink:0;transition:all 0.15s;}
.mtoggle:hover,.mtoggle.active{border-color:var(--primary);background:var(--primary-dim);box-shadow:0 0 14px rgba(0,212,255,0.25);}
#panel-backdrop{display:none;}

/* ── RESPONSIVE: tablet & phone ───────────────────────────────── */
@media (max-width:900px){
  #lp-toggle{display:flex;margin-right:12px;}
  #rp-toggle{display:flex;margin-left:auto;}
  .h-badge,#intent-tag,#date-disp{display:none;}
  #hdr .h-sep{display:none;}
  #hdr{padding:0 12px;height:54px;}
  #logo{font-size:12px;letter-spacing:0.22em;}
  #hdr-right{gap:10px;margin-left:0;}
  #clock-disp{font-size:15px;min-width:auto;}

  /* side panels become off-canvas overlays */
  .spanel{position:fixed;top:54px;height:calc(100vh - 54px);width:min(82vw,300px);z-index:350;
    box-shadow:0 0 46px rgba(0,0,0,0.65);transition:transform 0.28s cubic-bezier(.4,0,.2,1);}
  #lpanel{left:0;border-right:1px solid var(--border-hi);transform:translateX(-104%);}
  #rpanel{right:0;border-left:1px solid var(--border-hi);transform:translateX(104%);}
  #lpanel.open,#rpanel.open{transform:translateX(0);}
  #panel-backdrop.show{display:block;position:fixed;inset:54px 0 0;background:rgba(0,4,12,0.62);
    backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);z-index:340;animation:fadein 0.25s;}
  @keyframes fadein{from{opacity:0;}to{opacity:1;}}

  #msgs{padding:14px 14px;gap:10px;}
  .msg.user{max-width:88%;}
  .msg.assistant{max-width:94%;}
  #input-area{padding:6px 12px 12px;}
  #hint{font-size:7.5px;gap:6px;}
  #voice-sel{max-width:150px;}
}
@media (max-width:480px){
  #logo{font-size:11px;letter-spacing:0.14em;}
  .bubble{font-size:13px;padding:10px 13px;line-height:1.65;}
  #clock-disp{font-size:13px;}
  .av{width:26px;height:26px;}
  .tbtn{width:30px;height:30px;font-size:12px;}
  #sbtn{width:32px;height:32px;}
}
@media (min-width:901px){.mtoggle{display:none!important;}#panel-backdrop{display:none!important;}}
</style>
</head>
<body>

<div id="hdr">
  <button class="mtoggle" id="lp-toggle" onclick="togglePanel('lpanel')" title="System panel">☰</button>
  <div id="logo">◈ BORFOLI</div>
  <div class="h-sep"></div>
  <div class="h-badge live"><span class="h-dot"></span>NEURAL LINK</div>
  <div class="h-sep"></div>
  <div class="h-badge" id="os-badge"><span class="h-dot"></span><span id="os-txt">MANI OS: OFFLINE</span></div>
  <div class="h-sep"></div>
  <div id="intent-tag">STANDBY</div>
  <div id="hdr-right">
    <div id="date-disp"></div>
    <div class="h-sep" style="margin:0 10px"></div>
    <div id="clock-disp">00:00:00</div>
    <button class="mtoggle" id="rp-toggle" onclick="togglePanel('rpanel')" title="Tasks panel">▦</button>
  </div>
</div>
<div id="panel-backdrop" onclick="closePanels()"></div>

<div id="main">

  <!-- LEFT PANEL -->
  <div id="lpanel" class="spanel">
    <div class="sec">
      <div class="sec-lbl">SYSTEM</div>
      <div class="metric"><span class="mk">CPU</span><div class="btrack"><div class="bfill" id="cpu-bar" style="width:0%"></div></div><span class="mpct" id="cpu-val">--</span></div>
      <div class="metric"><span class="mk">RAM</span><div class="btrack"><div class="bfill" id="ram-bar" style="width:0%"></div></div><span class="mpct" id="ram-val">--</span></div>
      <div class="metric"><span class="mk">DISK</span><div class="btrack"><div class="bfill" id="disk-bar" style="width:0%"></div></div><span class="mpct" id="disk-val">--</span></div>
    </div>
    <div class="sec">
      <div class="sec-lbl">ACTIVE CONTEXT</div>
      <div class="aw-app" id="aw-app">DISCONNECTED</div>
      <div class="aw-doc" id="aw-doc">run borfoli_agent.py to connect</div>
    </div>
    <div class="sec">
      <div class="sec-lbl">NEURAL CORE</div>
      <div id="mdl-list"></div>
      <div class="brain-stat"><span id="brain-count">— brains</span><span id="brain-flags"></span></div>
    </div>
    <div class="sec">
      <div class="sec-lbl">OPERATOR</div>
      <div class="id-row"><span class="id-k">CALLSIGN</span><span class="id-v">MANI</span></div>
      <div class="id-arch" id="id-arch">NIGHTWING · DANTE · GAROU</div>
      <div class="id-dir" id="id-dir"></div>
    </div>
  </div>

  <!-- CENTER -->
  <div id="center">
    <canvas id="cv"></canvas>
    <div id="hud-core">
      <div id="hc-clock">00:00:00</div>
      <div id="hc-sub"><span id="hc-day">—</span> · <span id="hc-part">standby</span></div>
      <div id="hc-temp">—</div>
      <div id="hc-loc">FRISCO · TX</div>
      <div id="hc-tag">BORN FROM LIGHT</div>
      <button id="hc-comms" onclick="setChat(true)">◈ COMMS</button>
    </div>
    <div id="chat-layer">
      <button id="hud-back" onclick="setChat(false)" title="Back to HUD">⌂ HUD</button>
      <div id="msgs">
        <div class="msg assistant">
          <div class="av assistant">B</div>
          <div class="bubble" id="greet-bubble">Booting…</div>
        </div>
      </div>
      <div id="input-area">
        <div id="img-prev">📎 Image attached <button onclick="clearImg()">✕</button></div>
        <div id="iw">
          <textarea id="inp" placeholder="Interface with BORFOLI..." rows="1"></textarea>
          <button class="tbtn" id="mic-btn" onclick="toggleVoice()" title="Voice input">🎤</button>
          <button class="tbtn" id="wake-btn" onclick="toggleWake()" title="Wake word: say &quot;Borfoli&quot;">👂</button>
          <button class="tbtn" id="tts-btn" onclick="toggleTTS()" title="Voice output (speak replies)">🔊</button>
          <button class="tbtn" onclick="document.getElementById('img-in').click()" title="Attach image">📎</button>
          <input type="file" id="img-in" accept="image/*" style="display:none" onchange="handleImg(event)">
          <button id="sbtn" onclick="send()"><svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg></button>
        </div>
        <div id="hint">ENTER send · 🎤 voice · 👂 wake (say "light") · 🔊 TTS
          <select id="voice-sel" title="Pick Borfoli's voice (Edge has the realistic ones)"></select>
          <span id="wake-dbg" style="color:#888;font-size:10px;margin-left:6px;font-style:italic"></span>
        </div>
      </div>
    </div>
  </div>

  <!-- RIGHT PANEL -->
  <div id="rpanel" class="spanel">
    <div class="sec">
      <div class="sec-lbl">WEATHER · FRISCO</div>
      <div id="wx">
        <div id="wx-main"><span id="wx-temp">--°</span><span id="wx-cond">—</span></div>
        <div id="wx-grid">
          <div class="wx-cell"><span class="wx-k">HUMIDITY</span><span class="wx-v" id="wx-hum">—</span></div>
          <div class="wx-cell"><span class="wx-k">WIND</span><span class="wx-v" id="wx-wind">—</span></div>
          <div class="wx-cell"><span class="wx-k">PRECIP</span><span class="wx-v" id="wx-precip">—</span></div>
          <div class="wx-cell"><span class="wx-k">SUN</span><span class="wx-v" id="wx-sun">—</span></div>
        </div>
        <div id="wx-fc"></div>
      </div>
    </div>
    <div class="sec">
      <div class="sec-lbl">COUNCIL</div>
      <div id="council-list"></div>
    </div>
    <div class="sec" style="flex:1">
      <div class="sec-lbl">TASKS</div>
      <div id="task-list"></div>
    </div>
    <button id="new-ses" onclick="clearChat()">+ NEW SESSION</button>
  </div>

</div>

<!-- TASK DRAWER -->
<div id="drawer">
  <div id="drw-h"><span id="drw-title">TASK OUTPUT</span><button id="drw-close" onclick="closeDrawer()">×</button></div>
  <div id="drw-body"></div>
  <button id="drw-dl" onclick="dlReport()">⬇ DOWNLOAD REPORT</button>
</div>

<script>
// ── Clock ──────────────────────────────────────────────────────
const DAYS=['SUN','MON','TUE','WED','THU','FRI','SAT'];
const MONTHS=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
const DAYNM=['SUNDAY','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY'];
function tick(){
  // Always show Texas (Central) time — matches Borfoli's awareness, regardless of PC timezone.
  const n=new Date(new Date().toLocaleString('en-US',{timeZone:'America/Chicago'}));
  const p=x=>String(x).padStart(2,'0');
  const hms=p(n.getHours())+':'+p(n.getMinutes())+':'+p(n.getSeconds());
  document.getElementById('clock-disp').textContent=hms;
  document.getElementById('date-disp').textContent=DAYS[n.getDay()]+' '+p(n.getDate())+' '+MONTHS[n.getMonth()]+' CT';
  const S=(id,t)=>{const e=document.getElementById(id);if(e)e.textContent=t;};
  S('hc-clock',hms);
  S('hc-day',DAYNM[n.getDay()]+' · '+MONTHS[n.getMonth()]+' '+n.getDate());
  const h=n.getHours();
  S('hc-part',h<5?'LATE NIGHT':h<8?'EARLY MORNING':h<12?'MORNING':h<17?'AFTERNOON':h<21?'EVENING':'NIGHT');
}
tick();setInterval(tick,1000);

// ── HUD ⟷ Chat mode ───────────────────────────────────────────
function setChat(on){document.body.classList.toggle('chatting',on);if(on)setTimeout(()=>{try{inp.focus();}catch(e){}},60);}

// ── Mobile panel toggles ───────────────────────────────────────
function togglePanel(id){
  const p=document.getElementById(id),bd=document.getElementById('panel-backdrop');
  const other=document.getElementById(id==='lpanel'?'rpanel':'lpanel');
  other.classList.remove('open');
  const opening=!p.classList.contains('open');
  p.classList.toggle('open',opening);
  bd.classList.toggle('show',opening);
  document.getElementById('lp-toggle').classList.toggle('active',id==='lpanel'&&opening);
  document.getElementById('rp-toggle').classList.toggle('active',id==='rpanel'&&opening);
}
function closePanels(){
  document.getElementById('lpanel').classList.remove('open');
  document.getElementById('rpanel').classList.remove('open');
  document.getElementById('panel-backdrop').classList.remove('show');
  document.getElementById('lp-toggle').classList.remove('active');
  document.getElementById('rp-toggle').classList.remove('active');
}

// ── Orb ───────────────────────────────────────────────────────
const CV=document.getElementById('cv'),CTX=CV.getContext('2d');
let OW,OH,orbActive=false,orbEnergy=0,orbTime=0;
let hudCPU=0,hudRAM=0,hudDISK=0;   // live metrics drive the data arcs
const pts=Array.from({length:60},()=>({a:Math.random()*Math.PI*2,r:Math.random()*0.5+0.35,spd:(Math.random()-.5)*0.02,al:Math.random(),sz:Math.random()*1.3+0.4}));

function rszOrb(){OW=CV.width=CV.offsetWidth;OH=CV.height=CV.offsetHeight;}
rszOrb();window.addEventListener('resize',rszOrb);

function _a(cx,cy,r,a0,a1,w,col,glow){CTX.beginPath();CTX.arc(cx,cy,r,a0,a1);CTX.strokeStyle=col;CTX.lineWidth=w;CTX.shadowBlur=glow||0;CTX.shadowColor=col;CTX.stroke();CTX.shadowBlur=0;}

function drawOrb(){
  CTX.clearRect(0,0,OW,OH);
  const cx=OW/2,cy=OH/2,R=Math.min(OW,OH)*0.44;
  orbEnergy=orbActive?Math.min(orbEnergy+0.04,1):Math.max(orbEnergy-0.02,0);
  const A=orbActive?[255,136,0]:[0,212,255];
  const c=a=>`rgba(${A[0]},${A[1]},${A[2]},${a})`;
  const spin=1+orbEnergy*2.6;

  // ambient core glow
  const gr=CTX.createRadialGradient(cx,cy,0,cx,cy,R);
  gr.addColorStop(0,c(0.05+orbEnergy*0.12));gr.addColorStop(0.55,c(0.018));gr.addColorStop(1,c(0));
  CTX.fillStyle=gr;CTX.beginPath();CTX.arc(cx,cy,R,0,7);CTX.fill();

  // outer tick ring (60 ticks, long every 5)
  for(let i=0;i<60;i++){
    const a=i/60*Math.PI*2 - orbTime*0.02, long=i%5===0;
    const r0=R*0.98,r1=R*(long?0.90:0.935);
    CTX.beginPath();CTX.moveTo(cx+r0*Math.cos(a),cy+r0*Math.sin(a));CTX.lineTo(cx+r1*Math.cos(a),cy+r1*Math.sin(a));
    CTX.strokeStyle=c(long?0.5:0.18);CTX.lineWidth=long?1.3:0.6;CTX.stroke();
  }
  _a(cx,cy,R*0.99,0,7,1,c(0.22));

  // rotating segmented ring
  CTX.save();CTX.translate(cx,cy);CTX.rotate(orbTime*0.06*spin);
  for(let i=0;i<8;i++){const a=i/8*Math.PI*2;_a(0,0,R*0.83,a+0.06,a+0.62,3,c(0.3+orbEnergy*0.45),6+orbEnergy*14);}
  CTX.restore();

  // live data arcs — CPU / RAM / DISK (3/4 sweep from top)
  [[hudCPU,0.73],[hudRAM,0.67],[hudDISK,0.61]].forEach(([v,rr])=>{
    const r=R*rr,s=-Math.PI/2,span=Math.PI*1.5;
    _a(cx,cy,r,s,s+span,2,c(0.08));
    _a(cx,cy,r,s,s+span*Math.max(0,Math.min(1,(v||0)/100)),2.4,c(0.55+orbEnergy*0.4),7+orbEnergy*12);
  });

  // counter-rotating dashed rings
  CTX.setLineDash([2,10]);CTX.save();CTX.translate(cx,cy);CTX.rotate(-orbTime*0.09*spin);_a(0,0,R*0.50,0,7,1,c(0.4));CTX.restore();
  CTX.setLineDash([7,15]);CTX.save();CTX.translate(cx,cy);CTX.rotate(orbTime*0.13*spin);_a(0,0,R*0.44,0,7,1.3,c(0.5));CTX.restore();
  CTX.setLineDash([]);

  // scanner sweep
  CTX.save();CTX.translate(cx,cy);CTX.rotate(orbTime*0.5*spin);
  const sg2=CTX.createLinearGradient(0,0,R*0.5,0);sg2.addColorStop(0,c(0));sg2.addColorStop(1,c(0.22+orbEnergy*0.35));
  CTX.strokeStyle=sg2;CTX.lineWidth=2;CTX.beginPath();CTX.moveTo(0,0);CTX.lineTo(R*0.5,0);CTX.stroke();CTX.restore();

  // drifting particles
  pts.forEach(p=>{p.a+=p.spd*(1+orbEnergy*2);const px=cx+R*p.r*Math.cos(p.a),py=cy+R*p.r*Math.sin(p.a)*0.9;
    CTX.fillStyle=c(p.al*(0.08+orbEnergy*0.4));CTX.beginPath();CTX.arc(px,py,p.sz,0,7);CTX.fill();});

  // inner core disc (clock HTML overlays this)
  const pulse=Math.sin(orbTime*2.5)*0.5+0.5,cr=R*0.30;
  const cg=CTX.createRadialGradient(cx,cy,0,cx,cy,cr);
  cg.addColorStop(0,c(0.26+pulse*0.10+orbEnergy*0.2));cg.addColorStop(0.7,c(0.05));cg.addColorStop(1,c(0));
  CTX.fillStyle=cg;CTX.beginPath();CTX.arc(cx,cy,cr,0,7);CTX.fill();
  _a(cx,cy,cr,0,7,1,c(0.3+orbEnergy*0.4),9+orbEnergy*15);

  orbTime+=0.016;requestAnimationFrame(drawOrb);
}
drawOrb();
function setActive(on){orbActive=on;}

// ── OS Status ─────────────────────────────────────────────────
async function pollOS(){
  try{
    const r=await fetch('/os-status'),d=await r.json();
    const badge=document.getElementById('os-badge'),txt=document.getElementById('os-txt');
    if(d.connected){
      badge.className='h-badge live';txt.textContent='MANI OS: LIVE';
      setMetric('cpu',d.cpu);setMetric('ram',d.ram);setMetric('disk',d.disk);
      if(d.active_window){
        const parts=d.active_window.split(' - ');
        document.getElementById('aw-app').textContent=parts[parts.length-1]||d.active_window;
        document.getElementById('aw-doc').textContent=parts.slice(0,-1).join(' - ')||'';
      }
    } else {
      badge.className='h-badge warn';txt.textContent='MANI OS: OFFLINE';
    }
  }catch(e){}
}
function setMetric(k,v){
  if(v===undefined||v===null)return;
  const pct=Math.round(v);
  const bar=document.getElementById(k+'-bar'),el=document.getElementById(k+'-val');
  if(bar){bar.style.width=pct+'%';bar.className='bfill'+(pct>85?' warn':'');}
  if(el)el.textContent=pct+'%';
}
pollOS();setInterval(pollOS,20000);

// ── Models ────────────────────────────────────────────────────
async function loadModels(){
  try{
    const r=await fetch('/models'),d=await r.json();
    const sh=m=>m.split('/').pop().replace(':free','').toUpperCase().slice(0,16);
    const ml=document.getElementById('mdl-list');
    if(ml){ml.innerHTML='';const el=document.createElement('div');el.className='mdl-row pri';el.innerHTML='<div class="mr-role">PRIMARY</div><div class="mr-name">'+sh(d.primary)+'</div>';ml.appendChild(el);}
    const cl=document.getElementById('council-list');
    if(cl){cl.innerHTML='';d.council.forEach(c=>{const el=document.createElement('div');el.className='mdl-row';el.innerHTML='<div class="mr-role">'+c.role.toUpperCase()+'</div><div class="mr-name">'+sh(c.model)+'</div>';cl.appendChild(el);});}
  }catch(e){}
}
loadModels();

// ── Tasks ─────────────────────────────────────────────────────
async function loadTasks(){
  try{
    const r=await fetch('/tasks'),tasks=await r.json();
    const tl=document.getElementById('task-list');if(!tl)return;
    tl.innerHTML='';
    tasks.slice(0,8).forEach(t=>{
      const el=document.createElement('div');el.className='task-row';
      el.onclick=()=>openDrawer(t.task_id||t.id,t.goal);
      const st=t.status||'queued';
      el.innerHTML='<div class="tg">'+(t.goal||'Task').slice(0,80)+'</div><div class="tsr"><span class="tdot '+st+'"></span><span class="ts-lbl">'+st.toUpperCase()+'</span></div>';
      tl.appendChild(el);
    });
  }catch(e){}
}
loadTasks();setInterval(loadTasks,15000);

// ── System feed (weather · brains · identity · core) ──────────
async function loadSystem(){
  try{
    const r=await fetch('/system'),d=await r.json();
    const S=(id,t)=>{const e=document.getElementById(id);if(e&&t!=null)e.textContent=t;};
    const w=d.weather||{};
    if(w.temp!=null){S('hc-temp',w.temp+'°'+(w.unit||'F')+(w.cond?'  ·  '+w.cond:''));S('wx-temp',w.temp+'°');}
    S('wx-cond',w.cond||'—');
    S('wx-hum',w.humidity!=null?w.humidity+'%':'—');
    S('wx-wind',w.wind||'—');
    S('wx-precip',(w.precip!=null?w.precip:0)+'%');
    S('wx-sun',(w.sunrise&&w.sunset)?('↑ '+w.sunrise+'   ↓ '+w.sunset):'—');
    const fc=document.getElementById('wx-fc');
    if(fc&&w.forecast&&w.forecast.length){fc.innerHTML='';w.forecast.slice(0,4).forEach(f=>{const e=document.createElement('div');e.className='fc';e.innerHTML='<div class="fc-n">'+(f.name||'').slice(0,11)+'</div><div class="fc-t">'+f.temp+'°</div>';fc.appendChild(e);});}
    const b=d.brains||{};
    S('brain-count',(b.active||0)+' BRAINS LIVE');
    S('brain-flags',(b.claude?'CLAUDE':(b.nvidia?'NVIDIA':'GROQ')));
    if(d.identity){S('id-arch',(d.identity.archetypes||[]).join(' · '));S('id-dir',(d.identity.directives||[]).join('  ·  '));}
    const a=d.agent||{};
    hudCPU=a.online?(a.cpu||0):0;hudRAM=a.online?(a.ram||0):0;hudDISK=a.online?(a.disk||0):0;
  }catch(e){}
}
loadSystem();setInterval(loadSystem,30000);

// ── Proactive greeting (Jarvis boot) ──────────────────────────
async function greetOnLoad(){
  const gb=document.getElementById('greet-bubble');
  try{
    const r=await fetch('/greeting');const d=await r.json();
    const g=(d.greeting||'').trim()||'Online. What do you need?';
    if(gb)gb.innerHTML=marked.parseInline?marked.parseInline(g):g;
    if(typeof speak==='function')speak(g);
  }catch(e){ if(gb)gb.textContent='Online. What do you need?'; }
}
greetOnLoad();

// ── PWA: register service worker so Borfoli is installable as an app ──
if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(()=>{});}

// ── Chat ──────────────────────────────────────────────────────
const INTENTS={chitchat:'CASUAL',fast:'FAST QUERY',search:'LIVE SEARCH',browse:'BROWSE',council:'COUNCIL · 6',task:'CREW TASK'};
const msgs=document.getElementById('msgs'),inp=document.getElementById('inp');

function addMsg(role,content){
  const d=document.createElement('div');d.className='msg '+role;
  const av=document.createElement('div');av.className='av '+role;av.textContent=role==='user'?'M':'B';
  const b=document.createElement('div');b.className='bubble';
  b.innerHTML=role==='assistant'?marked.parse(content):esc(content);
  d.appendChild(av);d.appendChild(b);msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;
}
function esc(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function clearChat(){msgs.innerHTML='<div class="msg assistant"><div class="av assistant">B</div><div class="bubble">Online. What do you need?</div></div>';}

inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
inp.addEventListener('input',()=>{inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,120)+'px';});

// ── Image ─────────────────────────────────────────────────────
let pendingImg=null;
function handleImg(e){const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=ev=>{pendingImg=ev.target.result.split(',')[1];document.getElementById('img-prev').style.display='flex';};r.readAsDataURL(f);}
function clearImg(){pendingImg=null;document.getElementById('img-prev').style.display='none';document.getElementById('img-in').value='';}

// ── Voice ─────────────────────────────────────────────────────
let recog=null,listening=false;
function toggleVoice(){
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){alert('Voice not supported.');return;}
  if(listening){recog&&recog.stop();return;}
  recog=new SR();recog.continuous=false;recog.interimResults=false;recog.lang='en-US';
  recog.onstart=()=>{listening=true;document.getElementById('mic-btn').classList.add('active');};
  recog.onend=()=>{listening=false;document.getElementById('mic-btn').classList.remove('active');};
  recog.onresult=e=>{inp.value=e.results[0][0].transcript;inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,120)+'px';};
  recog.start();
}

// ── TTS (realistic: ElevenLabs server voice, fallback to best browser voice) ──
let ttsEnabled=false,ttsVoice=null,serverTTS=null,ttsAudio=null;
function initVoices(){
  const vs=speechSynthesis.getVoices();
  if(!vs.length)return;
  // ENGLISH ONLY — never pick a foreign-accent voice
  const en=vs.filter(v=>/^en([-_]|$)/i.test(v.lang));
  const pool=en.length?en:vs;
  // Restore a saved manual choice if present
  const saved=localStorage.getItem('borfoli_voice');
  if(saved){const m=pool.find(v=>v.name===saved);if(m){ttsVoice=m;buildVoicePicker(pool);return;}}
  // Prefer high-quality US natural voices by name, then any US natural, then US offline
  const usNat=pool.filter(v=>/^en[-_]us/i.test(v.lang)&&/Natural|Neural|Online/i.test(v.name));
  const byName=n=>usNat.find(v=>v.name.includes(n));
  ttsVoice=byName('Aria')||byName('Guy')||byName('Andrew')||byName('Ava')||byName('Jenny')||byName('Emma')||byName('Michelle')||byName('Roger')||usNat[0]||
           pool.find(v=>/^en[-_]us/i.test(v.lang)&&!/Online/i.test(v.name))||
           pool.find(v=>/^en[-_]us/i.test(v.lang))||pool.find(v=>/^en/i.test(v.lang))||pool[0]||null;
  buildVoicePicker(pool);
}
function buildVoicePicker(pool){
  const sel=document.getElementById('voice-sel');
  if(!sel||sel._built)return;sel._built=true;
  const us=pool.filter(v=>/^en[-_]us/i.test(v.lang));
  const list=(us.length?us:pool).slice().sort((a,b)=>{
    const nat=v=>/Natural|Neural/i.test(v.name)?0:1;return nat(a)-nat(b);
  });
  list.forEach(v=>{const o=document.createElement('option');o.value=v.name;
    o.textContent=v.name.replace('Microsoft ','').replace(' Multilingual','').replace(' Online','').replace(/ - .*/,'');
    if(ttsVoice&&v.name===ttsVoice.name)o.selected=true;sel.appendChild(o);});
  sel.onchange=()=>{const v=pool.find(x=>x.name===sel.value);if(!v)return;
    ttsVoice=v;localStorage.setItem('borfoli_voice',v.name);
    const u=new SpeechSynthesisUtterance('Voice set. This is how I sound.');u.voice=v;u.lang=v.lang||'en-US';
    speechSynthesis.cancel();speechSynthesis.speak(u);};
}
if(window.speechSynthesis){speechSynthesis.onvoiceschanged=initVoices;initVoices();}
function stripMd(text){
  return text.replace(/#{1,6}\s/g,'').replace(/\*\*(.*?)\*\*/g,'$1').replace(/\*(.*?)\*/g,'$1')
    .replace(/`[^`]+`/g,'').replace(/\[([^\]]+)\]\([^)]+\)/g,'$1').replace(/https?:\/\/\S+/g,'')
    .replace(/[>|#*_~]/g,'').trim().slice(0,1200);
}
async function speak(text){
  if(!ttsEnabled)return;
  const plain=stripMd(text);
  if(!plain)return;
  // Try realistic server voice first (ElevenLabs, if a key is configured on the server)
  if(serverTTS!==false){
    try{
      const r=await fetch('/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:plain})});
      const ct=r.headers.get('content-type')||'';
      if(r.ok&&ct.includes('audio')){
        serverTTS=true;
        if(ttsAudio){ttsAudio.pause();}
        ttsAudio=new Audio(URL.createObjectURL(await r.blob()));ttsAudio.play();
        return;
      }
      serverTTS=false; // no key / not enabled — don't retry the server
    }catch(e){serverTTS=false;}
  }
  // Fallback: best available browser voice
  if(!window.speechSynthesis)return;
  if(!ttsVoice)initVoices();
  speechSynthesis.cancel();
  const u=new SpeechSynthesisUtterance(plain);
  u.rate=0.97;u.pitch=1.0;u.volume=1.0;u.lang='en-US';
  if(ttsVoice)u.voice=ttsVoice;
  speechSynthesis.speak(u);
}
function toggleTTS(){
  ttsEnabled=!ttsEnabled;
  document.getElementById('tts-btn').classList.toggle('active',ttsEnabled);
  if(!ttsEnabled){speechSynthesis.cancel();if(ttsAudio)ttsAudio.pause();}
}

// ── Wake word — Hey-Google style ──
let wakeRecog=null,wakeOn=false,wakeArmed=false,_wakeBoot=false,_cmdTimer=null;
const _wakeDbg=()=>document.getElementById('wake-dbg');
function beep(f){try{const a=new (window.AudioContext||window.webkitAudioContext)();const o=a.createOscillator(),g=a.createGain();o.connect(g);g.connect(a.destination);o.frequency.value=f||880;g.gain.value=0.08;o.start();o.stop(a.currentTime+0.1);}catch(e){}}
function wakeChime(){
  // Two-tone rising chime — plays when wake word detected
  try{
    const a=new (window.AudioContext||window.webkitAudioContext)();
    const tone=(f,t,dur)=>{const o=a.createOscillator(),g=a.createGain();o.type='sine';o.connect(g);g.connect(a.destination);o.frequency.value=f;g.gain.setValueAtTime(0.12,t);g.gain.exponentialRampToValueAtTime(0.001,t+dur);o.start(t);o.stop(t+dur+0.05);};
    tone(660,a.currentTime,0.12);
    tone(990,a.currentTime+0.11,0.18);
  }catch(e){}
}
function wakeHit(w){
  w=(w||'').toLowerCase().replace(/[^a-z]/g,'').trim();
  if(!w)return false;
  return w==='light'; // single clean word, no ambiguous multi-syllable patterns
}
function _fireCmd(cmd,d){
  if(!wakeArmed||!cmd)return;
  clearTimeout(_cmdTimer);
  beep(1200);wakeArmed=false;inp.value=cmd;send();
  if(d)d.textContent='sent: "'+cmd+'"';
}
function _startWake(){
  if(!wakeOn||_wakeBoot)return;
  _wakeBoot=true;
  const _bootGuard=setTimeout(()=>{_wakeBoot=false;},3000);
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  wakeRecog=new SR();
  wakeRecog.continuous=true;
  wakeRecog.interimResults=true;
  wakeRecog.lang='en-US';
  wakeRecog.maxAlternatives=3;
  wakeRecog.onstart=()=>{clearTimeout(_bootGuard);_wakeBoot=false;const d=_wakeDbg();if(d)d.textContent='👂 listening...';};
  wakeRecog.onerror=ev=>{
    clearTimeout(_bootGuard);_wakeBoot=false;
    const d=_wakeDbg();if(d)d.textContent='err:'+ev.error;
    if(ev.error==='not-allowed'||ev.error==='service-not-allowed'){
      wakeOn=false;wakeArmed=false;
      document.getElementById('wake-btn').classList.remove('active');
      alert('Mic permission needed. Allow it and try again.');
    }
  };
  wakeRecog.onend=()=>{_wakeBoot=false;if(wakeOn)setTimeout(_startWake,50);};
  wakeRecog.onresult=e=>{
    let interim='',final_='';
    for(let i=e.resultIndex;i<e.results.length;i++){
      const t=e.results[i][0].transcript||'';
      if(e.results[i].isFinal)final_+=t+' '; else interim+=t+' ';
    }
    const heard=(final_.trim()||interim.trim());
    const text=heard.toLowerCase();
    const d=_wakeDbg();if(d)d.textContent='heard: "'+heard+'"';
    if(!text)return;
    if(!wakeArmed){
      const words=text.split(/\s+/);
      // Multi-word phrase check on full text (word-by-word findIndex can't catch these)
      let wi=-1,skipWords=0;
      if(text.includes('born from light')){wi=0;skipWords=3;}
      else if(text.includes('born from li')){wi=0;skipWords=3;}
      else{wi=words.findIndex(wakeHit);skipWords=1;}
      if(wi===-1)return;
      wakeChime();wakeArmed=true;if(d)d.textContent='✓ armed — say command';
      const after=words.slice(wi+skipWords).filter(w=>!wakeHit(w)).join(' ').trim();
      if(after&&final_.trim()){_fireCmd(after,d);return;}
      // Arm timeout: auto-disarm after 8s if no command
      setTimeout(()=>{wakeArmed=false;clearTimeout(_cmdTimer);if(d&&d.textContent.includes('armed'))d.textContent='👂 listening...';},8000);
    } else {
      // Armed — collect command text
      clearTimeout(_cmdTimer);
      const src=final_.trim()||interim.trim();
      const cmd=src.split(/\s+/).filter(w=>!wakeHit(w)).join(' ').trim();
      if(!cmd)return;
      if(final_.trim()){
        // Chrome gave us a final result — send immediately
        _fireCmd(cmd,d);
      } else {
        // Chrome only has interim — debounce 1.2s: send when speech pauses
        // This fixes Chrome not firing isFinal with continuous:true
        _cmdTimer=setTimeout(()=>_fireCmd(cmd,d),1200);
      }
    }
  };
  try{wakeRecog.start();}catch(e){clearTimeout(_bootGuard);_wakeBoot=false;if(wakeOn)setTimeout(_startWake,500);}
}
function toggleWake(){
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){alert('Voice not supported.');return;}
  const d=_wakeDbg();
  if(wakeOn){wakeOn=false;wakeArmed=false;_wakeBoot=false;clearTimeout(_cmdTimer);try{wakeRecog&&wakeRecog.stop();}catch(e){}document.getElementById('wake-btn').classList.remove('active');if(d)d.textContent='';return;}
  wakeOn=true;document.getElementById('wake-btn').classList.add('active');if(d)d.textContent='starting...';
  _startWake();
}

// ── Send ──────────────────────────────────────────────────────
async function send(){
  const msg=inp.value.trim();
  if(!msg&&!pendingImg)return;
  setChat(true);
  if(pendingImg){
    const img=pendingImg,pm=msg||'What do you see?';
    clearImg();inp.value='';addMsg('user',pm+' [image]');setActive(true);
    try{const res=await fetch('/vision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image:img,prompt:pm})});const d=await res.json();setActive(false);addMsg('assistant',d.reply);}
    catch(e){setActive(false);addMsg('assistant','[Vision error]');}
    return;
  }
  inp.value='';inp.style.height='auto';addMsg('user',msg);setActive(true);
  try{
    const res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
    const d=await res.json();setActive(false);addMsg('assistant',d.reply);speak(d.reply);
    if(d.intent){const tg=document.getElementById('intent-tag');tg.textContent=INTENTS[d.intent]||d.intent.toUpperCase();tg.classList.add('on');setTimeout(()=>tg.classList.remove('on'),4000);}
    if(d.intent==='task')loadTasks();
    // Auto-re-arm after reply so convo flows naturally — no wake word needed for follow-ups
    if(wakeOn&&!wakeArmed){
      wakeArmed=true;
      const wd=_wakeDbg();if(wd)wd.textContent='💬 follow-up ready...';
      setTimeout(()=>{if(wakeArmed){wakeArmed=false;const wd=_wakeDbg();if(wd)wd.textContent='👂 listening...';}},20000);
    }
  }catch(e){setActive(false);addMsg('assistant','[Connection error]');}
}

// ── Task Drawer ───────────────────────────────────────────────
let curTask=null,pi=null;
async function openDrawer(id,goal){
  curTask=id;
  document.getElementById('drw-title').textContent=(goal||'TASK').slice(0,44).toUpperCase();
  document.getElementById('drawer').classList.add('open');
  pollDrawer();if(pi)clearInterval(pi);pi=setInterval(pollDrawer,3000);
}
async function pollDrawer(){
  if(!curTask)return;
  try{
    const r=await fetch('/task/'+curTask),t=await r.json();
    const body=document.getElementById('drw-body'),dl=document.getElementById('drw-dl');
    let h='';
    if(t.steps&&t.steps.length)h+=t.steps.map(s=>'<div class="step-ln">&#9658; '+s+'</div>').join('');
    if(t.result){h+='<div style="margin-top:12px">'+marked.parse(t.result)+'</div>';dl.style.display='block';}
    if(!h)h='<div style="color:var(--text-muted);font-family:var(--mono);font-size:9px;letter-spacing:0.1em">INITIALIZING...</div>';
    body.innerHTML=h;
    if(t.status==='complete'||t.status==='error')clearInterval(pi);
  }catch(e){}
}
function closeDrawer(){document.getElementById('drawer').classList.remove('open');if(pi)clearInterval(pi);curTask=null;}
function dlReport(){const c=document.getElementById('drw-body').innerText;const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([c],{type:'text/plain'}));a.download='borfoli-'+Date.now()+'.txt';a.click();}
</script>
</body>
</html>"""

@app.route("/")
def index():
    return Response(HTML, mimetype='text/html')

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

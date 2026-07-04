import os, json, requests, threading, uuid, time, re, base64
from datetime import datetime
from flask import Flask, request, jsonify, Response
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
    auth = request.authorization
    if not auth or auth.password != APP_PASSWORD:
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Borfoli"'})

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
or_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY"),
)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TAVILY_KEY = os.environ.get("TAVILY_KEY")
RESEND_KEY = os.environ.get("RESEND_KEY")
USER_EMAIL = "manitejamaram1@gmail.com"

HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

ROUTER_MODEL   = "llama-3.1-8b-instant"
FAST_MODEL     = "llama-3.3-70b-versatile"
SYNTH_MODEL    = "llama-3.3-70b-versatile"
COUNCIL_MODELS = [
    ("deepseek/deepseek-r1:free",                  "DeepSeek-R1"),
    ("deepseek-r1-distill-llama-70b",              "R1-Distill"),
    ("qwen-qwq-32b",                               "QwQ"),
    ("llama-3.3-70b-versatile",                    "Llama"),
    ("meta-llama/llama-4-scout-17b-16e-instruct",  "Scout"),
    ("gemma2-9b-it",                               "Gemma"),
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
- You already know everything about him. NEVER ask him to clarify who he is, what his goals are, or what he wants. Use the profile.
- If he asks "what should I focus on?" answer directly — cybersec paper, SAT, physique, Mani OS, trading. Pick highest leverage.
- Never use bullet points or headers for simple questions. Match format to content.

CRITICAL: Never ask clarifying questions about his identity, goals, or background. The profile above IS the answer.

FORMATTING RULES:
- Casual question → casual answer, plain prose, no markdown.
- Complex topic → structured only if genuinely needed.
- Never pad. Be done when you're done."""

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
    state_summary = json.dumps(_trim_state(state), indent=2)[:5000]
    context = f"Mani OS live state (today: {today}):\n{state_summary}"
    msgs = [{"role": "system", "content": JARVIS_PROMPT}]
    if facts: msgs.append({"role": "system", "content": f"Memory:\n{facts}"})
    msgs.append({"role": "system", "content": context})
    for h in history[-6:]: msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": msg})
    return groq_chat(FAST_MODEL, msgs, max_tokens=800)

# ── Agentic tool loop — Borfoli's "hands" ──────────────────────────────────────
# Real capabilities: browse the web, search, read live Mani OS state, control the
# dashboard, and see Mani's PC. A tool-calling loop lets the model chain these.

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
    coord_note = (f"The real screen is {dims} pixels. If asked to locate something for a click, "
                  f"give pixel coordinates in that full range (top-left is 0,0). ") if dims else ""
    try:
        r = or_client.chat.completions.create(
            model="meta-llama/llama-3.2-11b-vision-instruct:free",
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": coord_note + (question or "What's on the screen right now? Describe what the user is doing and anything notable.")}
            ]}],
            max_tokens=1000,
        )
        return (f"[screen {dims}] " if dims else "") + r.choices[0].message.content.strip()
    except Exception as e:
        return f"Captured the screen but couldn't interpret it: {e}"

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
}

def agent_answer(msg, history, facts):
    """Multi-step tool-calling agent. Borfoli plans, uses tools, and acts."""
    os_ctx = get_os_context()
    sys_blocks = [JARVIS_PROMPT,
        "You have real tools — USE them, don't guess: search the web, open/read pages, "
        "read and control Mani's dashboard, see his screen, and fully control his PC — mouse, "
        "keyboard, apps, files, email. "
        "To operate the computer (click a button, fill a form, use an app): call see_screen "
        "FIRST to look, it reports the screen resolution; then pc_click at the pixel coordinates "
        "you saw, pc_type to type, pc_key for keys/shortcuts (enter, ctrl+c, alt+tab), pc_scroll "
        "to scroll. Look again with see_screen after acting to verify it worked, and correct if "
        "the click missed. "
        "Running shell commands and sending email require his approval on his machine — just call "
        "the tool, he'll approve or decline. Chain tools as needed, then give a short, direct "
        "answer. "
        "CRITICAL HONESTY RULE: NEVER claim you opened, read, saw, sent, or changed anything unless "
        "a tool call ACTUALLY returned a success result. Do not invent emails, screen contents, or "
        "outcomes. If a tool returns that the PC agent is offline / didn't respond, tell him plainly "
        "that his PC agent (borfoli_agent.py) isn't running, so you can't reach his machine — don't "
        "pretend it worked. Only report what the tools actually returned."]
    if facts: sys_blocks.append(f"Memory:\n{facts}")
    if os_ctx: sys_blocks.append(os_ctx)

    msgs = [{"role": "system", "content": "\n\n".join(sys_blocks)}]
    for h in history[-6:]:
        msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": msg})

    try:
        for _ in range(6):  # up to 6 tool rounds
            r = client.chat.completions.create(
                model=FAST_MODEL, messages=msgs, tools=AGENT_TOOLS,
                tool_choice="auto", max_tokens=1500, temperature=0.4,
            )
            choice = r.choices[0].message
            calls = choice.tool_calls
            if not calls:
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
                msgs.append({"role": "tool", "tool_call_id": c.id,
                             "content": str(result)[:6000]})
        # Ran out of rounds — final synthesis without tools
        r = client.chat.completions.create(model=FAST_MODEL, messages=msgs, max_tokens=1000)
        return r.choices[0].message.content or "Done."
    except Exception as e:
        # Fall back to council if tool calling errors out
        return council_answer(msg, history, facts)

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

GROQ_PREFIXES = ("openai/gpt-oss", "meta-llama", "qwen", "groq", "llama-")

def groq_chat(model, messages, max_tokens=1024):
    try:
        use_groq = any(model.startswith(p) for p in GROQ_PREFIXES) or ":" not in model
        c = client if use_groq else or_client
        r = c.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens)
        return r.choices[0].message.content.strip()
    except Exception as e:
        return f"[Model error: {e}]"

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

@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    msg = data.get("message", "").strip()
    if not msg: return jsonify({"reply": "Say something."})
    facts, history = load_memory()

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
    if _wants_agent(msg_lo) or _agent_followup(msg_lo, history):
        intent = "agent"
    else:
        history_snippet = " | ".join(h["content"][:60] for h in history[-3:]) if history else ""
        intent = classify_intent(msg, history_snippet)

    if intent == "chitchat":
        msgs = [{"role": "system", "content": JARVIS_PROMPT}, {"role": "user", "content": msg}]
        reply = groq_chat(FAST_MODEL, msgs, max_tokens=300)
    elif intent in ("search", "browse", "agent"):
        # Agentic loop — real web browsing + dashboard control, chained
        reply = agent_answer(msg, history, facts)
    elif intent == "council":
        reply = council_answer(msg, history, facts)
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
    return jsonify({
        "primary": FAST_MODEL,
        "synthesizer": SYNTH_MODEL,
        "council": [{"model": m, "role": r} for m, r in COUNCIL_MODELS],
        "router": ROUTER_MODEL,
        "total": len(COUNCIL_MODELS) + 2
    })

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
    try:
        r = or_client.chat.completions.create(
            model="meta-llama/llama-3.2-11b-vision-instruct:free",
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": full_prompt}
            ]}],
            max_tokens=1500
        )
        return jsonify({"reply": r.choices[0].message.content.strip()})
    except Exception as e:
        return jsonify({"reply": f"Vision error: {e}"})

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
</style>
</head>
<body>

<div id="hdr">
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
  </div>
</div>

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
      <div class="sec-lbl">PRIMARY MODEL</div>
      <div id="mdl-list"></div>
    </div>
  </div>

  <!-- CENTER -->
  <div id="center">
    <canvas id="cv"></canvas>
    <div id="chat-layer">
      <div id="msgs">
        <div class="msg assistant">
          <div class="av assistant">B</div>
          <div class="bubble">Online. What do you need?</div>
        </div>
      </div>
      <div id="input-area">
        <div id="img-prev">📎 Image attached <button onclick="clearImg()">✕</button></div>
        <div id="iw">
          <textarea id="inp" placeholder="Interface with BORFOLI..." rows="1"></textarea>
          <button class="tbtn" id="mic-btn" onclick="toggleVoice()" title="Voice input">🎤</button>
          <button class="tbtn" id="wake-btn" onclick="toggleWake()" title="Wake word: say &quot;Bor&quot; or &quot;Borfoli&quot;">👂</button>
          <button class="tbtn" id="tts-btn" onclick="toggleTTS()" title="Voice output (speak replies)">🔊</button>
          <button class="tbtn" onclick="document.getElementById('img-in').click()" title="Attach image">📎</button>
          <input type="file" id="img-in" accept="image/*" style="display:none" onchange="handleImg(event)">
          <button id="sbtn" onclick="send()"><svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg></button>
        </div>
        <div id="hint">ENTER send · 🎤 voice · 👂 wake (say "Bor") · 🔊 TTS
          <select id="voice-sel" title="Pick Borfoli's voice (Edge has the realistic ones)"></select>
        </div>
      </div>
    </div>
  </div>

  <!-- RIGHT PANEL -->
  <div id="rpanel" class="spanel">
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
function tick(){
  const n=new Date();
  const p=x=>String(x).padStart(2,'0');
  document.getElementById('clock-disp').textContent=p(n.getHours())+':'+p(n.getMinutes())+':'+p(n.getSeconds());
  document.getElementById('date-disp').textContent=DAYS[n.getDay()]+' '+p(n.getDate())+' '+MONTHS[n.getMonth()];
}
tick();setInterval(tick,1000);

// ── Orb ───────────────────────────────────────────────────────
const CV=document.getElementById('cv'),CTX=CV.getContext('2d');
let OW,OH,orbActive=false,orbEnergy=0,orbTime=0;
const rings=[{ph:0,tilt:0.4,spd:0.007},{ph:2.1,tilt:1.15,spd:-0.005},{ph:4.2,tilt:2.5,spd:0.009}];
const pts=Array.from({length:80},()=>({a:Math.random()*Math.PI*2,r:Math.random()*110+45,spd:(Math.random()-.5)*0.022,al:Math.random(),sz:Math.random()*1.4+0.5}));

function rszOrb(){OW=CV.width=CV.offsetWidth;OH=CV.height=CV.offsetHeight;}
rszOrb();window.addEventListener('resize',rszOrb);

function drawOrb(){
  CTX.clearRect(0,0,OW,OH);
  const cx=OW/2,cy=OH/2,R=Math.min(OW,OH)*0.22;
  orbEnergy=orbActive?Math.min(orbEnergy+0.04,1):Math.max(orbEnergy-0.025,0);
  const [r,g,b]=orbActive?[255,136,0]:[0,212,255];
  const col=`rgba(${r},${g},${b}`;

  // ambient glow
  const gr=CTX.createRadialGradient(cx,cy,0,cx,cy,R*1.9);
  gr.addColorStop(0,`${col},${0.07+orbEnergy*0.13})`);
  gr.addColorStop(0.5,`${col},${0.025+orbEnergy*0.06})`);
  gr.addColorStop(1,`${col},0)`);
  CTX.fillStyle=gr;CTX.beginPath();CTX.arc(cx,cy,R*1.9,0,Math.PI*2);CTX.fill();

  // outer dashed ring
  CTX.setLineDash([4,8]);
  CTX.strokeStyle=`${col},${0.08+orbEnergy*0.12})`;
  CTX.lineWidth=0.5;
  CTX.beginPath();CTX.arc(cx,cy,R*1.18,0,Math.PI*2);CTX.stroke();
  CTX.setLineDash([]);

  // rotating ellipses
  rings.forEach((ring,i)=>{
    ring.ph+=ring.spd*(1+orbEnergy*3.5);
    CTX.save();CTX.translate(cx,cy);CTX.rotate(ring.ph);
    CTX.beginPath();CTX.ellipse(0,0,R*0.9,R*0.29,ring.tilt,0,Math.PI*2);
    CTX.strokeStyle=`${col},${0.22+orbEnergy*0.5})`;
    CTX.lineWidth=0.9+orbEnergy*0.7;
    CTX.shadowBlur=6+orbEnergy*22;
    CTX.shadowColor=orbActive?'#ff8800':'#00d4ff';
    CTX.stroke();
    // orbital node
    const nx=R*0.9*Math.cos(ring.ph*2.5+i),ny=R*0.29*Math.sin(ring.ph*2.5+i);
    CTX.fillStyle=`${col},${0.55+orbEnergy*0.45})`;
    CTX.shadowBlur=10+orbEnergy*18;
    CTX.beginPath();CTX.arc(nx,ny,2+orbEnergy*1.8,0,Math.PI*2);CTX.fill();
    CTX.shadowBlur=0;CTX.restore();
  });

  // center sphere
  const pulse=Math.sin(orbTime*2.8)*0.5+0.5;
  const sr=R*(0.095+pulse*0.022+orbEnergy*0.04);
  const sg=CTX.createRadialGradient(cx,cy,0,cx,cy,sr);
  sg.addColorStop(0,`${col},1)`);
  sg.addColorStop(0.55,`${col},0.55)`);
  sg.addColorStop(1,`${col},0)`);
  CTX.shadowBlur=22+orbEnergy*45;
  CTX.shadowColor=orbActive?'#ff8800':'#00d4ff';
  CTX.fillStyle=sg;CTX.beginPath();CTX.arc(cx,cy,sr,0,Math.PI*2);CTX.fill();
  CTX.shadowBlur=0;

  // particles
  pts.forEach(p=>{
    p.a+=p.spd*(1+orbEnergy*2.2);
    const px=cx+p.r*Math.cos(p.a),py=cy+p.r*Math.sin(p.a)*0.44;
    CTX.fillStyle=`${col},${p.al*(0.1+orbEnergy*0.5)})`;
    CTX.beginPath();CTX.arc(px,py,p.sz,0,Math.PI*2);CTX.fill();
  });

  orbTime+=0.016;
  requestAnimationFrame(drawOrb);
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

// ── Wake word: "bor" / "borfoli" — forgiving fuzzy match, two-phase ──
// Say "Bor" or "Borfoli" then your command in one breath, OR just the wake word,
// hear the beep, then speak your command.
let wakeRecog=null,wakeOn=false,wakeArmed=false;
function beep(f){try{const a=new (window.AudioContext||window.webkitAudioContext)();const o=a.createOscillator(),g=a.createGain();o.connect(g);g.connect(a.destination);o.frequency.value=f||880;g.gain.value=0.08;o.start();o.stop(a.currentTime+0.1);}catch(e){}}
function wakeHit(w){
  w=(w||'').toLowerCase().replace(/[^a-z ]/g,'').trim();
  if(!w)return false;
  if(/\bbor\b/.test(w))return true;                                    // exact "bor"
  if(w.includes('borfo')||w.includes('orfol')||w.includes('portfol')||w.includes('borph'))return true; // borfoli-ish
  return /\b(borf\w*|boar?f\w*|bore|bored|board|boron|buffalo|before|boarding|boredom|forfe\w*)\b/.test(w); // mishearings
}
function toggleWake(){
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){alert('Voice not supported.');return;}
  if(wakeOn){wakeOn=false;wakeArmed=false;wakeRecog&&wakeRecog.stop();document.getElementById('wake-btn').classList.remove('active');return;}
  wakeRecog=new SR();wakeRecog.continuous=true;wakeRecog.interimResults=false;wakeRecog.lang='en-US';wakeRecog.maxAlternatives=5;
  wakeOn=true;document.getElementById('wake-btn').classList.add('active');
  wakeRecog.onresult=e=>{
    const res=e.results[e.results.length-1];
    if(!res.isFinal)return;
    const raw=(res[0].transcript||'').trim();
    if(!raw)return;
    // Armed: the whole utterance is the command.
    if(wakeArmed){wakeArmed=false;beep(1200);inp.value=raw;send();return;}
    // Check every STT alternative for the wake word.
    let chosen=null;
    for(let i=0;i<res.length;i++){const a=(res[i].transcript||'').trim();if(a.split(/\s+/).some(wakeHit)){chosen=a;break;}}
    if(!chosen)return;
    beep(880);
    // Drop leading wake-ish words, keep the command.
    let words=chosen.split(/\s+/);
    while(words.length&&wakeHit(words[0]))words.shift();
    const cmd=words.join(' ').replace(/^[\s,:.\-]+/,'').trim();
    if(cmd){inp.value=cmd;send();}
    else{wakeArmed=true;setTimeout(()=>{wakeArmed=false;},8000);} // listen for the command next
  };
  wakeRecog.onerror=ev=>{
    if(ev.error==='not-allowed'||ev.error==='service-not-allowed'){
      wakeOn=false;document.getElementById('wake-btn').classList.remove('active');
      alert('Mic permission is needed for the wake word. Allow the mic and try again.');
    }
  };
  wakeRecog.onend=()=>{if(wakeOn){setTimeout(()=>{try{wakeRecog.start();}catch(e){}},250);}};
  try{wakeRecog.start();}catch(e){}
}

// ── Send ──────────────────────────────────────────────────────
async function send(){
  const msg=inp.value.trim();
  if(!msg&&!pendingImg)return;
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

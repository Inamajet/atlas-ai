import os, json, requests, threading, uuid, time, re
from datetime import datetime
from flask import Flask, request, jsonify, Response
from groq import Groq
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
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
FAST_MODEL     = "nvidia/nemotron-3-ultra-550b-a55b:free"
SYNTH_MODEL    = "nvidia/nemotron-3-ultra-550b-a55b:free"
COUNCIL_MODELS = [
    ("nvidia/nemotron-3-ultra-550b-a55b:free",     "Nemotron"),
    ("nex-agi/nex-n2-pro:free",                    "Nex"),
    ("google/gemma-4-31b-it:free",                 "Gemma"),
    ("llama-3.3-70b-versatile",                    "Strategist"),
    ("meta-llama/llama-4-scout-17b-16e-instruct",  "Scout"),
    ("qwen/qwen3-32b",                             "Reasoner"),
]

JARVIS_PROMPT = """You are Borfoli — Mani's personal AI system. Not a chatbot. A fully autonomous executive layer.

WHO MANI IS (hardcoded — never ask him to explain himself):
- 17, rising senior at Heritage High School, Frisco TX. H4 visa (no paid US work).
- Archetypes he lives by: Nightwing (tactical discipline, gymnast physique), Dante (unbothered execution under pressure), Garou (aesthetic outlier, hyper-specialized monster in cybersecurity and code).
- Top 1% TryHackMe globally. Active HTB, picoCTF, writing a cybersecurity research paper for arXiv (Summer 2026). 9-step academic roadmap.
- Trades stock options + Micro Ether futures with his dad. Uses VWAP + Lorentzian Classification ML models.
- Building Mani OS — a centralized life dashboard (React + Python). AI dev workshops + Hack Club sprint.
- Physical protocol: 20k steps/day, 5-day split (lateral delts + upper back), GTG pull-ups. Target: complete physique shift September 2026.
- Style: Clean Masculine Minimalist Streetwear + Brutalist Prep. Ralph Lauren, baggy denim, no loud logos, Centella + Adapalene skincare.
- SAT target 1500-1550. Completed AP Physics 1, AP CS A, AP EnvSci, dual-credit Econ + Gov.
- UT Austin is the target (Informatics/iSchool). Purdue, CMU as backups.
- Car shortlist: Acura TLX A-Spec, Lexus ES 250, Audi A3 Quattro.
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
- chitchat: hi, hello, thanks, how are you, casual greetings only
- fast: direct questions, explanations, advice, opinions, recommendations
- search: needs current info, live prices, recent news, today's events
- council: big strategic decisions, life/career planning, multi-angle analysis
- task: user wants a DELIVERABLE — "write me a report", "research and summarize", "build", "create", "find and compile"

EXAMPLES:
"hi" → chitchat
"what is VWAP" → fast
"what should I focus on this summer" → council
"research the best cybersecurity certs and write a report" → task
"what's the current price of ETH" → search
"should I do X or Y" → council

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
    if facts: msgs.append({"role": "system", "content": f"Memory:\n{facts}"})
    for h in history[-10:]: msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": msg})
    return groq_chat(FAST_MODEL, msgs)

def search_answer(msg, history, facts):
    search_results = web_search(msg)
    context = f"Live search results:\n{search_results}\n\nAnswer using this data." if search_results else msg
    msgs = [{"role": "system", "content": JARVIS_PROMPT}]
    if facts: msgs.append({"role": "system", "content": f"Memory:\n{facts}"})
    for h in history[-6:]: msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": context})
    return groq_chat(FAST_MODEL, msgs, max_tokens=1500)

def council_answer(msg, history, facts):
    context_block = ""
    if facts: context_block += f"Memory:\n{facts}\n\n"
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

@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    msg = data.get("message", "").strip()
    if not msg: return jsonify({"reply": "Say something."})
    facts, history = load_memory()
    history_snippet = " | ".join(h["content"][:60] for h in history[-3:]) if history else ""
    intent = classify_intent(msg, history_snippet)

    if intent == "chitchat":
        reply = groq_chat(FAST_MODEL, [{"role": "system", "content": JARVIS_PROMPT}, {"role": "user", "content": msg}], max_tokens=300)
    elif intent == "search":
        reply = search_answer(msg, history, facts)
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

HTML = (
"<!DOCTYPE html>\n"
'<html lang="en">\n'
"<head>\n"
'<meta charset="UTF-8">\n'
'<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
"<title>BORFOLI</title>\n"
'<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>\n'
"<style>\n"
"@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');\n"
"*{margin:0;padding:0;box-sizing:border-box;}\n"
":root{\n"
"  --bg:#06070d;\n"
"  --surface:#0c0d18;\n"
"  --panel:#0f1020;\n"
"  --border:#1c1f35;\n"
"  --border2:#252844;\n"
"  --blue:#2196f3;\n"
"  --blue-dim:#1565c0;\n"
"  --blue-glow:rgba(33,150,243,0.12);\n"
"  --blue-bright:#64b5f6;\n"
"  --text:#ccd6f0;\n"
"  --muted:#3d4460;\n"
"  --muted2:#5a6380;\n"
"  --bright:#e8f0ff;\n"
"  --green:#00e676;\n"
"  --gold:#ffab40;\n"
"}\n"
"body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;height:100vh;overflow:hidden;display:flex;flex-direction:column;}\n"
"#header{height:50px;background:rgba(12,13,24,0.98);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 24px;gap:20px;flex-shrink:0;backdrop-filter:blur(12px);}\n"
"#wordmark{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:500;color:var(--blue-bright);letter-spacing:0.3em;}\n"
"#h-sep{width:1px;height:16px;background:var(--border2);}\n"
"#h-status{font-size:10px;color:var(--green);font-family:'JetBrains Mono',monospace;letter-spacing:0.1em;display:flex;align-items:center;gap:5px;}\n"
"#h-status::before{content:'';width:5px;height:5px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);}\n"
"#intent-tag{font-size:9px;padding:2px 9px;border:1px solid var(--border2);border-radius:20px;color:var(--muted2);font-family:'JetBrains Mono',monospace;letter-spacing:0.1em;transition:all 0.3s;}\n"
"#intent-tag.on{border-color:rgba(33,150,243,0.4);color:var(--blue-bright);background:var(--blue-glow);}\n"
"#h-right{margin-left:auto;display:flex;align-items:center;gap:14px;}\n"
"#h-model{font-size:10px;color:var(--muted2);font-family:'JetBrains Mono',monospace;} #h-model b{color:var(--blue-bright);font-weight:400;}\n"
"#clock{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--muted2);}\n"
"#body{flex:1;display:flex;overflow:hidden;}\n"
"#left{width:192px;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;padding:18px 12px 14px;gap:18px;flex-shrink:0;overflow-y:auto;}\n"
"#left::-webkit-scrollbar{width:2px;} #left::-webkit-scrollbar-thumb{background:var(--border2);}\n"
".lbl{font-size:8px;letter-spacing:0.25em;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-bottom:8px;}\n"
".kv{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:5px;}\n"
".kk{font-size:9px;color:var(--muted2);font-family:'JetBrains Mono',monospace;}\n"
".kv-val{font-size:10px;font-weight:500;color:var(--bright);font-family:'JetBrains Mono',monospace;}\n"
".kv-val.blue{color:var(--blue-bright);} .kv-val.green{color:var(--green);}\n"
".mdl{padding:5px 7px;border:1px solid var(--border);border-radius:3px;margin-bottom:3px;}\n"
".mdl.pri{border-color:rgba(33,150,243,0.3);background:var(--blue-glow);}\n"
".mdl-role{font-size:8px;color:var(--muted2);font-family:'JetBrains Mono',monospace;letter-spacing:0.1em;}\n"
".mdl-name{font-size:9px;color:var(--text);font-family:'JetBrains Mono',monospace;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}\n"
".mdl.pri .mdl-name{color:var(--blue-bright);}\n"
".div{height:1px;background:var(--border);}\n"
".ti{padding:7px;border:1px solid var(--border);border-radius:3px;margin-bottom:3px;cursor:pointer;transition:border-color 0.15s;}\n"
".ti:hover{border-color:var(--border2);}\n"
".tg{font-size:9px;color:var(--text);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}\n"
".ts{display:flex;align-items:center;gap:4px;margin-top:3px;}\n"
".td{width:4px;height:4px;border-radius:50%;}\n"
".td.running{background:var(--gold);box-shadow:0 0 4px var(--gold);}\n"
".td.complete{background:var(--green);} .td.error{background:#f44336;} .td.queued{background:var(--muted);}\n"
".ts-lbl{font-size:8px;color:var(--muted2);font-family:'JetBrains Mono',monospace;}\n"
"#ns{margin-top:auto;padding:6px;background:transparent;border:1px solid var(--border);color:var(--muted2);font-size:9px;font-family:'JetBrains Mono',monospace;letter-spacing:0.12em;cursor:pointer;border-radius:3px;transition:all 0.2s;width:100%;}\n"
"#ns:hover{border-color:var(--border2);color:var(--text);}\n"
"#center{flex:1;display:flex;flex-direction:column;position:relative;overflow:hidden;}\n"
"#orb-wrap{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;}\n"
"canvas{width:100%;height:100%;}\n"
"#orb-lbl{position:absolute;bottom:22px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--muted);font-family:'JetBrains Mono',monospace;letter-spacing:0.2em;white-space:nowrap;transition:color 0.3s;}\n"
"#orb-lbl.active{color:var(--blue-bright);}\n"
"#proc{position:absolute;top:20px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--blue-bright);font-family:'JetBrains Mono',monospace;letter-spacing:0.12em;opacity:0;transition:opacity 0.3s;white-space:nowrap;}\n"
"#proc.show{opacity:1;}\n"
"#chat{position:absolute;inset:0;display:flex;flex-direction:column;pointer-events:none;}\n"
"#msgs{flex:1;overflow-y:auto;padding:20px 44px;display:flex;flex-direction:column;gap:14px;pointer-events:all;}\n"
"#msgs::-webkit-scrollbar{width:2px;} #msgs::-webkit-scrollbar-thumb{background:var(--border2);}\n"
".msg{display:flex;gap:9px;}\n"
".msg.user{flex-direction:row-reverse;align-self:flex-end;max-width:68%;}\n"
".msg.assistant{align-self:flex-start;max-width:80%;}\n"
".av{width:24px;height:24px;border-radius:3px;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:600;flex-shrink:0;font-family:'JetBrains Mono',monospace;margin-top:2px;}\n"
".av.user{background:rgba(33,150,243,0.1);border:1px solid rgba(33,150,243,0.25);color:var(--blue-bright);}\n"
".av.assistant{background:rgba(0,230,118,0.07);border:1px solid rgba(0,230,118,0.2);color:var(--green);}\n"
".bubble{padding:10px 14px;border-radius:5px;font-size:13px;line-height:1.72;font-weight:300;}\n"
".msg.user .bubble{background:rgba(33,150,243,0.06);border:1px solid rgba(33,150,243,0.12);border-radius:5px 2px 5px 5px;}\n"
".msg.assistant .bubble{background:rgba(255,255,255,0.015);border:1px solid var(--border);border-radius:2px 5px 5px 5px;backdrop-filter:blur(16px);}\n"
".bubble p{margin-bottom:9px;} .bubble p:last-child{margin-bottom:0;}\n"
".bubble h1,.bubble h2,.bubble h3{font-size:12px;font-weight:600;color:var(--bright);margin:11px 0 4px;}\n"
".bubble h1:first-child,.bubble h2:first-child,.bubble h3:first-child{margin-top:0;}\n"
".bubble ul,.bubble ol{padding-left:16px;margin-bottom:9px;} .bubble li{margin-bottom:2px;}\n"
".bubble code{background:rgba(33,150,243,0.08);padding:1px 4px;border-radius:2px;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--blue-bright);}\n"
".bubble pre{background:var(--surface);border:1px solid var(--border);padding:10px;border-radius:3px;overflow-x:auto;margin:9px 0;}\n"
".bubble pre code{background:none;padding:0;color:var(--text);}\n"
".bubble strong{color:var(--bright);font-weight:500;} .bubble a{color:var(--blue-bright);}\n"
"#input-area{padding:10px 44px 18px;pointer-events:all;}\n"
"#iw{display:flex;align-items:flex-end;gap:8px;background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:9px 9px 9px 14px;transition:border-color 0.2s;}\n"
"#iw:focus-within{border-color:rgba(33,150,243,0.35);box-shadow:0 0 0 3px rgba(33,150,243,0.04);}\n"
"#inp{flex:1;background:none;border:none;outline:none;color:var(--bright);font-size:13px;font-family:'Inter',sans-serif;font-weight:300;resize:none;max-height:120px;line-height:1.6;}\n"
"#inp::placeholder{color:var(--muted);}\n"
"#sb{width:32px;height:32px;background:var(--blue);border:none;border-radius:5px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:all 0.2s;box-shadow:0 0 14px rgba(33,150,243,0.25);}\n"
"#sb:hover{background:var(--blue-dim);box-shadow:0 0 20px rgba(33,150,243,0.4);}\n"
"#sb svg{width:13px;height:13px;fill:white;}\n"
"#hint{font-size:9px;color:var(--muted);margin-top:6px;font-family:'JetBrains Mono',monospace;letter-spacing:0.06em;}\n"
"#tp{position:fixed;right:0;top:0;height:100vh;width:400px;background:var(--panel);border-left:1px solid var(--border);transform:translateX(100%);transition:transform 0.2s cubic-bezier(0.4,0,0.2,1);z-index:300;display:flex;flex-direction:column;}\n"
"#tp.open{transform:translateX(0);}\n"
"#tp-h{padding:16px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;}\n"
"#tp-t{font-size:10px;font-family:'JetBrains Mono',monospace;color:var(--blue-bright);letter-spacing:0.12em;}\n"
"#tp-x{background:none;border:none;color:var(--muted2);cursor:pointer;font-size:18px;line-height:1;}\n"
"#tp-b{flex:1;overflow-y:auto;padding:16px 18px;font-size:12px;line-height:1.8;}\n"
"#tp-b::-webkit-scrollbar{width:2px;} #tp-b::-webkit-scrollbar-thumb{background:var(--border2);}\n"
".sl{color:var(--gold);font-family:'JetBrains Mono',monospace;font-size:10px;margin-bottom:2px;opacity:0.9;}\n"
"#dl{margin:0 18px 18px;padding:8px;background:var(--blue);border:none;color:white;border-radius:4px;cursor:pointer;font-size:10px;font-family:'JetBrains Mono',monospace;letter-spacing:0.12em;display:none;}\n"
"</style>\n"
"</head>\n"
"<body>\n"
'<div id="header">\n'
'  <div id="wordmark">BORFOLI</div>\n'
'  <div id="h-sep"></div>\n'
'  <div id="h-status">ONLINE</div>\n'
'  <div id="intent-tag">STANDBY</div>\n'
'  <div id="h-right">\n'
'    <div id="h-model">PRIMARY <b id="hm">—</b></div>\n'
'    <div id="h-sep" style="width:1px;height:14px;background:var(--border2)"></div>\n'
'    <div id="clock">00:00</div>\n'
'  </div>\n'
'</div>\n'
'<div id="body">\n'
'<div id="left">\n'
'  <div>\n'
'    <div class="lbl">SYSTEM</div>\n'
'    <div class="kv"><span class="kk">PRIMARY</span><span class="kv-val blue" id="lp">—</span></div>\n'
'    <div class="kv"><span class="kk">COUNCIL</span><span class="kv-val green" id="lc">—</span></div>\n'
'    <div class="kv"><span class="kk">TOTAL</span><span class="kv-val" id="lt">—</span></div>\n'
'  </div>\n'
'  <div class="div"></div>\n'
'  <div>\n'
'    <div class="lbl">MODELS</div>\n'
'    <div id="ml"></div>\n'
'  </div>\n'
'  <div class="div"></div>\n'
'  <div style="flex:1">\n'
'    <div class="lbl">TASKS</div>\n'
'    <div id="tl"></div>\n'
'  </div>\n'
'  <button id="ns" onclick="clearChat()">+ NEW SESSION</button>\n'
'</div>\n'
'<div id="center">\n'
'  <div id="orb-wrap"><canvas id="cv"></canvas><div id="orb-lbl">STANDBY</div><div id="proc"></div></div>\n'
'  <div id="chat">\n'
'    <div id="msgs">\n'
'      <div class="msg assistant"><div class="av assistant">B</div><div class="bubble">Online. What do you need?</div></div>\n'
'    </div>\n'
'    <div id="input-area">\n'
'      <div id="iw">\n'
'        <textarea id="inp" placeholder="Ask Borfoli..." rows="1"></textarea>\n'
'        <button id="sb" onclick="send()"><svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg></button>\n'
'      </div>\n'
'      <div id="hint">ENTER to send &nbsp;·&nbsp; SHIFT+ENTER new line</div>\n'
'    </div>\n'
'  </div>\n'
'</div>\n'
'</div>\n'
'<div id="tp">\n'
'  <div id="tp-h"><span id="tp-t">TASK OUTPUT</span><button id="tp-x" onclick="closeTP()">×</button></div>\n'
'  <div id="tp-b"></div>\n'
'  <button id="dl" onclick="dlRpt()">DOWNLOAD REPORT</button>\n'
'</div>\n'
"<script>\n"
"function tick(){const n=new Date();document.getElementById('clock').textContent=String(n.getHours()).padStart(2,'0')+':'+String(n.getMinutes()).padStart(2,'0');}tick();setInterval(tick,1000);\n"
"const cv=document.getElementById('cv'),ctx=cv.getContext('2d');let W,H,nodes=[],act=false,nrg=0;\n"
"function rsz(){W=cv.width=cv.offsetWidth;H=cv.height=cv.offsetHeight;}rsz();window.addEventListener('resize',()=>{rsz();initN();});\n"
"function initN(){nodes=[];const N=80,cx=W/2,cy=H/2,R=Math.min(W,H)*.25;for(let i=0;i<N;i++){const t=Math.random()*Math.PI*2,p=Math.acos(2*Math.random()-1),r=R*(.5+Math.random()*.5);nodes.push({x:cx+r*Math.sin(p)*Math.cos(t),y:cy+r*Math.sin(p)*Math.sin(t),vx:(Math.random()-.5)*.2,vy:(Math.random()-.5)*.2,ph:Math.random()*Math.PI*2,s:Math.random()*1.1+.4});}}\n"
"initN();\n"
"function draw(){ctx.clearRect(0,0,W,H);const cx=W/2,cy=H/2,sp=act?1+nrg*4:.35;nrg=act?Math.min(nrg+.04,1):Math.max(nrg-.025,0);const g=ctx.createRadialGradient(cx,cy,0,cx,cy,Math.min(W,H)*.28);const a=.03+nrg*.1;g.addColorStop(0,`rgba(33,150,243,${a*2.5})`);g.addColorStop(.5,`rgba(33,150,243,${a})`);g.addColorStop(1,'rgba(33,150,243,0)');ctx.fillStyle=g;ctx.beginPath();ctx.arc(cx,cy,Math.min(W,H)*.3,0,Math.PI*2);ctx.fill();const R=Math.min(W,H)*.27;nodes.forEach(n=>{n.ph+=.015*sp;n.x+=n.vx*sp;n.y+=n.vy*sp;const dx=n.x-cx,dy=n.y-cy;if(Math.sqrt(dx*dx+dy*dy)>R){n.vx*=-.9;n.vy*=-.9;}});for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){const dx=nodes[i].x-nodes[j].x,dy=nodes[i].y-nodes[j].y,d=Math.sqrt(dx*dx+dy*dy);if(d<65){const al=(1-d/65)*(.05+nrg*.2);ctx.strokeStyle=`rgba(33,150,243,${al})`;ctx.lineWidth=.4+nrg*.4;ctx.beginPath();ctx.moveTo(nodes[i].x,nodes[i].y);ctx.lineTo(nodes[j].x,nodes[j].y);ctx.stroke();}}nodes.forEach(n=>{const p=Math.sin(n.ph)*.5+.5;ctx.fillStyle=`rgba(100,181,246,${.1+p*(.08+nrg*.4)})`;ctx.beginPath();ctx.arc(n.x,n.y,n.s*(1+nrg*p*.7),0,Math.PI*2);ctx.fill();});requestAnimationFrame(draw);}\n"
"draw();\n"
"function setAct(on,lbl=''){act=on;const ol=document.getElementById('orb-lbl'),pc=document.getElementById('proc');ol.textContent=on?'PROCESSING':'STANDBY';ol.classList.toggle('active',on);pc.textContent=lbl;pc.classList.toggle('show',on&&!!lbl);}\n"
"async function loadModels(){try{const r=await fetch('/models'),d=await r.json();const sh=m=>m.split('/').pop().replace(':free','').toUpperCase().slice(0,14);document.getElementById('lp').textContent=sh(d.primary).slice(0,10);document.getElementById('lc').textContent=d.council.length+' ACTIVE';document.getElementById('lt').textContent=d.total;document.getElementById('hm').textContent=sh(d.primary).slice(0,12);const ml=document.getElementById('ml');ml.innerHTML='';const pr=document.createElement('div');pr.className='mdl pri';pr.innerHTML='<div class=\"mdl-role\">PRIMARY</div><div class=\"mdl-name\">'+sh(d.primary)+'</div>';ml.appendChild(pr);d.council.forEach(c=>{const b=document.createElement('div');b.className='mdl';b.innerHTML='<div class=\"mdl-role\">'+c.role.toUpperCase()+'</div><div class=\"mdl-name\">'+sh(c.model)+'</div>';ml.appendChild(b);});}catch(e){}}\n"
"loadModels();\n"
"const inp=document.getElementById('inp'),msgs=document.getElementById('msgs');\n"
"const IL={chitchat:'CASUAL',fast:'FAST',search:'SEARCH',council:'COUNCIL · 6',task:'CREW · AUTO'};\n"
"inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});\n"
"inp.addEventListener('input',()=>{inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,120)+'px';});\n"
"function addMsg(role,content){const d=document.createElement('div');d.className='msg '+role;const av=document.createElement('div');av.className='av '+role;av.textContent=role==='user'?'M':'B';const b=document.createElement('div');b.className='bubble';b.innerHTML=role==='assistant'?marked.parse(content):esc(content);d.appendChild(av);d.appendChild(b);msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;}\n"
"function esc(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}\n"
"async function send(){const msg=inp.value.trim();if(!msg)return;inp.value='';inp.style.height='auto';addMsg('user',msg);setAct(true,'ROUTING...');msgs.scrollTop=msgs.scrollHeight;try{const res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});const data=await res.json();setAct(false);addMsg('assistant',data.reply);if(data.intent){const tg=document.getElementById('intent-tag');tg.textContent=IL[data.intent]||data.intent.toUpperCase();tg.classList.add('on');setTimeout(()=>tg.classList.remove('on'),4000);}if(data.intent==='task')loadTasks();}catch(e){setAct(false);addMsg('assistant','Connection error.');}}\n"
"function clearChat(){msgs.innerHTML='<div class=\"msg assistant\"><div class=\"av assistant\">B</div><div class=\"bubble\">Online. What do you need?</div></div>';}\n"
"async function loadTasks(){try{const r=await fetch('/tasks'),tasks=await r.json();const tl=document.getElementById('tl');tl.innerHTML='';tasks.slice(0,8).forEach(t=>{const el=document.createElement('div');el.className='ti';el.onclick=()=>openTP(t.task_id||t.id,t.goal);const st=t.status||'queued';el.innerHTML='<div class=\"tg\">'+(t.goal||'Task').slice(0,80)+'</div><div class=\"ts\"><span class=\"td '+st+'\"></span><span class=\"ts-lbl\">'+st.toUpperCase()+'</span></div>';tl.appendChild(el);});}catch(e){}}\n"
"let cur=null,pi=null;\n"
"async function openTP(id,goal){cur=id;document.getElementById('tp-t').textContent=(goal||'TASK').slice(0,44).toUpperCase();document.getElementById('tp').classList.add('open');pollT();if(pi)clearInterval(pi);pi=setInterval(pollT,3000);}\n"
"async function pollT(){if(!cur)return;try{const r=await fetch('/task/'+cur),t=await r.json();const b=document.getElementById('tp-b'),dl=document.getElementById('dl');let h='';if(t.steps&&t.steps.length)h+=t.steps.map(s=>'<div class=\"sl\">▸ '+s+'</div>').join('');if(t.result){h+='<div style=\"margin-top:12px\">'+marked.parse(t.result)+'</div>';dl.style.display='block';}if(!h)h='<div style=\"color:var(--muted);font-family:JetBrains Mono,monospace;font-size:10px\">INITIALIZING...</div>';b.innerHTML=h;if(t.status==='complete'||t.status==='error')clearInterval(pi);}catch(e){}}\n"
"function closeTP(){document.getElementById('tp').classList.remove('open');if(pi)clearInterval(pi);cur=null;}\n"
"function dlRpt(){const c=document.getElementById('tp-b').innerText;const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([c],{type:'text/plain'}));a.download='borfoli-'+Date.now()+'.txt';a.click();}\n"
"loadTasks();setInterval(loadTasks,15000);\n"
"</script>\n"
"</body>\n"
"</html>"
)

@app.route("/")
def index():
    return Response(HTML, mimetype='text/html')

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

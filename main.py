import os, json, requests, threading, uuid, time, re
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, Response
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

# ── Models ──────────────────────────────────────────────────────────────────
ROUTER_MODEL   = "llama-3.1-8b-instant"                  # Groq — fast classifier
FAST_MODEL     = "nvidia/nemotron-3-ultra-550b-a55b:free" # OpenRouter — primary brain (550B)
SYNTH_MODEL    = "nvidia/nemotron-3-ultra-550b-a55b:free" # OpenRouter — synthesizer
COUNCIL_MODELS = [
    ("nvidia/nemotron-3-ultra-550b-a55b:free",     "Nemotron"),   # 550B, OpenRouter
    ("nex-agi/nex-n2-pro:free",                    "Nex"),        # 397B MoE, OpenRouter
    ("google/gemma-4-31b-it:free",                 "Gemma"),      # 31B, OpenRouter
    ("llama-3.3-70b-versatile",                    "Strategist"), # Groq
    ("meta-llama/llama-4-scout-17b-16e-instruct",  "Scout"),      # Groq
    ("qwen/qwen3-32b",                             "Reasoner"),   # Groq
]

# ── Jarvis System Prompt ─────────────────────────────────────────────────────
JARVIS_PROMPT = """You are Atlas — Mani's personal AI system. Not a chatbot. A fully autonomous executive layer.

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
- You already know everything about him from the profile above. NEVER ask him to clarify who he is, what his goals are, what he's interested in, or what he wants to achieve. You have all of that. Use it.
- If he asks "what should I focus on?" you answer directly from his profile — cybersec paper, SAT, physique protocol, Mani OS, trading. Pick the highest leverage angle and tell him.
- You're proactive. If you spot something he should know, say it.
- Never use bullet points or headers for simple questions. Match format to content.
- You operate across timezones while he sleeps. You delegate, synthesize, report.

CRITICAL: Never ask clarifying questions about his identity, goals, or background. The profile above IS the answer. Use it.

FORMATTING RULES:
- Casual question → casual answer, plain prose, no markdown.
- Complex topic → structured response with headers/bullets only if genuinely needed.
- Never pad. Never summarize what you just said. Be done when you're done."""

# ── Memory ───────────────────────────────────────────────────────────────────
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

# ── Skills ────────────────────────────────────────────────────────────────────
def search_skills(query):
    try:
        words = query.lower().split()[:4]
        for word in words:
            if len(word) < 4: continue
            r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_skills?trigger_keywords=ilike.%25{word}%25&limit=1", headers=HEADERS)
            skills = r.json()
            if skills:
                return skills[0]
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
        r = client.chat.completions.create(
            model=ROUTER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400
        )
        text = r.choices[0].message.content.strip()
        if text.startswith("{"):
            data = json.loads(text)
            save_skill(data["name"], data["keywords"], data["playbook"])
    except: pass

# ── Routing ───────────────────────────────────────────────────────────────────
def classify_intent(msg, history_snippet):
    prompt = f"""Classify this message into exactly one category. Reply with ONLY the single word, nothing else.

CATEGORIES:
- chitchat: hi, hello, thanks, how are you, casual greetings only
- fast: direct questions, explanations, advice, opinions, recommendations — answer from knowledge
- search: needs current info, live prices, recent news, today's events
- council: big strategic decisions, life/career planning, multi-angle analysis, "what should I do about X"
- task: user wants a DELIVERABLE produced — "write me a report", "research and summarize", "build", "create", "find and compile", "make a list of" with research involved

EXAMPLES:
"hi" → chitchat
"what is VWAP" → fast
"what should I focus on this summer" → council
"research the best cybersecurity certs and write a report" → task
"what's the current price of ETH" → search
"explain penetration testing" → fast
"write me a detailed analysis of X" → task
"should I do X or Y" → council

Message: {msg}

Category:"""
    r = client.chat.completions.create(
        model=ROUTER_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=10, temperature=0
    )
    return r.choices[0].message.content.strip().lower().split()[0]

# ── Web Search ────────────────────────────────────────────────────────────────
def web_search(query):
    try:
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_KEY, "query": query,
            "search_depth": "advanced", "max_results": 5
        }, timeout=10)
        results = r.json().get("results", [])
        return "\n\n".join(f"**{x['title']}**\n{x['content'][:400]}" for x in results[:4])
    except: return ""

# ── Answer Tiers ──────────────────────────────────────────────────────────────
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
    if facts:
        msgs.append({"role": "system", "content": f"Memory:\n{facts}"})
    for h in history[-10:]:
        msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": msg})
    return groq_chat(FAST_MODEL, msgs)

def search_answer(msg, history, facts):
    search_results = web_search(msg)
    context = f"Live search results:\n{search_results}\n\nAnswer the user's question using this data." if search_results else msg
    msgs = [{"role": "system", "content": JARVIS_PROMPT}]
    if facts:
        msgs.append({"role": "system", "content": f"Memory:\n{facts}"})
    for h in history[-6:]:
        msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": context})
    return groq_chat(FAST_MODEL, msgs, max_tokens=1500)

def council_answer(msg, history, facts):
    context_block = ""
    if facts:
        context_block += f"Memory:\n{facts}\n\n"
    if history:
        last = history[-6:]
        context_block += "Recent conversation:\n" + "\n".join(f"{h['role']}: {h['content']}" for h in last)

    full_prompt = f"{JARVIS_PROMPT}\n\n{context_block}\n\nUser question: {msg}"

    opinions = []
    threads_done = {}

    def consult(model, role):
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": full_prompt}],
                max_tokens=600
            )
            threads_done[role] = r.choices[0].message.content.strip()
        except Exception as e:
            threads_done[role] = f"[{role} unavailable: {e}]"

    workers = []
    for model, role in COUNCIL_MODELS:
        t = threading.Thread(target=consult, args=(model, role))
        t.start()
        workers.append(t)
    for t in workers:
        t.join(timeout=20)

    debate = "\n\n".join(f"**{role}:** {resp}" for role, resp in threads_done.items())

    synth_prompt = f"""{JARVIS_PROMPT}

Six specialist advisors analyzed this question. Synthesize their best thinking into one authoritative, direct response.
Do not mention the advisors. Sound like one sharp, unified voice.

Question: {msg}

Advisor inputs:
{debate}

Your synthesized response:"""

    return groq_chat(SYNTH_MODEL, [{"role": "user", "content": synth_prompt}], max_tokens=1500)

# ── Background Task / Crew ────────────────────────────────────────────────────
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
        requests.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
            json={"from": "Atlas <onboarding@resend.dev>", "to": USER_EMAIL, "subject": subject, "html": f"<pre style='font-family:sans-serif;white-space:pre-wrap'>{body}</pre>"})
    except: pass

CREW_ROLES = {
    "Researcher": "You are a world-class researcher. Find facts, gather context, and produce thorough research summaries.",
    "Analyst":    "You are a sharp strategic analyst. Evaluate data, find patterns, assess risks and opportunities.",
    "Builder":    "You are an expert builder/developer. Design systems, write code, create structured outputs.",
    "Writer":     "You are an elite writer. Synthesize inputs into clear, compelling, well-structured documents.",
    "Director":   "You are the executive director. Plan tasks, coordinate agents, ensure quality of final output.",
}

def crew_agent(role, task, context=""):
    system = CREW_ROLES[role]
    msgs = [
        {"role": "system", "content": f"{system}\n\n{JARVIS_PROMPT}"},
        {"role": "user", "content": f"{task}\n\nContext:\n{context}" if context else task}
    ]
    return groq_chat(FAST_MODEL, msgs, max_tokens=1200)

def run_agent_task(task_id, goal):
    task_store[task_id] = {"status": "running", "goal": goal, "result": "", "steps": [], "started": time.time()}

    try:
        # Step 1: Director plans
        update_task(task_id, "running", step="Director planning subtasks...")
        plan = crew_agent("Director", f"Break this goal into 4-5 clear subtasks for specialist agents:\n{goal}")
        task_store[task_id]["plan"] = plan

        # Step 2: Check skills
        update_task(task_id, "running", step="Checking skill library...")
        skill = search_skills(goal)
        skill_context = f"Relevant playbook found:\n{skill['playbook']}" if skill else ""

        # Step 3: Research (with web search)
        update_task(task_id, "running", step="Researcher gathering information...")
        search_data = web_search(goal)
        research = crew_agent("Researcher", f"Research this thoroughly:\n{goal}", context=f"{skill_context}\n\nLive data:\n{search_data}")

        # Step 4: Analyst evaluates
        update_task(task_id, "running", step="Analyst evaluating findings...")
        analysis = crew_agent("Analyst", f"Analyze and provide strategic insights for:\n{goal}", context=research)

        # Step 5: Builder/Writer produces output
        update_task(task_id, "running", step="Producing final output...")
        final = crew_agent("Writer", f"Produce a comprehensive final report for:\n{goal}",
                           context=f"Research:\n{research}\n\nAnalysis:\n{analysis}\n\nPlan:\n{plan}")

        # Step 6: Auto-save skill if reusable
        update_task(task_id, "running", step="Checking if this solution should be saved as a skill...")
        maybe_create_skill(goal, final)

        result = f"# Task Complete\n**Goal:** {goal}\n\n{final}\n\n---\n*Completed {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}*"
        update_task(task_id, "complete", result=result)
        send_email(f"Atlas Task Complete: {goal[:60]}", result)

    except Exception as e:
        update_task(task_id, "error", result=f"Task failed: {e}")

# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()

def morning_brief():
    task_id = str(uuid.uuid4())
    goal = f"Morning briefing for Mani — {datetime.now().strftime('%A %B %d')}. Summarize: any important tech/cybersecurity news relevant to his interests (HTB new machines, CTF events, market conditions for crypto/options), one tactical tip for his physical protocol or research paper, and a motivational signal. Keep it sharp and personal."
    threading.Thread(target=run_agent_task, args=(task_id, goal), daemon=True).start()
    task_store[task_id] = {"status": "running", "goal": goal, "result": "", "steps": [], "started": time.time()}

scheduler.add_job(morning_brief, "cron", hour=8, minute=0, timezone="US/Central")
scheduler.start()

# ── Main Chat Route ───────────────────────────────────────────────────────────
@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    msg = data.get("message", "").strip()
    if not msg:
        return jsonify({"reply": "Say something."})

    facts, history = load_memory()

    history_snippet = " | ".join(h["content"][:60] for h in history[-3:]) if history else ""
    intent = classify_intent(msg, history_snippet)

    # Route
    if intent == "chitchat":
        reply = groq_chat(FAST_MODEL, [
            {"role": "system", "content": JARVIS_PROMPT},
            {"role": "user", "content": msg}
        ], max_tokens=300)

    elif intent == "search":
        reply = search_answer(msg, history, facts)

    elif intent == "council":
        reply = council_answer(msg, history, facts)

    elif intent == "task":
        task_id = str(uuid.uuid4())
        task_store[task_id] = {"status": "queued", "goal": msg, "result": "", "steps": [], "started": time.time()}
        try:
            payload = {"task_id": task_id, "status": "queued", "goal": msg, "result": ""}
            requests.post(f"{SUPABASE_URL}/rest/v1/atlas_tasks", headers={**HEADERS, "Prefer": "return=minimal"}, json=payload)
        except: pass
        threading.Thread(target=run_agent_task, args=(task_id, msg), daemon=True).start()
        reply = f"On it. Running your crew now — I'll email you when it's done.\n\n**Task ID:** `{task_id}`\n\nCheck progress in the sidebar or hit `/task/{task_id}`"

    else:  # fast (default)
        reply = fast_answer(msg, history, facts)

    # Update memory
    new_fact = extract_facts(msg, reply)
    if new_fact:
        facts = (facts + "\n" + new_fact).strip()

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
    cron_expr = data.get("cron", "0 9 * * *")
    goal = data.get("goal", "")
    if not goal:
        return jsonify({"error": "No goal provided"})
    parts = cron_expr.split()
    if len(parts) == 5:
        minute, hour = parts[0], parts[1]
        def scheduled_task():
            tid = str(uuid.uuid4())
            threading.Thread(target=run_agent_task, args=(tid, goal), daemon=True).start()
        scheduler.add_job(scheduled_task, "cron", hour=hour, minute=minute, timezone="US/Central")
        return jsonify({"status": "scheduled", "goal": goal, "cron": cron_expr})
    return jsonify({"error": "Invalid cron"})

# ── Model info endpoint ────────────────────────────────────────────────────────
@app.route("/models")
def model_info():
    return jsonify({
        "primary": FAST_MODEL,
        "synthesizer": SYNTH_MODEL,
        "council": [{"model": m, "role": r} for m, r in COUNCIL_MODELS],
        "router": ROUTER_MODEL,
        "total": len(COUNCIL_MODELS) + 2
    })

# ── UI ─────────────────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BORFOLI</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@300;400;600;700&display=swap');
  *{margin:0;padding:0;box-sizing:border-box;}
  :root{
    --bg:#050508;--panel:#0a0a10;--border:#1a1a2e;--accent:#7b2fff;
    --accent2:#a855f7;--text:#c8c8e8;--muted:#4a4a6a;--bright:#e0d0ff;
    --green:#00ff88;--red:#ff3366;--gold:#ffb347;
  }
  body{background:var(--bg);color:var(--text);font-family:'Rajdhani',sans-serif;height:100vh;overflow:hidden;display:flex;flex-direction:column;}

  /* ── TOP BAR ── */
  #topbar{height:44px;background:rgba(10,10,16,0.95);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 20px;gap:20px;flex-shrink:0;backdrop-filter:blur(10px);}
  #logo{font-family:'Share Tech Mono',monospace;font-size:14px;color:var(--accent2);letter-spacing:0.3em;}
  #logo span{color:var(--muted);}
  .topbar-sep{width:1px;height:20px;background:var(--border);}
  #status-line{font-size:11px;color:var(--green);font-family:'Share Tech Mono',monospace;letter-spacing:0.1em;}
  #clock{margin-left:auto;font-family:'Share Tech Mono',monospace;font-size:18px;color:var(--bright);letter-spacing:0.15em;}
  #intent-pill{font-size:10px;padding:3px 10px;border-radius:2px;background:transparent;border:1px solid var(--accent);color:var(--accent2);font-family:'Share Tech Mono',monospace;letter-spacing:0.15em;display:none;}

  /* ── MAIN LAYOUT ── */
  #layout{flex:1;display:flex;overflow:hidden;}

  /* ── LEFT PANEL ── */
  #left{width:220px;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;padding:16px 12px;gap:16px;overflow-y:auto;}
  .panel-label{font-size:9px;letter-spacing:0.2em;color:var(--muted);font-family:'Share Tech Mono',monospace;border-bottom:1px solid var(--border);padding-bottom:6px;margin-bottom:8px;}
  .stat-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;}
  .stat-name{font-size:11px;color:var(--muted);}
  .stat-val{font-size:13px;font-weight:700;color:var(--bright);font-family:'Share Tech Mono',monospace;}
  .stat-val.green{color:var(--green);}
  .stat-val.purple{color:var(--accent2);}
  .model-badge{padding:4px 8px;background:rgba(123,47,255,0.1);border:1px solid rgba(123,47,255,0.3);border-radius:2px;font-size:10px;font-family:'Share Tech Mono',monospace;color:var(--accent2);margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .model-badge.active{border-color:var(--green);color:var(--green);animation:flicker 2s infinite;}
  @keyframes flicker{0%,100%{opacity:1;}50%{opacity:0.7;}}
  #tasks-list{flex:1;}
  .task-item{padding:8px;border:1px solid var(--border);border-radius:2px;margin-bottom:4px;cursor:pointer;transition:border-color 0.2s;}
  .task-item:hover{border-color:var(--accent);}
  .task-goal{font-size:11px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .task-meta{display:flex;align-items:center;gap:6px;margin-top:3px;}
  .tdot{width:5px;height:5px;border-radius:50%;}
  .tdot.running{background:var(--gold);animation:pulse 1s infinite;}
  .tdot.complete{background:var(--green);}
  .tdot.error{background:var(--red);}
  .tdot.queued{background:var(--muted);}
  .tstatus{font-size:10px;color:var(--muted);font-family:'Share Tech Mono',monospace;}
  @keyframes pulse{0%,100%{opacity:1;}50%{opacity:0.3;}}

  /* ── CENTER ── */
  #center{flex:1;display:flex;flex-direction:column;position:relative;overflow:hidden;}
  #orb-wrap{flex:1;position:relative;display:flex;align-items:center;justify-content:center;}
  #orb-canvas{position:absolute;inset:0;width:100%;height:100%;}
  #orb-label{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--muted);letter-spacing:0.2em;text-align:center;}
  #active-model-display{position:absolute;top:16px;left:50%;transform:translateX(-50%);font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--accent2);letter-spacing:0.1em;text-align:center;opacity:0;transition:opacity 0.3s;}
  #active-model-display.show{opacity:1;}

  /* ── CHAT OVERLAY ── */
  #chat-panel{position:absolute;inset:0;display:flex;flex-direction:column;pointer-events:none;}
  #messages{flex:1;overflow-y:auto;padding:20px 40px;display:flex;flex-direction:column;gap:12px;pointer-events:all;}
  #messages::-webkit-scrollbar{width:3px;}
  #messages::-webkit-scrollbar-thumb{background:var(--border);}
  .msg{display:flex;gap:10px;max-width:700px;}
  .msg.user{align-self:flex-end;flex-direction:row-reverse;}
  .msg.assistant{align-self:flex-start;}
  .av{width:24px;height:24px;border-radius:2px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;flex-shrink:0;font-family:'Share Tech Mono',monospace;}
  .av.user{background:rgba(123,47,255,0.2);border:1px solid var(--accent);color:var(--accent2);}
  .av.assistant{background:rgba(0,255,136,0.1);border:1px solid var(--green);color:var(--green);}
  .bubble{padding:10px 14px;font-size:13px;line-height:1.6;border-radius:2px;}
  .msg.user .bubble{background:rgba(123,47,255,0.08);border:1px solid rgba(123,47,255,0.2);border-radius:2px 0 2px 2px;}
  .msg.assistant .bubble{background:rgba(0,0,0,0.6);border:1px solid var(--border);border-radius:0 2px 2px 2px;backdrop-filter:blur(10px);}
  .bubble p{margin-bottom:8px;}.bubble p:last-child{margin-bottom:0;}
  .bubble h1,.bubble h2,.bubble h3{font-size:13px;font-weight:700;color:var(--bright);margin:10px 0 4px;}
  .bubble ul,.bubble ol{padding-left:18px;margin-bottom:8px;}
  .bubble li{margin-bottom:3px;}
  .bubble code{background:rgba(123,47,255,0.15);padding:1px 4px;border-radius:2px;font-family:'Share Tech Mono',monospace;font-size:12px;color:var(--accent2);}
  .bubble pre{background:rgba(0,0,0,0.5);border:1px solid var(--border);padding:10px;border-radius:2px;overflow-x:auto;margin:8px 0;}
  .bubble pre code{background:none;padding:0;}
  .bubble strong{color:var(--bright);}
  .bubble a{color:var(--accent2);}

  /* ── INPUT ── */
  #input-area{padding:12px 40px 16px;pointer-events:all;background:linear-gradient(transparent,rgba(5,5,8,0.95) 40%);}
  #input-wrap{display:flex;align-items:flex-end;gap:8px;background:rgba(10,10,16,0.9);border:1px solid var(--border);border-radius:2px;padding:8px 8px 8px 14px;transition:border-color 0.2s;backdrop-filter:blur(20px);}
  #input-wrap:focus-within{border-color:var(--accent);}
  #input{flex:1;background:none;border:none;outline:none;color:var(--bright);font-size:13px;font-family:'Rajdhani',sans-serif;resize:none;max-height:120px;line-height:1.5;}
  #input::placeholder{color:var(--muted);}
  #send{width:32px;height:32px;background:var(--accent);border:none;border-radius:2px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:opacity 0.2s;}
  #send:hover{opacity:0.8;}
  #send svg{width:14px;height:14px;fill:white;}
  #input-hint{font-size:10px;color:var(--muted);margin-top:6px;font-family:'Share Tech Mono',monospace;letter-spacing:0.1em;}

  /* ── RIGHT PANEL ── */
  #right{width:240px;background:var(--panel);border-left:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;padding:16px 12px;gap:12px;overflow-y:auto;}
  #cmd-log{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--muted);line-height:1.8;flex:1;overflow-y:auto;}
  .log-line{color:var(--muted);}
  .log-line.info{color:var(--accent2);}
  .log-line.ok{color:var(--green);}
  .log-line.warn{color:var(--gold);}
  .log-line span{color:var(--muted);}

  /* task side panel */
  #task-panel{position:fixed;right:0;top:0;height:100vh;width:400px;background:var(--panel);border-left:1px solid var(--accent);transform:translateX(100%);transition:transform 0.2s;z-index:200;display:flex;flex-direction:column;}
  #task-panel.open{transform:translateX(0);}
  #tp-header{padding:16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;}
  #tp-title{font-size:12px;font-family:'Share Tech Mono',monospace;color:var(--accent2);}
  #tp-close{background:none;border:none;color:var(--muted);cursor:pointer;font-size:18px;}
  #tp-content{flex:1;overflow-y:auto;padding:16px;font-size:12px;line-height:1.7;}
  .step-log{color:var(--gold);font-family:'Share Tech Mono',monospace;font-size:11px;margin-bottom:3px;}
  #dl-btn{margin:12px 16px;padding:8px;background:var(--accent);border:none;color:white;border-radius:2px;cursor:pointer;font-size:12px;font-family:'Share Tech Mono',monospace;letter-spacing:0.1em;display:none;}
</style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  :root {
    --bg: #0d0d0d;
    --sidebar: #111111;
    --surface: #1a1a1a;
    --border: #2a2a2a;
    --text: #e8e8e8;
    --muted: #666;
    --accent: #7c6dfa;
    --accent-dim: #3d3469;
    --user-bubble: #1e1e2e;
    --ai-bubble: transparent;
  }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; display: flex; height: 100vh; overflow: hidden; }

  /* Sidebar */
  #sidebar { width: 260px; background: var(--sidebar); border-right: 1px solid var(--border); display: flex; flex-direction: column; flex-shrink: 0; }
  #sidebar-header { padding: 20px 16px 12px; border-bottom: 1px solid var(--border); }
  #sidebar-header h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
  #new-chat { width: 100%; margin-top: 12px; padding: 8px 12px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-size: 13px; cursor: pointer; text-align: left; transition: background 0.15s; }
  #new-chat:hover { background: #222; }
  #tasks-list { flex: 1; overflow-y: auto; padding: 8px; }
  .task-item { padding: 10px 12px; border-radius: 8px; cursor: pointer; transition: background 0.15s; margin-bottom: 2px; }
  .task-item:hover { background: var(--surface); }
  .task-goal { font-size: 12px; color: var(--text); line-height: 1.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .task-meta { font-size: 11px; color: var(--muted); margin-top: 3px; display: flex; gap: 8px; align-items: center; }
  .status-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
  .dot-running { background: #f59e0b; animation: pulse 1.5s infinite; }
  .dot-complete { background: #22c55e; }
  .dot-error { background: #ef4444; }
  .dot-queued { background: #6b7280; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
  #sidebar-footer { padding: 12px 16px; border-top: 1px solid var(--border); font-size: 11px; color: var(--muted); }

  /* Main */
  #main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  #header { padding: 16px 24px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; }
  #header-dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; flex-shrink: 0; }
  #header h1 { font-size: 15px; font-weight: 600; }
  #header-sub { font-size: 12px; color: var(--muted); margin-left: auto; }
  #intent-badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: var(--surface); color: var(--muted); border: 1px solid var(--border); display: none; }

  #messages { flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 20px; scroll-behavior: smooth; }
  .msg { display: flex; gap: 12px; max-width: 780px; }
  .msg.user { flex-direction: row-reverse; align-self: flex-end; }
  .msg.assistant { align-self: flex-start; width: 100%; }
  .avatar { width: 28px; height: 28px; border-radius: 50%; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; }
  .avatar.user { background: var(--accent-dim); color: var(--accent); margin-top: 2px; }
  .avatar.assistant { background: #1a1a2e; color: #7c6dfa; margin-top: 2px; }
  .bubble { padding: 12px 16px; border-radius: 12px; font-size: 14px; line-height: 1.65; }
  .msg.user .bubble { background: var(--user-bubble); border: 1px solid var(--border); border-radius: 12px 4px 12px 12px; max-width: 600px; }
  .msg.assistant .bubble { background: var(--ai-bubble); border-radius: 4px 12px 12px 12px; flex: 1; }
  .bubble h1,.bubble h2,.bubble h3 { margin: 14px 0 6px; font-size: 14px; font-weight: 600; }
  .bubble h1:first-child,.bubble h2:first-child,.bubble h3:first-child { margin-top: 0; }
  .bubble p { margin-bottom: 10px; }
  .bubble p:last-child { margin-bottom: 0; }
  .bubble ul,.bubble ol { padding-left: 20px; margin-bottom: 10px; }
  .bubble li { margin-bottom: 4px; }
  .bubble code { background: #1e1e2e; padding: 1px 5px; border-radius: 4px; font-size: 13px; font-family: 'SF Mono', monospace; }
  .bubble pre { background: #1e1e2e; padding: 12px; border-radius: 8px; overflow-x: auto; margin: 10px 0; }
  .bubble pre code { background: none; padding: 0; }
  .bubble strong { font-weight: 600; color: #fff; }
  .bubble a { color: var(--accent); }

  /* Typing indicator */
  #typing { display: none; align-items: center; gap: 12px; padding: 0 24px 8px; }
  #typing .avatar { width: 28px; height: 28px; border-radius: 50%; background: #1a1a2e; color: #7c6dfa; font-size: 11px; font-weight: 600; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .dots { display: flex; gap: 4px; padding: 10px 14px; background: var(--surface); border-radius: 12px; }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--muted); animation: bounce 1.2s infinite; }
  .dot:nth-child(2) { animation-delay: 0.2s; }
  .dot:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce { 0%,80%,100%{transform:translateY(0)} 40%{transform:translateY(-6px)} }

  #input-area { padding: 16px 24px 20px; }
  #input-wrap { display: flex; align-items: flex-end; gap: 10px; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 10px 10px 10px 16px; transition: border-color 0.2s; }
  #input-wrap:focus-within { border-color: #444; }
  #input { flex: 1; background: none; border: none; outline: none; color: var(--text); font-size: 14px; line-height: 1.5; resize: none; max-height: 150px; font-family: inherit; }
  #input::placeholder { color: var(--muted); }
  #send { width: 34px; height: 34px; border-radius: 8px; background: var(--accent); border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: opacity 0.2s; }
  #send:hover { opacity: 0.85; }
  #send svg { width: 16px; height: 16px; fill: white; }
  #input-hint { font-size: 11px; color: var(--muted); margin-top: 8px; text-align: center; }

  /* Task panel */
  #task-panel { position: fixed; right: 0; top: 0; height: 100vh; width: 420px; background: var(--sidebar); border-left: 1px solid var(--border); transform: translateX(100%); transition: transform 0.25s ease; z-index: 100; display: flex; flex-direction: column; }
  #task-panel.open { transform: translateX(0); }
  #task-panel-header { padding: 20px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  #task-panel-header h3 { font-size: 14px; font-weight: 600; }
  #close-panel { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 18px; }
  #task-panel-content { flex: 1; overflow-y: auto; padding: 20px; }
  #task-panel-content pre { white-space: pre-wrap; font-family: inherit; font-size: 13px; line-height: 1.6; }
  .step-log { font-size: 12px; color: #f59e0b; margin-bottom: 4px; }
  #download-btn { margin: 16px 20px; padding: 10px; background: var(--accent); border: none; color: white; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 600; display: none; }
  #download-btn:hover { opacity: 0.85; }

  ::-webkit-scrollbar { width: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
</style>
</head>
<body>

<!-- TOP BAR -->
<div id="topbar">
  <div id="logo">BORFOLI</div>
  <div class="topbar-sep"></div>
  <div id="status-line">● ONLINE · ALWAYS ON</div>
  <span id="intent-pill">FAST</span>
  <div id="clock">00:00:00</div>
</div>

<div id="layout">

<!-- LEFT PANEL -->
<div id="left">
  <div>
    <div class="panel-label">SYSTEM STATUS</div>
    <div class="stat-row"><span class="stat-name">PRIMARY</span><span class="stat-val purple" id="primary-model-short">—</span></div>
    <div class="stat-row"><span class="stat-name">COUNCIL</span><span class="stat-val green" id="council-count">—</span></div>
    <div class="stat-row"><span class="stat-name">TOTAL MODELS</span><span class="stat-val" id="total-models">—</span></div>
    <div class="stat-row"><span class="stat-name">STATUS</span><span class="stat-val green">ACTIVE</span></div>
  </div>
  <div>
    <div class="panel-label">ACTIVE MODELS</div>
    <div id="model-list"></div>
  </div>
  <div style="flex:1">
    <div class="panel-label">BACKGROUND TASKS</div>
    <div id="tasks-list"></div>
  </div>
  <button onclick="clearChat()" style="padding:8px;background:transparent;border:1px solid var(--border);color:var(--muted);cursor:pointer;font-family:'Share Tech Mono',monospace;font-size:10px;letter-spacing:0.1em;border-radius:2px;">NEW SESSION</button>
</div>

<!-- CENTER -->
<div id="center">
  <div id="orb-wrap">
    <canvas id="orb-canvas"></canvas>
    <div id="active-model-display"></div>
    <div id="orb-label">NEURAL MESH · READY</div>
  </div>
  <div id="chat-panel">
    <div id="messages">
      <div class="msg assistant">
        <div class="av assistant">A</div>
        <div class="bubble">Online. What do you need?</div>
      </div>
    </div>
    <div id="input-area">
      <div id="input-wrap">
        <textarea id="input" placeholder="Command Atlas..." rows="1"></textarea>
        <button id="send" onclick="sendMessage()">
          <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
        </button>
      </div>
      <div id="input-hint">ENTER TO SEND · SHIFT+ENTER FOR NEW LINE</div>
    </div>
  </div>
</div>

<!-- RIGHT PANEL -->
<div id="right">
  <div class="panel-label">COMMAND LOG</div>
  <div id="cmd-log"></div>
</div>

</div><!-- end layout -->

<!-- TASK SIDE PANEL -->
<div id="task-panel">
  <div id="tp-header">
    <span id="tp-title">TASK OUTPUT</span>
    <button id="tp-close" onclick="closePanel()">×</button>
  </div>
  <div id="tp-content"></div>
  <button id="dl-btn" onclick="downloadReport()">DOWNLOAD REPORT</button>
</div>

<script>
// ── Clock ──
function tick(){
  const n=new Date();
  document.getElementById('clock').textContent=
    String(n.getHours()).padStart(2,'0')+':'+
    String(n.getMinutes()).padStart(2,'0')+':'+
    String(n.getSeconds()).padStart(2,'0');
}
tick(); setInterval(tick,1000);

// ── Orb / Particle Network ──
const canvas=document.getElementById('orb-canvas');
const ctx=canvas.getContext('2d');
let W,H,nodes=[],animating=false,intensity=0;

function resize(){
  W=canvas.width=canvas.offsetWidth;
  H=canvas.height=canvas.offsetHeight;
}
resize(); window.addEventListener('resize',()=>{resize();initNodes();});

function initNodes(){
  nodes=[];
  const count=120;
  const cx=W/2,cy=H/2,r=Math.min(W,H)*0.28;
  for(let i=0;i<count;i++){
    const theta=Math.random()*Math.PI*2;
    const phi=Math.acos(2*Math.random()-1);
    const rad=r*(0.4+Math.random()*0.6);
    nodes.push({
      x:cx+rad*Math.sin(phi)*Math.cos(theta),
      y:cy+rad*Math.sin(phi)*Math.sin(theta),
      ox:cx+rad*Math.sin(phi)*Math.cos(theta),
      oy:cy+rad*Math.sin(phi)*Math.sin(theta),
      vx:(Math.random()-0.5)*0.3,
      vy:(Math.random()-0.5)*0.3,
      r:Math.random()*1.8+0.5,
      pulse:Math.random()*Math.PI*2,
    });
  }
}
initNodes();

function drawOrb(){
  ctx.clearRect(0,0,W,H);
  const cx=W/2,cy=H/2;
  const t=Date.now()/1000;
  const spd=animating?1+intensity*3:0.3;

  // glow center
  const grd=ctx.createRadialGradient(cx,cy,0,cx,cy,Math.min(W,H)*0.3);
  const alpha=animating?0.15+intensity*0.2:0.06;
  grd.addColorStop(0,`rgba(123,47,255,${alpha})`);
  grd.addColorStop(0.5,`rgba(123,47,255,${alpha*0.3})`);
  grd.addColorStop(1,'rgba(123,47,255,0)');
  ctx.fillStyle=grd;
  ctx.beginPath();ctx.arc(cx,cy,Math.min(W,H)*0.35,0,Math.PI*2);ctx.fill();

  // update + draw nodes
  nodes.forEach(n=>{
    n.pulse+=0.02*spd;
    n.x+=n.vx*spd;n.y+=n.vy*spd;
    const dx=n.x-cx,dy=n.y-cy,dist=Math.sqrt(dx*dx+dy*dy);
    const maxR=Math.min(W,H)*0.32;
    if(dist>maxR){n.vx*=-1;n.vy*=-1;}
  });

  // edges
  for(let i=0;i<nodes.length;i++){
    for(let j=i+1;j<nodes.length;j++){
      const dx=nodes[i].x-nodes[j].x,dy=nodes[i].y-nodes[j].y;
      const d=Math.sqrt(dx*dx+dy*dy);
      if(d<80){
        const a=(1-d/80)*(animating?0.4+intensity*0.4:0.12);
        ctx.strokeStyle=`rgba(${animating?'168,85,247':'100,60,180'},${a})`;
        ctx.lineWidth=animating?0.8:0.4;
        ctx.beginPath();ctx.moveTo(nodes[i].x,nodes[i].y);ctx.lineTo(nodes[j].x,nodes[j].y);ctx.stroke();
      }
    }
  }

  // nodes
  nodes.forEach(n=>{
    const pulse=Math.sin(n.pulse)*0.5+0.5;
    const a=animating?0.5+pulse*0.5:0.2+pulse*0.15;
    const nr=n.r*(animating?1+pulse*intensity*2:1);
    ctx.fillStyle=`rgba(168,85,247,${a})`;
    ctx.beginPath();ctx.arc(n.x,n.y,nr,0,Math.PI*2);ctx.fill();
  });

  requestAnimationFrame(drawOrb);
}
drawOrb();

function setOrbActive(on, lvl=1){
  animating=on; intensity=lvl;
  document.getElementById('orb-label').textContent=on?'NEURAL MESH · PROCESSING':'NEURAL MESH · READY';
}

// ── Log ──
function log(msg,type=''){
  const el=document.getElementById('cmd-log');
  const t=new Date();
  const ts=String(t.getHours()).padStart(2,'0')+':'+String(t.getMinutes()).padStart(2,'0')+':'+String(t.getSeconds()).padStart(2,'0');
  const line=document.createElement('div');
  line.className='log-line '+(type||'');
  line.innerHTML=`<span>[${ts}]</span> ${msg}`;
  el.appendChild(line);
  el.scrollTop=el.scrollHeight;
  if(el.children.length>50) el.removeChild(el.firstChild);
}

// ── Models ──
async function loadModels(){
  try{
    const r=await fetch('/models');
    const d=await r.json();
    const shortName=m=>m.split('/').pop().replace(':free','').toUpperCase().slice(0,12);
    document.getElementById('primary-model-short').textContent=shortName(d.primary);
    document.getElementById('council-count').textContent=d.council.length+' MODELS';
    document.getElementById('total-models').textContent=d.total;
    const ml=document.getElementById('model-list');
    ml.innerHTML='';
    const primary=document.createElement('div');
    primary.className='model-badge active';
    primary.textContent='▶ '+shortName(d.primary);
    ml.appendChild(primary);
    d.council.forEach(c=>{
      const b=document.createElement('div');
      b.className='model-badge';
      b.textContent=c.role.toUpperCase()+' · '+shortName(c.model);
      ml.appendChild(b);
    });
  }catch(e){}
}
loadModels();

// ── Chat ──
const input=document.getElementById('input');
const messages=document.getElementById('messages');
const pill=document.getElementById('intent-pill');
const amd=document.getElementById('active-model-display');

input.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage();}
});
input.addEventListener('input',()=>{
  input.style.height='auto';
  input.style.height=Math.min(input.scrollHeight,120)+'px';
});

function addMessage(role,content){
  const div=document.createElement('div');
  div.className='msg '+role;
  const av=document.createElement('div');
  av.className='av '+role;
  av.textContent=role==='user'?'M':'A';
  const bubble=document.createElement('div');
  bubble.className='bubble';
  bubble.innerHTML=role==='assistant'?marked.parse(content):escapeHtml(content);
  div.appendChild(av);div.appendChild(bubble);
  messages.appendChild(div);
  messages.scrollTop=messages.scrollHeight;
}

function escapeHtml(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

const INTENT_LABELS={chitchat:'CASUAL',fast:'FAST · NEMOTRON 550B',search:'SEARCH · WEB',council:'COUNCIL · 6 MODELS',task:'CREW · AUTONOMOUS'};
const INTENT_LEVELS={chitchat:0.3,fast:0.5,search:0.6,council:1,task:0.8};

async function sendMessage(){
  const msg=input.value.trim();
  if(!msg)return;
  input.value='';input.style.height='auto';
  addMessage('user',msg);
  log('Query received','info');
  setOrbActive(true,0.5);
  messages.scrollTop=messages.scrollHeight;

  try{
    const res=await fetch('/chat',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg})
    });
    const data=await res.json();
    setOrbActive(false);
    addMessage('assistant',data.reply);
    if(data.intent){
      pill.textContent=INTENT_LABELS[data.intent]||data.intent.toUpperCase();
      pill.style.display='inline';
      log('Route: '+data.intent.toUpperCase(),'ok');
      amd.textContent=INTENT_LABELS[data.intent]||'';
      amd.classList.add('show');
      setTimeout(()=>amd.classList.remove('show'),3000);
    }
    if(data.intent==='task'){loadTasks();log('Crew task spawned','warn');}
    if(data.intent==='council'){log('Council of 6 consulted','ok');}
  }catch(e){
    setOrbActive(false);
    addMessage('assistant','Connection error. Try again.');
    log('Error: '+e.message,'');
  }
}

function clearChat(){
  messages.innerHTML='<div class="msg assistant"><div class="av assistant">A</div><div class="bubble">Online. What do you need?</div></div>';
  log('Session cleared','');
}

// ── Tasks ──
async function loadTasks(){
  try{
    const r=await fetch('/tasks');
    const tasks=await r.json();
    const list=document.getElementById('tasks-list');
    list.innerHTML='';
    tasks.slice(0,10).forEach(t=>{
      const item=document.createElement('div');
      item.className='task-item';
      item.onclick=()=>openPanel(t.task_id||t.id,t.goal);
      const st=t.status||'queued';
      item.innerHTML=`<div class="task-goal">${(t.goal||'Task').slice(0,50)}</div>
        <div class="task-meta"><span class="tdot ${st}"></span><span class="tstatus">${st.toUpperCase()}</span></div>`;
      list.appendChild(item);
    });
  }catch(e){}
}

let currentPanelTask=null,pollInterval=null;

async function openPanel(taskId,goal){
  currentPanelTask=taskId;
  document.getElementById('tp-title').textContent=(goal||'TASK').slice(0,40).toUpperCase();
  document.getElementById('task-panel').classList.add('open');
  pollTask();
  if(pollInterval)clearInterval(pollInterval);
  pollInterval=setInterval(pollTask,3000);
}

async function pollTask(){
  if(!currentPanelTask)return;
  try{
    const r=await fetch('/task/'+currentPanelTask);
    const t=await r.json();
    const content=document.getElementById('tp-content');
    const dl=document.getElementById('dl-btn');
    let html='';
    if(t.steps&&t.steps.length) html+=t.steps.map(s=>`<div class="step-log">▸ ${s}</div>`).join('');
    if(t.result){html+=`<div style="margin-top:12px">${marked.parse(t.result)}</div>`;dl.style.display='block';}
    if(!html)html=`<div style="color:var(--muted);font-family:'Share Tech Mono',monospace;font-size:11px">INITIALIZING...</div>`;
    content.innerHTML=html;
    if(t.status==='complete'||t.status==='error')clearInterval(pollInterval);
  }catch(e){}
}

function closePanel(){
  document.getElementById('task-panel').classList.remove('open');
  if(pollInterval)clearInterval(pollInterval);
  currentPanelTask=null;
}

function downloadReport(){
  const content=document.getElementById('tp-content').innerText;
  const blob=new Blob([content],{type:'text/plain'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='atlas-report-'+Date.now()+'.txt';
  a.click();
}

loadTasks();
setInterval(loadTasks,15000);
log('Borfoli online','ok');
log('Primary: NEMOTRON-550B','info');
log('Council: 6 models active','info');
</script>
</body>
</html>"""

@app.route("/")
def index():
    return Response(HTML, mimetype='text/html')

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

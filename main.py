import os, json, requests, threading, uuid, time, re
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
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

# ── UI ─────────────────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Atlas</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
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

<div id="sidebar">
  <div id="sidebar-header">
    <h2>Atlas Tasks</h2>
    <button id="new-chat" onclick="clearChat()">+ New conversation</button>
  </div>
  <div id="tasks-list"></div>
  <div id="sidebar-footer">Atlas v2 · Always on</div>
</div>

<div id="main">
  <div id="header">
    <div id="header-dot"></div>
    <h1>Atlas</h1>
    <span id="intent-badge">fast</span>
    <span id="header-sub">Your AI executive layer</span>
  </div>

  <div id="messages">
    <div class="msg assistant">
      <div class="avatar assistant">A</div>
      <div class="bubble">Online. What do you need?</div>
    </div>
  </div>

  <div id="typing">
    <div class="avatar">A</div>
    <div class="dots">
      <div class="dot"></div><div class="dot"></div><div class="dot"></div>
    </div>
  </div>

  <div id="input-area">
    <div id="input-wrap">
      <textarea id="input" placeholder="Ask anything or give Atlas a task..." rows="1"></textarea>
      <button id="send" onclick="sendMessage()">
        <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
      </button>
    </div>
    <div id="input-hint">Enter to send · Shift+Enter for new line</div>
  </div>
</div>

<div id="task-panel">
  <div id="task-panel-header">
    <h3 id="panel-title">Task Details</h3>
    <button id="close-panel" onclick="closePanel()">×</button>
  </div>
  <div id="task-panel-content"></div>
  <button id="download-btn" onclick="downloadReport()">Download Report</button>
</div>

<script>
let currentPanelTask = null;
let pollInterval = null;

const input = document.getElementById('input');
const messages = document.getElementById('messages');
const typing = document.getElementById('typing');
const badge = document.getElementById('intent-badge');

input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 150) + 'px';
});

function addMessage(role, content) {
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  const av = document.createElement('div');
  av.className = `avatar ${role}`;
  av.textContent = role === 'user' ? 'M' : 'A';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (role === 'assistant') {
    bubble.innerHTML = marked.parse(content);
  } else {
    bubble.textContent = content;
  }
  div.appendChild(av);
  div.appendChild(bubble);
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  return div;
}

async function sendMessage() {
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  input.style.height = 'auto';
  addMessage('user', msg);
  typing.style.display = 'flex';
  messages.scrollTop = messages.scrollHeight;

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg})
    });
    const data = await res.json();
    typing.style.display = 'none';
    addMessage('assistant', data.reply);
    if (data.intent) {
      badge.textContent = data.intent;
      badge.style.display = 'inline';
    }
    if (data.intent === 'task') loadTasks();
  } catch(e) {
    typing.style.display = 'none';
    addMessage('assistant', 'Connection error. Try again.');
  }
}

function clearChat() {
  messages.innerHTML = `<div class="msg assistant"><div class="avatar assistant">A</div><div class="bubble">Online. What do you need?</div></div>`;
}

async function loadTasks() {
  try {
    const res = await fetch('/tasks');
    const tasks = await res.json();
    const list = document.getElementById('tasks-list');
    list.innerHTML = '';
    tasks.slice(0, 15).forEach(t => {
      const item = document.createElement('div');
      item.className = 'task-item';
      item.onclick = () => openPanel(t.task_id || t.id, t.goal);
      const status = t.status || 'queued';
      item.innerHTML = `
        <div class="task-goal">${(t.goal || 'Task').slice(0, 60)}</div>
        <div class="task-meta">
          <span class="status-dot dot-${status}"></span>
          <span>${status}</span>
        </div>`;
      list.appendChild(item);
    });
  } catch(e) {}
}

async function openPanel(taskId, goal) {
  currentPanelTask = taskId;
  document.getElementById('panel-title').textContent = (goal || 'Task').slice(0, 50);
  document.getElementById('task-panel').classList.add('open');
  pollTask();
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(pollTask, 3000);
}

async function pollTask() {
  if (!currentPanelTask) return;
  try {
    const res = await fetch(`/task/${currentPanelTask}`);
    const t = await res.json();
    const content = document.getElementById('task-panel-content');
    const dl = document.getElementById('download-btn');
    let html = '';
    if (t.steps && t.steps.length) {
      html += t.steps.map(s => `<div class="step-log">▸ ${s}</div>`).join('');
    }
    if (t.result) {
      html += `<div style="margin-top:16px">${marked.parse(t.result)}</div>`;
      dl.style.display = 'block';
    }
    if (!html) html = `<div style="color:var(--muted);font-size:13px">Waiting to start...</div>`;
    content.innerHTML = html;
    if (t.status === 'complete' || t.status === 'error') {
      clearInterval(pollInterval);
    }
  } catch(e) {}
}

function closePanel() {
  document.getElementById('task-panel').classList.remove('open');
  if (pollInterval) clearInterval(pollInterval);
  currentPanelTask = null;
}

function downloadReport() {
  const content = document.getElementById('task-panel-content').innerText;
  const blob = new Blob([content], {type: 'text/plain'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `atlas-report-${Date.now()}.txt`;
  a.click();
}

loadTasks();
setInterval(loadTasks, 15000);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

import os, json, requests, subprocess, threading
from flask import Flask, request, jsonify, render_template_string
from groq import Groq

app = Flask(__name__)
client = Groq(api_key=os.environ["GROQ_API_KEY"])

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TAVILY_KEY = os.environ["TAVILY_KEY"]
HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

ROUTER = "llama-3.1-8b-instant"
COUNCIL = [
    ("openai/gpt-oss-120b", "Powerhouse"),
    ("openai/gpt-oss-20b", "Swift"),
    ("llama-3.3-70b-versatile", "Strategist"),
    ("meta-llama/llama-4-scout-17b-16e-instruct", "Scout"),
    ("qwen/qwen3-32b", "Reasoner"),
    ("groq/compound-mini", "Orchestrator"),
]
SYNTHESIZER = "openai/gpt-oss-120b"

# ── Memory ──────────────────────────────────────────────────────────────────

def load_memory():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_memory?order=id.desc&limit=1", headers=HEADERS)
    if r.status_code == 200 and r.json():
        row = r.json()[0]
        facts = json.loads(row["facts"]) if isinstance(row["facts"], str) else row["facts"]
        history = json.loads(row["history"]) if isinstance(row["history"], str) else row["history"]
        return {"facts": facts or [], "history": history or []}
    return {"facts": [], "history": []}

def save_memory(mem):
    requests.delete(f"{SUPABASE_URL}/rest/v1/atlas_memory?id=gte.0", headers={**HEADERS, "Prefer": "return=minimal"})
    requests.post(f"{SUPABASE_URL}/rest/v1/atlas_memory", headers={**HEADERS, "Prefer": "return=minimal"},
                  json={"facts": json.dumps(mem["facts"]), "history": json.dumps(mem["history"])})

def extract_facts(text, mem):
    try:
        prompt = f"""Extract any personal facts about the user from this text. Return a JSON array of strings, or [] if none.
Existing facts: {json.dumps(mem['facts'])}
Text: {text}
Reply ONLY with a JSON array."""
        r = client.chat.completions.create(model=ROUTER, messages=[{"role": "user", "content": prompt}], max_tokens=200)
        raw = r.choices[0].message.content.strip()
        s, e = raw.find("["), raw.rfind("]")
        new_facts = json.loads(raw[s:e+1]) if s != -1 else []
        for f in new_facts:
            if f not in mem["facts"]:
                mem["facts"].append(f)
    except:
        pass

# ── Tools ────────────────────────────────────────────────────────────────────

def web_search(query):
    try:
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_KEY, "query": query, "max_results": 5, "search_depth": "basic"
        }, timeout=10)
        results = r.json().get("results", [])
        return "\n\n".join([f"{x['title']}\n{x['url']}\n{x['content'][:300]}" for x in results])
    except Exception as e:
        return f"Search failed: {e}"

def run_code(code):
    try:
        result = subprocess.run(["python3", "-c", code], capture_output=True, text=True, timeout=15)
        out = result.stdout or result.stderr
        return out[:1000] if out else "No output"
    except Exception as e:
        return f"Error: {e}"

# ── Tasks (background jobs) ──────────────────────────────────────────────────

def save_task(task_id, status, result=""):
    requests.post(f"{SUPABASE_URL}/rest/v1/atlas_tasks", headers={**HEADERS, "Prefer": "return=minimal"},
                  json={"task_id": task_id, "status": status, "result": result})

def update_task(task_id, status, result=""):
    requests.patch(f"{SUPABASE_URL}/rest/v1/atlas_tasks?task_id=eq.{task_id}",
                   headers={**HEADERS, "Prefer": "return=minimal"},
                   json={"status": status, "result": result})

def get_task(task_id):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_tasks?task_id=eq.{task_id}", headers=HEADERS)
    return r.json()[0] if r.json() else None

def run_agent_task(task_id, goal):
    update_task(task_id, "running", "Starting...")
    mem = load_memory()
    steps_prompt = f"""You are Atlas, an autonomous AI agent. Break this goal into up to 6 concrete steps.
Goal: {goal}
Return ONLY a JSON array of step strings. Example: ["Step 1", "Step 2"]"""
    r = client.chat.completions.create(model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": steps_prompt}], max_tokens=400)
    raw = r.choices[0].message.content.strip()
    try:
        s, e = raw.find("["), raw.rfind("]")
        steps = json.loads(raw[s:e+1])
    except:
        steps = [goal]

    full_log = f"Goal: {goal}\n\nSteps planned: {len(steps)}\n\n"
    context = ""

    for i, step in enumerate(steps):
        full_log += f"── Step {i+1}: {step}\n"
        update_task(task_id, "running", full_log)

        needs_search = any(w in step.lower() for w in ["research", "find", "search", "look up", "check", "market", "competitor", "price", "how to"])
        needs_code = any(w in step.lower() for w in ["calculate", "compute", "generate", "create file", "write code", "script", "analyze data"])

        tool_result = ""
        if needs_search:
            tool_result = web_search(step)
            full_log += f"[Search results]\n{tool_result[:500]}\n"
        elif needs_code:
            code_prompt = f"Write Python code to: {step}\nContext: {context}\nReturn ONLY the code, no explanation."
            cr = client.chat.completions.create(model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": code_prompt}], max_tokens=300)
            code = cr.choices[0].message.content.strip().replace("```python","").replace("```","")
            tool_result = run_code(code)
            full_log += f"[Code output]\n{tool_result}\n"

        step_prompt = f"""You are Atlas completing step {i+1} of {len(steps)} toward this goal: {goal}
Step: {step}
Tool output: {tool_result}
Previous context: {context}
Write a concise result for this step (2-4 sentences)."""
        sr = client.chat.completions.create(model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": step_prompt}], max_tokens=300)
        step_result = sr.choices[0].message.content.strip()
        context += f"\nStep {i+1} ({step}): {step_result}"
        full_log += f"{step_result}\n\n"
        update_task(task_id, "running", full_log)

    final_prompt = f"""You are Atlas. Summarize the completed work for this goal: {goal}
Work done: {context}
Write a clear final summary of what was accomplished."""
    fr = client.chat.completions.create(model=SYNTHESIZER,
        messages=[{"role": "user", "content": final_prompt}], max_tokens=500)
    summary = fr.choices[0].message.content.strip()
    full_log += f"\n── FINAL SUMMARY ──\n{summary}"
    update_task(task_id, "done", full_log)

# ── Routing ──────────────────────────────────────────────────────────────────

def route(msg):
    r = client.chat.completions.create(model=ROUTER,
        messages=[{"role": "user", "content": f'Classify: "{msg}"\nReply ONLY one word: "chitchat", "complex", or "task"'}],
        max_tokens=5)
    return r.choices[0].message.content.strip().lower()

def build_context(mem):
    ctx = ""
    if mem["facts"]:
        ctx += "Facts about user: " + "; ".join(mem["facts"]) + "\n\n"
    for h in mem["history"][-20:]:
        if isinstance(h, dict):
            ctx += f"User: {h['user']}\nAtlas: {h['atlas']}\n"
    return ctx

def council_answer(msg, mem):
    ctx = build_context(mem)
    needs_search = any(w in msg.lower() for w in ["search", "find", "latest", "current", "today", "news", "price", "who is", "what is"])
    search_ctx = ""
    if needs_search:
        search_ctx = f"\n\nWeb search results:\n{web_search(msg)}"
    opinions = []
    for model, name in COUNCIL:
        try:
            r = client.chat.completions.create(model=model,
                messages=[{"role": "system", "content": f"You are {name}, part of Atlas council. Be concise (2-3 sentences)."},
                          {"role": "user", "content": f"{ctx}{search_ctx}\nUser: {msg}"}],
                max_tokens=300)
            opinions.append(f"{name}: {r.choices[0].message.content.strip()}")
        except:
            pass
    combined = "\n".join(opinions)
    final = client.chat.completions.create(model=SYNTHESIZER,
        messages=[{"role": "system", "content": "You are Atlas. Synthesize the council's input into one clear response."},
                  {"role": "user", "content": f"Council input:\n{combined}\n\nUser asked: {msg}"}],
        max_tokens=600)
    return final.choices[0].message.content.strip()

def fast_answer(msg, mem):
    ctx = build_context(mem)
    r = client.chat.completions.create(model="llama-3.3-70b-versatile",
        messages=[{"role": "system", "content": "You are Atlas, a helpful AI assistant."},
                  {"role": "user", "content": f"{ctx}\nUser: {msg}"}],
        max_tokens=400)
    return r.choices[0].message.content.strip()

# ── HTML ─────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html><html><head><title>Atlas</title><style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:monospace;height:100vh;display:flex;flex-direction:column}
header{padding:16px 24px;border-bottom:1px solid #222;color:#7c6af7;font-size:18px;display:flex;justify-content:space-between;align-items:center}
#chat{flex:1;overflow-y:auto;padding:24px;display:flex;flex-direction:column;gap:16px}
.msg{max-width:75%;padding:12px 16px;border-radius:12px;line-height:1.5}
.user{align-self:flex-end;background:#1e1b4b;color:#c7d2fe}
.atlas{align-self:flex-start;background:#111;border:1px solid #333;white-space:pre-wrap}
.task-msg{align-self:flex-start;background:#0d1f0d;border:1px solid #1a4a1a;color:#86efac;white-space:pre-wrap;max-width:90%}
footer{padding:16px;border-top:1px solid #222;display:flex;gap:8px}
input{flex:1;background:#111;border:1px solid #333;color:#e0e0e0;padding:12px;border-radius:8px;font-family:monospace;font-size:14px}
button{background:#7c6af7;color:#fff;border:none;padding:12px 24px;border-radius:8px;cursor:pointer;font-size:14px}
.badge{font-size:11px;padding:2px 8px;border-radius:99px;background:#1a1a2e;color:#7c6af7;border:1px solid #7c6af7}
</style></head><body>
<header><span>⚡ Atlas — 6-Model Council</span><span class="badge">autonomous mode</span></header>
<div id="chat"></div>
<footer><input id="inp" placeholder="Ask anything or give Atlas a task to work on..." onkeydown="if(event.key==='Enter')send()"/><button onclick="send()">Send</button></footer>
<script>
let polling = null;
async function send(){
  const inp=document.getElementById('inp');
  const msg=inp.value.trim();if(!msg)return;
  inp.value='';
  addMsg(msg,'user');
  const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
  const d=await r.json();
  if(d.task_id){
    const el=addMsg('Starting task... this may take a minute.','task-msg');
    pollTask(d.task_id, el);
  } else {
    addMsg(d.response,'atlas');
  }
}
function addMsg(text,cls){
  const div=document.createElement('div');
  div.className='msg '+cls;div.textContent=text;
  document.getElementById('chat').appendChild(div);
  div.scrollIntoView();
  return div;
}
async function pollTask(id, el){
  const r=await fetch('/task/'+id);
  const d=await r.json();
  el.textContent=d.result||'Working...';
  el.scrollIntoView();
  if(d.status!=='done'){
    setTimeout(()=>pollTask(id,el),3000);
  }
}
</script></body></html>"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/chat", methods=["POST"])
def chat():
    msg = request.json.get("message", "")
    mem = load_memory()
    task_type = route(msg)

    if "task" in task_type or any(w in msg.lower() for w in ["build", "create", "make", "research", "plan", "write a", "develop", "set up", "launch"]):
        import uuid
        task_id = str(uuid.uuid4())[:8]
        save_task(task_id, "queued", "Task queued...")
        threading.Thread(target=run_agent_task, args=(task_id, msg), daemon=True).start()
        mem["history"].append({"user": msg, "atlas": f"[Running task: {task_id}]"})
        save_memory(mem)
        return jsonify({"task_id": task_id})

    if "complex" in task_type:
        response = council_answer(msg, mem)
    else:
        response = fast_answer(msg, mem)

    extract_facts(msg + " " + response, mem)
    mem["history"].append({"user": msg, "atlas": response})
    if len(mem["history"]) > 40:
        mem["history"] = mem["history"][-40:]
    save_memory(mem)
    return jsonify({"response": response})

@app.route("/task/<task_id>")
def task_status(task_id):
    t = get_task(task_id)
    if not t:
        return jsonify({"status": "not found", "result": ""})
    return jsonify({"status": t["status"], "result": t["result"]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

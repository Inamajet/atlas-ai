import os, json, requests, subprocess, threading, uuid, time
from flask import Flask, request, jsonify, render_template_string, Response, stream_with_context
from groq import Groq

app = Flask(__name__)
client = Groq(api_key=os.environ["GROQ_API_KEY"])

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TAVILY_KEY = os.environ["TAVILY_KEY"]
RESEND_KEY = os.environ["RESEND_KEY"]
USER_EMAIL = os.environ.get("USER_EMAIL", "manitejamaram1@gmail.com")
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

# ── Memory ───────────────────────────────────────────────────────────────────

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
        prompt = f"""Extract personal facts about the user from this text. Return JSON array of strings or [].
Existing: {json.dumps(mem['facts'])}
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

def send_email(subject, body):
    try:
        requests.post("https://api.resend.com/emails", headers={
            "Authorization": f"Bearer {RESEND_KEY}",
            "Content-Type": "application/json"
        }, json={
            "from": "Atlas <onboarding@resend.dev>",
            "to": [USER_EMAIL],
            "subject": subject,
            "text": body
        })
    except:
        pass

# ── Tasks ────────────────────────────────────────────────────────────────────

def save_task(task_id, goal, status, result=""):
    requests.post(f"{SUPABASE_URL}/rest/v1/atlas_tasks", headers={**HEADERS, "Prefer": "return=minimal"},
                  json={"task_id": task_id, "goal": goal, "status": status, "result": result})

def update_task(task_id, status, result=""):
    requests.patch(f"{SUPABASE_URL}/rest/v1/atlas_tasks?task_id=eq.{task_id}",
                   headers={**HEADERS, "Prefer": "return=minimal"},
                   json={"status": status, "result": result})

def get_task(task_id):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_tasks?task_id=eq.{task_id}", headers=HEADERS)
    return r.json()[0] if r.json() else None

def get_all_tasks():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/atlas_tasks?order=id.desc&limit=20", headers=HEADERS)
    return r.json() if r.status_code == 200 else []

def run_agent_task(task_id, goal):
    update_task(task_id, "running", "🧠 Planning steps...")
    steps_prompt = f"""You are Atlas, an autonomous AI agent. Break this goal into up to 6 concrete steps.
Goal: {goal}
Return ONLY a JSON array of step strings."""
    r = client.chat.completions.create(model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": steps_prompt}], max_tokens=400)
    raw = r.choices[0].message.content.strip()
    try:
        s, e = raw.find("["), raw.rfind("]")
        steps = json.loads(raw[s:e+1])
    except:
        steps = [goal]

    full_log = f"**Goal:** {goal}\n**Steps planned:** {len(steps)}\n\n"
    context = ""

    for i, step in enumerate(steps):
        full_log += f"---\n**Step {i+1}/{len(steps)}:** {step}\n"
        update_task(task_id, "running", full_log + "\n⏳ Working...")

        needs_search = any(w in step.lower() for w in ["research", "find", "search", "look up", "check", "market", "competitor", "price", "how to", "latest", "news"])
        needs_code = any(w in step.lower() for w in ["calculate", "compute", "generate", "script", "analyze data"])

        tool_result = ""
        if needs_search:
            tool_result = web_search(step)
            full_log += f"🔍 *Searched the web*\n"
        elif needs_code:
            code_prompt = f"Write Python code to: {step}\nReturn ONLY the code."
            cr = client.chat.completions.create(model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": code_prompt}], max_tokens=300)
            code = cr.choices[0].message.content.strip().replace("```python","").replace("```","")
            tool_result = run_code(code)
            full_log += f"💻 *Ran code*\n"

        step_prompt = f"""You are Atlas completing step {i+1} of {len(steps)} toward: {goal}
Step: {step}
Tool output: {tool_result}
Previous context: {context}
Write a clear, concise result (3-5 sentences)."""
        sr = client.chat.completions.create(model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": step_prompt}], max_tokens=400)
        step_result = sr.choices[0].message.content.strip()
        context += f"\nStep {i+1}: {step_result}"
        full_log += f"{step_result}\n\n"
        update_task(task_id, "running", full_log)

    final_prompt = f"""You are Atlas. Write a comprehensive final report for: {goal}
Work completed: {context}
Format it clearly with sections and key findings."""
    fr = client.chat.completions.create(model=SYNTHESIZER,
        messages=[{"role": "user", "content": final_prompt}], max_tokens=800)
    summary = fr.choices[0].message.content.strip()
    full_log += f"\n---\n## ✅ Final Report\n\n{summary}"
    update_task(task_id, "done", full_log)
    send_email(f"Atlas completed: {goal[:50]}", f"Task complete!\n\n{summary}")

# ── Routing ──────────────────────────────────────────────────────────────────

def route(msg):
    r = client.chat.completions.create(model=ROUTER,
        messages=[{"role": "user", "content": f'Classify this message. Reply ONLY one word — "chitchat", "complex", or "task".\nMessage: "{msg}"'}],
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

# ── HTML ─────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atlas</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root{--bg:#0d0d0d;--sidebar:#111;--border:#222;--accent:#7c6af7;--user-bg:#1e1b4b;--msg-bg:#161616;--text:#e0e0e0;--muted:#666;--green:#22c55e;--radius:12px}
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;height:100vh;overflow:hidden}
  /* Sidebar */
  #sidebar{width:260px;background:var(--sidebar);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0}
  #sidebar-header{padding:20px 16px 12px;border-bottom:1px solid var(--border)}
  #sidebar-header h1{font-size:17px;color:var(--accent);font-weight:600;display:flex;align-items:center;gap:8px}
  #sidebar-header p{font-size:11px;color:var(--muted);margin-top:4px}
  #new-chat{margin:12px;padding:10px;background:var(--accent);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;width:calc(100% - 24px)}
  #new-chat:hover{opacity:.9}
  .sidebar-section{padding:8px 12px 4px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
  #task-list{flex:1;overflow-y:auto;padding:4px 8px}
  .task-item{padding:8px 10px;border-radius:8px;cursor:pointer;margin-bottom:2px;font-size:13px;color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;gap:6px}
  .task-item:hover{background:#1a1a1a}
  .task-item.active{background:#1e1b3a;color:var(--accent)}
  .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
  .dot.done{background:var(--green)}
  .dot.running{background:#f59e0b;animation:pulse 1s infinite}
  .dot.queued{background:var(--muted)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  /* Main */
  #main{flex:1;display:flex;flex-direction:column;overflow:hidden}
  #chat-area{flex:1;overflow-y:auto;padding:0}
  #chat-area::-webkit-scrollbar{width:6px}
  #chat-area::-webkit-scrollbar-thumb{background:#333;border-radius:3px}
  .msg-wrap{padding:24px 10% ;max-width:100%}
  .msg-wrap.user-wrap{background:transparent}
  .msg-wrap.atlas-wrap{background:#111}
  .msg-inner{max-width:720px;margin:0 auto;display:flex;gap:14px;align-items:flex-start}
  .avatar{width:32px;height:32px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:600}
  .avatar.user-av{background:var(--user-bg);color:#a5b4fc}
  .avatar.atlas-av{background:linear-gradient(135deg,#7c6af7,#a855f7);color:#fff}
  .msg-content{flex:1;padding-top:4px;font-size:15px;line-height:1.7;color:var(--text)}
  .msg-content.user-content{color:#c7d2fe}
  .msg-content p{margin-bottom:12px}
  .msg-content p:last-child{margin-bottom:0}
  .msg-content h1,.msg-content h2,.msg-content h3{margin:16px 0 8px;color:#fff}
  .msg-content ul,.msg-content ol{padding-left:20px;margin-bottom:12px}
  .msg-content li{margin-bottom:4px}
  .msg-content code{background:#222;padding:2px 6px;border-radius:4px;font-family:monospace;font-size:13px}
  .msg-content pre{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:16px;overflow-x:auto;margin:12px 0}
  .msg-content pre code{background:none;padding:0}
  .msg-content strong{color:#fff}
  .msg-content hr{border:none;border-top:1px solid #333;margin:16px 0}
  /* Thinking indicator */
  .thinking{display:flex;gap:5px;align-items:center;padding-top:6px}
  .thinking span{width:7px;height:7px;background:var(--muted);border-radius:50%;animation:bounce .9s infinite}
  .thinking span:nth-child(2){animation-delay:.15s}
  .thinking span:nth-child(3){animation-delay:.3s}
  @keyframes bounce{0%,80%,100%{transform:translateY(0)}40%{transform:translateY(-6px)}}
  /* Task progress */
  .task-progress{background:#0d1a0d;border:1px solid #1a3a1a;border-radius:10px;padding:16px;font-size:13px;line-height:1.8;font-family:monospace;color:#86efac;white-space:pre-wrap;max-height:500px;overflow-y:auto}
  .task-progress h2{color:#4ade80;font-size:14px}
  .task-progress strong{color:#4ade80}
  .dl-btn{margin-top:10px;padding:8px 16px;background:#1a3a1a;color:#4ade80;border:1px solid #2a5a2a;border-radius:6px;cursor:pointer;font-size:12px}
  .dl-btn:hover{background:#2a5a2a}
  /* Input */
  #input-area{padding:16px 10%;border-top:1px solid var(--border);background:var(--bg)}
  #input-box{max-width:720px;margin:0 auto;background:#1a1a1a;border:1px solid #333;border-radius:12px;display:flex;align-items:flex-end;gap:8px;padding:12px 14px}
  #input-box:focus-within{border-color:#444}
  #inp{flex:1;background:none;border:none;color:var(--text);font-size:15px;resize:none;outline:none;max-height:200px;line-height:1.5;font-family:inherit}
  #inp::placeholder{color:var(--muted)}
  #send-btn{background:var(--accent);border:none;color:#fff;width:34px;height:34px;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:.2s}
  #send-btn:hover{opacity:.85}
  #send-btn svg{width:16px;height:16px}
  .hint{text-align:center;font-size:12px;color:var(--muted);margin-top:8px}
  /* Empty state */
  #empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:12px;color:var(--muted)}
  #empty h2{font-size:24px;color:#555;font-weight:500}
  #empty p{font-size:14px;max-width:400px;text-align:center;line-height:1.6}
  .suggestions{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:8px;max-width:500px}
  .sug{padding:8px 14px;background:#1a1a1a;border:1px solid #333;border-radius:8px;font-size:13px;cursor:pointer;color:#aaa}
  .sug:hover{background:#222;color:#fff}
</style>
</head>
<body>
<div id="sidebar">
  <div id="sidebar-header">
    <h1>⚡ Atlas</h1>
    <p>6-Model Council · Autonomous Agent</p>
  </div>
  <button id="new-chat" onclick="newChat()">+ New Chat</button>
  <div class="sidebar-section">Background Tasks</div>
  <div id="task-list"></div>
</div>

<div id="main">
  <div id="chat-area">
    <div id="empty">
      <h2>What can I help with?</h2>
      <p>Ask me anything or give me a complex task — I'll work on it autonomously.</p>
      <div class="suggestions">
        <div class="sug" onclick="suggest(this)">Build me a business plan</div>
        <div class="sug" onclick="suggest(this)">Research my competitors</div>
        <div class="sug" onclick="suggest(this)">Write and run Python code</div>
        <div class="sug" onclick="suggest(this)">What do you know about me?</div>
      </div>
    </div>
  </div>
  <div id="input-area">
    <div id="input-box">
      <textarea id="inp" rows="1" placeholder="Message Atlas..." onkeydown="handleKey(event)" oninput="autosize(this)"></textarea>
      <button id="send-btn" onclick="send()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      </button>
    </div>
    <div class="hint">Atlas can make mistakes. Verify important information.</div>
  </div>
</div>

<script>
marked.setOptions({breaks:true});
let thinking = false;

function autosize(el){
  el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,200)+'px';
}
function handleKey(e){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}
}
function suggest(el){
  document.getElementById('inp').value=el.textContent;
  send();
}
function newChat(){
  document.getElementById('chat-area').innerHTML='<div id="empty"><h2>What can I help with?</h2><p>Ask me anything or give me a complex task — I\'ll work on it autonomously.</p></div>';
}

function addMsg(html, cls, raw){
  const empty=document.getElementById('empty');
  if(empty)empty.remove();
  const wrap=document.createElement('div');
  wrap.className='msg-wrap '+(cls==='user'?'user-wrap':'atlas-wrap');
  const inner=document.createElement('div');
  inner.className='msg-inner';
  const av=document.createElement('div');
  av.className='avatar '+(cls==='user'?'user-av':'atlas-av');
  av.textContent=cls==='user'?'M':'A';
  const content=document.createElement('div');
  content.className='msg-content '+(cls==='user'?'user-content':'');
  if(cls==='user'){content.textContent=raw||html;}
  else{content.innerHTML=marked.parse(html);}
  inner.appendChild(av);inner.appendChild(content);
  wrap.appendChild(inner);
  document.getElementById('chat-area').appendChild(wrap);
  wrap.scrollIntoView({behavior:'smooth'});
  return content;
}

function addThinking(){
  const empty=document.getElementById('empty');
  if(empty)empty.remove();
  const wrap=document.createElement('div');
  wrap.className='msg-wrap atlas-wrap';wrap.id='thinking-wrap';
  const inner=document.createElement('div');inner.className='msg-inner';
  const av=document.createElement('div');av.className='avatar atlas-av';av.textContent='A';
  const content=document.createElement('div');content.className='msg-content';
  content.innerHTML='<div class="thinking"><span></span><span></span><span></span></div>';
  inner.appendChild(av);inner.appendChild(content);
  wrap.appendChild(inner);
  document.getElementById('chat-area').appendChild(wrap);
  wrap.scrollIntoView({behavior:'smooth'});
}
function removeThinking(){
  const t=document.getElementById('thinking-wrap');if(t)t.remove();
}

async function send(){
  if(thinking)return;
  const inp=document.getElementById('inp');
  const msg=inp.value.trim();if(!msg)return;
  inp.value='';inp.style.height='auto';
  addMsg(msg,'user',msg);
  thinking=true;
  addThinking();
  try{
    const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
    const d=await r.json();
    removeThinking();
    if(d.task_id){
      const content=addMsg('','atlas');
      const prog=document.createElement('div');prog.className='task-progress';prog.textContent='⏳ Starting task...';
      content.appendChild(prog);
      const dl=document.createElement('button');dl.className='dl-btn';dl.textContent='⬇ Download Report';dl.style.display='none';
      dl.onclick=()=>{
        const blob=new Blob([prog.textContent],{type:'text/plain'});
        const a=document.createElement('a');a.href=URL.createObjectURL(blob);
        a.download='atlas-report.txt';a.click();
      };
      content.appendChild(dl);
      pollTask(d.task_id,prog,dl);
    } else {
      addMsg(d.response,'atlas');
    }
  }catch(e){removeThinking();addMsg('Something went wrong. Try again.','atlas');}
  thinking=false;
}

async function pollTask(id,el,dlBtn){
  const r=await fetch('/task/'+id);
  const d=await r.json();
  el.innerHTML=marked.parse(d.result||'Working...');
  el.scrollIntoView({behavior:'smooth'});
  if(d.status==='done'){
    dlBtn.style.display='inline-block';
    loadTasks();
  } else {
    setTimeout(()=>pollTask(id,el,dlBtn),2500);
  }
}

async function loadTasks(){
  const r=await fetch('/tasks');
  const tasks=await r.json();
  const list=document.getElementById('task-list');
  list.innerHTML='';
  tasks.forEach(t=>{
    const el=document.createElement('div');
    el.className='task-item';
    const dot=document.createElement('div');
    dot.className='dot '+(t.status==='done'?'done':t.status==='running'?'running':'queued');
    const label=document.createElement('span');
    label.textContent=(t.goal||'Task').substring(0,35);
    el.appendChild(dot);el.appendChild(label);
    el.onclick=()=>showTaskResult(t);
    list.appendChild(el);
    if(t.status==='running')setTimeout(loadTasks,3000);
  });
}

function showTaskResult(t){
  const content=addMsg('','atlas');
  const prog=document.createElement('div');prog.className='task-progress';
  prog.innerHTML=marked.parse(t.result||'No result yet');
  content.appendChild(prog);
  const dl=document.createElement('button');dl.className='dl-btn';dl.textContent='⬇ Download Report';
  dl.onclick=()=>{
    const blob=new Blob([t.result],{type:'text/plain'});
    const a=document.createElement('a');a.href=URL.createObjectURL(blob);
    a.download='atlas-report.txt';a.click();
  };
  content.appendChild(dl);
}

loadTasks();
setInterval(loadTasks,10000);
</script>
</body>
</html>"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/chat", methods=["POST"])
def chat():
    msg = request.json.get("message", "")
    mem = load_memory()
    task_type = route(msg)

    is_task = "task" in task_type or any(w in msg.lower() for w in [
        "build", "create", "make me", "research", "plan", "write a", "develop",
        "set up", "launch", "analyze", "find me", "compile", "generate a report"
    ])

    if is_task:
        task_id = str(uuid.uuid4())[:8]
        save_task(task_id, msg, "queued", "Task queued...")
        threading.Thread(target=run_agent_task, args=(task_id, msg), daemon=True).start()
        mem["history"].append({"user": msg, "atlas": f"[Running background task: {task_id}]"})
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

def council_answer(msg, mem):
    ctx = build_context(mem)
    needs_search = any(w in msg.lower() for w in ["search","find","latest","current","today","news","price","who is","what is"])
    search_ctx = f"\n\nWeb search results:\n{web_search(msg)}" if needs_search else ""
    opinions = []
    for model, name in COUNCIL:
        try:
            r = client.chat.completions.create(model=model,
                messages=[{"role":"system","content":f"You are {name}, part of Atlas council. Be concise (2-3 sentences)."},
                          {"role":"user","content":f"{ctx}{search_ctx}\nUser: {msg}"}],
                max_tokens=300)
            opinions.append(f"{name}: {r.choices[0].message.content.strip()}")
        except:
            pass
    combined = "\n".join(opinions)
    final = client.chat.completions.create(model=SYNTHESIZER,
        messages=[{"role":"system","content":"You are Atlas. Synthesize the council's input into one clear, well-formatted response. Use markdown."},
                  {"role":"user","content":f"Council input:\n{combined}\n\nUser asked: {msg}"}],
        max_tokens=700)
    return final.choices[0].message.content.strip()

def fast_answer(msg, mem):
    ctx = build_context(mem)
    r = client.chat.completions.create(model="llama-3.3-70b-versatile",
        messages=[{"role":"system","content":"You are Atlas, a helpful AI assistant. Use markdown for formatting when appropriate."},
                  {"role":"user","content":f"{ctx}\nUser: {msg}"}],
        max_tokens=500)
    return r.choices[0].message.content.strip()

def build_context(mem):
    ctx = ""
    if mem["facts"]:
        ctx += "Facts about user: " + "; ".join(mem["facts"]) + "\n\n"
    for h in mem["history"][-20:]:
        if isinstance(h, dict):
            ctx += f"User: {h['user']}\nAtlas: {h['atlas']}\n"
    return ctx

@app.route("/task/<task_id>")
def task_status(task_id):
    t = get_task(task_id)
    if not t:
        return jsonify({"status":"not found","result":""})
    return jsonify({"status":t["status"],"result":t["result"]})

@app.route("/tasks")
def all_tasks():
    return jsonify(get_all_tasks())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

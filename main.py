import os, json
from flask import Flask, request, jsonify, render_template_string
from groq import Groq

app = Flask(__name__)
client = Groq(api_key=os.environ["GROQ_API_KEY"])

ROUTER = "llama-3.1-8b-instant"
COUNCIL = [
    ("openai/gpt-oss-120b",                       "Powerhouse"),
    ("openai/gpt-oss-20b",                        "Swift"),
    ("llama-3.3-70b-versatile",                   "Strategist"),
    ("meta-llama/llama-4-scout-17b-16e-instruct", "Scout"),
    ("qwen/qwen3-32b",                            "Reasoner"),
    ("groq/compound",                             "Orchestrator"),
]
SYNTHESIZER = "openai/gpt-oss-120b"
MEMORY_FILE = "/tmp/atlas_memory.json"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        return json.load(open(MEMORY_FILE))
    return {"facts": [], "history": []}

def save_memory(mem):
    json.dump(mem, open(MEMORY_FILE, "w"), indent=2)

def extract_facts(user_msg, reply):
    try:
        r = client.chat.completions.create(model=ROUTER, max_tokens=80,
            messages=[{"role":"system","content":'Extract personal facts about the user. Return JSON list of strings or [].'},
                      {"role":"user","content":f"User: {user_msg}\nAtlas: {reply}"}])
        text = r.choices[0].message.content.strip()
        s, e = text.find("["), text.rfind("]")
        if s != -1 and e != -1:
            facts = json.loads(text[s:e+1])
            return facts if isinstance(facts, list) else []
    except: pass
    return []

def route(text):
    r = client.chat.completions.create(model=ROUTER, max_tokens=5,
        messages=[{"role":"system","content":'Reply ONLY: "chitchat" or "complex"'},
                  {"role":"user","content":text}])
    return r.choices[0].message.content.strip().lower()

def build_context(mem):
    if not mem["facts"]: return ""
    return "What you know about the user:\n" + "\n".join(f"- {f}" for f in mem["facts"][-20:])

def council_answer(text, mem):
    opinions = []
    for model, role in COUNCIL:
        try:
            r = client.chat.completions.create(model=model, max_tokens=200,
                messages=[{"role":"system","content":f"You are the {role}. Give your best input in 2-3 sentences."},
                          {"role":"user","content":text}])
            opinions.append(f"[{role}]: {r.choices[0].message.content.strip()}")
        except: pass
    if not opinions: return fast_answer(text, mem)
    ctx = build_context(mem)
    final = client.chat.completions.create(model=SYNTHESIZER,
        messages=[{"role":"system","content":f"You are Atlas. Synthesize into one powerful answer. Don't mention the council.\n{ctx}"},
                  {"role":"user","content":f"User asked: {text}\n\nCouncil:\n" + "\n".join(opinions)}])
    return final.choices[0].message.content

def fast_answer(text, mem):
    recent = mem["history"][-6:]
    msgs = [{"role":"system","content":f"You are Atlas. Be concise.\n{build_context(mem)}"}] + recent + [{"role":"user","content":text}]
    r = client.chat.completions.create(model=ROUTER, messages=msgs)
    return r.choices[0].message.content

HTML = '''<!DOCTYPE html>
<html>
<head>
<title>Atlas</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0a0a0a; color:#e0e0e0; font-family:monospace; height:100vh; display:flex; flex-direction:column; }
#header { padding:16px 24px; border-bottom:1px solid #222; font-size:18px; color:#7c6ff7; }
#chat { flex:1; overflow-y:auto; padding:24px; display:flex; flex-direction:column; gap:12px; }
.msg { max-width:75%; padding:12px 16px; border-radius:12px; line-height:1.5; white-space:pre-wrap; }
.user { background:#1e1e2e; align-self:flex-end; color:#cdd6f4; }
.atlas { background:#181825; align-self:flex-start; color:#cba6f7; border-left:3px solid #7c6ff7; }
.tag { font-size:10px; color:#555; margin-bottom:4px; }
#input-row { display:flex; padding:16px 24px; gap:12px; border-top:1px solid #222; }
#input { flex:1; background:#1e1e2e; border:1px solid #333; border-radius:8px; padding:12px 16px; color:#e0e0e0; font-family:monospace; font-size:14px; outline:none; }
#send { background:#7c6ff7; border:none; border-radius:8px; padding:12px 20px; color:white; cursor:pointer; font-size:14px; }
#send:hover { background:#9d8fff; }
</style>
</head>
<body>
<div id="header">⚡ Atlas — 6-Model Council</div>
<div id="chat" id="chat"></div>
<div id="input-row">
  <input id="input" placeholder="Ask Atlas anything..." onkeydown="if(event.key==='Enter')send()">
  <button id="send" onclick="send()">Send</button>
</div>
<script>
const chat = document.getElementById("chat");
function addMsg(text, cls, tag) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  if(tag) { const t=document.createElement("div"); t.className="tag"; t.textContent=tag; d.appendChild(t); }
  const p = document.createElement("div"); p.textContent=text; d.appendChild(p);
  chat.appendChild(d); chat.scrollTop=chat.scrollHeight;
}
async function send() {
  const inp = document.getElementById("input");
  const text = inp.value.trim(); if(!text) return;
  inp.value = ""; addMsg(text, "user");
  const thinking = document.createElement("div");
  thinking.className="msg atlas"; thinking.textContent="thinking..."; chat.appendChild(thinking);
  const res = await fetch("/chat", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({message:text})});
  const data = await res.json();
  chat.removeChild(thinking);
  addMsg(data.reply, "atlas", data.mode);
}
</script>
</body>
</html>'''

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/chat", methods=["POST"])
def chat():
    text = request.json.get("message","").strip()
    if not text: return jsonify({"reply":"","mode":""})
    mem = load_memory()
    kind = route(text)
    if "chitchat" in kind:
        reply = fast_answer(text, mem)
        mode = "fast → 1 model"
    else:
        reply = council_answer(text, mem)
        mode = "council → 6 models"
    mem["history"].append({"role":"user","content":text})
    mem["history"].append({"role":"assistant","content":reply})
    mem["history"] = mem["history"][-40:]
    mem["facts"].extend(extract_facts(text, reply))
    save_memory(mem)
    return jsonify({"reply": reply, "mode": mode})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

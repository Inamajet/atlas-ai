import os, json, requests
from flask import Flask, request, jsonify, render_template_string
from groq import Groq

app = Flask(__name__)
client = Groq(api_key=os.environ["GROQ_API_KEY"])

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
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
    prompt = f"""Extract any personal facts about the user from this text. Return a JSON array of strings, or [] if none.
Existing facts: {json.dumps(mem['facts'])}
Text: {text}
Reply ONLY with a JSON array."""
    r = client.chat.completions.create(model=ROUTER, messages=[{"role": "user", "content": prompt}], max_tokens=200)
    raw = r.choices[0].message.content.strip()
    try:
        s, e = raw.find("["), raw.rfind("]")
        new_facts = json.loads(raw[s:e+1]) if s != -1 else []
        for f in new_facts:
            if f not in mem["facts"]:
                mem["facts"].append(f)
    except:
        pass

def route(msg):
    r = client.chat.completions.create(model=ROUTER,
        messages=[{"role": "user", "content": f'Classify: "{msg}"\nReply ONLY: "chitchat" or "complex"'}],
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
    opinions = []
    for model, name in COUNCIL:
        try:
            r = client.chat.completions.create(model=model,
                messages=[{"role": "system", "content": f"You are {name}, part of Atlas council. Be concise (2-3 sentences)."},
                          {"role": "user", "content": f"{ctx}\nUser: {msg}"}],
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

HTML = """<!DOCTYPE html><html><head><title>Atlas</title><style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:monospace;height:100vh;display:flex;flex-direction:column}
header{padding:16px 24px;border-bottom:1px solid #222;color:#7c6af7;font-size:18px}
#chat{flex:1;overflow-y:auto;padding:24px;display:flex;flex-direction:column;gap:16px}
.msg{max-width:75%;padding:12px 16px;border-radius:12px;line-height:1.5}
.user{align-self:flex-end;background:#1e1b4b;color:#c7d2fe}
.atlas{align-self:flex-start;background:#111;border:1px solid #333;white-space:pre-wrap}
footer{padding:16px;border-top:1px solid #222;display:flex;gap:8px}
input{flex:1;background:#111;border:1px solid #333;color:#e0e0e0;padding:12px;border-radius:8px;font-family:monospace;font-size:14px}
button{background:#7c6af7;color:#fff;border:none;padding:12px 24px;border-radius:8px;cursor:pointer;font-size:14px}
</style></head><body>
<header>⚡ Atlas — 6-Model Council</header>
<div id="chat"></div>
<footer><input id="inp" placeholder="Ask Atlas anything..." onkeydown="if(event.key==='Enter')send()"/><button onclick="send()">Send</button></footer>
<script>
async function send(){
  const inp=document.getElementById('inp');
  const msg=inp.value.trim();if(!msg)return;
  inp.value='';
  addMsg(msg,'user');
  const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
  const d=await r.json();
  addMsg(d.response,'atlas');
}
function addMsg(text,cls){
  const div=document.createElement('div');
  div.className='msg '+cls;div.textContent=text;
  document.getElementById('chat').appendChild(div);
  div.scrollIntoView();
}
</script></body></html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/chat", methods=["POST"])
def chat():
    msg = request.json.get("message", "")
    mem = load_memory()
    task = route(msg)
    if "complex" in task:
        response = council_answer(msg, mem)
    else:
        response = fast_answer(msg, mem)
    extract_facts(msg + " " + response, mem)
    mem["history"].append({"user": msg, "atlas": response})
    if len(mem["history"]) > 40:
        mem["history"] = mem["history"][-40:]
    save_memory(mem)
    return jsonify({"response": response})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

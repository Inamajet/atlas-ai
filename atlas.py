import os, json
from groq import Groq

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
MEMORY_FILE = "atlas_memory.json"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        return json.load(open(MEMORY_FILE))
    return {"facts": [], "history": []}

def save_memory(mem):
    json.dump(mem, open(MEMORY_FILE, "w"), indent=2)

def extract_facts(user_msg, reply):
    try:
        r = client.chat.completions.create(model=ROUTER, max_tokens=80,
            messages=[{"role":"system","content":'Extract personal facts about the user worth remembering (name, goals, preferences, etc). Return JSON list of strings or []. Example: ["User\'s name is Mani"]'},
                      {"role":"user","content":f"User: {user_msg}\nAtlas: {reply}"}])
        text = r.choices[0].message.content.strip()
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end != -1:
            facts = json.loads(text[start:end+1])
            return facts if isinstance(facts, list) else []
    except:
        pass
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
                messages=[{"role":"system","content":f"You are the {role} in an AI council. Give your best input in 2-3 sentences."},
                          {"role":"user","content":text}])
            opinions.append(f"[{role}]: {r.choices[0].message.content.strip()}")
            print(f"  ✓ {role}")
        except:
            print(f"  ✗ {role} skipped")

    if not opinions:
        return fast_answer(text, mem)

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

if __name__ == "__main__":
    mem = load_memory()
    print(f"Atlas online. {len(mem['facts'])} memories loaded.\n")
    while True:
        t = input("You: ").strip()
        if not t: continue
        if t.lower() in ("quit","exit"): break
        kind = route(t)
        if "chitchat" in kind:
            print("[fast]")
            reply = fast_answer(t, mem)
        else:
            print("[council]")
            reply = council_answer(t, mem)
        print("Atlas:", reply, "\n")
        mem["history"].append({"role":"user","content":t})
        mem["history"].append({"role":"assistant","content":reply})
        mem["history"] = mem["history"][-40:]
        facts = extract_facts(t, reply)
        mem["facts"].extend(facts)
        save_memory(mem)

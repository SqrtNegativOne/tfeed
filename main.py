import os, json, hashlib, sqlite3, requests
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request

HANDLE = os.environ["BSKY_HANDLE"]
APP_PW = os.environ["BSKY_APP_PW"]
PDS = "https://bsky.social"  # change if you're on a different PDS
LLM = "http://localhost:8080/v1/chat/completions"
FEED_HANDLE, FEED_RKEY = "spacecowboy17.bsky.social", "for-you"

# label -> description. Edit freely; the cache keys on a hash of this dict.
RULES = {
    "dunk": "The post mainly exists to mock, dunk on, or express outrage at another person or their post "
    "(often a quote post or screenshot of someone the author disagrees with) and adds little substance of its own.",
    "rage_bait": "Deliberately inflammatory or outrage-farming content aimed at provoking reactions.",
    "engagement_bait": "Vague posts or questions whose main purpose is to farm replies or likes.",
}
HIDE = {"dunk", "rage_bait"}  # labels that get hidden; others are only tagged

RULES_HASH = hashlib.sha1(json.dumps(RULES, sort_keys=True).encode()).hexdigest()[:8]
SCHEMA = {
    "type": "object",
    "properties": {"labels": {"type": "array", "items": {"enum": list(RULES)}}},
    "required": ["labels"],
}

db = sqlite3.connect("cache.db", check_same_thread=False)
db.execute("create table if not exists c (k text primary key, labels text)")

session = {}


def login():
    r = requests.post(
        f"{PDS}/xrpc/com.atproto.server.createSession",
        json={"identifier": HANDLE, "password": APP_PW},
    )
    r.raise_for_status()
    session["jwt"] = r.json()["accessJwt"]


def xrpc(method, **params):
    for attempt in range(2):
        if "jwt" not in session:
            login()
        r = requests.get(
            f"{PDS}/xrpc/{method}",
            params=params,
            headers={"Authorization": f"Bearer {session['jwt']}"},
        )
        if r.status_code in (400, 401) and attempt == 0 and "ExpiredToken" in r.text:
            session.clear()
            continue
        r.raise_for_status()
        return r.json()


feed_did = requests.get(
    "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle",
    params={"handle": FEED_HANDLE},
).json()["did"]
FEED_URI = f"at://{feed_did}/app.bsky.feed.generator/{FEED_RKEY}"


def extract(post):
    """Flatten a postView into text the LLM can judge, plus display fields."""
    text = post["record"].get("text", "")
    parts = [f"POST by @{post['author']['handle']}: {text}"]
    images, quote = [], None

    def handle_embed(e):
        nonlocal quote
        if not e:
            return
        t = e["$type"]
        if t == "app.bsky.embed.images#view":
            for im in e["images"]:
                images.append(im["thumb"])
                parts.append(f"[image, alt text: {im.get('alt') or 'none'}]")
        elif t == "app.bsky.embed.external#view":
            x = e["external"]
            parts.append(
                f"[link card: {x.get('title', '')} - {x.get('description', '')}]"
            )
        elif t == "app.bsky.embed.record#view":
            r = e["record"]
            if r.get("$type") == "app.bsky.embed.record#viewRecord":
                qt = r["value"].get("text", "")
                quote = f"@{r['author']['handle']}: {qt}"
                parts.append(f"[QUOTING @{r['author']['handle']}: {qt}]")
                for sub in r.get("embeds", []):
                    handle_embed(sub)
        elif t == "app.bsky.embed.recordWithMedia#view":
            handle_embed(
                {"$type": "app.bsky.embed.record#view", "record": e["record"]["record"]}
            )
            handle_embed(e["media"])
        elif t == "app.bsky.embed.video#view":
            parts.append(f"[video, alt text: {e.get('alt') or 'none'}]")

    handle_embed(post.get("embed"))
    return "\n".join(parts), text, images, quote


def classify(blob):
    rules = "\n".join(f"- {k}: {v}" for k, v in RULES.items())
    prompt = (
        f"Label this social media post. Apply each label only if it clearly fits.\n"
        f"Labels:\n{rules}\n\n{blob}\n\nReturn JSON with a 'labels' array (empty if none apply)."
    )
    try:
        r = requests.post(
            LLM,
            json={
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "labels", "schema": SCHEMA},
                },
            },
            timeout=120,
        )
        return json.loads(r.json()["choices"][0]["message"]["content"])["labels"]
    except Exception as ex:
        print("classify failed:", ex)
        return None  # fail open, don't cache


app = Flask(__name__)


@app.get("/api/feed")
def api_feed():
    params = {"feed": FEED_URI, "limit": 50}
    if request.args.get("cursor"):
        params["cursor"] = request.args["cursor"]
    data = xrpc("app.bsky.feed.getFeed", **params)

    items = []
    for it in data["feed"]:
        p = it["post"]
        blob, text, images, quote = extract(p)
        key = f"{p['uri']}|{RULES_HASH}"
        row = db.execute("select labels from c where k=?", (key,)).fetchone()
        items.append(
            dict(
                p=p,
                blob=blob,
                text=text,
                images=images,
                quote=quote,
                key=key,
                labels=json.loads(row[0]) if row else None,
            )
        )

    todo = [i for i in items if i["labels"] is None]
    with ThreadPoolExecutor(4) as ex:  # match llama-server -np
        for i, labels in zip(todo, ex.map(lambda i: classify(i["blob"]), todo)):
            if labels is not None:
                db.execute(
                    "insert or replace into c values (?,?)",
                    (i["key"], json.dumps(labels)),
                )
                i["labels"] = labels
    db.commit()

    out = []
    for i in items:
        p, labels = i["p"], i["labels"] or []
        out.append(
            dict(
                url=f"https://bsky.app/profile/{p['author']['handle']}/post/{p['uri'].rsplit('/', 1)[1]}",
                author=p["author"].get("displayName") or p["author"]["handle"],
                handle=p["author"]["handle"],
                text=i["text"],
                images=i["images"],
                quote=i["quote"],
                labels=labels,
                hidden=bool(set(labels) & HIDE),
            )
        )
    return jsonify(posts=out, cursor=data.get("cursor"))


PAGE = """<!doctype html><meta charset=utf-8><title>my feed</title>
<style>
body{font:15px system-ui;max-width:600px;margin:20px auto;padding:0 12px;background:#111;color:#ddd}
.post{border-bottom:1px solid #333;padding:12px 0}.post.hid{opacity:.4}
.h{color:#888;font-size:13px}.q{border-left:3px solid #444;padding-left:8px;margin:6px 0;color:#aaa}
img{max-width:48%;margin:4px 2px;border-radius:6px}a{color:#6af}
.tag{background:#522;color:#fcc;border-radius:4px;padding:0 5px;font-size:12px}
</style>
<label><input type=checkbox id=sh> show hidden</label>
<div id=f></div><button id=m>more</button>
<script>
let cur=null,loading=false;
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load(){
  if(loading)return;loading=true;
  const r=await fetch('/api/feed'+(cur?'?cursor='+encodeURIComponent(cur):''));
  const d=await r.json();cur=d.cursor;
  for(const p of d.posts){
    const e=document.createElement('div');
    e.className='post'+(p.hidden?' hid':'');e.dataset.hidden=p.hidden;
    e.innerHTML=`<div class=h>${esc(p.author)} @${esc(p.handle)} <a href="${p.url}" target=_blank>open</a> `+
      p.labels.map(l=>`<span class=tag>${esc(l)}</span>`).join(' ')+`</div>`+
      `<div style="white-space:pre-wrap">${esc(p.text)}</div>`+
      (p.quote?`<div class=q>${esc(p.quote)}</div>`:'')+
      p.images.map(u=>`<img src="${u}">`).join('');
    document.getElementById('f').appendChild(e);
  }
  apply();loading=false;
}
function apply(){const s=document.getElementById('sh').checked;
  document.querySelectorAll('.post').forEach(e=>e.style.display=(e.dataset.hidden==='true'&&!s)?'none':'');}
document.getElementById('sh').onchange=apply;
document.getElementById('m').onclick=load;
addEventListener('scroll',()=>{if(innerHeight+scrollY>document.body.offsetHeight-800&&cur)load()});
load();
</script>"""


@app.get("/")
def index():
    return PAGE


if __name__ == "__main__":
    app.run(port=5000)

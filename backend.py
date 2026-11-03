"""
Vidhyantra Router - OpenAI-compatible AI gateway powered ONLY by Vidhyantra.
Models:
  - codepro -> Code-X API, upstream "vidhyora-code" (OpenAI-compatible passthrough)
  - sol, terra, ultra, quick, chatgpt56, code, ... -> Get API (translated; any other model ID passes through)
Run: python backend.py   (PORT env, default 5000)
Env: VIDHYANTRA_CODEX_KEY (vdc_...), VIDHYANTRA_GET_KEY (vdk_...)
"""
import os, json, time, secrets, requests
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

CODEX_BASE = "https://www.vidhyantra.online/api/v1/code"
GETAPI_URL = "https://www.vidhyantra.online/api/v1/chat/"
CODEX_KEY = os.environ.get("VIDHYANTRA_CODEX_KEY", "")
GET_KEY = os.environ.get("VIDHYANTRA_GET_KEY", "")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KEYS_FILE = os.path.join(DATA_DIR, "keys.json")
os.makedirs(DATA_DIR, exist_ok=True)

# Supabase persistence (keys survive redeploys); file fallback if DB unreachable
try:
    from db_store import db_load, db_save, db_enabled, _DB_ERROR, _MISSING
    HAS_DB = True
except ImportError:
    HAS_DB = False
KEYS_DOC = "router.keys"

# ---------------- key store ----------------
def load_keys():
    if HAS_DB and db_enabled():
        d = db_load(KEYS_DOC)
        if d is not _DB_ERROR and d is not _MISSING:
            return d if isinstance(d, dict) else {}
    try:
        with open(KEYS_FILE) as f: return json.load(f)
    except Exception: return {}

def save_keys(d):
    try:
        tmp = KEYS_FILE + ".tmp"
        with open(tmp, "w") as f: json.dump(d, f)
        os.replace(tmp, KEYS_FILE)
    except Exception: pass
    if HAS_DB and db_enabled():
        try: db_save(KEYS_DOC, d)
        except Exception: pass

def new_api_key():
    return "vdr_" + secrets.token_urlsafe(24)

def check_key():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "): return None
    key = auth[7:].strip()
    keys = load_keys()
    rec = keys.get(key)
    if not rec: return None
    return {"key": key, **rec}

# Public model name -> (upstream, upstream_model)
MODEL_MAP = {
    "codepro": ("codex", "vidhyora-code"),
}

# ---------------- upstream: Code-X (OpenAI passthrough) ----------------
def call_codex(payload):
    """vidhyora-code: Code-X is already OpenAI-compatible -> direct proxy."""
    if not CODEX_KEY:
        return {"ok": False, "status": 500,
                "error": "server misconfigured: VIDHYANTRA_CODEX_KEY not set"}
    try:
        r = requests.post(CODEX_BASE + "/chat/completions", json=payload,
                          headers={"Authorization": "Bearer " + CODEX_KEY,
                                   "Content-Type": "application/json"},
                          timeout=120)
        try: data = r.json()
        except Exception: data = {"error": r.text[:500]}
        return {"ok": r.status_code < 300, "status": r.status_code, "data": data}
    except Exception as e:
        return {"ok": False, "status": 502, "error": "upstream: " + str(e)}

# ---------------- upstream: Get API (translation) ----------------
def extract_getapi_text(d):
    """Defensive parser - Get API response shape TBD (see TODO)."""
    try:
        if d is None: return ""
        if isinstance(d, str): return d
        text = ""
        if isinstance(d.get("choices"), list) and d["choices"]:
            m = d["choices"][0].get("message") or {}
            if isinstance(m.get("content"), str) and m["content"].strip():
                text = m["content"]
        if not text:
            for k in ["reply", "response", "message", "answer", "text",
                      "content", "output", "result"]:
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    text = v
                    break
        # Get API double-escapes: literal \n instead of real newlines
        if text and "\\n" in text:
            text = text.replace("\\n", "\n").replace("\\t", "\t")
            text = text.replace('\\"', '"').replace("\\'", "'")
        return text
    except Exception: return ""

def call_get_api(model, message):
    # TODO: confirm exact Get API response shape with a live test,
    # then tighten extract_getapi_text(). Current parser is defensive.
    if not GET_KEY:
        return {"ok": False, "status": 500,
                "error": "server misconfigured: VIDHYANTRA_GET_KEY not set"}
    # Get API limit: each message must be 8000 chars or fewer.
    # Truncate from the front (keep the most recent = user's actual question).
    if len(message) > 7500:
        message = "...[earlier content truncated]...\n" + message[-7500:]
    try:
        r = requests.post(GETAPI_URL,
                          json={"model": model, "message": message},
                          headers={"Authorization": "Bearer " + GET_KEY,
                                   "Content-Type": "application/json"},
                          timeout=300)
        try: data = r.json()
        except Exception: data = {"_raw": r.text[:2000]}
        if r.status_code >= 300:
            err = data.get("error") if isinstance(data, dict) else r.text[:300]
            return {"ok": False, "status": r.status_code,
                    "error": "get-api: " + str(err)}
        text = extract_getapi_text(data)
        if not text:
            return {"ok": False, "status": 502,
                    "error": "get-api: could not parse response",
                    "raw": data}
        return {"ok": True, "status": 200, "data": {
            "id": "chatcmpl-vdr-" + secrets.token_hex(8),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0},
        }}
    except Exception as e:
        return {"ok": False, "status": 502, "error": "upstream: " + str(e)}

def openai_messages_to_text(messages):
    """Fold OpenAI messages into the single string Get API wants."""
    lines = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):  # content blocks
            content = " ".join(
                b.get("text", "") for b in content if isinstance(b, dict))
        lines.append(f"{role}: {content}")
    return "\n".join(lines[-20:])

# ---------------- routes ----------------
@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

@app.route("/api/chat", methods=["OPTIONS"])
@app.route("/v1/chat/completions", methods=["OPTIONS"])
def options_ok():
    return ("", 204)

@app.route("/")
def index():
    return Response(
        "<h2>Vidhyantra Router</h2><p>OpenAI-compatible API, powered only by "
        "Vidhyantra.</p><p><a href='/get-key'>Get a free API key</a></p>"
        "<p><code>POST /v1/chat/completions</code> · "
        "<code>GET /v1/models</code></p>",
        mimetype="text/html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "vidhyantra-router",
                    "time": int(time.time())})

@app.route("/v1/models")
def models():
    return jsonify({"object": "list", "data": [
        {"id": "codepro", "object": "model", "owned_by": "vidhyantra-router"},
        {"id": "sol", "object": "model", "owned_by": "vidhyantra"},
        {"id": "terra", "object": "model", "owned_by": "vidhyantra"},
        {"id": "ultra", "object": "model", "owned_by": "vidhyantra"},
        {"id": "quick", "object": "model", "owned_by": "vidhyantra"},
        {"id": "chatgpt56", "object": "model", "owned_by": "vidhyantra"},
        {"id": "code", "object": "model", "owned_by": "vidhyantra"},
    ]})

@app.route("/get-key", methods=["GET", "POST"])
def get_key():
    if request.method == "POST":
        keys = load_keys()
        k = new_api_key()
        keys[k] = {"created": int(time.time()), "credits": 100000}
        save_keys(keys)
        return jsonify({"ok": True, "api_key": k,
                        "note": "Free key. Keep it private."})
    return Response(
        "<h2>Get a free API key</h2>"
        "<form method='post'><button type='submit'>Generate key</button></form>"
        "<p>Use it as <code>Authorization: Bearer &lt;key&gt;</code> on "
        "<code>/v1/chat/completions</code>.</p>",
        mimetype="text/html")

def sse_stream_response(model, text):
    """Emit OpenAI-style SSE stream for clients that request stream:true."""
    def gen():
        cid = "chatcmpl-vdr-" + secrets.token_hex(8)
        ts = int(time.time())
        # Chunk by words for a natural typing effect
        words = text.split(" ")
        for i, w in enumerate(words):
            piece = w + (" " if i < len(words) - 1 else "")
            yield "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk",
                "created": ts, "model": model,
                "choices": [{"index": 0, "delta": {"content": piece},
                             "finish_reason": None}],
            }) + "\n\n"
        yield "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk",
            "created": ts, "model": model,
            "choices": [{"index": 0, "delta": {},
                         "finish_reason": "stop"}],
        }) + "\n\n"
        yield "data: [DONE]\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})

@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    rec = check_key()
    if not rec:
        return jsonify({"error": {"message": "Invalid API key",
                                  "code": "invalid_api_key"}}), 401
    try: body = request.get_json(force=True)
    except Exception:
        return jsonify({"error": {"message": "Invalid JSON",
                                  "code": "bad_request"}}), 400
    model = (body.get("model") or "").strip()
    messages = body.get("messages") or []
    if not model or not messages:
        return jsonify({"error": {"message": "model and messages required",
                                  "code": "bad_request"}}), 400

    mapped = MODEL_MAP.get(model)
    if mapped and mapped[0] == "codex":
        up = call_codex({"model": mapped[1], "messages": messages,
                         "max_tokens": body.get("max_tokens", 4096),
                         "temperature": body.get("temperature", 0.7)})
        # echo the public name back, like OpenAI does
        if up.get("ok") and isinstance(up.get("data"), dict):
            up["data"]["model"] = model
    else:
        # Any other model ID goes to the Get API as-is
        up = call_get_api(model, openai_messages_to_text(messages))

    if not up["ok"]:
        code = up.get("status", 502)
        return jsonify({"error": {"message": up.get("error", "upstream error"),
                                  "code": "upstream_error"}}), code
    # touch last-used
    try:
        keys = load_keys(); r = keys.get(rec["key"])
        if r: r["last_used"] = int(time.time()); save_keys(keys)
    except Exception: pass
    # Streaming mode (CodeGPT, Cline, etc. request stream:true)
    if body.get("stream"):
        data = up["data"] if isinstance(up.get("data"), dict) else {}
        text = ""
        try:
            ch = data.get("choices") or []
            if ch:
                msg = ch[0].get("message") or {}
                text = msg.get("content") or ""
        except Exception:
            text = ""
        if not text:
            text = "(empty response)"
        return sse_stream_response(model, text)
    return jsonify(up["data"])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))


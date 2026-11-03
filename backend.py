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

# ==================== SHOPIFY APP (FamBridge UPI) ====================
# Mounted at /shopify/* — merged from fambridge-shopify-app
try:
    from shopify_client import ShopifyClient, verify_webhook_hmac
    from fambridge_client import FamBridgeClient, verify_webhook as verify_fb_webhook
    HAS_SHOPIFY = True
except ImportError:
    HAS_SHOPIFY = False

SHOPIFY_API_KEY = os.environ.get("SHOPIFY_API_KEY", "")
SHOPIFY_API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "")
SHOPIFY_APP_URL = os.environ.get("APP_URL", "").rstrip("/") or "https://codepro-ai.onrender.com/shopify"
FB_API_KEY = os.environ.get("FAMBRIDGE_API_KEY", "")
FB_BASE = os.environ.get("FAMBRIDGE_BASE", "https://fambridge-gateway.onrender.com")
SHOPIFY_SCOPES = "read_orders,write_orders"

SHOPS_DOC = "shopify.shops"
ORDERS_DOC = "shopify.orders"

def _shop_load(doc):
    if HAS_DB and db_enabled():
        d = db_load(doc)
        if d is not _DB_ERROR and d is not _MISSING:
            return d if isinstance(d, dict) else {}
    try:
        with open(os.path.join(DATA_DIR, doc.replace(".", "_") + ".json")) as f:
            return json.load(f)
    except Exception:
        return {}

def _shop_save(doc, data):
    try:
        with open(os.path.join(DATA_DIR, doc.replace(".", "_") + ".json"), "w") as f:
            json.dump(data, f)
    except Exception:
        pass
    if HAS_DB and db_enabled():
        try:
            db_save(doc, data)
        except Exception:
            pass

def _fb_for(shop):
    shops = _shop_load(SHOPS_DOC)
    key = shops.get(shop, {}).get("fambridge_key") or FB_API_KEY
    return FamBridgeClient(key, FB_BASE)

def _sc_for(shop):
    shops = _shop_load(SHOPS_DOC)
    rec = shops.get(shop)
    if not rec:
        return None
    return ShopifyClient(shop, rec["token"])

@app.route("/shopify/")
def shopify_index():
    return Response(
        "<h2>FamBridge UPI for Shopify</h2>"
        "<p>Auto-verified UPI payments for your Shopify store.</p>"
        "<p>Install: <code>/shopify/auth?shop=YOURSTORE.myshopify.com</code></p>",
        mimetype="text/html")

@app.route("/shopify/auth")
def shopify_auth():
    import urllib.parse as up
    shop = (request.args.get("shop") or "").strip().lower()
    if not shop.endswith(".myshopify.com"):
        return "Invalid shop domain. Use ?shop=yourstore.myshopify.com", 400
    params = up.urlencode({
        "client_id": SHOPIFY_API_KEY,
        "scope": SHOPIFY_SCOPES,
        "redirect_uri": SHOPIFY_APP_URL + "/auth/callback",
        "state": secrets.token_hex(8),
    })
    return redirect(f"https://{shop}/admin/oauth/authorize?{params}")

@app.route("/shopify/auth/callback")
def shopify_callback():
    shop = (request.args.get("shop") or "").strip().lower()
    code = request.args.get("code") or ""
    if not shop or not code:
        return "Missing shop/code", 400
    r = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        json={"client_id": SHOPIFY_API_KEY,
              "client_secret": SHOPIFY_API_SECRET,
              "code": code},
        timeout=20)
    if r.status_code != 200:
        return f"OAuth failed: {r.text[:200]}", 400
    token = r.json()["access_token"]
    shops = _shop_load(SHOPS_DOC)
    shops[shop] = {"token": token, "installed_at": int(time.time())}
    _shop_save(SHOPS_DOC, shops)
    try:
        sc = ShopifyClient(shop, token)
        requests.post(
            sc.base + "/webhooks.json",
            headers=sc._headers(),
            json={"webhook": {
                "topic": "orders/create",
                "address": SHOPIFY_APP_URL + "/webhooks/orders/create",
                "format": "json"}},
            timeout=20)
    except Exception as e:
        print("webhook register failed:", e, flush=True)
    return (f"Installed on {shop}!<br><br>"
            "Next: Shopify Admin -> Settings -> Payments -> "
            "add a <b>Manual payment method</b> named exactly "
            "<b>UPI (FamBridge)</b>.")

@app.route("/shopify/webhooks/orders/create", methods=["POST"])
def shopify_orders_create():
    raw = request.get_data()
    if not verify_webhook_hmac(raw, request.headers.get(
            "X-Shopify-Hmac-Sha256"), SHOPIFY_API_SECRET):
        return "bad hmac", 401
    shop = request.headers.get("X-Shopify-Shop-Domain", "").lower()
    order = request.get_json(force=True) or {}
    gateways = order.get("payment_gateway_names", [])
    if isinstance(gateways, list):
        gw = gateways
    else:
        gw = [g.get("gateway", "") for g in gateways] if isinstance(gateways, list) else []
    if not any("fambridge" in str(g).lower() for g in gw):
        return jsonify({"ok": True, "skipped": "not fambridge"})
    order_id = str(order["id"])
    total = float(order.get("total_price", "0") or 0)
    currency = order.get("currency", "INR")
    note = f"shopify_{shop}_{order_id}"
    try:
        fb = _fb_for(shop)
        fb_order = fb.create_order(total, note)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    pay_token = secrets.token_urlsafe(16)
    orders = _shop_load(ORDERS_DOC)
    orders[pay_token] = {
        "shop": shop,
        "shopify_order_id": order_id,
        "fambridge_order_id": fb_order["order_id"],
        "amount": total,
        "currency": currency,
        "qr_base64": fb_order.get("qr_base64", ""),
        "upi_link": fb_order.get("upi_link", ""),
        "status": "pending",
        "created": int(time.time()),
    }
    _shop_save(ORDERS_DOC, orders)
    pay_url = f"{SHOPIFY_APP_URL}/pay/{pay_token}"
    try:
        sc = _sc_for(shop)
        if sc:
            sc.add_order_note(order_id,
                              f"FamBridge UPI: customer pay link: {pay_url}")
    except Exception as e:
        print("add note failed:", e, flush=True)
    return jsonify({"ok": True, "pay_url": pay_url})

@app.route("/shopify/webhooks/fambridge", methods=["POST"])
def shopify_fb_hook():
    raw = request.get_data()
    try:
        data = json.loads(raw)
    except Exception:
        return "bad json", 400
    note = data.get("note", "")
    parts = note.split("_")
    if len(parts) < 3 or parts[0] != "shopify":
        return jsonify({"ok": True, "skipped": "note not shopify"})
    order_id = parts[-1]
    shop = "_".join(parts[1:-1])
    shops = _shop_load(SHOPS_DOC)
    fb_key = shops.get(shop, {}).get("fambridge_key") or FB_API_KEY
    if not verify_fb_webhook(raw, request.headers.get("X-Signature"), fb_key):
        return "bad hmac", 401
    orders = _shop_load(ORDERS_DOC)
    pay_token = None
    for tok, rec in orders.items():
        if rec.get("shop") == shop and rec.get("shopify_order_id") == order_id:
            pay_token = tok
            break
    if pay_token:
        orders[pay_token]["status"] = "paid"
        orders[pay_token]["paid_at"] = int(time.time())
        orders[pay_token]["utr"] = data.get("utr", "")
        _shop_save(ORDERS_DOC, orders)
    try:
        sc = _sc_for(shop)
        if not sc:
            return jsonify({"ok": False, "error": "shop not installed"}), 400
        order = sc.get_order(order_id)
        amount = float(order.get("total_price", "0") or 0)
        sc.mark_paid(order_id, amount, order.get("currency", "INR"))
        sc.add_order_note(order_id,
            f"FamBridge UPI payment verified "
            f"(UTR: {data.get('utr', 'n/a')}, "
            f"Rs.{data.get('amount', '?')})")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "order_id": order_id})

@app.route("/shopify/pay/<token>")
def shopify_pay_page(token):
    from flask import render_template
    orders = _shop_load(ORDERS_DOC)
    rec = orders.get(token)
    if not rec:
        return "Invalid or expired payment link.", 404
    return render_template(
        "qr_page.html",
        qr_base64=rec.get("qr_base64", ""),
        upi_link=rec.get("upi_link", ""),
        amount=rec.get("amount", 0),
        currency=rec.get("currency", "INR"),
        order_id=rec.get("shopify_order_id", ""),
        status=rec.get("status", "pending"),
        token=token)

@app.route("/shopify/api/pay_status/<token>")
def shopify_pay_status(token):
    orders = _shop_load(ORDERS_DOC)
    rec = orders.get(token)
    if not rec:
        return jsonify({"status": "unknown"}), 404
    return jsonify({"status": rec.get("status", "pending")})

import os, sqlite3, secrets, statistics, math, requests, stripe, datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash

# -------------------- Config --------------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(16))

# Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")  # create a recurring price in Stripe dashboard
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")

# eBay
EBAY_APP_ID = os.getenv("EBAY_APP_ID")  # required for searches

# Basic constants
DB = "app.db"
DEFAULT_CURRENCY = "GBP"

# -------------------- DB Helpers --------------------
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            stripe_customer_id TEXT,
            plan TEXT,
            api_key TEXT,
            created_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            created_at TEXT
        )""")

# -------------------- Auth Helpers --------------------
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return row

def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper

# -------------------- eBay helpers --------------------
DISALLOWED_DEFAULT = ["psa","bgs","cgc","graded","proxy","reprint","replica","lot","bundle","job lot"]

def contains_disallowed(title, disallowed):
    t = (title or "").lower()
    return any(term in t for term in disallowed)

def trimmed_mean(prices):
    if not prices:
        return None
    prices_sorted = sorted(prices)
    n = len(prices_sorted)
    if n < 6:
        return statistics.median(prices_sorted)
    trim = max(1, math.floor(n*0.15))
    core = prices_sorted[trim:n-trim] if (n-2*trim)>=3 else prices_sorted
    return sum(core)/len(core) if core else statistics.median(prices_sorted)

def get_completed_prices(app_id, keywords, global_id, category_id, currency, sold_fetch, disallowed):
    url = "https://svcs.ebay.com/services/search/FindingService/v1"
    params = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.13.0",
        "SECURITY-APPNAME": app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "true",
        "keywords": keywords,
        "GLOBAL-ID": global_id,
        "categoryId": category_id,
        "itemFilter(0).name": "SoldItemsOnly",
        "itemFilter(0).value": "true",
        "paginationInput.entriesPerPage": str(sold_fetch),
        "sortOrder": "EndTimeSoonest",
    }
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    items = data.get("findCompletedItemsResponse", [{}])[0].get("searchResult", [{}])[0].get("item", []) or []
    prices = []
    for it in items:
        title = it.get("title", [""])[0]
        if contains_disallowed(title, disallowed):
            continue
        selling = it.get("sellingStatus", [{}])[0]
        if selling.get("sellingState", [""])[0] != "EndedWithSales":
            continue
        price_obj = selling.get("currentPrice", [{}])[0]
        if price_obj.get("@currencyId") != currency:
            continue
        try:
            price = float(price_obj.get("__value__", "0"))
        except:
            continue
        if price > 0:
            prices.append(price)
    return prices

def get_active_under(app_id, keywords, global_id, category_id, currency, active_fetch, max_price, disallowed):
    url = "https://svcs.ebay.com/services/search/FindingService/v1"
    params = {
        "OPERATION-NAME": "findItemsAdvanced",
        "SERVICE-VERSION": "1.13.0",
        "SECURITY-APPNAME": app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "true",
        "keywords": keywords,
        "GLOBAL-ID": global_id,
        "categoryId": category_id,
        "itemFilter(0).name": "MaxPrice",
        "itemFilter(0).value": f"{max_price:.2f}",
        "itemFilter(0).paramName": "Currency",
        "itemFilter(0).paramValue": currency,
        "paginationInput.entriesPerPage": str(active_fetch),
        "sortOrder": "EndTimeSoonest",
    }
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    items = data.get("findItemsAdvancedResponse", [{}])[0].get("searchResult", [{}])[0].get("item", []) or []
    results = []
    for it in items:
        title = it.get("title", [""])[0]
        if contains_disallowed(title, disallowed):
            continue
        price_obj = it.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
        if price_obj.get("@currencyId") != currency:
            continue
        try:
            price = float(price_obj.get("__value__", "0"))
        except:
            continue
        if price <= 0:
            continue
        url_item = it.get("viewItemURL", [""])[0]
        item_id = it.get("itemId", [""])[0]
        results.append({"id": item_id, "title": title, "price": price, "url": url_item})
    return results

# -------------------- Routes --------------------
@app.route("/")
def index():
    return render_template("index.html", pk=STRIPE_PUBLISHABLE_KEY)

@app.route("/pricing")
def pricing():
    return render_template("pricing.html", pk=STRIPE_PUBLISHABLE_KEY, price_id=STRIPE_PRICE_ID)

@app.route("/create-checkout", methods=["POST"])
def create_checkout():
    # Create a Stripe Checkout Session
    if not (stripe.api_key and STRIPE_PRICE_ID and STRIPE_PUBLISHABLE_KEY):
        return "Stripe not configured. Add STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_PRICE_ID.", 400
    domain = request.host_url.strip("/")
    session_stripe = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{domain}{url_for('welcome')}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{domain}{url_for('pricing')}",
        automatic_tax={"enabled": True},
    )
    return redirect(session_stripe.url, code=303)

@app.route("/welcome")
def welcome():
    # Post-checkout: create user, set session
    session_id = request.args.get("session_id")
    if not session_id:
        return redirect(url_for("pricing"))
    try:
        sess = stripe.checkout.Session.retrieve(session_id, expand=["customer"])
        customer = sess.get("customer")
        email = None
        if isinstance(customer, dict):
            email = customer.get("email")
            customer_id = customer.get("id")
        else:
            # If only id is returned, fetch the customer
            cust = stripe.Customer.retrieve(customer)
            email = cust.get("email")
            customer_id = cust.get("id")
        if not email:
            email = f"user-{customer_id}@example.local"
        # upsert user
        with db() as c:
            row = c.execute("SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()
            if row:
                uid = row["id"]
            else:
                api_key = secrets.token_urlsafe(24)
                c.execute("INSERT INTO users(email, stripe_customer_id, plan, api_key, created_at) VALUES(?,?,?,?,?)",
                    (email, customer_id, "pro", api_key, datetime.datetime.utcnow().isoformat()))
                uid = c.lastrowid
        session["uid"] = uid
        return render_template("welcome.html")
    except Exception as e:
        return f"Error verifying Stripe session: {e}", 400

@app.route("/dashboard", methods=["GET","POST"])
@login_required
def dashboard():
    # Defaults for the form
    defaults = {
        "keywords": "vintage pokemon cards holo",
        "global_id": "EBAY-GB",
        "category_id": "183454",
        "currency": DEFAULT_CURRENCY,
        "sold_fetch": 50,
        "min_sold_count": 12,
        "discount_percent": 40,
        "active_fetch": 50,
        "exclude_terms": ",".join(DISALLOWED_DEFAULT),
    }
    results = None
    if request.method == "POST":
        if not EBAY_APP_ID:
            flash("Missing EBAY_APP_ID in Replit Secrets.", "error")
            return render_template("dashboard.html", state=defaults, results=None)
        form = request.form
        try:
            keywords = form.get("keywords", defaults["keywords"]).strip()
            global_id = form.get("global_id", defaults["global_id"]).strip()
            category_id = form.get("category_id", defaults["category_id"]).strip()
            currency = form.get("currency", defaults["currency"]).strip()
            sold_fetch = int(form.get("sold_fetch", defaults["sold_fetch"]))
            min_sold = int(form.get("min_sold_count", defaults["min_sold_count"]))
            discount_percent = float(form.get("discount_percent", defaults["discount_percent"]))
            active_fetch = int(form.get("active_fetch", defaults["active_fetch"]))
            exclude_terms = form.get("exclude_terms", defaults["exclude_terms"])
            disallowed = [t.strip().lower() for t in exclude_terms.split(",") if t.strip()]

            if discount_percent <= 0 or discount_percent >= 100:
                raise ValueError("Discount % must be between 1 and 99.")

            sold_prices = get_completed_prices(EBAY_APP_ID, keywords, global_id, category_id, currency, sold_fetch, disallowed)
            avg_price = trimmed_mean(sold_prices) if sold_prices else None

            if not avg_price or len(sold_prices) < min_sold:
                results = {"note": f"Not enough sold data (got {len(sold_prices)}).", "sold_count": len(sold_prices), "avg_price": avg_price, "threshold": None, "hits": []}
            else:
                threshold = round(avg_price * (1 - discount_percent/100.0), 2)
                hits = get_active_under(EBAY_APP_ID, keywords, global_id, category_id, currency, active_fetch, threshold, disallowed)
                results = {"sold_count": len(sold_prices), "avg_price": round(avg_price,2), "threshold": threshold, "hits": hits}
            state = dict(defaults, **{
                "keywords": keywords, "global_id": global_id, "category_id": category_id, "currency": currency,
                "sold_fetch": sold_fetch, "min_sold_count": min_sold, "discount_percent": int(discount_percent),
                "active_fetch": active_fetch, "exclude_terms": exclude_terms
            })
            return render_template("dashboard.html", state=state, results=results)
        except Exception as e:
            flash(str(e), "error")
    return render_template("dashboard.html", state=defaults, results=results)

@app.route("/api/search", methods=["POST"])
def api_search():
    # API: requires X-API-Key header from paying users
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return jsonify({"ok": False, "error": "Missing X-API-Key"}), 401
    with db() as c:
        user = c.execute("SELECT * FROM users WHERE api_key=?", (api_key,)).fetchone()
        if not user:
            return jsonify({"ok": False, "error": "Invalid API key"}), 403

    data = request.get_json(force=True) or {}
    # defaults
    keywords = data.get("keywords", "vintage pokemon cards holo")
    global_id = data.get("global_id", "EBAY-GB")
    category_id = data.get("category_id", "183454")
    currency = data.get("currency", DEFAULT_CURRENCY)
    sold_fetch = int(data.get("sold_fetch", 50))
    min_sold = int(data.get("min_sold_count", 12))
    discount_percent = float(data.get("discount_percent", 40))
    active_fetch = int(data.get("active_fetch", 50))
    disallowed = [t.strip().lower() for t in data.get("exclude_terms","psa,bgs,cgc,graded,proxy,reprint,replica,lot,bundle,job lot").split(",") if t.strip()]

    if not EBAY_APP_ID:
        return jsonify({"ok": False, "error": "Server missing EBAY_APP_ID"}), 500
    try:
        sold_prices = get_completed_prices(EBAY_APP_ID, keywords, global_id, category_id, currency, sold_fetch, disallowed)
        avg_price = trimmed_mean(sold_prices) if sold_prices else None
        if not avg_price or len(sold_prices) < min_sold:
            return jsonify({"ok": True, "note": f"Not enough sold data ({len(sold_prices)}).", "sold_count": len(sold_prices), "avg_price": avg_price, "threshold": None, "hits": []})
        threshold = round(avg_price * (1 - discount_percent/100.0), 2)
        hits = get_active_under(EBAY_APP_ID, keywords, global_id, category_id, currency, active_fetch, threshold, disallowed)
        return jsonify({"ok": True, "sold_count": len(sold_prices), "avg_price": round(avg_price,2), "threshold": threshold, "hits": hits})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# -------------------- Startup --------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080, debug=True)

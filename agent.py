# agent-adk-demo/agent.py
import os
import requests
import re
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS

# --- Config ---
BACKEND = os.environ.get("BACKEND_URL", "http://localhost:3000").rstrip("/")
REQUEST_TIMEOUT = 8  # seconds

app = Flask(__name__)
CORS(app)

# ---------- Helper wrappers ----------
def backend_get(path, params=None):
    """
    GET to backend service. path must start with / (e.g. '/api/invoices').
    Returns parsed JSON or raises requests.HTTPError.
    """
    url = BACKEND + path
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise RuntimeError(f"backend_get error: {e}")

def backend_post(path, payload):
    """
    POST to backend service. path must start with / (e.g. '/api/mandates').
    Returns parsed JSON or raises requests.HTTPError.
    """
    url = BACKEND + path
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise RuntimeError(f"backend_post error: {e}")

# ---------- Response helpers ----------
def assistant_ok(data=None, speak=None, cards=None, message=None):
    return jsonify({
        "success": True,
        "message": message or "ok",
        "data": data or {},
        "speak": speak or "",
        "cards": cards or []
    })

def assistant_err(msg, code=400):
    return jsonify({"success": False, "message": msg}), code

# ---------- VoiceRef matching ----------
def find_invoice_by_spoken_local(ref, invoices_list):
    """
    Try to resolve a natural language reference (voiceRef) to an invoice
    within invoices_list (list of invoice dicts). Returns invoice dict or None.
    """
    if not ref:
        return None
    s = ref.lower().strip()

    # 1) inv-125 or invoice 125
    m = re.search(r'inv[-\s]?(\d{2,6})', s)
    if m:
        sid = m.group(1)
        for inv in invoices_list:
            if str(inv.get('shortId')) == sid or (inv.get('invoiceId') and sid in inv.get('invoiceId')):
                return inv

    # 2) digits (pay 125)
    m2 = re.search(r'(\d{2,6})', s)
    if m2:
        sid = m2.group(1)
        for inv in invoices_list:
            if str(inv.get('shortId')) == sid:
                return inv

    # 3) vendor/label contains
    for inv in invoices_list:
        if s in (inv.get('vendor','').lower()) or s in (inv.get('label','').lower()) or s in (inv.get('description','').lower()):
            return inv

    # 4) keywords: most recent, oldest, largest, smallest
    not_paid = [i for i in invoices_list if not i.get('paid')]
    if not not_paid:
        return None
    if 'last' in s or 'latest' in s or 'most recent' in s:
        return sorted(not_paid, key=lambda x: datetime.fromisoformat(x['dueDate']), reverse=True)[0]
    if 'oldest' in s or 'earliest' in s:
        return sorted(not_paid, key=lambda x: datetime.fromisoformat(x['dueDate']))[0]
    if 'largest' in s or 'biggest' in s or 'highest' in s:
        return sorted(not_paid, key=lambda x: x['amount'], reverse=True)[0]
    if 'smallest' in s or 'small' in s:
        return sorted(not_paid, key=lambda x: x['amount'])[0]

    return None

# ---------- Routes ----------
@app.route('/')
def home():
    return jsonify({"success": True, "message": "Agent service running", "backend": BACKEND})

@app.route('/health')
def health():
    return jsonify({"status": "ok", "backend": BACKEND})

@app.route('/agent/invoices/<user_id>', methods=['GET'])
def agent_list_invoices(user_id):
    """
    GET /agent/invoices/<user_id>?category=&q=
    Returns a speakable summary and cards with invoice metadata.
    """
    q = request.args.get('q','').strip()
    category = request.args.get('category','').strip()
    params = {}
    if q: params['q'] = q
    if category: params['category'] = category
    if user_id: params['userId'] = user_id

    try:
        res = backend_get('/api/invoices', params=params)
        invoices = res.get('data', {}).get('invoices', [])
        if not invoices:
            speak = "You have no matching invoices."
        else:
            top = invoices[0]
            speak = f"You have {len(invoices)} invoice(s). First: {top.get('label')} for ${top.get('amount')} due {top.get('dueDate')}."
        cards = [{"title": i.get('label'), "subtitle": f"#{i.get('shortId')} • ${i.get('amount')} • due {i.get('dueDate')}", "metadata": i} for i in invoices]
        return assistant_ok(data={"raw": invoices}, speak=speak, cards=cards, message=res.get('message'))
    except Exception as e:
        return assistant_err(f"Failed to fetch invoices: {str(e)}", 502)

@app.route('/agent/search', methods=['GET'])
def agent_search():
    """
    GET /agent/search?q=...&userId=...&category=...
    """
    q = request.args.get('q','').strip()
    if not q:
        return assistant_err("query 'q' required", 400)
    user = request.args.get('userId','').strip()
    category = request.args.get('category','').strip()
    try:
        params = {'q': q}
        if user: params['userId'] = user
        if category: params['category'] = category
        res = backend_get('/api/invoices', params=params)
        invoices = res.get('data', {}).get('invoices', [])
        speak = f"Found {len(invoices)} invoices matching {q}."
        cards = [{"title": inv.get('label'), "subtitle": f"#{inv.get('shortId')} • ${inv.get('amount')}", "metadata": inv} for inv in invoices]
        return assistant_ok(data={"invoices": invoices}, speak=speak, cards=cards, message=res.get('message'))
    except Exception as e:
        return assistant_err(f"Search failed: {e}", 502)

@app.route('/agent/pay', methods=['POST'])
def agent_pay_invoice():
    """
    POST /agent/pay
    body: { userId, invoiceId (optional), voiceRef (optional) }
    """
    body = request.json or {}
    user = body.get('userId')
    invoiceId = body.get('invoiceId')
    voiceRef = body.get('voiceRef')

    if not user:
        return assistant_err('userId required', 400)

    # Fetch user's invoices (scope matching to user)
    try:
        invs_res = backend_get('/api/invoices', params={'userId': user})
        invoice_list = invs_res.get('data', {}).get('invoices', [])
    except Exception as e:
        return assistant_err(f"Could not fetch invoices: {e}", 502)

    # Resolve voiceRef -> invoice if provided
    if voiceRef and not invoiceId:
        inv = find_invoice_by_spoken_local(voiceRef, invoice_list)
        if not inv:
            return assistant_err("I couldn't find an invoice matching that description", 404)
        invoiceId = inv.get('invoiceId')

    if not invoiceId:
        return assistant_err('invoiceId or voiceRef required', 400)

    # Lookup invoice details
    try:
        inv_res = backend_get(f'/api/invoices/{invoiceId}')
        invoice = inv_res.get('data', {}).get('invoice')
        if not invoice:
            return assistant_err('Invoice not found', 404)
    except Exception as e:
        return assistant_err(f"Invoice lookup failed: {e}", 404)

    # Create cart mandate
    cart_payload = {
        "userId": user,
        "type": "Cart",
        "action": f"Pay invoice {invoiceId}",
        "amountLimit": invoice.get('amount'),
        "invoiceId": invoiceId
    }
    try:
        mandate_res = backend_post('/api/mandates', cart_payload)
        mandate_data = mandate_res.get('data') or mandate_res
        signedMandate = mandate_data.get('signedMandate')
        mandateId = mandate_data.get('mandateId')
    except Exception as e:
        return assistant_err(f"Failed to create cart mandate: {e}", 502)

    # Execute payment
    pay_payload = {
        "mandateId": mandateId,
        "signedMandate": signedMandate,
        "invoiceId": invoiceId,
        "paymentMethod": "agent-demo-card"
    }
    try:
        pay_res = backend_post('/api/pay', pay_payload)
        pay_data = pay_res.get('data') or pay_res
        receipt = (pay_data.get('receipt') if isinstance(pay_data, dict) else pay_data)
        speak = f"Payment successful. Invoice {invoiceId} paid for ${receipt.get('amount')}."
        card = {"title": f"Receipt {receipt.get('receiptId')}", "subtitle": f"${receipt.get('amount')} • {invoiceId}", "metadata": receipt}
        return assistant_ok(data={"cart": mandate_data, "payment": pay_data}, speak=speak, cards=[card], message=pay_res.get('message'))
    except Exception as e:
        return assistant_err(f"Payment failed: {e}", 502)

@app.route('/agent/intent', methods=['POST'])
def agent_create_intent():
    """
    A simple endpoint to create an 'Intent' mandate (delegated permission).
    Body: { userId, action, amountLimit (optional) }
    """
    body = request.json or {}
    user = body.get('userId')
    action = body.get('action', 'autopay')
    amountLimit = body.get('amountLimit')

    if not user:
        return assistant_err('userId required', 400)

    payload = {"userId": user, "type": "Intent", "action": action, "amountLimit": amountLimit or 0, "invoiceId": None}
    try:
        res = backend_post('/api/mandates', payload)
        return assistant_ok(data=res.get('data') if res else {}, speak=f"Intent created for {action}", message="Intent created")
    except Exception as e:
        return assistant_err(f"Failed to create intent: {e}", 502)

# ---------- Run ----------
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    # Log some startup info
    print(f"Starting agent on 0.0.0.0:{port} — BACKEND={BACKEND}")
    app.run(host="0.0.0.0", port=port, debug=True)

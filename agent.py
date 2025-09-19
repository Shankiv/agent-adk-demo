# agent-adk-demo/agent.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import os, requests, re
from datetime import datetime

app = Flask(__name__)
CORS(app)

BACKEND = os.environ.get('BACKEND_URL', 'http://localhost:3000')

# ---------- Helper wrappers ----------
def backend_get(path, params=None):
    r = requests.get(BACKEND + path, params=params)
    r.raise_for_status()
    return r.json()

def backend_post(path, payload):
    r = requests.post(BACKEND + path, json=payload)
    r.raise_for_status()
    return r.json()

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
    if not ref: return None
    s = ref.lower().strip()
    m = re.search(r'inv[-\s]?(\d{2,6})', s)
    if m:
        sid = m.group(1)
        for inv in invoices_list:
            if inv.get('shortId') == sid or sid in inv.get('invoiceId',''):
                return inv
    m2 = re.search(r'(\d{2,6})', s)
    if m2:
        sid = m2.group(1)
        for inv in invoices_list:
            if inv.get('shortId') == sid:
                return inv
    for inv in invoices_list:
        if s in inv.get('vendor','').lower() or s in inv.get('label','').lower() or s in inv.get('description','').lower():
            return inv
    not_paid = [i for i in invoices_list if not i.get('paid')]
    if 'last' in s or 'latest' in s or 'most recent' in s:
        return sorted(not_paid, key=lambda x: datetime.fromisoformat(x['dueDate']), reverse=True)[0] if not_paid else None
    if 'oldest' in s:
        return sorted(not_paid, key=lambda x: datetime.fromisoformat(x['dueDate']))[0] if not_paid else None
    if 'largest' in s or 'biggest' in s:
        return sorted(not_paid, key=lambda x: x['amount'], reverse=True)[0] if not_paid else None
    if 'smallest' in s:
        return sorted(not_paid, key=lambda x: x['amount'])[0] if not_paid else None
    return None

# ---------- Routes ----------
@app.route('/')
def home():
    return "Agent service running."

@app.route('/agent/invoices/<user_id>', methods=['GET'])
def agent_list_invoices(user_id):
    q = request.args.get('q','').strip()
    category = request.args.get('category','').strip()
    params = {}
    if q: params['q']=q
    if category: params['category']=category
    if user_id: params['userId']=user_id
    try:
        res = backend_get('/api/invoices', params=params)
        invoices = res['data'].get('invoices', [])
        if not invoices:
            speak = "You have no matching invoices."
        else:
            top = invoices[0]
            speak = f"You have {len(invoices)} invoices. First: {top.get('label')} for ${top.get('amount')} due {top.get('dueDate')}."
        cards = [{"title": i['label'], "subtitle": f"#{i['shortId']} • ${i['amount']} • due {i['dueDate']}", "metadata": i} for i in invoices]
        return assistant_ok(data={"raw": invoices}, speak=speak, cards=cards, message=res.get('message'))
    except Exception as e:
        return assistant_err(f"Failed to fetch invoices: {str(e)}", 502)

@app.route('/agent/search', methods=['GET'])
def agent_search():
    q = request.args.get('q','').strip()
    user = request.args.get('userId','').strip()
    category = request.args.get('category','').strip()
    if not q:
        return assistant_err("query 'q' required", 400)
    try:
        params = {'q': q}
        if user: params['userId'] = user
        if category: params['category'] = category
        res = backend_get('/api/invoices', params=params)
        invoices = res['data'].get('invoices', [])
        speak = f"Found {len(invoices)} invoices matching {q}."
        cards = [{"title": inv['label'], "subtitle": f"#{inv['shortId']} • ${inv['amount']}", "metadata": inv} for inv in invoices]
        return assistant_ok(data={"invoices": invoices}, speak=speak, cards=cards, message=res.get('message'))
    except Exception as e:
        return assistant_err(str(e), 502)

@app.route('/agent/pay', methods=['POST'])
def agent_pay_invoice():
    body = request.json or {}
    user = body.get('userId'); invoiceId = body.get('invoiceId'); voiceRef = body.get('voiceRef')
    if not user: return assistant_err('userId required', 400)

    try:
        invs_res = backend_get('/api/invoices', params={'userId': user})
        invoice_list = invs_res['data'].get('invoices', [])
    except Exception as e:
        return assistant_err(f"Could not fetch invoices: {e}", 502)

    if voiceRef and not invoiceId:
        inv = find_invoice_by_spoken_local(voiceRef, invoice_list)
        if not inv: return assistant_err("No invoice matched that description", 404)
        invoiceId = inv['invoiceId']

    if not invoiceId:
        return assistant_err('invoiceId or voiceRef required', 400)

    try:
        inv_res = backend_get(f'/api/invoices/{invoiceId}')
        invoice = inv_res['data']['invoice']
    except Exception as e:
        return assistant_err(f"Invoice lookup failed: {e}", 404)

    cart_payload = {"userId": user, "type":"Cart","action":f"Pay invoice {invoiceId}","amountLimit": invoice['amount'],"invoiceId": invoiceId}
    try:
        mandate_res = backend_post('/api/mandates', cart_payload)
        signedMandate = mandate_res['data']['signedMandate']
        mandateId = mandate_res['data']['mandateId']
    except Exception as e:
        return assistant_err(f"Mandate creation failed: {e}", 502)

    pay_payload = {"mandateId": mandateId,"signedMandate": signedMandate,"invoiceId": invoiceId,"paymentMethod":"agent-demo-card"}
    try:
        pay_res = backend_post('/api/pay', pay_payload)
        receipt = pay_res['data']['receipt']
        speak = f"Payment successful. Invoice {invoiceId} paid for ${receipt.get('amount')}."
        card = {"title": f"Receipt {receipt.get('receiptId')}", "subtitle": f"${receipt.get('amount')} • {invoiceId}", "metadata": receipt}
        return assistant_ok(data={"cart": mandate_res['data'], "payment": pay_res['data']}, speak=speak, cards=[card], message=pay_res.get('message'))
    except Exception as e:
        return assistant_err(f"Payment failed: {e}", 502)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)

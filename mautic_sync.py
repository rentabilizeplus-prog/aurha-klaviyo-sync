#!/usr/bin/env python3
"""
Sync going-forward Shopify -> Mautic (a cada 2h, GitHub Actions).
- Recalcula LTV real + RFM dos clientes com pedidos alterados.
- Faz upsert no Mautic (poucos contatos por rodada -> rapido mesmo no server lento).
- Para LEAD NOVA (nao existia no Mautic), marca welcome_eligible=sim (entra no funil).
- Quando vira Cliente, customer_type muda -> sai do segmento de Leads (welcome encerra).

Credenciais via env (GitHub Secrets):
  SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, MAUTIC_BASE, MAUTIC_USER, MAUTIC_PASS
"""
import os, json, time, base64, urllib.request, urllib.error, urllib.parse, datetime, collections

SHOP = os.environ.get("SHOP_DOMAIN", "artesanatoholistico.myshopify.com")
CID = os.environ["SHOPIFY_CLIENT_ID"]; SECRET = os.environ["SHOPIFY_CLIENT_SECRET"]
MBASE = os.environ["MAUTIC_BASE"]
MU = os.environ["MAUTIC_USER"]; MP = os.environ["MAUTIC_PASS"]
LOOKBACK_H = int(os.environ.get("LOOKBACK_HOURS", "3"))
API_VER = "2025-01"
PAID = {"PAID","PARTIALLY_PAID","PARTIALLY_REFUNDED","REFUNDED"}
UNPAID = {"PENDING","AUTHORIZED","VOIDED","EXPIRED"}
NOW = datetime.datetime.now(datetime.timezone.utc)
MAUTH = base64.b64encode(f"{MU}:{MP}".encode()).decode()

def http(url, data=None, headers=None, method=None, timeout=90):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    for a in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (429,500,502,503): time.sleep(3+a*2); continue
            return e.code, json.loads((e.read().decode() or "{}") if True else "{}")
    return 0, {}

# Shopify
def shopify_token():
    body = json.dumps({"client_id":CID,"client_secret":SECRET,"grant_type":"client_credentials"}).encode()
    st,d = http(f"https://{SHOP}/admin/oauth/access_token", data=body, headers={"Content-Type":"application/json"})
    return d["access_token"]
TOKEN = shopify_token()
GQL = f"https://{SHOP}/admin/api/{API_VER}/graphql.json"
GHDR = {"X-Shopify-Access-Token":TOKEN,"Content-Type":"application/json"}
def gql(q, v=None):
    for a in range(8):
        st,d = http(GQL, data=json.dumps({"query":q,"variables":v or {}}).encode(), headers=GHDR)
        if d.get("errors") and "throttled" in json.dumps(d["errors"]).lower(): time.sleep(2+a); continue
        return d
    raise RuntimeError("throttled")
ORDERS_Q = """query($q:String!,$c:String){ orders(first:250, after:$c, query:$q){
  pageInfo{hasNextPage endCursor}
  edges{ node{ createdAt displayFinancialStatus netPaymentSet{presentmentMoney{amount}} customer{email} email } } } }"""
def iter_orders(query):
    cur=None
    while True:
        d=gql(ORDERS_Q,{"q":query,"c":cur}); conn=d["data"]["orders"]
        for e in conn["edges"]: yield e["node"]
        if conn["pageInfo"]["hasNextPage"]: cur=conn["pageInfo"]["endCursor"]
        else: break

# RFM
def r_s(d): return 1 if d is None else 5 if d<=30 else 4 if d<=90 else 3 if d<=180 else 2 if d<=365 else 1
def f_s(n): return 5 if n>=6 else 4 if n>=4 else 3 if n==3 else 2 if n==2 else 1
def m_s(v): return 5 if v>=800 else 4 if v>=400 else 3 if v>=200 else 2 if v>=100 else 1
def seg(R,F,M,paid):
    if paid==0: return "Lead (nunca pagou)"
    if F>=4 and R>=4: return "Campeoes"
    if F>=4 and R<=2 and M>=4: return "Nao Posso Perder"
    if F>=4: return "Clientes Fieis"
    if R>=4 and F>=2: return "Potenciais Fieis"
    if R==5 and F==1: return "Novos Clientes"
    if R==4 and F==1: return "Promissores"
    if R==3: return "Precisam Atencao"
    if R<=2 and F>=3: return "Em Risco"
    if R<=2: return "Hibernando"
    return "Perdidos"
def aggregate(email):
    ltv=0.0; paid=unpaid=0; last=first=None
    for o in iter_orders(f"email:{email}"):
        st=o.get("displayFinancialStatus") or ""
        try: net=float((o.get("netPaymentSet") or {}).get("presentmentMoney",{}).get("amount") or 0)
        except: net=0.0
        ltv+=net
        try: dt=datetime.datetime.fromisoformat(o["createdAt"].replace("Z","+00:00"))
        except: dt=None
        if st in PAID:
            paid+=1
            if dt and (last is None or dt>last): last=dt
            if dt and (first is None or dt<first): first=dt
        elif st in UNPAID: unpaid+=1
    days=None if last is None else (NOW-last).days
    R,F,M=r_s(days),f_s(paid),m_s(ltv)
    p={"customer_type":"Cliente" if paid>=1 else "Lead","ltv_real":round(ltv,2),
       "pedidos_pagos":paid,"pedidos_nao_pagos":unpaid,"r_score":R,"f_score":F,"m_score":M,
       "rfm_score":f"{R}{F}{M}","rfm_segment":seg(R,F,M,paid)}
    if days is not None: p["recencia_dias"]=days
    if last: p["ultimo_pedido_pago"]=last.date().isoformat()
    if first: p["primeiro_pedido_pago"]=first.date().isoformat()
    return p

# Mautic
MHDR = {"Authorization":f"Basic {MAUTH}","Content-Type":"application/json"}
def mautic_find(email):
    st,d = http(f"{MBASE}/api/contacts?search=email:{urllib.parse.quote(email)}&limit=1", headers=MHDR)
    cs = d.get("contacts",{})
    if cs:
        cid=list(cs.keys())[0]; return cid
    return None
import urllib.parse
def mautic_upsert(email, props, new_lead):
    if new_lead and props.get("customer_type")=="Lead":
        props=dict(props); props["welcome_eligible"]="sim"
    payload=dict(props); payload["email"]=email
    st,d = http(f"{MBASE}/api/contacts/new", data=json.dumps(payload).encode(), headers=MHDR, method="POST")
    return st

def main():
    cutoff=(NOW-datetime.timedelta(hours=LOOKBACK_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{NOW.isoformat()}] orders updated since {cutoff}", flush=True)
    emails=set()
    for o in iter_orders(f"updated_at:>={cutoff}"):
        em=(o.get("customer") or {}).get("email") or o.get("email")
        if em: emails.add(em.strip().lower())
    print(f"clientes afetados: {len(emails)}", flush=True)
    done=0
    for em in sorted(emails):
        existed = mautic_find(em) is not None
        props = aggregate(em)
        mautic_upsert(em, props, new_lead=not existed)
        done+=1
    print(f"OK: {done} contatos sincronizados no Mautic.", flush=True)

if __name__=="__main__":
    main()

#!/usr/bin/env python3
"""
Sincroniza Shopify -> Klaviyo: recalcula LTV real (líquido pago), RFM e
classificação Cliente/Lead dos clientes com pedidos alterados desde o último
intervalo, e atualiza os perfis no Klaviyo (sem mexer em consentimento).

Roda na nuvem (GitHub Actions). Não depende do Mac.
Credenciais via variáveis de ambiente (GitHub Secrets).
"""
import os, json, time, urllib.request, urllib.error, datetime, collections

SHOP   = os.environ.get("SHOP_DOMAIN", "artesanatoholistico.myshopify.com")
CID    = os.environ["SHOPIFY_CLIENT_ID"]
SECRET = os.environ["SHOPIFY_CLIENT_SECRET"]
KKEY   = os.environ["KLAVIYO_API_KEY"]
LIST_ID= os.environ.get("KLAVIYO_LIST_ID", "Y3w4H4")
LOOKBACK_H = int(os.environ.get("LOOKBACK_HOURS", "24"))
API_VER = "2025-01"

PAID   = {"PAID","PARTIALLY_PAID","PARTIALLY_REFUNDED","REFUNDED"}
UNPAID = {"PENDING","AUTHORIZED","VOIDED","EXPIRED"}
NOW = datetime.datetime.now(datetime.timezone.utc)

def http(url, data=None, headers=None, method=None, timeout=90):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            code = e.code; body = e.read().decode() or "{}"
            if code in (429, 502, 503):
                time.sleep(3 + attempt*2); continue
            return code, (json.loads(body) if body.strip().startswith("{") else {"raw": body[:200]})
    return 0, {"error": "max retries"}

# ---- Shopify ----
def shopify_token():
    body = json.dumps({"client_id":CID,"client_secret":SECRET,"grant_type":"client_credentials"}).encode()
    st, d = http(f"https://{SHOP}/admin/oauth/access_token", data=body,
                 headers={"Content-Type":"application/json"})
    if "access_token" not in d:
        raise SystemExit(f"Falha ao obter token Shopify: {st} {d}")
    return d["access_token"]

TOKEN = shopify_token()
GQL_URL = f"https://{SHOP}/admin/api/{API_VER}/graphql.json"
GQL_HDR = {"X-Shopify-Access-Token":TOKEN, "Content-Type":"application/json"}

def gql(query, variables=None):
    for attempt in range(8):
        body = json.dumps({"query":query,"variables":variables or {}}).encode()
        st, d = http(GQL_URL, data=body, headers=GQL_HDR)
        errs = d.get("errors")
        if errs and any("throttled" in json.dumps(e).lower() for e in (errs if isinstance(errs,list) else [errs])):
            time.sleep(2 + attempt); continue
        return d
    raise RuntimeError("GraphQL throttled demais")

ORDERS_Q = """query($q:String!,$c:String){
  orders(first:250, after:$c, query:$q) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      createdAt displayFinancialStatus
      netPaymentSet { presentmentMoney { amount } }
      customer { email } email
    } }
  }
}"""

def iter_orders(query):
    cursor=None
    while True:
        d = gql(ORDERS_Q, {"q":query, "c":cursor})
        conn = d["data"]["orders"]
        for e in conn["edges"]:
            yield e["node"]
        if conn["pageInfo"]["hasNextPage"]:
            cursor = conn["pageInfo"]["endCursor"]
        else:
            break

# ---- RFM ----
def r_score(days):
    if days is None: return 1
    return 5 if days<=30 else 4 if days<=90 else 3 if days<=180 else 2 if days<=365 else 1
def f_score(n):
    return 5 if n>=6 else 4 if n>=4 else 3 if n==3 else 2 if n==2 else 1
def m_score(v):
    return 5 if v>=800 else 4 if v>=400 else 3 if v>=200 else 2 if v>=100 else 1
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
        try: d=datetime.datetime.fromisoformat(o["createdAt"].replace("Z","+00:00"))
        except: d=None
        if st in PAID:
            paid+=1
            if d and (last is None or d>last): last=d
            if d and (first is None or d<first): first=d
        elif st in UNPAID:
            unpaid+=1
    days=None if last is None else (NOW-last).days
    R,F,M=r_score(days),f_score(paid),m_score(ltv)
    props={"customer_type":"Cliente" if paid>=1 else "Lead",
           "ltv_real":round(ltv,2),"pedidos_pagos":paid,"pedidos_nao_pagos":unpaid,
           "r_score":R,"f_score":F,"m_score":M,"rfm_score":f"{R}{F}{M}",
           "rfm_segment":seg(R,F,M,paid)}
    if days is not None: props["recencia_dias"]=days
    if last: props["ultimo_pedido_pago"]=last.date().isoformat()
    if first: props["primeiro_pedido_pago"]=first.date().isoformat()
    return props

# ---- Klaviyo ----
KL_HDR={"Authorization":f"Klaviyo-API-Key {KKEY}","revision":"2024-10-15",
        "content-type":"application/json","accept":"application/json"}

def klaviyo_bulk(profiles):
    for i in range(0,len(profiles),9000):
        chunk=profiles[i:i+9000]
        payload={"data":{"type":"profile-bulk-import-job",
            "attributes":{"profiles":{"data":chunk}},
            "relationships":{"lists":{"data":[{"type":"list","id":LIST_ID}]}}}}
        st,d=http("https://a.klaviyo.com/api/profile-bulk-import-jobs/",
                  data=json.dumps(payload).encode(), headers=KL_HDR, method="POST")
        print(f"  Klaviyo bulk {len(chunk)} perfis -> HTTP {st}", flush=True)

# ---- main ----
def main():
    cutoff=(NOW-datetime.timedelta(hours=LOOKBACK_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{NOW.isoformat()}] Buscando pedidos alterados desde {cutoff} (lookback {LOOKBACK_H}h)", flush=True)
    emails=set()
    for o in iter_orders(f"updated_at:>={cutoff}"):
        em=(o.get("customer") or {}).get("email") or o.get("email")
        if em: emails.add(em.strip().lower())
    print(f"Clientes afetados: {len(emails)}", flush=True)
    if not emails:
        print("Nada a atualizar."); return
    profiles=[]
    for n,em in enumerate(sorted(emails),1):
        props=aggregate(em)
        profiles.append({"type":"profile","attributes":{"email":em,"properties":props}})
        if n%50==0: print(f"  recalculados {n}/{len(emails)}", flush=True)
    klaviyo_bulk(profiles)
    print(f"OK: {len(profiles)} perfis atualizados no Klaviyo.", flush=True)

if __name__=="__main__":
    main()

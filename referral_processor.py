#!/usr/bin/env python3
"""
Programa de Embaixadoras Aurha - processador de cashback (roda a cada 2h, GitHub Actions).
Para cada embaixadora (contato Mautic com referral_code), procura no SHOPIFY os pedidos PAGOS
que usaram o cupom dela, credita 15% do total pago (COM frete) na carteira Yampi da embaixadora
(SEM validade) e envia o e-mail de recompensa. Fonte de verdade = Shopify (pago), nao a Yampi
(que conta voided como pago). Exclui pedidos de troca (cupom troca*) e auto-uso. Dedup por ID de
PEDIDO (credita a cada compra) via referral_state.json.

Env (GitHub Secrets): MAUTIC_BASE/USER/PASS, YAMPI_ALIAS/TOKEN/SECRET, SHOPIFY_CLIENT_ID/SECRET.
Opcionais: REWARD_PCT(15), EMBAIXADORES_SEGMENT(embaixadores), REWARD_EMAIL_ID(158),
CASHBACK_DAYS(vazio=sem validade), DRY_RUN(true|false), TEST_CODE, SHOP_DOMAIN.
"""
import os, json, time, base64, urllib.request, urllib.error, urllib.parse, datetime

MB=os.environ["MAUTIC_BASE"].rstrip("/"); MU=os.environ["MAUTIC_USER"]; MP=os.environ["MAUTIC_PASS"]
AL=os.environ["YAMPI_ALIAS"]; YT=os.environ["YAMPI_TOKEN"]; YS=os.environ["YAMPI_SECRET"]
CID=os.environ["SHOPIFY_CLIENT_ID"]; CSECRET=os.environ["SHOPIFY_CLIENT_SECRET"]
SHOP=os.environ.get("SHOP_DOMAIN","artesanatoholistico.myshopify.com"); API_VER="2025-01"
REWARD_PCT=float(os.environ.get("REWARD_PCT","15"))
CBDAYS=os.environ.get("CASHBACK_DAYS","").strip()   # vazio => SEM validade
SEG=os.environ.get("EMBAIXADORES_SEGMENT","embaixadores")
DRY=os.environ.get("DRY_RUN","false").lower()=="true"
TEST_CODE=os.environ.get("TEST_CODE"); STATE="referral_state.json"
REWARD_EMAIL_ID=os.environ.get("REWARD_EMAIL_ID","158")
PAID={"PAID","PARTIALLY_PAID","PARTIALLY_REFUNDED","REFUNDED"}
MA=base64.b64encode(f"{MU}:{MP}".encode()).decode()

def http(url,data=None,headers=None,method=None,timeout=90):
    req=urllib.request.Request(url,data=data,headers=headers or {},method=method)
    for a in range(5):
        try:
            with urllib.request.urlopen(req,timeout=timeout) as r:
                return r.status,json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (429,500,502,503): time.sleep(3+a*2); continue
            return e.code,json.loads(e.read().decode() or "{}")
        except urllib.error.URLError:
            time.sleep(2+a); continue
    return 0,{}

def m(path,data=None,method="GET"):
    st,d=http(f"{MB}{path}",data=(json.dumps(data).encode() if data is not None else None),
        headers={"Authorization":f"Basic {MA}","Content-Type":"application/json","Accept":"application/json"},method=method)
    return d

# ---- Shopify (fonte de verdade de pago + valor com frete) ----
def shopify_token():
    st,d=http(f"https://{SHOP}/admin/oauth/access_token",
        data=json.dumps({"client_id":CID,"client_secret":CSECRET,"grant_type":"client_credentials"}).encode(),
        headers={"Content-Type":"application/json"})
    return d["access_token"]
TOKEN=shopify_token()
GQL=f"https://{SHOP}/admin/api/{API_VER}/graphql.json"
GHDR={"X-Shopify-Access-Token":TOKEN,"Content-Type":"application/json"}
def gql(q,v=None):
    for a in range(8):
        st,d=http(GQL,data=json.dumps({"query":q,"variables":v or {}}).encode(),headers=GHDR)
        if d.get("errors") and "throttled" in json.dumps(d["errors"]).lower(): time.sleep(2+a); continue
        return d
    raise RuntimeError("throttled")
ORDERS_Q="""query($q:String!,$c:String){ orders(first:250, after:$c, query:$q){
  pageInfo{hasNextPage endCursor}
  edges{ node{ id name displayFinancialStatus
    totalPriceSet{shopMoney{amount}} discountCodes customer{email} email } } } }"""
def orders_with_coupon(code):
    """Pedidos Shopify que usaram o cupom `code` (busca por discount_code)."""
    cur=None; out=[]
    q=f"discount_code:{code}"
    while True:
        d=gql(ORDERS_Q,{"q":q,"c":cur}); conn=(d.get("data") or {}).get("orders")
        if not conn: break
        for e in conn["edges"]: out.append(e["node"])
        if conn["pageInfo"]["hasNextPage"]: cur=conn["pageInfo"]["endCursor"]
        else: break
    return out

# ---- Yampi (carteira/cashback) ----
def ypost(path,body):
    try:
        st,d=http(f"https://api.dooki.com.br/v2/{AL}{path}",data=json.dumps(body).encode(),
            headers={"User-Token":YT,"User-Secret-Key":YS,"Content-Type":"application/json","Accept":"application/json"},method="POST")
        return st,d
    except Exception as e:
        print(f"[emb] aviso: Yampi POST {path} falhou ({e})",flush=True); return 0,{}
def credit(referrer_email,amount):
    body={"customer_email":referrer_email,"transaction_type":"credit","amount":round(amount,2),
          "description":"Cashback Programa de Embaixadoras Aurha"}
    if CBDAYS:  # se definido, aplica validade; vazio => SEM validade
        body["expires_at"]=(datetime.date.today()+datetime.timedelta(days=int(CBDAYS))).strftime("%Y-%m-%d")
    return ypost("/pricing/wallet/transaction",body)

# ---- monta lista de embaixadoras (mautic_id, email, code) ----
state=json.load(open(STATE)) if os.path.exists(STATE) else {}
refs=[]
if TEST_CODE:
    refs=[(0,"teste@teste.com",TEST_CODE)]
else:
    start=0
    while True:
        d=m(f"/api/contacts?search="+urllib.parse.quote(f"segment:{SEG}")+f"&limit=200&start={start}")
        cs=list((d.get("contacts") or {}).values())
        if not cs: break
        for c in cs:
            code=c["fields"]["all"].get("referral_code")
            if code: refs.append((c["id"],(c["fields"]["all"].get("email") or "").lower(),code))
        start+=len(cs)
        if start>=int(d.get("total") or 0): break

print(f"[emb] embaixadoras={len(refs)} pct={REWARD_PCT} sem_validade={not CBDAYS} dry={DRY}",flush=True)
rewarded=0; credited_total=0.0
for cid,remail,code in refs:
    try: orders=orders_with_coupon(code)
    except Exception as e:
        print(f"[emb] aviso: Shopify falhou p/ {code} ({e})",flush=True); continue
    done=set(state.get(code,[]))
    for o in orders:
        oid=o.get("id"); status=o.get("displayFinancialStatus")
        codes=[str(x).lower() for x in (o.get("discountCodes") or [])]
        buyer=((o.get("customer") or {}).get("email") or o.get("email") or "").lower()
        if oid in done: continue                                  # dedupe por PEDIDO
        if status not in PAID: continue                           # so pago (Shopify)
        if any(c.startswith("troca") for c in codes): continue    # exclui troca
        if buyer and buyer==remail: continue                      # exclui auto-uso
        amt=float(((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount") or 0)  # COM frete
        reward=round(amt*REWARD_PCT/100.0,2)
        if reward<=0: continue
        if DRY:
            print(f"    [DRY] {code}: creditaria R${reward:.2f} a {remail} (pedido {o.get('name')} R${amt:.2f})",flush=True)
            continue
        st,_=credit(remail,reward)
        if st in (200,201):
            rewarded+=1; credited_total+=reward
            state.setdefault(code,[]).append(oid)
            if cid:
                cont=m(f"/api/contacts/{cid}").get("contact",{}).get("fields",{}).get("all",{})
                cnt=int(float(cont.get("referral_count") or 0))+1
                earn=round(float(cont.get("referral_earned") or 0)+reward,2)
                m(f"/api/contacts/{cid}/edit",{"referral_count":cnt,"referral_earned":earn},"PATCH")
                if REWARD_EMAIL_ID:
                    try: m(f"/api/emails/{REWARD_EMAIL_ID}/contact/{cid}/send",{},"POST")
                    except Exception as e: print("    aviso: falha no e-mail de recompensa",e,flush=True)
            print(f"    creditado R${reward:.2f} a {remail} (pedido {o.get('name')})",flush=True)
        else:
            print(f"    aviso: credito falhou p/ {remail} pedido {o.get('name')} (HTTP {st})",flush=True)
if not DRY:
    json.dump(state,open(STATE,"w"))
print(f"[emb] pedidos creditados={rewarded} total_cashback=R${credited_total:.2f}",flush=True)

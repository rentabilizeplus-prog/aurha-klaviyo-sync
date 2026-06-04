#!/usr/bin/env python3
"""
Indique-e-ganhe Aurha (nativo). Roda diario (GitHub Actions).
Para cada embaixador (contato Mautic com referral_code), le quem usou o cupom dele na Yampi,
valida que a amiga e' cliente nova/pagante e credita cashback ao embaixador. Dedup via referral_state.json.
Env: MAUTIC_BASE/USER/PASS, YAMPI_ALIAS/TOKEN/SECRET. Opcionais: REWARD_AMOUNT(20), FRIEND_DISCOUNT(15),
CASHBACK_DAYS(90), EMBAIXADORES_SEGMENT(embaixadores), REWARD_EMAIL_ID, DRY_RUN, TEST_CODE.
"""
import os, json, base64, urllib.request, urllib.error, urllib.parse, datetime
MB=os.environ["MAUTIC_BASE"].rstrip("/"); MU=os.environ["MAUTIC_USER"]; MP=os.environ["MAUTIC_PASS"]
AL=os.environ["YAMPI_ALIAS"]; YT=os.environ["YAMPI_TOKEN"]; YS=os.environ["YAMPI_SECRET"]
REWARD=float(os.environ.get("REWARD_AMOUNT","20")); CBDAYS=int(os.environ.get("CASHBACK_DAYS","90"))
SEG=os.environ.get("EMBAIXADORES_SEGMENT","embaixadores"); DRY=os.environ.get("DRY_RUN","false").lower()=="true"
TEST_CODE=os.environ.get("TEST_CODE"); STATE="referral_state.json"; REWARD_EMAIL_ID=os.environ.get("REWARD_EMAIL_ID")
MA=base64.b64encode(f"{MU}:{MP}".encode()).decode()
def m(path,data=None,method="GET"):
    req=urllib.request.Request(f"{MB}{path}",data=(json.dumps(data).encode() if data is not None else None),
        headers={"Authorization":f"Basic {MA}","Content-Type":"application/json","Accept":"application/json"},method=method)
    with urllib.request.urlopen(req,timeout=90) as r: return json.load(r)
def y(path):
    req=urllib.request.Request(f"https://api.dooki.com.br/v2/{AL}{path}",headers={"User-Token":YT,"User-Secret-Key":YS,"Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=90) as r: return json.load(r)
def ypost(path,body):
    req=urllib.request.Request(f"https://api.dooki.com.br/v2/{AL}{path}",data=json.dumps(body).encode(),
        headers={"User-Token":YT,"User-Secret-Key":YS,"Content-Type":"application/json","Accept":"application/json"},method="POST")
    with urllib.request.urlopen(req,timeout=90) as r: return r.status,json.load(r)

def coupon_id(code):
    d=y(f"/pricing/promocodes?q={urllib.parse.quote(code)}&limit=20")
    for c in d.get("data",[]):
        if str(c.get("code","")).upper()==code.upper(): return c.get("id")
    return None
def coupon_users(cid):
    d=y(f"/pricing/promocodes/{cid}/customers")
    return [ (u.get("email") or "").lower() for u in d.get("data",[]) if u.get("email") ]
def friend_is_paying(email):
    d=m(f"/api/contacts?search="+urllib.parse.quote(f"email:{email}")+"&limit=1")
    cs=list(d.get("contacts",{}).values())
    if not cs: return False
    return (cs[0]["fields"]["all"].get("customer_type")=="Cliente")
def reward(referrer_email):
    body={"customer_email":referrer_email,"transaction_type":"credit","amount":REWARD,
          "expires_at":(datetime.date.today()+datetime.timedelta(days=CBDAYS)).strftime("%Y-%m-%d"),
          "description":"Recompensa indique e ganhe Aurha"}
    return ypost("/pricing/wallet/transaction",body)

state=json.load(open(STATE)) if os.path.exists(STATE) else {}
# lista de embaixadores: (mautic_id, email, code)
refs=[]
if TEST_CODE:
    refs=[(0,"teste@teste.com",TEST_CODE)]
else:
    start=0
    while True:
        d=m(f"/api/contacts?search="+urllib.parse.quote(f"segment:{SEG}")+f"&limit=200&start={start}")
        cs=list(d.get("contacts",{}).values())
        if not cs: break
        for c in cs:
            code=c["fields"]["all"].get("referral_code")
            if code: refs.append((c["id"], (c["fields"]["all"].get("email") or "").lower(), code))
        start+=len(cs)
        if start>=int(d.get("total") or 0): break
print(f"[referral] embaixadores={len(refs)} dry={DRY}",flush=True)
rewarded=0
for cid,remail,code in refs:
    yid=coupon_id(code)
    if not yid: continue
    users=coupon_users(yid)
    already=set(state.get(code,[]))
    new=[u for u in users if u not in already and u!=remail]
    valid=[u for u in new if friend_is_paying(u)]
    if not valid: continue
    print(f"[referral] {code} (referrer {remail}): {len(valid)} nova(s) indicacao(oes) valida(s)",flush=True)
    for friend in valid:
        if DRY:
            print(f"    [DRY] creditaria R${REWARD:.0f} a {remail} (amiga {friend})",flush=True)
        else:
            st,_=reward(remail); 
            if st==200:
                rewarded+=1
                state.setdefault(code,[]).append(friend)
                # incrementa contadores no Mautic
                if cid:
                    cont=m(f"/api/contacts/{cid}")["contact"]["fields"]["all"]
                    cnt=int(float(cont.get("referral_count") or 0))+1; earn=float(cont.get("referral_earned") or 0)+REWARD
                    m(f"/api/contacts/{cid}/edit",{"referral_count":cnt,"referral_earned":earn},"PATCH")
                    if REWARD_EMAIL_ID:
                        try: m(f"/api/emails/{REWARD_EMAIL_ID}/contact/{cid}/send",{},"POST")
                        except Exception as e: print("    aviso: falha ao enviar e-mail de recompensa",e,flush=True)
                print(f"    creditado R${REWARD:.0f} a {remail} (amiga {friend})",flush=True)
        # marca processado mesmo em dry pra nao recontar? nao: dry nao altera state
        if DRY: state.setdefault(code,[])  # noop
if not DRY:
    json.dump(state,open(STATE,"w"))
print(f"[referral] recompensas creditadas={rewarded}",flush=True)

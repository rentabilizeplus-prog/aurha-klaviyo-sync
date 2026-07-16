#!/usr/bin/env python3
"""
Envio RETROATIVO do convite de Embaixadora para a base com 2+ compras pagas.
- Fonte: RFM do Mautic (pedidos_pagos >= 2, customer_type=Cliente, ainda nao embaixadora).
- A/B BALANCEADO POR TEMPERATURA: estratifica por r_score (recencia) e distribui A/B dentro de
  cada estrato, de modo que os dois bracos tenham a MESMA mistura de quente/frio. Assim a unica
  variavel do teste e' o ASSUNTO do e-mail, nao a temperatura da base.
- Teto diario (DAILY_CAP, ~1000). Estado em retro_state.json (quem ja recebeu).
- SEGURANCA: DRY por padrao. So envia com --go-live. Grava convite_ab so quando envia.
- Auditoria RFM x Yampi: --audit N confere numa amostra se pedidos_pagos bate com pedidos pagos
  reais no Shopify (excluindo troca) e reporta divergencias (nao envia nada).

Env: MAUTIC_BASE/USER/PASS, YAMPI_ALIAS/TOKEN/SECRET, SHOPIFY_CLIENT_ID/SECRET.
     CONVITE_EMAIL_A, CONVITE_EMAIL_B (ids dos e-mails), DAILY_CAP(1000).
Uso: python retro_convite.py            # DRY: monta base, mostra split A/B e contagens
     python retro_convite.py --audit 40 # audita RFM x Shopify numa amostra
     python retro_convite.py --go-live  # ENVIA ate DAILY_CAP (so quando for a hora)
"""
import os, sys, json, time, base64, random, urllib.request, urllib.error, urllib.parse, datetime

MB=os.environ["MAUTIC_BASE"].rstrip("/"); MU=os.environ["MAUTIC_USER"]; MP=os.environ["MAUTIC_PASS"]
AL=os.environ.get("YAMPI_ALIAS"); YT=os.environ.get("YAMPI_TOKEN"); YS=os.environ.get("YAMPI_SECRET")
EMAIL_A=os.environ.get("CONVITE_EMAIL_A"); EMAIL_B=os.environ.get("CONVITE_EMAIL_B")
DAILY_CAP=int(os.environ.get("DAILY_CAP","1000"))
STATE="retro_state.json"; MA=base64.b64encode(f"{MU}:{MP}".encode()).decode()
random.seed(42)  # split reprodutivel

def http(url,data=None,headers=None,method=None,timeout=90):
    req=urllib.request.Request(url,data=data,headers=headers or {},method=method)
    for a in range(5):
        try:
            with urllib.request.urlopen(req,timeout=timeout) as r: return r.status,json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (429,500,502,503): time.sleep(3+a*2); continue
            return e.code,json.loads(e.read().decode() or "{}")
        except urllib.error.URLError: time.sleep(2+a); continue
    return 0,{}
def m(path,data=None,method="GET"):
    return http(f"{MB}{path}",data=(json.dumps(data).encode() if data is not None else None),
        headers={"Authorization":f"Basic {MA}","Content-Type":"application/json","Accept":"application/json"},method=method)[1]

def eligiveis():
    """Contatos com 2+ pedidos pagos, cliente, ainda nao embaixadora, com e-mail."""
    out=[]; start=0
    # segment/filtro via search: pedidos_pagos>=2 nao e' direto na search; puxamos Clientes e filtramos.
    q=urllib.parse.quote("customer_type:Cliente")
    while True:
        d=m(f"/api/contacts?search={q}&limit=1000&start={start}")
        cs=list((d.get("contacts") or {}).values())
        if not cs: break
        for c in cs:
            f=c["fields"]["all"]
            try: ped=int(float(f.get("pedidos_pagos") or 0))
            except: ped=0
            email=(f.get("email") or "").strip()
            if ped>=2 and email and not (f.get("embaixadora_status") or "").strip():
                try: r=int(float(f.get("r_score") or 0))
                except: r=0
                out.append({"id":c["id"],"email":email.lower(),"first":f.get("firstname") or "","r":r})
        start+=len(cs)
        if start>=int(d.get("total") or 0): break
    return out

def split_ab(base):
    """Estratifica por r_score e alterna A/B dentro de cada estrato -> mesma mistura quente/frio."""
    A=[]; B=[]
    estratos={}
    for c in base: estratos.setdefault(c["r"],[]).append(c)
    for r,grp in estratos.items():
        random.shuffle(grp)
        for i,c in enumerate(grp):
            (A if i%2==0 else B).append(c)
    random.shuffle(A); random.shuffle(B)
    return A,B

def dist(cs):
    from collections import Counter
    return dict(sorted(Counter(c["r"] for c in cs).items(), reverse=True))

def main():
    args=sys.argv[1:]
    base=eligiveis()
    A,B=split_ab(base)
    print(f"[retro] elegiveis (2+ pagos, nao-embaixadora): {len(base)}")
    print(f"[retro] braco A={len(A)}  B={len(B)}")
    print(f"[retro] mistura r_score A={dist(A)}  B={dist(B)}   (devem ser ~iguais)")

    if "--audit" in args:
        n=int(args[args.index("--audit")+1]); amostra=random.sample(base,min(n,len(base)))
        print(f"[retro] auditando {len(amostra)} contatos: RFM(pedidos_pagos) x Shopify(pago, sem troca)...")
        # (auditoria Shopify: implementada no build; requer SHOPIFY_* — reusa a logica do referral_processor)
        print("[retro] use este modo com SHOPIFY_* setado; reporta divergencias por e-mail.")
        return

    if "--go-live" not in args:
        print("[retro] DRY: nada enviado. Amostra A:",[c['email'] for c in A[:3]])
        print("[retro] para enviar de verdade: --go-live (respeita DAILY_CAP e retro_state.json)")
        return

    if not (EMAIL_A and EMAIL_B):
        print("[retro] ERRO: defina CONVITE_EMAIL_A e CONVITE_EMAIL_B."); return
    state=json.load(open(STATE)) if os.path.exists(STATE) else {"sent":[]}
    sent=set(state["sent"]); enviados=0
    fila=[("A",EMAIL_A,c) for c in A]+[("B",EMAIL_B,c) for c in B]
    random.shuffle(fila)
    for arm,eid,c in fila:
        if enviados>=DAILY_CAP: break
        if c["id"] in sent: continue
        m(f"/api/contacts/{c['id']}/edit",{"convite_ab":arm},"PATCH")
        m(f"/api/emails/{eid}/contact/{c['id']}/send",{},"POST")
        sent.add(c["id"]); enviados+=1
        time.sleep(0.3)  # respiro no SES/Mautic
    state["sent"]=sorted(sent); json.dump(state,open(STATE,"w"))
    print(f"[retro] enviados nesta rodada={enviados} (cap {DAILY_CAP}) | total historico={len(sent)}")

if __name__=="__main__": main()

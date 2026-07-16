#!/usr/bin/env python3
"""
Aniversariantes -> Mautic (F3). Roda diariamente (GitHub Actions).
A cada execucao:
  - ADICIONA ao segmento de entrada quem faz aniversario em LEAD_DAYS (default 30) -> entra na campanha em D-30.
  - REMOVE do segmento quem fez aniversario ha GRACE_DAYS (default 4) -> ja terminou a jornada, libera p/ reentrar no ano seguinte (campanha tem allowRestart).
Credenciais via env (GitHub Secrets): MAUTIC_BASE, MAUTIC_USER, MAUTIC_PASS.
"""
import os, base64, json, time, urllib.request, urllib.error, datetime
MBASE=os.environ["MAUTIC_BASE"].rstrip("/"); MU=os.environ["MAUTIC_USER"]; MP=os.environ["MAUTIC_PASS"]
SEG=int(os.environ.get("BIRTHDAY_SEGMENT_ID","530"))
LEAD=int(os.environ.get("LEAD_DAYS","30")); GRACE=int(os.environ.get("GRACE_DAYS","4"))
DRY=os.environ.get("DRY_RUN","false").lower()=="true"
AUTH=base64.b64encode(f"{MU}:{MP}".encode()).decode()
HDR={"Authorization":f"Basic {AUTH}","Accept":"application/json","Content-Type":"application/json"}
def http(url,method="GET",data=None,timeout=120):
    req=urllib.request.Request(url,data=(json.dumps(data).encode() if data is not None else None),headers=HDR,method=method)
    for a in range(6):
        try:
            with urllib.request.urlopen(req,timeout=timeout) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            # Mautic (t3.medium) retorna 500 na paginacao profunda da base grande: backoff e tenta de novo
            if e.code in (429,500,502,503) and a<5: time.sleep(3+a*3); continue
            raise
        except urllib.error.URLError:
            if a<5: time.sleep(3+a*3); continue
            raise
today=datetime.date.today()
add_md=(today+datetime.timedelta(days=LEAD)).strftime("%m-%d")
rem_md=(today-datetime.timedelta(days=GRACE)).strftime("%m-%d")
print(f"[birthday] today={today} add_md={add_md} rem_md={rem_md} seg={SEG} dry={DRY}",flush=True)
add_ids=[]; rem_ids=[]; start=0; total=None; scanned=0; withdob=0
while True:
    d=http(f"{MBASE}/api/contacts?limit=1000&start={start}&minimal=false")
    if total is None: total=int(d.get("total") or 0); print(f"[birthday] total contatos={total}",flush=True)
    cs=d.get("contacts",{}); cs=list(cs.values()) if isinstance(cs,dict) else cs
    if not cs: break
    for c in cs:
        scanned+=1
        v=(((c.get("fields") or {}).get("all") or {}).get("data_nascimento"))
        if not v or len(str(v))<10: continue
        withdob+=1; md=str(v)[5:10]
        if md==add_md: add_ids.append(c["id"])
        elif md==rem_md: rem_ids.append(c["id"])
    start+=len(cs)
    if total and start>=total: break
print(f"[birthday] scanned={scanned} com_data={withdob} add={len(add_ids)} remove={len(rem_ids)}",flush=True)
if DRY:
    print("[birthday] DRY RUN - nada alterado",flush=True)
else:
    a=r=0
    for cid in add_ids:
        try: http(f"{MBASE}/api/segments/{SEG}/contact/{cid}/add","POST",{}); a+=1
        except Exception as e: print("add err",cid,e,flush=True)
    for cid in rem_ids:
        try: http(f"{MBASE}/api/segments/{SEG}/contact/{cid}/remove","POST",{}); r+=1
        except Exception as e: print("rem err",cid,e,flush=True)
    print(f"[birthday] adicionados={a} removidos={r}",flush=True)

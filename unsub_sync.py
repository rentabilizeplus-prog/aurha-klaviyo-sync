#!/usr/bin/env python3
"""
Descadastro da Shopify -> "nao contatar" (DNC) no Mautic. GitHub Actions, de hora em hora.

Por que existe (25/09/2026): quem se descadastrava pela LOJA (conta, rodape, checkout) continuava
recebendo e-mail do Mautic. O Mautic so conhecia o descadastro feito pelo link dele. Medido: 25 de 26
descadastradas da Shopify entre jul e set/2026 receberam de 3 a 18 e-mails depois.

- Busca clientes com emailMarketingConsent UNSUBSCRIBED atualizados nas ultimas LOOKBACK_HOURS (26h
  por padrao: o cron do GitHub atrasa ate ~7h, e repetir e idempotente).
- Para cada e-mail, TODOS os contatos do Mautic com esse e-mail recebem DNC (reason 1 = descadastrou).
- Nunca remove DNC. Recadastro volta pelo proprio Mautic (formulario/link), nao por aqui.

  python unsub_sync.py                 # rodada normal
  python unsub_sync.py --listar        # so imprime os e-mails descadastrados (todos), nao mexe no Mautic

Credenciais via env: SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, MAUTIC_BASE, MAUTIC_USER, MAUTIC_PASS
"""
import base64, datetime, json, os, sys, time, urllib.error, urllib.parse, urllib.request

SHOP = os.environ.get("SHOP_DOMAIN", "artesanatoholistico.myshopify.com")
API_VER = "2025-01"
LOOKBACK_H = int(os.environ.get("LOOKBACK_HOURS", "26"))
LISTAR = "--listar" in sys.argv


def http(url, data=None, headers=None, method=None, timeout=90):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    for a in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                time.sleep(3 + a * 2)
                continue
            return e.code, {}
        except urllib.error.URLError:
            time.sleep(3 + a * 2)
    return 0, {}


def shopify_token():
    body = json.dumps({"client_id": os.environ["SHOPIFY_CLIENT_ID"],
                       "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"],
                       "grant_type": "client_credentials"}).encode()
    st, d = http(f"https://{SHOP}/admin/oauth/access_token", data=body, headers={"Content-Type": "application/json"})
    if "access_token" not in d:
        sys.exit(f"ERRO: token Shopify (HTTP {st})")
    return d["access_token"]


Q = """query($q:String!,$c:String){ customers(first:250, after:$c, query:$q){
  pageInfo{hasNextPage endCursor}
  nodes{ email emailMarketingConsent{ marketingState } } } }"""


def descadastrados(tok):
    q = "email_marketing_state:UNSUBSCRIBED"
    if not LISTAR:
        desde = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=LOOKBACK_H))
        q += f" updated_at:>='{desde.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
    hdr = {"X-Shopify-Access-Token": tok, "Content-Type": "application/json"}
    url = f"https://{SHOP}/admin/api/{API_VER}/graphql.json"
    cur, out = None, set()
    while True:
        for a in range(8):
            st, d = http(url, data=json.dumps({"query": Q, "variables": {"q": q, "c": cur}}).encode(), headers=hdr)
            if d.get("errors") and "throttled" in json.dumps(d["errors"]).lower():
                time.sleep(2 + a)
                continue
            break
        conn = d["data"]["customers"]
        for n in conn["nodes"]:
            if n.get("email") and (n.get("emailMarketingConsent") or {}).get("marketingState") == "UNSUBSCRIBED":
                out.add(n["email"].strip().lower())
        if not conn["pageInfo"]["hasNextPage"]:
            return sorted(out)
        cur = conn["pageInfo"]["endCursor"]


def main():
    emails = descadastrados(shopify_token())
    if LISTAR:
        print("\n".join(emails))
        return
    base = os.environ["MAUTIC_BASE"].rstrip("/")
    auth = base64.b64encode(f"{os.environ['MAUTIC_USER']}:{os.environ['MAUTIC_PASS']}".encode()).decode()
    hdr = {"Authorization": f"Basic {auth}"}
    novos = ja = sem_contato = falhas = 0
    for em in emails:
        q = urllib.parse.urlencode({"search": f'email:"{em}"', "limit": 10, "minimal": "true"})
        st, d = http(f"{base}/api/contacts?{q}", headers=hdr)
        contatos = d.get("contacts") or {}
        contatos = list(contatos.values()) if isinstance(contatos, dict) else contatos
        contatos = [c for c in contatos if (c.get("fields", {}).get("all", {}).get("email") or "").strip().lower() == em] or contatos
        if not contatos:
            sem_contato += 1
            continue
        for c in contatos:
            st, full = http(f"{base}/api/contacts/{c['id']}", headers=hdr)
            dnc = [x for x in (full.get("contact", {}).get("doNotContact") or []) if x.get("channel") == "email"]
            if dnc:
                ja += 1
                continue
            body = json.dumps({"reason": 1, "comments": "Descadastro feito na Shopify (unsub_sync.py)"}).encode()
            st, _ = http(f"{base}/api/contacts/{c['id']}/dnc/email/add", data=body,
                         headers={**hdr, "Content-Type": "application/json"}, method="POST")
            if st in (200, 201):
                novos += 1
            else:
                falhas += 1
                print(f"falha DNC contato {c['id']}: HTTP {st}")
    print(f"OK: {len(emails)} descadastrados na Shopify nas ultimas {LOOKBACK_H}h | "
          f"DNC novo {novos} | ja tinha {ja} | sem contato no Mautic {sem_contato} | falhas {falhas}")
    if falhas:
        sys.exit(1)


if __name__ == "__main__":
    main()

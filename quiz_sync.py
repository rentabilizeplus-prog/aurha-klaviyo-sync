#!/usr/bin/env python3
"""
Rede de seguranca: reconcilia a planilha do quiz (CSV publico) com o segmento 533 do Mautic.
- A integracao principal e Lovable -> Mautic (form id 1, tempo real). Este job (1x/h via
  GitHub Actions) so PEGA O QUE ESCAPOU: leads com e-mail na planilha que ainda nao estao no 533.
- Eficiente/stateless: o proprio segmento 533 e o estado. So upserta + adiciona quem falta.
  Nunca remove ninguem de lugar nenhum.

Credenciais via env (GitHub Secrets): MAUTIC_BASE, MAUTIC_USER, MAUTIC_PASS
(fallback local: ~/.mautic_user / ~/.mautic_pass)
"""
import os, re, csv, io, json, time, base64, urllib.request, urllib.error, urllib.parse

def _cred(env, path):
    v = os.environ.get(env)
    if v:
        return v.strip()
    try:
        return open(os.path.expanduser(path)).read().strip()
    except Exception:
        return ""

MBASE = os.environ.get("MAUTIC_BASE", "https://mautic.aurha.com.br").rstrip("/")
MU = _cred("MAUTIC_USER", "~/.mautic_user")
MP = _cred("MAUTIC_PASS", "~/.mautic_pass")
MHDR = {"Authorization": "Basic " + base64.b64encode(f"{MU}:{MP}".encode()).decode(),
        "Content-Type": "application/json"}

SEGMENT_ID = int(os.environ.get("QUIZ_SEGMENT_ID", "533"))
SEGMENT_ALIAS = os.environ.get("QUIZ_SEGMENT_ALIAS", "quiz-pulseira-lovable")
SHEET_ID = os.environ.get("QUIZ_SHEET_ID", "1LgVOZP6gL1Qf1eMOLzp7SbRipD6TNk3_oZOUB9dMG2Q")
SHEET_GID = os.environ.get("QUIZ_SHEET_GID", "448553860")
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")


def http(url, method="GET", body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=MHDR, method=method)
    for a in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                time.sleep(2 + a * 2); continue
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            time.sleep(2 + a * 2)
    return 0, {}


def fetch_sheet_leads():
    req = urllib.request.Request(CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read().decode("utf-8")
    leads = {}
    for rec in csv.DictReader(io.StringIO(text)):
        def g(c): return (rec.get(c) or "").strip()
        m = EMAIL_RE.search(g("etapa_05_contato_email"))
        if not m:
            continue
        email = m.group(1).lower()
        dob = ""
        try:
            d = int(g("etapa_01_nascimento_dia")); mo = int(g("etapa_01_nascimento_mes")); y = int(g("etapa_01_nascimento_ano"))
            if 1 <= d <= 31 and 1 <= mo <= 12 and 1900 <= y <= 2026:
                dob = f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            pass
        leads[email] = {
            "email": email,
            "firstname": g("etapa_03_nome_resposta"),
            "mobile": re.sub(r"\D", "", g("etapa_05_contato_whatsapp")),
            "data_nascimento": dob,
            "quiz_intencao": g("etapa_02_intencao_resposta"),
            "quiz_signo": g("signo"),
            "quiz_pedra": g("pedra"),
            "quiz_peca": g("peca_aurha_recomendada"),
            "quiz_peca_url": g("url_produto_recomendado"),
            "quiz_status": g("status_quiz"),
        }
    return leads


def segment_emails():
    emails, start = set(), 0
    while True:
        st, d = http(f"{MBASE}/api/contacts?search=segment:{SEGMENT_ALIAS}&limit=200&start={start}&minimal=true")
        cs = d.get("contacts", {})
        if not cs:
            break
        for c in cs.values():
            e = (c.get("fields", {}).get("all", {}) or {}).get("email")
            if e:
                emails.add(e.lower())
        start += 200
        if start >= int(d.get("total", 0)):
            break
    return emails


def find_contact(email):
    st, d = http(f"{MBASE}/api/contacts?search=email:{urllib.parse.quote(email)}&limit=1")
    cs = d.get("contacts", {})
    return list(cs.keys())[0] if cs else None


def main():
    sheet = fetch_sheet_leads()
    in_seg = segment_emails()
    missing = [e for e in sheet if e not in in_seg]
    print(f"planilha: {len(sheet)} e-mails | no segmento {SEGMENT_ID}: {len(in_seg)} | faltando: {len(missing)}")
    added = fail = 0
    for email in missing:
        lead = {k: v for k, v in sheet[email].items() if v}
        cid = find_contact(email)
        if cid:
            http(f"{MBASE}/api/contacts/{cid}/edit", "PATCH", lead)
        else:
            st, d = http(f"{MBASE}/api/contacts/new", "POST", lead)
            cid = (d.get("contact") or {}).get("id")
        if not cid:
            fail += 1; print(f"  FALHA: {email}"); continue
        st, _ = http(f"{MBASE}/api/segments/{SEGMENT_ID}/contact/{cid}/add", "POST", {})
        if st in (200, 201):
            added += 1
        time.sleep(0.2)
    print(f"reconciliacao: adicionados {added} | falhas {fail}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Vigia de entrega de broadcast do Mautic.

Problema que ele resolve
------------------------
Na Queima de Estoque (26/08 a 01/09/2026) a fila do Mautic travou tres vezes.
Resultado: de 7 e-mails, 2 nao sairam (0 envios) e 2 pararam com ~13 mil de
~78 mil contatos, 19 minutos depois do horario marcado. Ninguem ficou sabendo
ate alguem ir olhar na mao, dias depois. O e-mail de fechamento chegou a 16%
da base no dia do fechamento.

O que este script faz, a cada rodada
------------------------------------
1. Acha as campanhas vigiadas (prefixo de nome) que estao DENTRO da janela de
   envio: do horario marcado do evento ate +JANELA_HORAS.
2. Se a campanha ou o e-mail dela cairam pra despublicado, republica. Campanha
   despublicada e' o que congela os eventos agendados: eles ficam parados pra
   sempre em vez de sair.
3. Le quantos contatos ainda estao com o evento agendado (nao executado). Se
   passou PACIENCIA_MIN do horario marcado e ainda sobra fila, sai com codigo 1
   pra rodada do Actions ficar vermelha e o alerta chegar por e-mail.

O que ele NAO faz, de proposito
-------------------------------
Nao mexe em campanha fora da janela de envio. As campanhas mortas da queima
(128, 129, 131, 132) tem ~287 mil eventos agendados vencidos parados nelas;
republicar qualquer uma dispara e-mail vencido pra base inteira. Por isso a
janela e' condicao pra agir, nao so pra reportar.

Uso local:
    MAUTIC_BASE=... MAUTIC_USER=... MAUTIC_PASS=... DRY_RUN=true \
        python3 broadcast_watchdog.py
"""
import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ["MAUTIC_BASE"].rstrip("/")
USER = os.environ["MAUTIC_USER"]
PASS = os.environ["MAUTIC_PASS"]

PREFIXO = os.environ.get("WATCH_PREFIX", "lc")
JANELA_HORAS = float(os.environ.get("WINDOW_HOURS", "8"))
PACIENCIA_MIN = float(os.environ.get("STALL_MINUTES", "45"))
RESTO_TOLERADO = int(os.environ.get("STALL_REMAINING", "500"))
MOTOR_PARADO_MIN = float(os.environ.get("ENGINE_IDLE_MINUTES", "90"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

AUTH = base64.b64encode(f"{USER}:{PASS}".encode()).decode()


def api(path, payload=None, method="GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}/api/{path}", data=data, method=method,
        headers={"Authorization": "Basic " + AUTH,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def total(tabela, filtros):
    q = {"limit": 1}
    for i, (col, val) in enumerate(filtros):
        q[f"where[{i}][col]"] = col
        q[f"where[{i}][expr]"] = "eq"
        q[f"where[{i}][val]"] = val
    return api(f"stats/{tabela}?{urllib.parse.urlencode(q)}").get("total", 0)


def eventos(campanha):
    evs = campanha.get("events") or []
    return list(evs.values()) if isinstance(evs, dict) else evs


def parse_utc(txt):
    if not txt:
        return None
    txt = txt.replace("T", " ")[:19]
    return dt.datetime.strptime(txt, "%Y-%m-%d %H:%M:%S")


def vigiadas():
    """Campanhas do prefixo, com o evento de envio e o horario marcado."""
    saida = []
    pagina = 0
    while True:
        r = api(f"campaigns?limit=100&start={pagina * 100}")
        lote = r.get("campaigns") or {}
        if not lote:
            break
        for c in lote.values():
            if not str(c.get("name", "")).startswith(PREFIXO):
                continue
            evs = [e for e in eventos(c) if (e.get("properties") or {}).get("email")]
            if len(evs) != 1:
                continue
            ev = evs[0]
            saida.append({
                "id": int(c["id"]),
                "nome": c["name"],
                "publicada": bool(c.get("isPublished")),
                "email_id": int(ev["properties"]["email"]),
                "marcado": parse_utc(ev.get("triggerDate")),
            })
        if len(lote) < 100:
            break
        pagina += 1
    return sorted(saida, key=lambda x: (x["marcado"] or dt.datetime.max))


def ultimo_envio():
    """Data/hora do envio mais recente de QUALQUER e-mail do Mautic.

    Ordena por `id`, nao por `date_sent`. Ordenar email_stats por date_sent
    devolve linha errada: medido em 01/09/2026, o mesmo pedido com limit=1 e
    limit=3 trouxe linhas diferentes, e uma delas com hora futura em relacao ao
    envio real. Por `id` (auto-incremento) a leitura e' estavel -- cinco
    chamadas seguidas devolveram a mesma linha.
    """
    q = urllib.parse.urlencode(
        {"limit": 1, "order[0][col]": "id", "order[0][dir]": "DESC"})
    linhas = api(f"stats/email_stats?{q}").get("stats") or []
    return parse_utc(linhas[0]["date_sent"]) if linhas else None


def main():
    agora = dt.datetime.utcnow()
    print(f"vigia de broadcast | {agora:%Y-%m-%d %H:%M} UTC | "
          f"prefixo={PREFIXO!r} janela={JANELA_HORAS}h "
          f"paciencia={PACIENCIA_MIN}min dry_run={DRY_RUN}")

    problemas, agiu = [], []
    linhas = []

    # Saude do motor, independente de campanha. Em 01/09/2026 o
    # mautic:campaigns:trigger parou as 12:19 UTC e o Mautic ficou horas sem
    # mandar um unico e-mail -- funis perenes junto. O rebuild continuava
    # rodando (contato entrava na campanha), so o trigger e' que nao executava,
    # entao olhar so a campanha nao acusava nada.
    ult = ultimo_envio()
    if ult is None:
        problemas.append("nao consegui ler o ultimo envio (email_stats vazio)")
    else:
        parado_min = (agora - ult).total_seconds() / 60
        linhas.append(f"  motor: ultimo envio de qualquer e-mail "
                      f"{ult:%d/%m %H:%M} UTC ({parado_min:.0f} min atras)")
        if parado_min > MOTOR_PARADO_MIN:
            problemas.append(
                f"MOTOR PARADO: nenhum e-mail saiu do Mautic ha {parado_min:.0f} min "
                f"(ultimo {ult:%d/%m %H:%M} UTC). Nao e' a campanha: e' o "
                f"mautic:campaigns:trigger. Olhar lock/cron no EC2.")

    for c in vigiadas():
        marcado = c["marcado"]
        if marcado is None:
            continue
        fim = marcado + dt.timedelta(hours=JANELA_HORAS)
        dentro = marcado <= agora <= fim

        email = api(f"emails/{c['email_id']}")["email"]
        pendentes = total("campaign_lead_event_log",
                          [("campaign_id", c["id"]), ("is_scheduled", 1)])

        estado = "NA JANELA" if dentro else (
            "aguardando" if agora < marcado else "encerrada")
        linhas.append(
            f"  camp {c['id']:<4} {c['nome']:<22} {estado:<10} "
            f"marcado={marcado:%d/%m %H:%M} pub={str(c['publicada']):<5} "
            f"enviados={email.get('sentCount', 0):<7} agendados={pendentes}")

        if not dentro:
            continue

        # 1. re-armar o que caiu
        if not c["publicada"] or not email.get("isPublished"):
            alvo = []
            if not c["publicada"]:
                alvo.append(f"campanha {c['id']}")
            if not email.get("isPublished"):
                alvo.append(f"e-mail {c['email_id']}")
            aviso = f"despublicado dentro da janela: {', '.join(alvo)}"
            if DRY_RUN:
                print(f"  [DRY] republicaria: {aviso}")
            else:
                if not c["publicada"]:
                    api(f"campaigns/{c['id']}/edit",
                        {"isPublished": True}, method="PATCH")
                if not email.get("isPublished"):
                    api(f"emails/{c['email_id']}/edit",
                        {"isPublished": True}, method="PATCH")
                print(f"  RE-ARMADO: {aviso}")
            agiu.append(aviso)
            problemas.append(aviso)

        # 2. fila parada
        atraso = (agora - marcado).total_seconds() / 60
        if atraso >= PACIENCIA_MIN and pendentes > RESTO_TOLERADO:
            problemas.append(
                f"campanha {c['id']} ({c['nome']}): {pendentes} contatos ainda "
                f"agendados {atraso:.0f} min depois do horario. E-mail "
                f"{c['email_id']} enviou {email.get('sentCount', 0)}. "
                f"Fila do Mautic travada -- olhar cron/lock no EC2.")

    print("\n".join(linhas) or "  nenhuma campanha com o prefixo")

    resumo = os.environ.get("GITHUB_STEP_SUMMARY")
    if resumo:
        with open(resumo, "a") as f:
            f.write(f"### Vigia de broadcast — {agora:%d/%m %H:%M} UTC\n\n")
            f.write("```\n" + "\n".join(linhas) + "\n```\n")
            if problemas:
                f.write("\n**Problemas**\n\n")
                for p in problemas:
                    f.write(f"- {p}\n")

    if problemas:
        print("\nPROBLEMA:")
        for p in problemas:
            print(f"  - {p}")
        # re-armar sozinho nao e' falha: so falha se sobrou fila parada.
        if len(problemas) > len(agiu):
            return 1
    print("\nok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} do Mautic: {e.read()[:300]!r}", file=sys.stderr)
        sys.exit(2)

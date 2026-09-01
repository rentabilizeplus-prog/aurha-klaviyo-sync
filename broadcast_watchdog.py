#!/usr/bin/env python3
"""
Vigia de entrega de broadcast do Mautic.

O QUE TRAVA OS DISPAROS (medido em 01/09/2026)
----------------------------------------------
A conta do Amazon SES tem teto de ~73.000 e-mails por 24 HORAS CORRIDAS (janela
rolante, nao dia de calendario). Quando a base encosta nele, o Mautic para de
mandar TUDO -- broadcast e funis perenes junto -- ate a janela envelhecer.

Provas, na Queima de Estoque:

    travou 29/08 12:19 -> 73.466 enviados nas 24h anteriores
    travou 01/09 12:19 -> 73.204 enviados nas 24h anteriores
    e-mail de 28/08    -> 0 envios, 24h anteriores ja em ~73.000
    e-mail de 31/08    -> 0 envios, 24h anteriores ja em ~72.970
    30/08, com so 11.900 nas 24h anteriores -> mandou os 72.777 inteiros

Maximo que a conta ja conseguiu numa janela de 24h: 74.859. Nunca passou disso.

Resultado pratico: de 7 e-mails da campanha, 2 nao sairam e 2 entregaram ~11,5
mil de 78 mil, com corte aleatorio. O "Hoje fecha" chegou a 16% da base no dia
do fechamento. Ninguem soube ate alguem conferir na mao, dias depois.

O QUE ESTE SCRIPT FAZ
---------------------
1. Compara o publico de cada disparo que esta pra sair com a cota que SOBRA na
   janela de 24h. Avisa ANTES, enquanto ainda da pra encolher a base -- que e' a
   checagem que teria evitado os quatro estragos acima.
2. Avisa se o motor passou de ENGINE_IDLE_MINUTES sem mandar nenhum e-mail.
3. Avisa se uma campanha na janela de envio esta despublicada.

Falha a rodada (codigo 1) em qualquer um dos tres, pro Actions ficar vermelho e
o alerta chegar por e-mail.

O QUE ELE NAO FAZ, DE PROPOSITO
-------------------------------
Nao publica nem despublica nada. A versao anterior republicava campanha
despublicada dentro da janela, e isso era perigoso por dois motivos:

  - despublicar NAO impede o envio. Medido: as campanhas 129 e 132 estavam
    despublicadas e mesmo assim mandaram 11.722 e 11.503 e-mails. Quem desarma
    de verdade e' `publishDown` no passado, como o verificador_pos_lote.py do
    repo ja registrava.
  - campanha desarmada de proposito seria re-armada pelo vigia. Em 01/09 as
    campanhas 132, 137 e 139 foram desarmadas justamente pra liberar cota pro
    lancamento; re-armar qualquer uma despejaria dezenas de milhares de e-mails
    vencidos e mataria o disparo do dia seguinte.

Regra dura: campanha com `publishDown` no passado esta desarmada de proposito.
O vigia nem reporta problema nela.

Uso local:
    MAUTIC_BASE=... MAUTIC_USER=... MAUTIC_PASS=... python3 broadcast_watchdog.py
"""
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ["MAUTIC_BASE"].rstrip("/")
USER = os.environ["MAUTIC_USER"]
PASS = os.environ["MAUTIC_PASS"]

PREFIXO = os.environ.get("WATCH_PREFIX", "lc")
# Teto do SES por 24h corridas. 73.000 e' o numero de planejamento, nao o limite
# nominal: os dois travamentos medidos aconteceram em 73.204 e 73.466, porque o
# SES recusa o LOTE inteiro que cruzaria o limite, nao o e-mail exato.
TETO_24H = int(os.environ.get("SES_DAILY_QUOTA", "73000"))
ANTECEDENCIA_H = float(os.environ.get("LOOKAHEAD_HOURS", "6"))
# Fatia dos matriculados que de fato recebe (o resto e' DNC: descadastro/bounce).
ENTREGA_RATIO = float(os.environ.get("DELIVERY_RATIO", "0.924"))
JANELA_HORAS = float(os.environ.get("WINDOW_HOURS", "8"))
PACIENCIA_MIN = float(os.environ.get("STALL_MINUTES", "45"))
RESTO_TOLERADO = int(os.environ.get("STALL_REMAINING", "500"))
MOTOR_PARADO_MIN = float(os.environ.get("ENGINE_IDLE_MINUTES", "90"))

AUTH = base64.b64encode(f"{USER}:{PASS}".encode()).decode()


def api(path):
    req = urllib.request.Request(
        f"{BASE}/api/{path}", headers={"Authorization": "Basic " + AUTH})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def tentar(fn, tentativas=3, espera=10):
    """A API de stats varre tabelas de milhoes de linhas e as vezes estoura o
    tempo, com o Mautic respondendo em 0,7s no resto. Um blip nao pode virar
    alarme: quem le isso todo dia aprende a ignorar vermelho que mente."""
    for i in range(tentativas):
        try:
            return fn()
        except Exception as erro:
            print(f"  (leitura falhou, tentativa {i + 1}/{tentativas}: {erro})")
            if i < tentativas - 1:
                time.sleep(espera)
    return None


def total(tabela, filtros):
    q = {"limit": 1}
    for i, (col, expr, val) in enumerate(filtros):
        q[f"where[{i}][col]"] = col
        q[f"where[{i}][expr]"] = expr
        q[f"where[{i}][val]"] = val
    return int(api(f"stats/{tabela}?{urllib.parse.urlencode(q)}").get("total") or 0)


def parse_utc(txt):
    if not txt:
        return None
    return dt.datetime.strptime(txt.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")


def usado_24h(agora):
    """Quantos e-mails sairam na janela de 24h que termina agora."""
    ini = (agora - dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    return total("email_stats", [("date_sent", "gte", ini)])


def sobra_para(marcado):
    """Cota disponivel para um disparo marcado para `marcado`.

    Nao adianta olhar a cota de agora: o que importa e' quanto ainda estara
    DENTRO da janela de 24h quando o envio terminar. Um disparo de 60 mil leva
    ~2h a 550/min, entao tudo que saiu antes de (marcado - 22h) ja envelheceu e
    nao disputa mais.

    Sem isso o vigia mente: em 01/09, olhando a cota do momento (73.052 usados,
    zero de sobra), ele acusaria "nao cabe" para o disparo do dia seguinte --
    quando na verdade o bloco de hoje sai da janela 19 minutos depois do
    disparo comecar e sobra a cota inteira.
    """
    ini = (marcado - dt.timedelta(hours=22)).strftime("%Y-%m-%d %H:%M:%S")
    return TETO_24H - total("email_stats", [("date_sent", "gte", ini)])


def ultimo_envio():
    """Envio mais recente de QUALQUER e-mail.

    Ordena por `id`, nao por `date_sent`. Ordenar por date_sent devolve linha
    errada: medido em 01/09/2026, o mesmo pedido com limit=1 e limit=3 trouxe
    linhas diferentes, e a do limit=1 vinha 40 min a frente do envio real -- o
    vigia deu "motor saudavel" com o motor parado ha quase 3 horas.
    """
    q = urllib.parse.urlencode(
        {"limit": 1, "order[0][col]": "id", "order[0][dir]": "DESC"})
    linhas = api(f"stats/email_stats?{q}").get("stats") or []
    return parse_utc(linhas[0]["date_sent"]) if linhas else None


def vigiadas():
    saida, pagina = [], 0
    while True:
        lote = (api(f"campaigns?limit=100&start={pagina * 100}")
                .get("campaigns") or {})
        if not lote:
            break
        for c in lote.values():
            if not str(c.get("name", "")).startswith(PREFIXO):
                continue
            evs = c.get("events") or []
            if isinstance(evs, dict):
                evs = list(evs.values())
            evs = [e for e in evs if (e.get("properties") or {}).get("email")]
            if len(evs) != 1:
                continue
            saida.append({
                "id": int(c["id"]),
                "nome": c["name"],
                "publicada": bool(c.get("isPublished")),
                "email_id": int(evs[0]["properties"]["email"]),
                "marcado": parse_utc(evs[0].get("triggerDate")),
                "desarma": parse_utc(c.get("publishDown")),
            })
        if len(lote) < 100:
            break
        pagina += 1
    return sorted(saida, key=lambda x: (x["marcado"] or dt.datetime.max))


def main():
    agora = dt.datetime.utcnow()
    print(f"vigia de broadcast | {agora:%Y-%m-%d %H:%M} UTC | prefixo={PREFIXO!r} "
          f"teto24h={TETO_24H} antecedencia={ANTECEDENCIA_H}h")

    problemas, linhas = [], []

    usado = tentar(lambda: usado_24h(agora))
    ult = tentar(ultimo_envio)

    if usado is None:
        problemas.append("nao consegui ler o volume das ultimas 24h -- conferir na mao")
        sobra = None
    else:
        sobra = TETO_24H - usado
        linhas.append(f"  cota: {usado} enviados nas ultimas 24h, "
                      f"sobram {sobra} de {TETO_24H}")

    if ult is None:
        problemas.append("nao consegui ler o ultimo envio -- conferir na mao")
    else:
        parado = (agora - ult).total_seconds() / 60
        linhas.append(f"  motor: ultimo envio {ult:%d/%m %H:%M} UTC "
                      f"({parado:.0f} min atras)")
        if parado > MOTOR_PARADO_MIN:
            # Motor parado com a cota estourada e' consequencia, nao causa nova.
            causa = ("cota do SES estourada, volta sozinho quando a janela "
                     "envelhecer" if sobra is not None and sobra <= 0
                     else "NAO e' cota -- olhar cron e log no EC2")
            problemas.append(
                f"MOTOR PARADO ha {parado:.0f} min (ultimo {ult:%d/%m %H:%M} UTC). "
                f"Nenhum e-mail saiu, funis perenes junto. {causa}.")

    for c in vigiadas():
        marcado, desarma = c["marcado"], c["desarma"]
        if marcado is None:
            continue

        # Desarmada de proposito: publishDown no passado. Nao reportar, nao agir.
        if desarma is not None and desarma <= agora:
            linhas.append(f"  camp {c['id']:<4} {c['nome']:<26} DESARMADA "
                          f"(publishDown {desarma:%d/%m %H:%M})")
            continue

        fim = marcado + dt.timedelta(hours=JANELA_HORAS)
        dentro = marcado <= agora <= fim
        chegando = agora < marcado <= agora + dt.timedelta(hours=ANTECEDENCIA_H)
        if not (dentro or chegando):
            continue

        email = api(f"emails/{c['email_id']}")["email"]
        pendentes = total("campaign_lead_event_log",
                          [("campaign_id", "eq", c["id"]),
                           ("is_scheduled", "eq", 1)])
        # Nem todo matriculado recebe: descadastro e bounce tiram uma fatia
        # estavel. Medido em 4 disparos de 2026: 61.554/66.600 e 72.777/78.794,
        # os dois em 92,4%.
        entrega = int(pendentes * ENTREGA_RATIO)
        disponivel = tentar(lambda: sobra_para(marcado))

        estado = "NA JANELA" if dentro else "chega em breve"
        linhas.append(
            f"  camp {c['id']:<4} {c['nome']:<26} {estado:<14} "
            f"marcado={marcado:%d/%m %H:%M} pub={str(c['publicada']):<5} "
            f"na fila={pendentes} (~{entrega} entregues) "
            f"cota no disparo={disponivel if disponivel is not None else '?'}")

        # 1. o publico cabe na cota que estara livre na hora do disparo?
        if disponivel is None:
            problemas.append(f"nao consegui calcular a cota do disparo da camp "
                             f"{c['id']} -- conferir na mao")
        elif entrega > disponivel:
            problemas.append(
                f"NAO CABE NA COTA: camp {c['id']} ({c['nome']}) vai tentar "
                f"entregar ~{entrega} e a cota livre no horario do disparo e' "
                f"{disponivel}. Trava no meio e corta ~{entrega - disponivel} "
                f"pessoas ALEATORIAMENTE. Encolher a base, adiar o disparo, ou "
                f"pedir aumento de cota no SES.")

        # 2. despublicada (so aviso -- nao mexo).
        # Vale tambem pra disparo que ainda vai acontecer: campanha despublicada
        # nao dispara, e sem publishDown ela nao conta como desarmada de
        # proposito. Se so avisasse dentro da janela, uma campanha que caisse de
        # madrugada so apareceria depois da hora do disparo, tarde demais pra
        # consertar.
        if not c["publicada"] or not email.get("isPublished"):
            quando = "esta na janela de envio" if dentro else \
                f"dispara em {(marcado - agora).total_seconds() / 3600:.1f}h"
            problemas.append(
                f"DESPUBLICADA: camp {c['id']} ({c['nome']}) {quando} mas esta "
                f"despublicada (campanha={c['publicada']}, "
                f"e-mail={email.get('isPublished')}) e nao tem publishDown no "
                f"passado. Campanha despublicada nao dispara. Se o desarme nao "
                f"foi de proposito, republicar antes do horario.")

        # 3. fila parada depois do horario
        atraso = (agora - marcado).total_seconds() / 60
        if dentro and atraso >= PACIENCIA_MIN and pendentes > RESTO_TOLERADO:
            problemas.append(
                f"FILA PARADA: camp {c['id']} ({c['nome']}) com {pendentes} "
                f"contatos ainda na fila {atraso:.0f} min depois do horario.")

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
        return 1
    print("\nok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} do Mautic: {e.read()[:300]!r}", file=sys.stderr)
        sys.exit(2)

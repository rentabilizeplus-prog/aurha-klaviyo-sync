# Handlers do Programa de Embaixadoras (servidor Mautic / EC2)

Arquivos que rodam no servidor do Mautic (Apache, pasta dos hooks — a mesma do `sync.php`,
provavelmente `/var/www/webhook/`). **Não vão pro cron do GitHub** — são endpoints HTTP.

| Arquivo | Papel |
|---|---|
| `embaixadora.php` | Gatilho: recebe o webhook de form-submit do Mautic (form id 2), gera o cupom único na Yampi, grava no contato, adiciona ao segmento 532 e (se `DISPATCH_ENABLED`) envia e-mail + enfileira WhatsApp. |
| `embaixadora-optout.php` | Recebe o callback de opt-out do sistema de atendimento (Hetzner) e marca `embaixadora_status=opt-out` + DNC SMS. |
| `embaixadora.config.example.php` | Modelo de config. Copiar para `embaixadora.config.php` e preencher credenciais (chmod 600). |
| `deploy_embaixadora.sh` | Script de deploy de 1 passo (base64 embutido). |

## Deploy (via AWS Console → EC2 Instance Connect)
1. Abra o Instance Connect na instância do Mautic (`54.232.146.247`, sa-east-1).
2. **Cole o conteúdo inteiro de `deploy_embaixadora.sh`** no terminal e rode. Ele detecta a pasta
   dos hooks, faz backup, escreve os 3 arquivos e cria o `embaixadora.config.php` (se não existir).
3. **Edite `embaixadora.config.php`** e preencha `MAUTIC_USER/PASS` e `YAMPI_TOKEN/SECRET`
   (os demais já vêm preenchidos). `sudo chmod 600 embaixadora.config.php`.
4. Teste: `curl -s 'https://mautic.aurha.com.br/hook/embaixadora.php?token=fd374b40d3aecf856ae9985bd9165e70' -d '{}'`
   → deve responder `{"ok":true,"noop":"empty"}`.

## Trava de disparo (o "não envia")
No `embaixadora.config.php`, `DISPATCH_ENABLED => false` mantém o handler criando cupom e gravando
tudo, **sem enviar e-mail nem WhatsApp**. Para colocar o programa no ar, vire para `true`.
(No lado do cashback, o gate equivalente é `GO_LIVE` no `.github/workflows/referral.yml`.)

## Correção do RFM (excluir cupom de troca) — passo manual no `sync.php`
Não auto-aplico porque não consigo ler o `sync.php` de produção com segurança pela janela.
Faça no Instance Connect, **com backup** (`sudo cp sync.php sync.php.bak.$(date +%s)`):

Na parte onde o `sync.php` conta os pedidos pagos (monta `pedidos_pagos`/`f_score`/`ltv_real`
a partir dos pedidos do Shopify), adicione na query GraphQL de orders o campo `discountCodes` e
**pule o pedido** quando algum código começar com `troca`:

```php
// dentro do loop de pedidos, logo após pegar o status/valor:
$codes = $order['discountCodes'] ?? [];
$isTroca = false;
foreach ($codes as $cc) { if (stripos($cc, 'troca') === 0) { $isTroca = true; break; } }
if ($isTroca) continue;   // troca não conta como compra (nem em nº de pedidos, nem em LTV)
```

Depois force o recálculo de um contato de teste:
`curl -s 'https://mautic.aurha.com.br/hook/sync.php?token=fd374b40d3aecf856ae9985bd9165e70' -d '{"id":<shopify_customer_id>}'`
e confira que um pedido `troca*` deixou de contar em `pedidos_pagos`.

> Obs.: o script de envio retroativo (`retro_convite.py`) já exclui `troca*` e não-pagos por conta
> própria (consulta o Shopify direto), então a base do A/B fica correta mesmo antes deste patch.

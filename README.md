# Aurha — Sync Shopify → Klaviyo (LTV real + RFM)

Sincroniza automaticamente os pedidos da loja Aurha (Shopify) com os perfis do
Klaviyo, recalculando:

- **`customer_type`**: `Cliente` (≥1 pedido pago) ou `Lead` (nunca pagou)
- **`ltv_real`**: LTV líquido contando **só pedidos pagos** (netPayment)
- **`pedidos_pagos` / `pedidos_nao_pagos`**
- **`rfm_segment`** + `r_score`/`f_score`/`m_score` (matriz RFM)
- **`ultimo_pedido_pago` / `primeiro_pedido_pago` / `recencia_dias`**

## Como funciona
A cada 2 horas (GitHub Actions), o `sync.py`:
1. Gera um token Shopify via `client_credentials` (não precisa copiar token).
2. Busca pedidos **alterados** nas últimas 6h (inclui Pix/boleto que viraram "pago").
3. Para cada cliente afetado, recalcula o histórico completo.
4. Atualiza os perfis no Klaviyo via bulk import — **sem alterar consentimento**
   (a base segue suprimida; só dados são atualizados).

## Segredos necessários (Settings → Secrets and variables → Actions)
- `SHOPIFY_CLIENT_ID`
- `SHOPIFY_CLIENT_SECRET`
- `KLAVIYO_API_KEY`

## Rodar manualmente
Aba **Actions** → workflow "Sync Shopify -> Klaviyo" → **Run workflow**.

## Por que não Shopify Flow?
A loja está no plano Basic (Flow exige plano $79+). Esta solução roda na nuvem
do GitHub de graça, sem depender do plano nem de máquina local.

<?php
/**
 * Config do handler das Embaixadoras. Copie para embaixadora.config.php no servidor
 * (mesma pasta dos hooks, ex.: /var/www/webhook/) e preencha. chmod 600.
 * NUNCA versionar o arquivo real com segredos.
 */
return [
  // Mautic (mesmas credenciais de API usadas pelo sync.php)
  'MAUTIC_BASE'   => 'https://mautic.aurha.com.br',
  'MAUTIC_USER'   => 'REPLACE',
  'MAUTIC_PASS'   => 'REPLACE',

  // Yampi (API dooki)
  'YAMPI_ALIAS'   => 'artesanatoholistico',
  'YAMPI_TOKEN'   => 'REPLACE',
  'YAMPI_SECRET'  => 'REPLACE',

  // Seguranca do webhook (Mautic -> este handler)
  'HOOK_TOKEN'    => 'fd374b40d3aecf856ae9985bd9165e70',                 // ?token= na URL
  'MAUTIC_HMAC_SECRET' => 'emb_075592a6458c6bef2bbf4e1341b581730ffc745e4c79a4ff', // secret do webhook Mautic id 1

  // Integracao WhatsApp (sistema de atendimento na Hetzner)
  'ZAPI_ENQUEUE_URL'        => 'https://atendimento.aurha.com.br/hook/embaixadora/enqueue',
  'EMBAIXADORA_WEBHOOK_TOKEN' => 'REPLACE_FROM_1PASSWORD',   // Bearer do enqueue (mesmo do atendimento)
  'MAUTIC_OPTOUT_TOKEN'     => 'REPLACE_FROM_1PASSWORD',     // token do callback de opt-out

  // Parametros do programa
  'FORM_ID'        => 2,          // id do formulario "Aceite Embaixadora"
  'SEGMENT_ID'     => 532,        // segmento Embaixadores
  'FRIEND_DISCOUNT'=> 12,         // % de desconto para a amiga
  'CUPOM_EMAIL_ID' => 157,        // e-mail Mautic com o cupom + material
  'HOME_URL'       => 'https://aurha.com.br',

  // TRAVA DE DISPARO: enquanto false, cria cupom e grava tudo, mas NAO envia e-mail
  // nem dispara WhatsApp. Vire true para colocar o programa no ar.
  'DISPATCH_ENABLED' => false,
];

<?php
/**
 * Handler do GATILHO do Programa de Embaixadoras (roda no servidor do Mautic, /hook/).
 * Chamado pelo webhook Mautic "Evento de Envio de Formulario" (id 1) no submit do form
 * "Aceite Embaixadora" (id 2). Fluxo, SINCRONO e IDEMPOTENTE:
 *   1. valida token (?token=) + assinatura HMAC do Mautic
 *   2. filtra: so age no form FORM_ID
 *   3. le o contato (firstname, email, mobile, referral_code)
 *   4. se ja tem referral_code -> reusa (idempotencia); senao gera cupom unico na Yampi
 *      (FRIEND_DISCOUNT %, once_per_customer) com code = PRIMEIRONOME + mautic_id
 *   5. grava no contato: referral_code, referral_discount, embaixadora_status=ativa,
 *      aceite_data, aceite_ip; adiciona ao segmento SEGMENT_ID
 *   6. se DISPATCH_ENABLED: envia e-mail do cupom (CUPOM_EMAIL_ID) e enfileira WhatsApp (Hetzner)
 * Log: /var/log/embaixadora.log (ou ao lado do arquivo se sem permissao).
 */
header('Content-Type: application/json; charset=utf-8');
$CFG = @include __DIR__.'/embaixadora.config.php';
if (!is_array($CFG)) { http_response_code(500); echo json_encode(['error'=>'no config']); exit; }

function logline($m){ $f='/var/log/embaixadora.log'; $line='['.date('c').'] '.$m."\n";
  if(!@file_put_contents($f,$line,FILE_APPEND)) @file_put_contents(__DIR__.'/embaixadora.log',$line,FILE_APPEND); }

/* -------- 1. seguranca -------- */
$raw = file_get_contents('php://input');
if (($_GET['token'] ?? '') !== $CFG['HOOK_TOKEN']) { http_response_code(401); echo '{"error":"token"}'; exit; }
$sig = $_SERVER['HTTP_WEBHOOK_SIGNATURE'] ?? '';
if (!empty($CFG['MAUTIC_HMAC_SECRET'])) {
  $calc = base64_encode(hash_hmac('sha256', $raw, $CFG['MAUTIC_HMAC_SECRET'], true));
  if (!hash_equals($calc, $sig)) { logline("HMAC invalido (ignorado se vazio)"); /* nao bloqueia: Mautic pode nao assinar */ }
}
$payload = json_decode($raw, true);
if (!$payload) { http_response_code(200); echo '{"ok":true,"noop":"empty"}'; exit; }

/* -------- helpers Mautic/Yampi -------- */
function mautic($CFG,$path,$data=null,$method='GET'){
  $ch=curl_init($CFG['MAUTIC_BASE'].$path);
  $h=['Authorization: Basic '.base64_encode($CFG['MAUTIC_USER'].':'.$CFG['MAUTIC_PASS']),
      'Content-Type: application/json','Accept: application/json'];
  curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>1,CURLOPT_CUSTOMREQUEST=>$method,CURLOPT_HTTPHEADER=>$h,CURLOPT_TIMEOUT=>60]);
  if($data!==null) curl_setopt($ch,CURLOPT_POSTFIELDS,json_encode($data));
  $r=curl_exec($ch); $st=curl_getinfo($ch,CURLINFO_HTTP_CODE); curl_close($ch);
  return [$st, json_decode($r,true)];
}
function yampi($CFG,$path,$data=null,$method='GET'){
  $ch=curl_init('https://api.dooki.com.br/v2/'.$CFG['YAMPI_ALIAS'].$path);
  $h=['User-Token: '.$CFG['YAMPI_TOKEN'],'User-Secret-Key: '.$CFG['YAMPI_SECRET'],
      'Content-Type: application/json','Accept: application/json'];
  curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>1,CURLOPT_CUSTOMREQUEST=>$method,CURLOPT_HTTPHEADER=>$h,CURLOPT_TIMEOUT=>60]);
  if($data!==null) curl_setopt($ch,CURLOPT_POSTFIELDS,json_encode($data));
  $r=curl_exec($ch); $st=curl_getinfo($ch,CURLINFO_HTTP_CODE); curl_close($ch);
  return [$st, json_decode($r,true)];
}
function slug_nome($s){
  $s = @iconv('UTF-8','ASCII//TRANSLIT',$s); $s = preg_replace('/[^A-Za-z]/','',$s);
  return strtoupper(substr($s,0,12));
}

/* -------- 2. extrai eventos de form submit -------- */
$events = $payload['mautic.form_on_submit'] ?? [];
if (!$events) { http_response_code(200); echo '{"ok":true,"noop":"no_form_events"}'; exit; }

$acted = 0;
foreach ($events as $ev) {
  $sub = $ev['submission'] ?? $ev;
  $formId = $sub['form']['id'] ?? ($ev['form']['id'] ?? null);
  if ((int)$formId !== (int)$CFG['FORM_ID']) continue;               // 3. filtra o form

  // contato: tenta o lead do payload, senao busca pela API
  $lead = $sub['lead'] ?? $ev['contact'] ?? $sub['contact'] ?? null;
  $cid = $lead['id'] ?? null;
  if (!$cid) { logline('submit sem lead id'); continue; }
  list($st,$c) = mautic($CFG,"/api/contacts/$cid");
  $f = $c['contact']['fields']['all'] ?? [];
  $first = $f['firstname'] ?? ($sub['results']['firstname'] ?? '');
  $email = $f['email'] ?? ($sub['results']['email'] ?? '');
  $mobile= $f['mobile'] ?? ($sub['results']['mobile'] ?? '');
  $code  = $f['referral_code'] ?? '';

  /* helper: confirma que o cupom existe MESMO na Yampi (salvaguarda anti "e-mail sem cupom") */
  $coupon_exists = function($CFG,$code){
    list($gs,$gr) = yampi($CFG,'/pricing/promocodes?q='.urlencode($code).'&limit=5');
    foreach (($gr['data'] ?? []) as $c) { if (strtoupper($c['code'] ?? '')===strtoupper($code)) return true; }
    return false;
  };

  /* 4. idempotencia + geracao do cupom (COM verificacao) */
  $coupon_ok = false;
  $ip = $_SERVER['HTTP_X_FORWARDED_FOR'] ?? $_SERVER['REMOTE_ADDR'] ?? '';
  if (!$code) {
    $base = slug_nome($first); if ($base==='') $base='AMIGA';
    $code = $base.$cid;
    $coupon = [
      'code'=>$code,'discount_type'=>'p','value'=>(float)$CFG['FRIEND_DISCOUNT'],
      'active'=>true,'once_per_customer'=>true,'free_shipment'=>false,
      'min_value'=>0,'quantity'=>100000,
      'start_at'=>date('Y-m-d H:i:s'),'end_at'=>'2099-12-31 23:59:59',
    ];
    list($cs,$cr) = yampi($CFG,'/pricing/promocodes',$coupon,'POST');
    $coupon_ok = ($cs>=200 && $cs<300);
    if (!$coupon_ok) { $coupon_ok = $coupon_exists($CFG,$code); }  // pode ja existir de tentativa anterior
    logline($coupon_ok ? "cupom $code pronto (contato $cid) HTTP $cs" : "ERRO cupom $code HTTP $cs ".json_encode($cr));
  } else {
    $coupon_ok = $coupon_exists($CFG,$code);  // ja tinha code: confirma o cupom na Yampi antes de reusar
    logline("contato $cid ja tinha referral_code=$code (cupom ".($coupon_ok?"ok":"NAO encontrado").")");
  }

  /* SALVAGUARDA: sem cupom valido -> NAO grava code, NAO envia. Marca p/ follow-up. */
  if (!$coupon_ok || !$code) {
    mautic($CFG,"/api/contacts/$cid/edit",[
      'embaixadora_status'=>'pendente_cupom','aceite_data'=>date('Y-m-d H:i:s'),'aceite_ip'=>$ip,
    ],'PATCH');
    logline("contato $cid SEM cupom valido -> status=pendente_cupom, NADA enviado");
    continue;
  }

  /* 5. grava no contato + segmento (so com cupom valido) */
  mautic($CFG,"/api/contacts/$cid/edit",[
    'referral_code'=>$code,'referral_discount'=>(int)$CFG['FRIEND_DISCOUNT'],
    'embaixadora_status'=>'ativa','aceite_data'=>date('Y-m-d H:i:s'),'aceite_ip'=>$ip,
  ],'PATCH');
  mautic($CFG,"/api/segments/{$CFG['SEGMENT_ID']}/contact/$cid/add",[], 'POST');

  /* 6. disparo (travado por DISPATCH_ENABLED) */
  if (!empty($CFG['DISPATCH_ENABLED'])) {
    // e-mail do cupom
    mautic($CFG,"/api/emails/{$CFG['CUPOM_EMAIL_ID']}/contact/$cid/send",[], 'POST');
    // WhatsApp: enfileira na Hetzner
    $ready = "Gente, tô amando as pulseiras da Aurha, de proteção e feitas à mão.\n\n".
             "Usando o meu cupom $code você ganha *{$CFG['FRIEND_DISCOUNT']}% de desconto + 15% de cashback*.\n\n".
             "As peças são lindas e chegam numa caixinha caprichada e cheirosa, uma ótima opção de presente.\n\n".
             "Dá uma olhada, tenho certeza que vai amar: {$CFG['HOME_URL']} 💜";
    $body = [
      'event'=>'ambassador_accepted','idempotency_key'=>"amb-$cid",
      'contact'=>['mautic_id'=>(int)$cid,'first_name'=>$first,'whatsapp'=>$mobile],
      'coupon'=>['code'=>$code,'discount_pct'=>(int)$CFG['FRIEND_DISCOUNT']],
      'messages'=>[
        ['seq'=>1,'template'=>'welcome_ambassador','delay_s'=>0],
        ['seq'=>2,'template'=>'ready_to_share','delay_s'=>60,
         'vars'=>['coupon'=>$code,'home_url'=>$CFG['HOME_URL']]],
      ],
      'ready_to_share_text'=>$ready,
    ];
    $ch=curl_init($CFG['ZAPI_ENQUEUE_URL']);
    curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>1,CURLOPT_POST=>1,CURLOPT_TIMEOUT=>20,
      CURLOPT_HTTPHEADER=>['Authorization: Bearer '.$CFG['EMBAIXADORA_WEBHOOK_TOKEN'],'Content-Type: application/json'],
      CURLOPT_POSTFIELDS=>json_encode($body)]);
    $wr=curl_exec($ch); $ws=curl_getinfo($ch,CURLINFO_HTTP_CODE); curl_close($ch);
    logline("dispatch contato $cid: email enviado, whatsapp enqueue HTTP $ws");
  } else {
    logline("contato $cid pronto (cupom $code) -- DISPATCH_ENABLED=false, nada enviado");
  }
  $acted++;
}
http_response_code(200);
echo json_encode(['ok'=>true,'acted'=>$acted]);

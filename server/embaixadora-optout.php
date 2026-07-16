<?php
/**
 * Receiver de OPT-OUT do WhatsApp (chamado pelo sistema de atendimento na Hetzner quando a
 * embaixadora responde SAIR/PARAR/CANCELAR). Marca embaixadora_status=opt-out no Mautic e,
 * por seguranca, marca o contato como Do Not Contact no canal SMS.
 * URL: /hook/embaixadora-optout.php?token=<MAUTIC_OPTOUT_TOKEN>
 * Body JSON: { "whatsapp": "+55...", "mautic_id": 12345 }
 */
header('Content-Type: application/json; charset=utf-8');
$CFG = @include __DIR__.'/embaixadora.config.php';
if (!is_array($CFG)) { http_response_code(500); echo '{"error":"no config"}'; exit; }
if (empty($CFG['MAUTIC_OPTOUT_TOKEN']) || ($_GET['token'] ?? '') !== $CFG['MAUTIC_OPTOUT_TOKEN']) {
  http_response_code(401); echo '{"error":"token"}'; exit;
}
$in = json_decode(file_get_contents('php://input'), true) ?: [];
$cid = $in['mautic_id'] ?? null; $wa = $in['whatsapp'] ?? '';

function mautic($CFG,$path,$data=null,$method='GET'){
  $ch=curl_init($CFG['MAUTIC_BASE'].$path);
  $h=['Authorization: Basic '.base64_encode($CFG['MAUTIC_USER'].':'.$CFG['MAUTIC_PASS']),
      'Content-Type: application/json','Accept: application/json'];
  curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>1,CURLOPT_CUSTOMREQUEST=>$method,CURLOPT_HTTPHEADER=>$h,CURLOPT_TIMEOUT=>60]);
  if($data!==null) curl_setopt($ch,CURLOPT_POSTFIELDS,json_encode($data));
  $r=curl_exec($ch); $st=curl_getinfo($ch,CURLINFO_HTTP_CODE); curl_close($ch);
  return [$st, json_decode($r,true)];
}
// resolve contato por id ou por telefone
if (!$cid && $wa) {
  $digits = preg_replace('/\D/','',$wa);
  list($st,$d) = mautic($CFG,'/api/contacts?search='.urlencode('mobile:'.$digits).'&limit=1');
  $cs = array_values($d['contacts'] ?? []); if ($cs) $cid = $cs[0]['id'];
}
if (!$cid) { http_response_code(200); echo '{"ok":true,"noop":"no_contact"}'; exit; }

mautic($CFG,"/api/contacts/$cid/edit",['embaixadora_status'=>'opt-out'],'PATCH');
// Do Not Contact no canal SMS (nao afeta e-mail)
mautic($CFG,"/api/contacts/$cid/dnc/sms/add",['reason'=>3,'comments'=>'opt-out WhatsApp embaixadora'],'POST');
@file_put_contents('/var/log/embaixadora.log','['.date('c')."] opt-out contato $cid ($wa)\n",FILE_APPEND);
http_response_code(200);
echo json_encode(['ok'=>true,'opted_out'=>(int)$cid]);

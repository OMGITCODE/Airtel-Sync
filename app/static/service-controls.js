/* Role credentials live only in memory. A session token belongs to one browser tab. */
function lockVerifiedIdentity() {
  const verified = S.kyc?.status === 'VERIFIED';
  for (const el of document.querySelectorAll('#kyc-gstin,#kyc-udyam,#kyc-name,#profile-form [name="gstin"],#profile-form [name="name"]')) el.readOnly = verified;
}

async function syncJourney() {
  if (!S.journey) return;
  const r = await api('/api/journeys/'+S.journey);
  S.profile = r.profile;
  S.kyc = r.kyc;
  S.consent = r.consents.kyc;
  S.documents = r.documents;
  const last = r.quotes[0];
  S.quote = last ? {...last,...last.terms,quote_id:last.id,accepted:!!last.accepted} : null;
  S.cart = last?.items || [];
  S.order = r.orders[0] ? {...r.orders[0],order_id:r.orders[0].id} : null;
  fillProfile();
  renderOcrDocuments();
  lockVerifiedIdentity();
  renderJourneyPulse();
  $('#consent-check').checked = S.consent;
  $('#quote-version').textContent=last?'VERSION '+last.version:'NOT ISSUED';
  $('#accept-quote').disabled=!last?.valid || !!last.accepted;
  if(last?.terms){
    const t=last.terms;$('#quote-term').value=String(t.term_months);$('#site-feasibility').value=t.site_code||'MARKET';
    $('#quote-result').innerHTML=t.items.map(it=>'<div class="receipt-line"><span>'+esc(it.name)+' × '+it.quantity+'</span><b>'+fmt(it.monthly_paise)+'</b></div>').join('')+'<p>Monthly GST '+fmt(t.gst_paise)+' · Setup including GST '+fmt(t.setup_paise+t.setup_gst_paise)+'</p><b>Monthly including GST '+fmt(t.monthly_with_gst_paise)+'</b><p>'+t.term_months+' months · Demo quote valid through '+esc(t.valid_until||t.expires_at.slice(0,10))+'</p>';
  }
  $('#verification-result').textContent = r.kyc ? [r.kyc.status,...r.kyc.reasons,...(r.kyc.notes||[])].join(' · ') : '';
  if(S.order) await refreshOrder();
}

function renderTracking(o) {
  const active=o.steps.indexOf(o.status);
  $('#tracker-progress').innerHTML=o.steps.map((step,i)=>'<li class="'+(active>=i?'recorded':'')+'">'+esc(step.toLowerCase().replaceAll('_',' '))+'</li>').join('');
  $('#tracker-eta').textContent=o.expected_activation?'Demo calendar · Installation slot '+o.install_date+' · Expected activation '+o.expected_activation:'Expected date unavailable for this older order.';
  $('#sim-notices').innerHTML=o.notifications.map(n=>'<p><b>Simulated '+esc(n.channel)+'</b> · '+esc(n.message)+'</p>').join('');
}

async function refreshStaff() {
  const [reviews, leads, handoffs, kpis] = await Promise.all([
    api('/api/partners/reviews'),api('/api/partners/leads'),api('/api/partners/handoffs'),api('/api/partners/kpis')
  ]);
  const root=$('#staff-results');
  root.innerHTML='<h3>KYC review queue</h3>'+ (reviews.map(c=>'<article class="queue-case"><b>'+esc(c.gstin)+'</b><ul>'+c.reasons.map(s=>'<li>'+esc(s)+'</li>').join('')+'</ul><p>Signatory: '+esc(c.evidence.document_signatory)+' · Applicant: '+esc(c.evidence.expected_applicants.join(', '))+'</p><p>Certificate: '+esc(c.evidence.certificate_legal_name)+' · Registry: '+esc(c.evidence.registry_legal_name)+'</p><label>Decision rationale<input maxlength="500"></label><button class="btn secondary" data-decision="approve" data-jid="'+esc(c.journey_id)+'" data-revision="'+esc(c.revision)+'">Approve</button> <button class="btn outline" data-decision="reject" data-jid="'+esc(c.journey_id)+'" data-revision="'+esc(c.revision)+'">Reject</button></article>').join('') || '<p>No shared cases awaiting review.</p>')+'<p id="audit-receipt" role="status"></p><h3>Live journey measures</h3><p>'+esc(kpis.activated)+' activations from '+esc(kpis.journeys)+' retained journeys · '+esc(kpis.activation_pct)+'% · '+(kpis.mean_days_journey_to_activation==null?'No completed activation interval':Number(kpis.mean_days_journey_to_activation).toFixed(2)+' days on average')+'</p>';
  for(const button of root.querySelectorAll('[data-decision]')) button.onclick=async()=>{
    try{
      const rationale=button.closest('article').querySelector('input').value;
      await api('/api/partners/reviews/'+button.dataset.jid,{method:'POST',body:JSON.stringify({decision:button.dataset.decision,revision:button.dataset.revision,rationale})});
      const log=await api('/api/partners/reviews/'+button.dataset.jid+'/audit');
      await refreshStaff();
      $('#audit-receipt').textContent=log.map(e=>e.decision+' · '+e.actor+' · '+e.created_at+' · '+e.rationale).join(' / ');
      if(S.journey===button.dataset.jid)await syncJourney();
    }catch(e){toast(e.message,true)}
  };
  $('#propensity-summary').textContent='Synthetic holdout AUC '+leads.heldout_auc+' · '+leads.heldout_rows+' held-out leads';
  $('#propensity-list').innerHTML=leads.prospects.map(l=>'<article class="propensity-lead"><b>'+esc(l.business_type)+' · '+esc(l.city)+'</b><small>'+esc(l.drop_reason)+' · '+esc(l.action)+'</small><span>'+Math.round(100*l.propensity_score)+'%</span><button type="button" class="btn outline" data-resume="'+esc(l.lead_id)+'">Resume in advisor</button></article>').join('');
  for(const button of document.querySelectorAll('[data-resume]')) button.onclick=async()=>{
    try {
      const r=await api('/api/partners/leads/'+button.dataset.resume+'/resume',{method:'POST'});
      rememberJourney(r.id,r.token);
      S.demo=null;
      $('#messages').textContent='Resumed a synthetic lead. Confirm the business profile before requesting a quote.';
      $('#tool-evidence').replaceChildren();$('#quote-result').replaceChildren();$('#recommendations').replaceChildren();$('#timeline').replaceChildren();
      await syncJourney();view('advisor');
    }catch(e){toast(e.message,true)}
  };
  $('#partner-key').value='';
  $('#handoff-list').innerHTML=handoffs.length?handoffs.map(h=>'<div class="handoff-item"><b>'+esc(h.reason)+' · '+Math.round(h.score*100)+'%</b><p>'+esc(h.action)+'</p></div>').join(''):'<p>No shared opportunities.</p>';
  $('.analytics-console').hidden=false;await loadAnalytics();
}

function lockDesk() {
  S.partnerKey=null;$('#partner-key').value='';S.analyticsRequest++;S.metrics=null;
  $('#staff-results').replaceChildren();$('#handoff-list').textContent='Partner key required';
  $('#propensity-list').replaceChildren();$('#propensity-summary').textContent='Partner key required';
  $('.analytics-console').hidden=true;
}

async function restoreSession() {
  const stored=sessionStorage.getItem('onejourney-session');
  if(!stored || S.journey)return;
  try {
    const [jid,token]=JSON.parse(stored);rememberJourney(jid,token);await syncJourney();
  } catch(error) {
    if(error.status===401 || error.status===403){rememberJourney(null,null);toast('Journey expired. Start a new journey.');}
    else toast(error.message,true);
  }
}

window.addEventListener('DOMContentLoaded',()=>{
  const bind=(id,fn)=>$(id).addEventListener('click',async()=>{try{await fn()}catch(e){toast(e.message,true)}});
  bind('#prepare-applicant',async()=>{
    if(!guard())return;
    await api('/api/journeys/'+S.journey+'/demo-profile/'+$('#demo-applicant').value,{method:'POST'});
    await syncJourney();await recommendations();view('plans');
  });
  bind('#import-specimen',async()=>{
    if(!guard() || !S.consent)throw Error('Grant KYC consent before importing demo documents.');
    const button=$('#import-specimen');button.disabled=true;
    try {const form=new FormData();form.set('authorized','true');await api('/api/journeys/'+S.journey+'/digilocker/'+$('#demo-applicant').value,{method:'POST',body:form});await syncJourney();}
    finally {button.disabled=false}
  });
  bind('#refresh-kyc-status',async()=>{if(guard())await syncJourney()});
  bind('#refer-review',async()=>{if(!guard())return;const form=new FormData();form.set('accepted','true');await api('/api/journeys/'+S.journey+'/consent/partner',{method:'POST',body:form});toast('Partner review authorized. Refresh verification status after review.')});
  bind('#send-grievance',async()=>{if(!guard())return;const r=await api('/api/journeys/'+S.journey+'/grievances',{method:'POST',body:JSON.stringify({message:$('#grievance-text').value})});$('#grievance-text').value='';$('#grievance-ref').textContent='Local operator reference: '+r.reference});
  bind('#withdraw-kyc',async()=>{if(!guard())return;await api('/api/journeys/'+S.journey+'/consent/kyc',{method:'DELETE'});await syncJourney();$('#kyc-gstin').value='';$('#kyc-udyam').value='';$('#kyc-name').value='';$('#lookup-result').textContent='KYC consent withdrawn. Stored evidence erased.'});
  bind('#lookup-registry',async()=>{if(!guard())return;const r=await api('/api/journeys/'+S.journey+'/registry',{method:'POST',body:JSON.stringify({gstin:$('#kyc-gstin').value})});$('#kyc-name').value=r.name;$('#lookup-result').textContent=[r.name,r.constitution,r.state,r.status,r.gstin].join(' · ');});
  bind('#mock-esign',async()=>{if(!guard())return;const form=new FormData();form.set('accepted','true');const r=await api('/api/journeys/'+S.journey+'/esign',{method:'POST',body:form});toast('Demo signature recorded for quote '+r.signed_quote)});
  bind('#share-partner',async()=>{if(!guard())return;const form=new FormData();form.set('accepted','true');await api('/api/journeys/'+S.journey+'/consent/partner',{method:'POST',body:form});toast('Partner sharing enabled for this journey.')});
  bind('#withdraw-partner',async()=>{if(!guard())return;await api('/api/journeys/'+S.journey+'/consent/partner',{method:'DELETE'});$('#handoff-permission').checked=false;if(S.partnerKey)await refreshStaff();toast('Partner sharing withdrawn and shared handoffs erased.')});
  bind('#staff-unlock',async()=>{S.partnerKey=$('#partner-key').value;try{await refreshStaff()}catch(e){lockDesk();throw e}});
  bind('#staff-lock',lockDesk);
  $('#refresh-propensity').onclick=()=>refreshStaff().catch(e=>toast(e.message,true));
  const Speech=window.SpeechRecognition||window.webkitSpeechRecognition;
  let recognizer;
  $('#voice-opt').disabled=!Speech;
  if(!Speech)$('#voice-opt').parentElement.append(' Voice input is unavailable in this browser.');
  $('#voice-opt').onchange=()=>{if(!$('#voice-opt').checked)recognizer?.abort();$('#dictate').disabled=!$('#voice-opt').checked};
  bind('#dictate',async()=>{
    if(!Speech||!$('#voice-opt').checked)return;
    recognizer=new Speech();recognizer.lang=$('#language').value+'-IN';
    recognizer.onresult=event=>{$('#chat-input').value=event.results[0][0].transcript};
    recognizer.onerror=event=>toast('Dictation: '+event.error,true);
    recognizer.onend=()=>{$('#dictate').disabled=!$('#voice-opt').checked};
    $('#dictate').disabled=true;recognizer.start();
  });
  $('#chat-form').addEventListener('submit',()=>{if(S.journey)$('#mode-label').textContent='Waiting for local advisor…'});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)recognizer?.abort()});
  document.addEventListener('click',event=>{if(event.target.closest('#nav') && !$('#view-advisor').classList.contains('active'))recognizer?.abort()});
  restoreSession();
});

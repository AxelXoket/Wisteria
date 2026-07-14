/* index.html'deki kurtarma bootstrap'i icin: bu bayrak yoksa app.js yuklenememis
   demektir ve script etiketi yeniden denenir (soguk ilk aciliste yerel sunucudan
   tek seferlik yukleme hatasi gozlemlendi - basarisiz script tag kendini asla
   yeniden denemez, sayfa sonsuza dek "Model yukleniyor"da kalirdi). */
window.__appBooted=true;

/* ===== HAREKETLI DOTLAR (parametreler buradan) ===== */
const CFG = { count:150, minR:1.9, maxR:4.4, speedX:[0.09,0.38], driftY:0.26, minA:0.20, maxA:0.56 };
const canvas = document.getElementById('bg'), ctx = canvas.getContext('2d');
let W, H, dots; const rand = (a,b)=>a+Math.random()*(b-a);
const dotColor = a=>`rgba(${getComputedStyle(document.documentElement).getPropertyValue('--dot').trim()}, ${a})`;
function resize(){ W=canvas.width=innerWidth; H=canvas.height=innerHeight; }
function mk(fr){ return { x:fr?W+rand(0,40):rand(0,W), y:rand(0,H), r:rand(CFG.minR,CFG.maxR),
  vx:-rand(CFG.speedX[0],CFG.speedX[1]), phase:rand(0,Math.PI*2), amp:rand(0,CFG.driftY), a:rand(CFG.minA,CFG.maxA) }; }
function initDots(){ resize(); dots=Array.from({length:CFG.count},()=>mk(false)); }
function frame(t){ ctx.clearRect(0,0,W,H);
  for(let i=0;i<dots.length;i++){ const d=dots[i]; d.x+=d.vx;
    const y=d.y+Math.sin(t*0.0006+d.phase)*d.amp*30; let e=1;
    if(d.x<70)e=Math.max(0,d.x/70); if(d.x<-12){dots[i]=mk(true);continue;}
    ctx.beginPath(); ctx.arc(d.x,y,d.r,0,6.283); ctx.fillStyle=dotColor(d.a*e); ctx.fill(); }
  requestAnimationFrame(frame); }
addEventListener('resize',resize); initDots(); requestAnimationFrame(frame);

/* ===== DOM ===== */
const messages=document.getElementById('messages'), text=document.getElementById('text'), send=document.getElementById('send');
const attach=document.getElementById('attach'), file=document.getElementById('file');
const stage=document.getElementById('stage'), drop=document.getElementById('drop'), composer=document.getElementById('composer');
const lb=document.getElementById('lb'), lbimg=document.getElementById('lbimg');
const overlay=document.getElementById('overlay'), overlayTitle=document.getElementById('overlayTitle'), overlayDetail=document.getElementById('overlayDetail');
const liveDot=document.getElementById('liveDot'), statusText=document.getElementById('statusText'), charName=document.getElementById('charName');
let staged=null, busy=false, curAi=null, curRaw='', curTyping=null, curNote=null, ready=false;
/* Sol ustteki charName MARKA adidir (Wisteria) - hic degismez. Sohbet mesajlarindaki
   yazar etiketi ise AKTIF KARAKTERI izler; o ayri tutulur: */
let aiName='Wisteria';

/* ===== yardimcilar ===== */
function esc(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function renderContent(t){ // *narrasyon* -> span; guvenli
  return esc(t).replace(/\*([^*]+)\*/g,'<span class="narration">*$1*</span>'); }
/* Akilli kaydirma: kullanici dipten uzaktaysa (okuma modunda) asla asagi CEKME;
   ok belirir, akis devam ediyorsa neon nabiz atar. force=true (kendi mesajini
   gonderme ani) her zaman dibe iner. */
const jumpDown=document.getElementById('jumpDown');
function nearBottom(){ return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 160; }
function scrollDown(force){
  if(force || nearBottom()){ messages.scrollTop=messages.scrollHeight; jumpDown.classList.remove('show','pulse'); }
  else { jumpDown.classList.add('show'); if(busy) jumpDown.classList.add('pulse'); }
}
messages.addEventListener('scroll',()=>{
  if(nearBottom()) jumpDown.classList.remove('show','pulse');
  else jumpDown.classList.add('show');
});
jumpDown.onclick=()=>{
  /* kisa el yapimi ease (native smooth guvenilmez cikti); rAF kisitlanirsa bile
     asagidaki zamanlayici DIBE OTURMAYI garantiler */
  const from=messages.scrollTop, t0=performance.now(), dur=280;
  (function step(t){
    const k=Math.min(1,(t-t0)/dur), e=1-Math.pow(1-k,3);
    messages.scrollTop=from+((messages.scrollHeight-messages.clientHeight)-from)*e;
    if(k<1) requestAnimationFrame(step);
  })(t0);
  setTimeout(()=>{ messages.scrollTop=messages.scrollHeight; },dur+90);
  jumpDown.classList.remove('show','pulse');
};
function openLightbox(url){ lbimg.src=url; lb.classList.add('show'); }

function addUserMessage(t, imgUrl){
  const el=document.createElement('div'); el.className='msg user';
  if(t) el.textContent=t;
  if(imgUrl){ const im=document.createElement('img'); im.className='msg-img'; im.src=imgUrl;
    im.onclick=()=>openLightbox(imgUrl); if(t) el.appendChild(document.createElement('br')); el.appendChild(im); }
  messages.appendChild(el); scrollDown(true);  // kendi mesajin = dibe inme niyeti
}

/* ===== streaming (Python -> JS) ===== */
window.appNote=function(txt){ clearNote();
  curNote=document.createElement('div'); curNote.className='note'; curNote.textContent=txt;
  messages.appendChild(curNote); scrollDown(); };
function clearNote(){ if(curNote){ curNote.remove(); curNote=null; } }
function noteFlash(txt){ window.appNote(txt); const el=curNote;   // gecici not: 3sn sonra kaybolur
  setTimeout(()=>{ if(el&&el.parentNode) el.remove(); if(curNote===el) curNote=null; },3000); }
window.appStreamStart=function(){ clearNote(); resetSpk(); // yeni tur = sunucu tarafinda barge-in
  curAi=document.createElement('div'); curAi.className='msg ai';
  const row=document.createElement('div'); row.className='name-row';
  const nm=document.createElement('span'); nm.className='name'; nm.textContent=aiName; row.appendChild(nm);
  const sp=document.createElement('button'); sp.className='spk'; sp.title='Bu mesaji seslendir';
  sp.innerHTML=SPK_SVG; row.appendChild(sp);
  const msgEl=curAi;  // kopyalama, akis bittikten sonra da DOGRU mesaji okusun
  const cp=document.createElement('button'); cp.className='spk cpy'; cp.title='Mesaji kopyala';
  cp.innerHTML=COPY_SVG;
  cp.onclick=async()=>{
    const raw=msgEl.dataset.raw || (msgEl.querySelector('.body')?msgEl.querySelector('.body').innerText:'');
    if(!raw) return;
    try{ await navigator.clipboard.writeText(raw); }catch(e){ return; }
    cp.innerHTML=CHECK_SVG; cp.classList.add('playing');           // kisa onay: spk'nin aktif dili
    setTimeout(()=>{ cp.innerHTML=COPY_SVG; cp.classList.remove('playing'); },1100);
  };
  row.appendChild(cp);
  curAi.appendChild(row);
  const body=document.createElement('span'); body.className='body'; curAi.appendChild(body);
  messages.appendChild(curAi);
  curTyping=document.createElement('div'); curTyping.className='typing'; curTyping.innerHTML='<span></span><span></span><span></span>';
  messages.appendChild(curTyping); curRaw=''; scrollDown(); };
window.appStream=function(chunk){ if(!curAi) return; if(curTyping){ curTyping.remove(); curTyping=null; }
  curRaw+=chunk; curAi.querySelector('.body').innerHTML=renderContent(curRaw); scrollDown(); };
window.appStreamEnd=function(data){ if(curTyping){ curTyping.remove(); curTyping=null; }
  if(curAi){ const fin=(data&&data.final)||curRaw;
    curAi.querySelector('.body').innerHTML=renderContent(fin);
    curAi.dataset.raw=fin; }  // per-mesaj seslendirme icin ham metin
  curAi=null; curRaw=''; setBusy(false); scrollDown(); };
window.appSources=function(list){ if(!list||!list.length) return;
  const wrap=document.createElement('div'); wrap.style.marginTop='6px';
  list.forEach(s=>{ const c=document.createElement('span'); c.className='source'; c.textContent='◆ '+(s.domain||''); wrap.appendChild(c); });
  (curAi||messages.lastElementChild||messages).appendChild(wrap); scrollDown(); };

/* ===== gonderme ===== */
const SEND_SVG=send.innerHTML;                    // ok isareti (bosta)
const CANCEL_SVG='<svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="1.5"/></svg>';
function setBusy(b){ busy=b; refreshSend(); }
function refreshSend(){
  if(busy){ send.disabled=!ready; send.innerHTML=CANCEL_SVG; send.title='Durdur'; }
  else { send.innerHTML=SEND_SVG; send.title='Gonder';
    send.disabled = !ready || !(text.value.trim()||staged); }
}
text.addEventListener('input',refreshSend);
async function doSend(){
  if(!ready) return;
  if(busy){ try{ await window.pywebview.api.cancel_gen(); }catch(e){} return; }  // durdur
  const t=text.value.trim(); const img=staged?staged.dataUrl:null;
  if(!t && !img) return;
  addUserMessage(t,img); clearStage(); text.value=''; setBusy(true);
  let r; try{ r=await window.pywebview.api.send(t, img); }catch(e){ r={ok:false}; }
  if(!r||!r.ok){                                   // erken ret: composer'i asla kilitleme
    setBusy(false);
    if(r&&r.error==='locked') window.appNote&&window.appNote('Önce kilidi açman gerekiyor.');
    else if(r&&r.error==='busy'){ /* onceki tur hala kapaniyor; tekrar dene */ }
  }
}
send.onclick=doSend;
text.addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); doSend(); } });

/* ===== gorsel ekleme ===== */
function stageImage(f){ const r=new FileReader(); r.onload=()=>{ staged={dataUrl:r.result,name:f.name};
  stage.innerHTML=`<div class="chip"><img src="${r.result}"><span class="fn"></span><button class="rm">&times;</button></div>`;
  stage.querySelector('.fn').textContent=f.name; stage.classList.add('show');
  stage.querySelector('img').onclick=()=>openLightbox(r.result);
  stage.querySelector('.rm').onclick=clearStage; refreshSend(); }; r.readAsDataURL(f); }
function clearStage(){ staged=null; stage.classList.remove('show'); stage.innerHTML=''; refreshSend(); }
attach.onclick=()=>file.click();
file.onchange=e=>{ if(e.target.files[0]) stageImage(e.target.files[0]); };
const chat=document.getElementById('chat');
['dragenter','dragover'].forEach(ev=>chat.addEventListener(ev,e=>{ e.preventDefault(); drop.classList.add('show'); }));
['dragleave','drop'].forEach(ev=>chat.addEventListener(ev,e=>{ e.preventDefault();
  if(ev==='drop'){ const f=e.dataTransfer.files[0]; if(f&&f.type.startsWith('image/')) stageImage(f); }
  if(ev==='dragleave'&&e.relatedTarget&&chat.contains(e.relatedTarget)) return; drop.classList.remove('show'); }));
addEventListener('paste',e=>{ const it=[...(e.clipboardData?.items||[])].find(i=>i.type.startsWith('image/'));
  if(it) stageImage(it.getAsFile()); });

/* ===== lightbox ===== */
document.getElementById('lbclose').onclick=()=>lb.classList.remove('show');
lb.onclick=e=>{ if(e.target===lb) lb.classList.remove('show'); };
/* Esc onceligi: menu -> modal -> lightbox (tek handler) */
addEventListener('keydown',e=>{ if(e.key!=='Escape') return;
  if(ncPop.classList.contains('show')){ ncPopHide(); return; }
  if(menu.classList.contains('show')){ menu.classList.remove('show'); return; }
  if(modal.classList.contains('show')){ closeModal(); return; }
  lb.classList.remove('show'); });

/* ===== yeni sohbet (onayli - yanlis tik sohbeti kaybettirmesin) ===== */
const ncPop=document.getElementById('ncPop'), newChatBtn=document.getElementById('newChat');
function ncPopHide(){ ncPop.classList.remove('show'); }
function ncPopShow(){ if(busy) return; menu.classList.remove('show'); ncPop.classList.add('show'); }
newChatBtn.onclick=(e)=>{ e.stopPropagation();
  if(ncPop.classList.contains('show')) ncPopHide(); else ncPopShow(); };
document.getElementById('ncYes').onclick=async()=>{ ncPopHide();
  try{ await window.pywebview.api.new_chat(); }catch(e){}
  messages.innerHTML=''; jumpDown.classList.remove('show','pulse'); };
document.getElementById('ncNo').onclick=ncPopHide;
addEventListener('click',e=>{ if(ncPop.classList.contains('show') && !ncPop.contains(e.target)) ncPopHide(); });
addEventListener('keydown',e=>{ if((e.ctrlKey||e.metaKey)&&(e.key==='n'||e.key==='N')){
  e.preventDefault(); ncPopShow(); } });  // Ctrl+N = ayni onayli akis

/* ===== genel API kopruleri (pywebview yoksa mock: tarayici onizlemesi) ===== */
const api=()=>(window.pywebview&&window.pywebview.api&&window.pywebview.api.tts_status)?window.pywebview.api:MOCK_API;

/* ===== otomatik seslendirme (header ikonu = auto-speak modu) ===== */
const ttsBtn=document.getElementById('ttsBtn');
let ttsOn=false, ttsPoll=null;
function applyTts(st){
  ttsOn=!!(st&&st.auto);
  ttsBtn.classList.remove('on','off','loading','unavail');
  let title;
  if(st && st.state==='loading'){ ttsBtn.classList.add('loading'); title='Ses motoru yukleniyor...'; }
  else if(st && (st.state==='unavailable'||st.state==='error')){
    ttsBtn.classList.add('unavail');
    title=(st.state==='unavailable'?'Ses kullanilamiyor: ':'Ses hatasi: ')+(st.detail||'');
  }
  else{ ttsBtn.classList.add(ttsOn?'on':'off');
    title=ttsOn?'Otomatik seslendirme acik':'Otomatik seslendirme kapali (mesaj basina ▸ ile okutabilirsin)'; }
  ttsBtn.title=title;
}
/* Kendi kendini yoneten tazeleme: 'loading' gorurse yoklamayi KURAR (eskiden sadece
   kapatiyordu - motor gec yuklenmeye baslayinca dugme sonsuza dek yanip sonuyordu),
   yukleme bitince kapatir. Boylece hangi yoldan cagrilirsa cagrilsin dogru calisir. */
async function refreshTts(){ try{ const st=await api().tts_status(); applyTts(st);
  if(st.state==='loading'){ if(!ttsPoll) ttsPoll=setInterval(refreshTts,1500); }
  else if(ttsPoll){ clearInterval(ttsPoll); ttsPoll=null; } }catch(e){} }
function startTtsPoll(){ refreshTts(); if(!ttsPoll) ttsPoll=setInterval(refreshTts,1500); }
ttsBtn.onclick=async()=>{
  try{ const st=await api().set_tts_enabled(!ttsOn); applyTts(st);
    if(st.state==='loading'&&!ttsPoll){ ttsPoll=setInterval(refreshTts,1500); }
  }catch(e){} };

/* ===== mesaj basina seslendirme (▸ / durdur) ===== */
const SPK_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" fill="currentColor" stroke="none"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>';
const STOP_SVG='<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>';
const COPY_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const CHECK_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
let playingBtn=null, spkPoll=null;
function resetSpk(){ if(spkPoll){ clearInterval(spkPoll); spkPoll=null; }
  if(playingBtn){ playingBtn.classList.remove('playing'); playingBtn.innerHTML=SPK_SVG;
    playingBtn.title='Bu mesaji seslendir'; playingBtn=null; } }
async function toggleSpeak(btn, raw){
  if(btn===playingBtn){ try{ await api().stop_speaking(); }catch(e){} resetSpk(); return; }
  resetSpk();
  let r; try{ r=await api().speak_message(raw||''); }catch(e){ r={ok:false}; }
  if(!r||!r.ok) return;                    // motor hazir degilse sessiz gec (tooltip ikonda)
  playingBtn=btn; btn.classList.add('playing'); btn.innerHTML=STOP_SVG; btn.title='Durdur';
  let ticks=0;                              // emniyet: ~1 dk sonra gostergeyi birak
  spkPoll=setInterval(async()=>{ let st; try{ st=await api().tts_status(); }catch(e){ st=null; }
    if(!st||!st.speaking||++ticks>75) resetSpk(); },800);
}
messages.addEventListener('click',e=>{
  const btn=e.target.closest('.spk'); if(!btn) return;
  const msg=btn.closest('.msg.ai'); if(!msg) return;
  toggleSpeak(btn, msg.dataset.raw||'');
});

/* ===== uc-nokta menusu ===== */
const menu=document.getElementById('menu'), menuBtn=document.getElementById('menuBtn');
menuBtn.onclick=e=>{ e.stopPropagation(); menu.classList.toggle('show'); };
document.addEventListener('click',e=>{
  if(menu.classList.contains('show') && !menu.contains(e.target) && e.target!==menuBtn && !menuBtn.contains(e.target))
    menu.classList.remove('show');
});
menu.querySelectorAll('.mi').forEach(mi=>{ mi.onclick=()=>{ menu.classList.remove('show');
  if(mi.dataset.modal) openModal(mi.dataset.modal);
  else if(mi.dataset.action==='exportChat') exportChat(); }; });
async function exportChat(){
  let r; try{ r=await api().export_chat(); }catch(e){ r=null; }
  if(r&&r.ok) noteFlash('Sohbet kaydedildi: '+r.path);
  else if(r&&r.error==='empty') noteFlash('Aktarılacak sohbet yok.');
  else if(r&&r.error==='cancelled'){ /* sessiz */ }
  else noteFlash('Sohbet dışa aktarılamadı.');
}

/* ===== modal cercevesi ===== */
const modal=document.getElementById('modal'), modalTitle=document.getElementById('modalTitle');
const MODAL_TITLES={memory:'Hafıza', prompts:'Promptlar', voice:'Ses ayarları', text:'Yazı ayarları', bg:'Sohbet arka planı'};
let modalOpen='';
function openModal(which){
  modalOpen=which;
  modalTitle.textContent=MODAL_TITLES[which]||'';
  document.querySelectorAll('.mpane').forEach(p=>p.classList.remove('show'));
  const pane={memory:'mMemory',prompts:'mPrompts',voice:'mVoice',text:'mText',bg:'mBg'}[which];
  if(pane) document.getElementById(pane).classList.add('show');
  modal.classList.add('show');
  if(which==='memory'){ loadMemory(); startMemPoll(); }
  else { stopMemPoll();
    if(which==='prompts') loadPrompts();
    else if(which==='voice') loadVoice();
    else if(which==='text') loadTextPrefs();
    else if(which==='bg') bgPaneOpen(); }
}
function closeModal(){
  if(modalOpen==='prompts' && P.dirty){
    P.pendingSwitch={close:true};            // kapama niyetini KAYBETME (bar karari sonrasi kapanir)
    pdirty.classList.add('show'); return;
  }
  stopMemPoll();
  modal.classList.remove('show'); modalOpen='';
}
document.getElementById('modalClose').onclick=closeModal;
modal.addEventListener('click',e=>{ if(e.target===modal) closeModal(); });

/* ===== Hafiza paneli ===== */
const memLoad=document.getElementById('memLoad'), memRecap=document.getElementById('memRecap'),
  memFacts=document.getElementById('memFacts'), memEpisodes=document.getElementById('memEpisodes'),
  memEpiCount=document.getElementById('memEpiCount'), memFoot=document.getElementById('memFoot');
function fmtTs(ts){ if(!ts||isNaN(+ts)) return '';
  try{ return new Date(ts*1000).toLocaleDateString('tr-TR',{day:'numeric',month:'long',year:'numeric'}); }catch(e){ return ''; } }
async function loadMemory(){
  // yeniden cizim scroll konumunu SIFIRLAMASIN (unut sonrasi en uste ziplama)
  const mbody=document.querySelector('.modal-body'); const mscroll=mbody?mbody.scrollTop:0;
  memLoad.style.display='block'; memRecap.textContent=''; memFacts.innerHTML='';
  memEpisodes.innerHTML=''; memFoot.textContent=''; memEpiCount.textContent='0';
  let r; try{ r=await api().memory_overview(); }catch(e){ r=null; }
  memLoad.style.display='none';
  if(!r||!r.ok){ memRecap.textContent = (r&&r.error==='locked')?'Önce kilidi açman gerekiyor.':'Hafıza okunamadı.'; return; }
  memRecap.textContent = r.recap || 'Henüz bir özet yok - konuştukça oluşur.';
  if(!r.facts||!r.facts.length){
    const li=document.createElement('li'); li.className='mem-empty'; li.textContent='Henüz kayıtlı bilgi yok.';
    memFacts.appendChild(li);
  } else r.facts.forEach(f=>memFacts.appendChild(renderFact(f)));
  memEpiCount.textContent=(r.episodes||[]).length;
  (r.episodes||[]).forEach(ep=>{ const li=document.createElement('li');
    li.textContent=ep.text; const ts=document.createElement('span'); ts.className='ets';
    ts.textContent=fmtTs(ep.ts); li.appendChild(ts); memEpisodes.appendChild(li); });
  memFoot.textContent=(r.message_count||0)+' mesaj hatırlanıyor.';
  applyMemFilter();
  if(mbody) mbody.scrollTop=mscroll;  // kullanicinin kaldigi yer
}

/* Canli hafiza: panel acikken 3 sn'de bir sessiz tazeleme. DOM YERINDE uzlastirilir
   (ozet metni + silinen satir kalkar, yeni gelen eklenir, onem noktalari guncellenir) -
   spinner yok, komple yeniden cizim yok, scroll ve acik onay satiri korunur. */
let memPollIv=null;
function startMemPoll(){ stopMemPoll(); memPollIv=setInterval(refreshMemoryQuiet, 3000); }
function stopMemPoll(){ if(memPollIv){ clearInterval(memPollIv); memPollIv=null; } }
function factDots(el, importance){
  const on=Math.max(0,Math.min(5,Math.round((importance||0)/2)));
  if(el.querySelectorAll('i.on').length===on) return;
  el.innerHTML='';
  for(let i=0;i<5;i++){ const d=document.createElement('i'); if(i<on) d.className='on'; el.appendChild(d); }
}
async function refreshMemoryQuiet(){
  if(modalOpen!=='memory'){ stopMemPoll(); return; }  // emniyet: kacak interval kendini oldurur
  let r; try{ r=await api().memory_overview(); }catch(e){ return; }
  if(!r||!r.ok) return;
  const recap=r.recap||'Henüz bir özet yok - konuştukça oluşur.';
  if(memRecap.textContent!==recap) memRecap.textContent=recap;
  const byId=new Map((r.facts||[]).map(f=>[String(f.id),f]));
  [...memFacts.querySelectorAll('li.fact')].forEach(li=>{
    if(li.classList.contains('editing')){ byId.delete(li.dataset.id); return; }  // form acik - dokunma
    const f=byId.get(li.dataset.id);
    if(!f){ li.remove(); return; }                       // db'den gitmis - satir kalkar
    byId.delete(li.dataset.id);
    const tx=li.querySelector('.tx'); if(tx&&tx.textContent!==f.text) tx.textContent=f.text;
    const ch=li.querySelector('.chip-t'); const ty=f.type||'bilgi';
    if(ch&&ch.textContent!==ty) ch.textContent=ty;
    li.dataset.imp=f.importance||0;
    const dt=li.querySelector('.dots'); if(dt) factDots(dt, f.importance);
  });
  if(byId.size){                                         // yeni konsolide olanlar sona
    const empty=memFacts.querySelector('.mem-empty'); if(empty) empty.remove();
    byId.forEach(f=>memFacts.appendChild(renderFact(f)));
  }
  if(!memFacts.children.length){
    const li=document.createElement('li'); li.className='mem-empty';
    li.textContent='Henüz kayıtlı bilgi yok.'; memFacts.appendChild(li);
  }
  memEpiCount.textContent=(r.episodes||[]).length;
  memFoot.textContent=(r.message_count||0)+' mesaj hatırlanıyor.';
  applyMemFilter();  // yeni gelen satirlar da mevcut suzmeye uysun
}

function renderFact(f){
  const li=document.createElement('li'); li.className='fact'; li.dataset.id=f.id;
  li.dataset.imp=f.importance||0;  // duzenleme formu icin kayipsiz onem (dots yuvarlanmis)
  const row=document.createElement('div'); row.className='frow';
  const chip=document.createElement('span'); chip.className='chip-t'; chip.textContent=f.type||'bilgi'; row.appendChild(chip);
  const tx=document.createElement('span'); tx.className='tx'; tx.textContent=f.text; row.appendChild(tx);
  const dots=document.createElement('span'); dots.className='dots';
  factDots(dots, f.importance);
  row.appendChild(dots);
  const ed=document.createElement('button'); ed.className='edt'; ed.title='Düzenle';
  ed.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';
  ed.onclick=()=>toggleFactEdit(li); row.appendChild(ed);
  const del=document.createElement('button'); del.className='del'; del.innerHTML='&times;'; del.title='Unut';
  del.onclick=()=>li.classList.toggle('confirm'); row.appendChild(del);
  li.appendChild(row);
  const conf=document.createElement('div'); conf.className='fact-confirm';
  conf.innerHTML='<span>Bunu unutsun mu?</span>';
  const yes=document.createElement('button'); yes.className='yes'; yes.textContent='Unut';
  yes.onclick=async()=>{ try{ await api().memory_delete_fact(f.id); }catch(e){}
    refreshMemoryQuiet(); };  // yerinde kaldir: spinner yok, scroll ziplamasi yok
  const no=document.createElement('button'); no.className='no'; no.textContent='Vazgeç';
  no.onclick=()=>li.classList.remove('confirm');
  conf.appendChild(yes); conf.appendChild(no); li.appendChild(conf);
  return li;
}

/* bilgi duzenleme: METIN KUTUSUNUN KENDISI duzenlenir (kopya input yok) -
   .tx contenteditable olur, kac satirsa icinde gezinilir; altta yalniz
   Onem + Kaydet/Vazgec kalir. Acikken sessiz uzlastirma satiri atlar. */
function factEditClose(li, restore){
  const tx=li.querySelector('.tx');
  if(restore && li.dataset.orig!==undefined) tx.textContent=li.dataset.orig;
  delete li.dataset.orig;
  tx.contentEditable='false'; tx.removeAttribute('contenteditable');
  li.classList.remove('editing');
  const box=li.querySelector('.fact-edit'); if(box) box.remove();
}
function toggleFactEdit(li){
  if(li.classList.contains('editing')){ factEditClose(li, true); return; }
  li.classList.remove('confirm');
  li.classList.add('editing');
  const tx=li.querySelector('.tx');
  li.dataset.orig=tx.textContent;
  try{ tx.contentEditable='plaintext-only'; }catch(e){ tx.contentEditable='true'; }
  const box=document.createElement('div'); box.className='fact-edit';
  const r2=document.createElement('div'); r2.className='fadd-row2';
  const lbl=document.createElement('span'); lbl.className='vl'; lbl.textContent='Önem'; r2.appendChild(lbl);
  const rng=document.createElement('input'); rng.type='range'; rng.className='vrange';
  rng.min='1'; rng.max='10'; rng.step='1'; rng.value=String(+li.dataset.imp||7); r2.appendChild(rng);
  const rv=document.createElement('span'); rv.className='vval'; rv.textContent=rng.value; r2.appendChild(rv);
  rng.oninput=()=>{ rv.textContent=rng.value; };
  const ok=document.createElement('button'); ok.className='fadd-ok'; ok.textContent='Kaydet';
  ok.onclick=async()=>{
    const t=tx.innerText.trim(); if(!t){ tx.focus(); return; }
    let r; try{ r=await api().memory_update_fact(+li.dataset.id, t, +rng.value); }catch(e){ r=null; }
    if(r&&r.ok){ factEditClose(li, false); refreshMemoryQuiet(); }
  };
  r2.appendChild(ok);
  const no2=document.createElement('button'); no2.className='fadd-no'; no2.textContent='Vazgeç';
  no2.onclick=()=>factEditClose(li, true);
  r2.appendChild(no2);
  box.appendChild(r2); li.appendChild(box);
  tx.addEventListener('keydown',function esc(e){
    if(e.key==='Escape'){ e.stopPropagation(); factEditClose(li, true); tx.removeEventListener('keydown',esc); }
  });
  tx.focus();
  const sel=window.getSelection(), rgn=document.createRange();  // imlec metnin sonuna
  rgn.selectNodeContents(tx); rgn.collapse(false); sel.removeAllRanges(); sel.addRange(rgn);
}

/* ===== elle bilgi ekleme + arama ===== */
const memSearch=document.getElementById('memSearch'), memAddBtn=document.getElementById('memAddBtn'),
  memAddRow=document.getElementById('memAddRow'), faddType=document.getElementById('faddType'),
  faddText=document.getElementById('faddText'), faddImp=document.getElementById('faddImp'),
  faddImpVal=document.getElementById('faddImpVal');
let memFilter='';
memSearch.addEventListener('input',()=>{ memFilter=memSearch.value.trim().toLowerCase(); applyMemFilter(); });
function applyMemFilter(){
  [...memFacts.querySelectorAll('li.fact')].forEach(li=>{
    if(li.classList.contains('editing')){ li.style.display=''; return; }  // duzenlenen asla gizlenmez
    if(!memFilter){ li.style.display=''; return; }
    const tx=li.querySelector('.tx'), ch=li.querySelector('.chip-t');
    const hay=((tx?tx.textContent:'')+' '+(ch?ch.textContent:'')).toLowerCase();
    li.style.display=hay.includes(memFilter)?'':'none';
  });
}
faddImp.addEventListener('input',()=>{ faddImpVal.textContent=faddImp.value; });
memAddBtn.onclick=()=>{ memAddRow.classList.toggle('show');
  if(memAddRow.classList.contains('show')) faddText.focus(); };
document.getElementById('faddNo').onclick=()=>{ memAddRow.classList.remove('show'); };
document.getElementById('faddOk').onclick=async()=>{
  const t=faddText.value.trim();
  if(!t){ faddText.focus(); return; }
  let r; try{ r=await api().memory_add_fact(faddType.value, t, +faddImp.value); }catch(e){ r=null; }
  if(r&&r.ok){ faddText.value=''; faddText.style.borderColor='';
    memAddRow.classList.remove('show'); refreshMemoryQuiet(); }
  else faddText.style.borderColor='var(--accent-deep)';
};
faddText.addEventListener('keydown',e=>{ if(e.key==='Enter') document.getElementById('faddOk').click();
  if(e.key==='Escape') memAddRow.classList.remove('show'); });

/* ===== Promptlar paneli ===== */
const pnames=document.getElementById('pnames'), promptText=document.getElementById('promptText'),
  pdirty=document.getElementById('pdirty'), pmsg=document.getElementById('pmsg');
const P={kinds:{system:[],character:[],persona:[]}, active:{}, kind:'system', name:'', loadedText:'', dirty:false, pendingSwitch:null};
function pmsgShow(t, ok){ pmsg.textContent=t; pmsg.className='pmsg '+(ok?'ok':'err');
  setTimeout(()=>{ if(pmsg.textContent===t) pmsg.textContent=''; },1800); }
async function loadPrompts(){
  if(P.dirty){ renderNames(); return; } // kaydedilmemis metni yeniden yukleyip EZME
  let r; try{ r=await api().prompts_list(); }catch(e){ r=null; }
  if(!r||!r.ok){ promptText.value=''; promptText.placeholder=(r&&r.error==='locked')?'Önce kilidi açman gerekiyor.':'Okunamadı.';
    promptText.disabled=true; return; }
  promptText.disabled=false;
  P.kinds=r.kinds; P.active=r.active||{};
  await switchTab(P.kind||'system', true);
}
document.querySelectorAll('.ptab').forEach(tb=>{ tb.onclick=()=>switchTab(tb.dataset.kind,false); });
async function switchTab(kind, force){
  if(P.dirty && !force){ P.pendingSwitch={kind:kind,name:null}; pdirty.classList.add('show'); return; }
  P.kind=kind;
  document.querySelectorAll('.ptab').forEach(t=>t.classList.toggle('active',t.dataset.kind===kind));
  const names=P.kinds[kind]||[];
  P.name = (kind==='system') ? (P.active.system||'system_prompt')
         : (names.includes(P.active[kind]) ? P.active[kind] : (names[0]||''));
  renderNames();
  await loadPromptText();
}
async function switchName(name, force){
  if(P.dirty && !force){ P.pendingSwitch={kind:P.kind,name:name}; pdirty.classList.add('show'); return; }
  P.name=name; renderNames(); await loadPromptText();
}
async function loadPromptText(){
  pdirty.classList.remove('show'); P.pendingSwitch=null;
  if(!P.name){ promptText.value=''; P.loadedText=''; P.dirty=false;
    promptText.placeholder='Henüz yok - "+ Yeni" ile oluştur.'; return; }
  let r; try{ r=await api().prompts_get(P.kind,P.name); }catch(e){ r=null; }
  promptText.value=(r&&r.ok)?r.text:''; P.loadedText=promptText.value; P.dirty=false;
  promptText.placeholder='Boş...';
}
function renderNames(){
  document.querySelectorAll('.pdel-confirm').forEach(e=>e.remove());  // acik onay satiri varsa kapat
  if(P.kind==='system'){ pnames.classList.remove('show'); pnames.innerHTML=''; return; }
  pnames.classList.add('show'); pnames.innerHTML='';
  (P.kinds[P.kind]||[]).forEach(n=>{
    const b=document.createElement('button'); b.className='pname';
    const lbl=document.createElement('span'); lbl.textContent=n; b.appendChild(lbl);
    if(n===P.name) b.classList.add('sel');
    if(n===P.active[P.kind]) b.classList.add('act');
    b.onclick=()=>switchName(n,false);
    if(n===P.name){  // araclar yalniz secili cipte gorunur: once sec, sonra duzenle
      const ren=document.createElement('span'); ren.className='pact ren'; ren.title='Yeniden adlandır';
      ren.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';
      ren.onclick=(e)=>{ e.stopPropagation(); showRenameInput(b,n); };
      const del=document.createElement('span'); del.className='pact del'; del.title='Sil';
      del.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
      del.onclick=(e)=>{ e.stopPropagation(); showDeleteConfirm(n); };
      b.appendChild(ren); b.appendChild(del);
    }
    pnames.appendChild(b);
  });
  const nw=document.createElement('button'); nw.className='pname new'; nw.textContent='+ Yeni';
  nw.onclick=()=>showNewInput(nw);
  pnames.appendChild(nw);
  if(P.name && P.name!==P.active[P.kind]){
    const ab=document.createElement('button'); ab.className='pactive-btn'; ab.textContent='Aktif yap';
    ab.onclick=async()=>{ let r; try{ r=await api().prompts_set_active(P.kind,P.name); }catch(e){ r=null; }
      if(r&&r.ok){ P.active[P.kind]=P.name; renderNames(); pmsgShow('Aktif: '+P.name,true);
        if(P.kind==='character') refreshCharName(); }  // sohbetteki yazar adi aktif karakteri izler
      else pmsgShow('Aktif yapılamadı.',false); };
    pnames.appendChild(ab);
  }
}
function showNewInput(anchor){
  const inp=document.createElement('input'); inp.className='pnew-input'; inp.placeholder='isim (küçük harf)';
  inp.maxLength=40;
  inp.oninput=()=>{ inp.value=inp.value.toLowerCase().replace(/[^a-z0-9_-]/g,''); };
  inp.onkeydown=async e=>{
    if(e.key==='Escape'){ inp.replaceWith(anchor); return; }
    if(e.key!=='Enter') return;
    const nm=inp.value.trim(); if(!nm) return;
    let r; try{ r=await api().prompts_create(P.kind,nm); }catch(err){ r=null; }
    if(r&&r.ok){ P.kinds[P.kind].push(r.name); P.kinds[P.kind].sort();
      inp.replaceWith(anchor); await switchName(r.name,true); pmsgShow('Oluşturuldu: '+r.name,true); }
    else pmsgShow(r&&r.error==='exists'?'Bu isim zaten var.':'Oluşturulamadı.',false);
  };
  anchor.replaceWith(inp); inp.focus();
}
function showRenameInput(anchor, oldName){
  const inp=document.createElement('input'); inp.className='pnew-input'; inp.value=oldName;
  inp.maxLength=40;
  inp.oninput=()=>{ inp.value=inp.value.toLowerCase().replace(/[^a-z0-9_-]/g,''); };
  inp.onkeydown=async e=>{
    if(e.key==='Escape'){ renderNames(); return; }
    if(e.key!=='Enter') return;
    const nm=inp.value.trim(); if(!nm) return;
    if(nm===oldName){ renderNames(); return; }
    let r; try{ r=await api().prompts_rename(P.kind,oldName,nm); }catch(err){ r=null; }
    if(r&&r.ok){
      P.kinds[P.kind]=P.kinds[P.kind].filter(x=>x!==oldName).concat([r.name]).sort();
      if(P.active[P.kind]===oldName) P.active[P.kind]=r.name;  // meta backend'de tasindi, aynala
      if(P.name===oldName) P.name=r.name;
      renderNames(); pmsgShow('Yeniden adlandırıldı: '+r.name,true);
      if(P.kind==='character') refreshCharName();  // aktif karakterin gorunen adi degismis olabilir
    }
    else pmsgShow(r&&r.error==='exists'?'Bu isim zaten var.':(r&&r.error==='name'?'Geçersiz isim.':'Yeniden adlandırılamadı.'),false);
  };
  anchor.replaceWith(inp); inp.focus(); inp.select();
}
function showDeleteConfirm(name){
  document.querySelectorAll('.pdel-confirm').forEach(e=>e.remove());  // ayni anda tek onay
  const row=document.createElement('div'); row.className='pdel-confirm';
  const t=document.createElement('span'); t.textContent='"'+name+'" kalıcı olarak silinsin mi?'; row.appendChild(t);
  const yes=document.createElement('button'); yes.className='yes'; yes.textContent='Sil';
  yes.onclick=async()=>{
    let r; try{ r=await api().prompts_delete(P.kind,name); }catch(e){ r=null; }
    if(r&&r.ok){
      P.kinds[P.kind]=P.kinds[P.kind].filter(x=>x!==name);
      P.active[P.kind]=r.active||P.kinds[P.kind][0]||'';
      if(P.name===name){ P.name=P.active[P.kind]||(P.kinds[P.kind][0]||''); P.dirty=false; await loadPromptText(); }
      renderNames(); pmsgShow('Silindi: '+name,true);
      if(P.kind==='character') refreshCharName();  // aktif silindiyse devralanin adi gecti
    }
    else pmsgShow(r&&r.error==='last'?'Türün sonuncusu silinemez.':'Silinemedi.',false);
  };
  const no=document.createElement('button'); no.className='no'; no.textContent='Vazgeç';
  no.onclick=()=>row.remove();
  row.appendChild(yes); row.appendChild(no);
  pnames.insertAdjacentElement('afterend', row);
}
promptText.addEventListener('input',()=>{ P.dirty=(promptText.value!==P.loadedText);
  if(!P.dirty) pdirty.classList.remove('show'); });
async function savePrompt(){
  if(!P.name){ pmsgShow('Önce bir isim seç/oluştur.',false); return false; }
  let r; try{ r=await api().prompts_save(P.kind,P.name,promptText.value); }catch(e){ r=null; }
  if(r&&r.ok){ P.loadedText=promptText.value; P.dirty=false; pdirty.classList.remove('show');
    pmsgShow('Kaydedildi ✓',true); return true; }
  pmsgShow(r&&r.error==='too_big'?'Metin çok büyük.':'Kaydedilemedi.',false); return false;
}
document.getElementById('promptSave').onclick=savePrompt;
document.getElementById('pdirtySave').onclick=async()=>{ if(await savePrompt()) resumePending(); };
document.getElementById('pdirtyDrop').onclick=()=>{ P.dirty=false; pdirty.classList.remove('show'); resumePending(); };
function resumePending(){
  const p=P.pendingSwitch; P.pendingSwitch=null;
  if(p&&p.close){ modal.classList.remove('show'); modalOpen=''; return; }
  if(p){ if(p.name) switchName(p.name,true); else switchTab(p.kind,true); }
  else if(modalOpen==='prompts') closeModal();  // bar kapama denemesinden acilmisti
}
document.getElementById('promptExport').onclick=async()=>{
  if(!P.name) return;
  let r; try{ r=await api().prompts_export(P.kind,P.name); }catch(e){ r=null; }
  if(r&&r.ok) pmsgShow('Dışa aktarıldı.',true);
  else if(r&&r.error==='cancelled'){ /* sessiz */ }
  else pmsgShow('Aktarılamadı.',false);
};

/* ===== Ses ayarlari paneli ===== */
const vAuto=document.getElementById('vAuto'), vSpeed=document.getElementById('vSpeed'),
  vDenoi=document.getElementById('vDenoi'), vExag=document.getElementById('vExag'),
  vSpeedVal=document.getElementById('vSpeedVal'), vDenoiVal=document.getElementById('vDenoiVal'),
  vExagVal=document.getElementById('vExagVal');
function vReadouts(){ vSpeedVal.textContent=(+vSpeed.value).toFixed(2);
  vDenoiVal.textContent=(+vDenoi.value).toFixed(2); vExagVal.textContent=(+vExag.value).toFixed(2); }
async function loadVoice(){
  let r; try{ r=await api().tts_get_params(); }catch(e){ r=null; }
  if(r&&r.ok){ vSpeed.value=r.speed; vDenoi.value=r.denoise_prop; vExag.value=r.exaggeration;
    vAuto.checked=!!r.auto; }
  vReadouts();
}
let vDeb=null;
function vPush(){ clearTimeout(vDeb); vDeb=setTimeout(async()=>{
  try{ await api().tts_set_params({speed:+vSpeed.value, denoise_prop:+vDenoi.value, exaggeration:+vExag.value}); }catch(e){}
},250); }
[vSpeed,vDenoi,vExag].forEach(sl=>{ sl.addEventListener('input',()=>{ vReadouts(); vPush(); }); });
vAuto.addEventListener('change',async()=>{
  let r; try{ r=await api().tts_set_params({auto:vAuto.checked}); }catch(e){ r=null; }
  if(r&&r.ok) applyTts({auto:r.auto,state:r.state});
});
document.getElementById('vTest').onclick=()=>{
  api().speak_message("Hey, it's me... just testing my voice. Do I sound alright to you?").catch(()=>{});
};

/* ===== Yazi ayarlari paneli =====
   Punto + satir araligi YALNIZCA sohbet mesajlarina uygulanir: degiskenler
   .messages kabina yazilir, .msg onlari okur - kompozer/basik/paneller sabit. */
const tFont=document.getElementById('tFont'), tLine=document.getElementById('tLine'),
  tFontVal=document.getElementById('tFontVal'), tLineVal=document.getElementById('tLineVal');
function applyMsgText(fp,lh){
  messages.style.setProperty('--msg-fs',fp+'px');
  messages.style.setProperty('--msg-lh',String(lh));
}
function tReadouts(){ tFontVal.textContent=(+tFont.value).toFixed(1); tLineVal.textContent=(+tLine.value).toFixed(2); }
let tSaveT=null;
function tPush(){
  tReadouts(); applyMsgText(+tFont.value,+tLine.value);            // canli onizleme
  clearTimeout(tSaveT);
  tSaveT=setTimeout(async()=>{ try{ await api().ui_text_set(+tFont.value,+tLine.value); }catch(e){} },350);
}
tFont.oninput=tPush; tLine.oninput=tPush;
document.getElementById('tReset').onclick=()=>{ tFont.value=15.5; tLine.value=1.62; tPush(); };
async function loadTextPrefs(){
  let r; try{ r=await api().ui_text_get(); }catch(e){ r=null; }
  const fp=(r&&r.ok)?+r.font_px:15.5, lh=(r&&r.ok)?+r.line_height:1.62;
  tFont.value=fp; tLine.value=lh; tReadouts(); applyMsgText(fp,lh);
}

/* ===== Sohbet arka plani =====
   Gorsel kopruden data URI olarak gelir/gider (dosya userdata/'da - rebuild silmez).
   Scrim = tek background yigini: linear-gradient(ton,ton) + gorsel. Metin tonu
   OTOMATIK: efektif parlaklik = karisim(gorselLum, tonLum, kontrast); esik 0.5. */
const bgPick=document.getElementById('bgPick'), bgFile=document.getElementById('bgFile'),
  bgClearBtn=document.getElementById('bgClear'), bgCropBox=document.getElementById('bgCrop'),
  bgCropImg=document.getElementById('bgCropImg'), bgZoom=document.getElementById('bgZoom'),
  bgZoomVal=document.getElementById('bgZoomVal'), bgSave=document.getElementById('bgSave');
const tContrast=document.getElementById('tContrast'), tContrastVal=document.getElementById('tContrastVal'),
  tintBar=document.getElementById('tintBar');
const BGC={img:null,iw:0,ih:0,scale:1,minScale:1,ox:0,oy:0,aspect:1.6};
let bgState={has:false,lum:0.5,contrast:0.35,tint:'auto',dataurl:'',rect:[0,0,1,1]};
let bgNatural=null;  // {w,h} - orijinal gorselin dogal boyutu (geometri icin)

function hexRgb(h){ return [parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)]; }
function hexLum(h){ const [r,g,b]=hexRgb(h).map(v=>v/255); return 0.2126*r+0.7152*g+0.0722*b; }

/* Odak-cerceve geometrisi (SAF fonksiyon - test edilebilir):
   kullanicinin sectigi odak dikdortgenini, ANLIK panel oranina uyacak sekilde
   MUMKUNSE tamamen kapsayarak genisletir (orijinalin gercek pikselleriyle),
   gorsel sinirlarina taparsa ancak o zaman odaktan kirpar. Donen deger px. */
function bgComputeView(iw, ih, rect, cw, ch){
  const A=cw/Math.max(1,ch);
  const rx=rect[0]*iw, ry=rect[1]*ih, rw=Math.max(1,rect[2]*iw), rh=Math.max(1,rect[3]*ih);
  let w=Math.max(rw, rh*A), h2=w/A;
  if(h2<rh){ h2=rh; w=h2*A; }
  if(w>iw){ w=iw; h2=w/A; }
  if(h2>ih){ h2=ih; w=h2*A; if(w>iw){ w=iw; h2=w/A; } }
  let x=rx+rw/2-w/2, y=ry+rh/2-h2/2;      // odak merkezine capalan
  x=Math.min(Math.max(0,x), iw-w); y=Math.min(Math.max(0,y), ih-h2);
  const scale=cw/w;
  return {sizeW:iw*scale, sizeH:ih*scale, posX:-x*scale, posY:-y*scale};
}
function bgGeometry(){
  if(!bgState.has||!bgNatural){ return; }
  const cw=messages.clientWidth, ch=messages.clientHeight;
  if(cw<2||ch<2) return;
  const v=bgComputeView(bgNatural.w, bgNatural.h, bgState.rect||[0,0,1,1], cw, ch);
  // iki katman: gradient (scrim) panel boyu, gorsel hesaplanan kadraj
  messages.style.backgroundSize='100% 100%, '+v.sizeW.toFixed(1)+'px '+v.sizeH.toFixed(1)+'px';
  messages.style.backgroundPosition='0 0, '+v.posX.toFixed(1)+'px '+v.posY.toFixed(1)+'px';
}
function bgApply(){
  if(!bgState.has||!bgState.dataurl){
    messages.style.backgroundImage=''; messages.style.backgroundSize='';
    messages.style.backgroundPosition=''; messages.classList.remove('bg-light-text');
    bgNatural=null; return;
  }
  const tint=bgState.tint==='auto' ? (bgState.lum>=0.55?'#ece8e1':'#1e1e22') : bgState.tint;
  const eff=bgState.lum*(1-bgState.contrast)+hexLum(tint)*bgState.contrast;
  messages.classList.toggle('bg-light-text', eff<0.5);
  const [r,g,b]=hexRgb(tint);
  messages.style.backgroundImage=
    'linear-gradient(rgba('+r+','+g+','+b+','+bgState.contrast+'),rgba('+r+','+g+','+b+','+bgState.contrast+')),url("'+bgState.dataurl+'")';
  if(bgNatural){ bgGeometry(); }
  else{
    const im=new Image();
    im.onload=()=>{ bgNatural={w:im.naturalWidth,h:im.naturalHeight}; bgGeometry(); };
    im.src=bgState.dataurl;
  }
}
let bgResizeT=null;
addEventListener('resize',()=>{ if(!bgState.has) return;
  clearTimeout(bgResizeT); bgResizeT=setTimeout(bgGeometry,90); });  // kadraj odaga sadik kalir
function syncBgUI(){
  tContrast.value=bgState.contrast; tContrastVal.textContent=(+bgState.contrast).toFixed(2);
  [...tintBar.querySelectorAll('.tint')].forEach(b=>b.classList.toggle('sel', b.dataset.tint===bgState.tint));
  bgClearBtn.style.display=bgState.has?'':'none';
}
async function loadBg(){
  let r; try{ r=await api().ui_bg_get(); }catch(e){ r=null; }
  if(!r||!r.ok) return;
  bgState={has:!!r.has, lum:+r.lum||0.5, contrast:(+r.contrast||0), tint:r.tint||'auto',
    dataurl:r.dataurl||'', rect:(Array.isArray(r.rect)&&r.rect.length===4)?r.rect.map(Number):[0,0,1,1]};
  if(isNaN(bgState.contrast)) bgState.contrast=0.35;
  bgNatural=null;
  bgApply(); syncBgUI();
}
let bgPrefT=null;
function bgPrefPush(){
  bgApply(); syncBgUI();
  clearTimeout(bgPrefT);
  bgPrefT=setTimeout(async()=>{ try{ await api().ui_bg_prefs(bgState.contrast, bgState.tint); }catch(e){} },350);
}
tContrast.addEventListener('input',()=>{ bgState.contrast=+tContrast.value; bgPrefPush(); });
tintBar.addEventListener('click',e=>{ const b=e.target.closest('.tint'); if(!b) return;
  bgState.tint=b.dataset.tint; bgPrefPush(); });

/* --- kirpici: sabit oranli cerceve (mesaj alaninin o anki orani), pan + zoom --- */
function bgPaneOpen(){
  BGC.img=null; bgCropImg.removeAttribute('src');
  bgCropBox.classList.remove('show'); document.getElementById('bgZoomRow').classList.remove('show');
  syncBgUI();
}
bgPick.onclick=()=>bgFile.click();
bgFile.onchange=()=>{
  const f=bgFile.files&&bgFile.files[0]; bgFile.value=''; if(!f) return;
  const rd=new FileReader();
  rd.onload=()=>{ const im=new Image(); im.onload=()=>startCrop(im); im.src=rd.result; };
  rd.readAsDataURL(f);
};
function startCrop(im){
  BGC.img=im; BGC.iw=im.naturalWidth; BGC.ih=im.naturalHeight;
  BGC.aspect=Math.max(1.05, messages.clientWidth/Math.max(1,messages.clientHeight));
  bgCropBox.classList.add('show');
  bgCropBox.style.height=Math.round(bgCropBox.clientWidth/BGC.aspect)+'px';
  document.getElementById('bgZoomRow').classList.add('show');
  bgCropImg.src=im.src;
  const vw=bgCropBox.clientWidth, vh=bgCropBox.clientHeight;
  BGC.minScale=Math.max(vw/BGC.iw, vh/BGC.ih);   // kucuk gorsel buyur, buyuk kuculur: cover taban
  BGC.scale=BGC.minScale;
  BGC.ox=(vw-BGC.iw*BGC.scale)/2; BGC.oy=(vh-BGC.ih*BGC.scale)/2;
  bgZoom.value='1'; bgZoomVal.textContent='1.00';
  bgRender();
}
function bgClamp(){
  const vw=bgCropBox.clientWidth, vh=bgCropBox.clientHeight;
  BGC.ox=Math.min(0, Math.max(vw-BGC.iw*BGC.scale, BGC.ox));
  BGC.oy=Math.min(0, Math.max(vh-BGC.ih*BGC.scale, BGC.oy));
}
function bgRender(){ bgClamp();
  bgCropImg.style.transform='translate('+BGC.ox+'px,'+BGC.oy+'px) scale('+BGC.scale+')'; }
bgZoom.addEventListener('input',()=>{
  if(!BGC.img) return;
  const vw=bgCropBox.clientWidth, vh=bgCropBox.clientHeight, k=+bgZoom.value, ns=BGC.minScale*k;
  const cx=(vw/2-BGC.ox)/BGC.scale, cy=(vh/2-BGC.oy)/BGC.scale;  // merkez sabit kalsin
  BGC.scale=ns; BGC.ox=vw/2-cx*ns; BGC.oy=vh/2-cy*ns;
  bgZoomVal.textContent=k.toFixed(2); bgRender();
});
let bgDrag=null;
bgCropBox.addEventListener('pointerdown',e=>{ if(!BGC.img) return;
  bgDrag={x:e.clientX,y:e.clientY,ox:BGC.ox,oy:BGC.oy}; bgCropBox.setPointerCapture(e.pointerId); });
bgCropBox.addEventListener('pointermove',e=>{ if(!bgDrag) return;
  BGC.ox=bgDrag.ox+(e.clientX-bgDrag.x); BGC.oy=bgDrag.oy+(e.clientY-bgDrag.y); bgRender(); });
bgCropBox.addEventListener('pointerup',()=>{ bgDrag=null; });
bgCropBox.addEventListener('pointercancel',()=>{ bgDrag=null; });
bgSave.onclick=async()=>{
  if(!BGC.img) return;
  // odak dikdortgeni (orijinal koordinatlarda, normalize)
  const sx=(-BGC.ox)/BGC.scale, sy=(-BGC.oy)/BGC.scale,
        sw=bgCropBox.clientWidth/BGC.scale, sh=bgCropBox.clientHeight/BGC.scale;
  const rect=[Math.max(0,sx/BGC.iw), Math.max(0,sy/BGC.ih),
              Math.min(1,sw/BGC.iw), Math.min(1,sh/BGC.ih)];
  // parlaklik: odak bolgesinden olculur (metin tonu karari oraya gore)
  const sc=document.createElement('canvas'); sc.width=24; sc.height=24;
  sc.getContext('2d').drawImage(BGC.img, sx,sy,sw,sh, 0,0,24,24);
  const d=sc.getContext('2d').getImageData(0,0,24,24).data;
  let lum=0; for(let i=0;i<d.length;i+=4){ lum+=0.2126*d[i]+0.7152*d[i+1]+0.0722*d[i+2]; }
  lum/=(255*(d.length/4));
  // ORIJINAL kaydedilir (gerekirse 2048'e indirilir) - pencere degisince
  // kadraj odaga capalanir, fazlasi gercek cevre pikselleriyle dolar
  let ow=BGC.iw, oh=BGC.ih;
  const mx=Math.max(ow,oh);
  if(mx>2048){ const k=2048/mx; ow=Math.round(ow*k); oh=Math.round(oh*k); }
  const cv=document.createElement('canvas'); cv.width=ow; cv.height=oh;
  cv.getContext('2d').drawImage(BGC.img, 0,0,ow,oh);
  const dataurl=cv.toDataURL('image/jpeg',0.9);
  let r; try{ r=await api().ui_bg_set(dataurl, lum, rect); }catch(e){ r=null; }
  if(r&&r.ok){ bgState.has=true; bgState.dataurl=dataurl; bgState.lum=lum; bgState.rect=rect;
    bgNatural={w:ow,h:oh}; bgApply(); syncBgUI(); closeModal(); }
};
bgClearBtn.onclick=async()=>{
  let r; try{ r=await api().ui_bg_clear(); }catch(e){ r=null; }
  if(r&&r.ok){ bgState.has=false; bgState.dataurl=''; bgApply(); bgPaneOpen(); }
};

/* ===== durum polling ===== */
let lastState='';
async function pollStatus(){
  if(!(window.pywebview&&window.pywebview.api)) return;                    // kopru henuz yok - interval yeniden dener
  if(typeof window.pywebview.api.status!=='function') return;              // stub var ama metotlar bagli degil - bekle
  let s; try{ s=await window.pywebview.api.status(); }catch(e){ return; }
  lastState=s.state||'';
  if(s.character) aiName=s.character;  // yalniz mesaj etiketleri icin - baslik marka adinda kalir
  if(s.state==='ready'){ ready=true; liveDot.classList.add('ok'); statusText.textContent='yerel . cevrimdisi';
    overlay.classList.remove('show'); composer.classList.remove('disabled'); refreshSend(); refreshTts(); }
  else if(s.state==='error'){ ready=false; liveDot.classList.remove('ok'); statusText.textContent='hata';
    overlayTitle.textContent='Baslatilamadi'; overlayDetail.textContent=s.detail||'llama-server hazir degil.';
    document.getElementById('spinner').style.display='none'; }
  else { statusText.textContent='yukleniyor...'; }
}
function startPolling(){ pollStatus(); const iv=setInterval(async()=>{ await pollStatus();
  if(ready||lastState==='error'){ clearInterval(iv); } }, 900); }  // error = nihai durum, polli birak

/* Yazar adi tazeleme: polling 'ready'de durur ama aktif karakter kilit ACILINCA
   (ya da panelden degistirilince) belli olur - o anlarda tek seferlik ceker.
   Yalniz sohbet mesajlarinin yazar etiketini besler; soldaki baslik markadir. */
async function refreshCharName(){
  if(!(window.pywebview&&window.pywebview.api&&typeof window.pywebview.api.status==='function')) return;
  let s; try{ s=await window.pywebview.api.status(); }catch(e){ return; }
  if(s&&s.character) aiName=s.character;
}

/* ===== kilit ekrani (parola) ===== */
const lock=document.getElementById('lock'), lockCard=document.getElementById('lockCard'),
  lockForm=document.getElementById('lockForm'), lockPass=document.getElementById('lockPass'),
  lockPass2=document.getElementById('lockPass2'), lockErr=document.getElementById('lockErr'),
  lockBtn=document.getElementById('lockBtn'), lockTitle=document.getElementById('lockTitle'),
  lockSub=document.getElementById('lockSub'), lockRemember=document.getElementById('lockRemember'),
  lockInfo=document.getElementById('lockInfo'), lockConfirm=document.getElementById('lockConfirm');
let lockFirstRun=false, rememberConfirmed=false;
const memApi=()=>(window.pywebview&&window.pywebview.api&&window.pywebview.api.memory_state)?window.pywebview.api:MOCK_MEM;

async function initLock(){
  let st; try{ st=await memApi().memory_state(); }catch(e){ st=null; }
  if(!st || !st.enabled || st.unlocked){ lock.classList.remove('show'); return; } // kapali/acik -> ekran yok (mock kalintisi da gider)
  lockFirstRun=!st.initialized;
  if(lockFirstRun){
    lockTitle.textContent='Özel hafıza oluştur';
    lockSub.textContent='Sohbetlerini şifreli tutmak için bir parola belirle.';
    lockPass.placeholder='Yeni parola'; lockPass2.style.display='block';
    lockBtn.textContent='Oluştur ve Başla';
  } else {                                   // gec gelen GERCEK api, mock'un ilk-kurulum
    lockTitle.textContent='Hafızan kilitli';  // metinlerini birakmis olabilir -> sifirla
    lockSub.textContent='Devam etmek için parolanı gir.';
    lockPass.placeholder='Parola'; lockPass2.style.display='none';
    lockBtn.textContent='Kilidi Aç';
  }
  lock.classList.add('show'); setTimeout(()=>lockPass.focus(),350);
}
function lockErrShow(m){ lockErr.textContent=m; lockErr.classList.add('show'); }
function lockShake(){ lockCard.classList.add('shake'); setTimeout(()=>lockCard.classList.remove('shake'),500); }
lockForm.addEventListener('submit', async(e)=>{
  e.preventDefault(); const p=lockPass.value; if(!p) return; lockErr.classList.remove('show');
  if(lockFirstRun){
    if(p.length<4){ lockErrShow('En az 4 karakter.'); return; }
    if(p!==lockPass2.value){ lockErrShow('Parolalar eşleşmiyor.'); lockShake(); return; }
  }
  lockBtn.disabled=true;
  let res; try{ res=await memApi().memory_unlock(p, !!(lockRemember.checked&&rememberConfirmed)); }catch(err){ res={ok:false}; }
  lockBtn.disabled=false;
  if(res&&res.ok){ unlockReveal(); }
  else { lockErrShow(lockFirstRun?'Oluşturulamadı.':'Parola yanlış.'); lockShake(); lockPass.value=''; lockPass.focus(); }
});
function unlockReveal(){
  const chatEl=document.getElementById('chat');
  lock.classList.add('hide');
  void chatEl.offsetWidth; chatEl.classList.add('reveal');
  setTimeout(()=>{ lock.classList.remove('show','hide'); }, 640);
  refreshCharName();  // kasa acildi: aktif karakter artik belli, basliktaki adi guncelle
}
lockRemember.addEventListener('change', ()=>{
  if(lockRemember.checked && !rememberConfirmed) lockConfirm.classList.add('show');
  else if(!lockRemember.checked){ rememberConfirmed=false; lockConfirm.classList.remove('show'); }
});
lockInfo.onclick=()=>{ lockRemember.checked=true; lockConfirm.classList.add('show'); };
document.getElementById('rememberOk').onclick=()=>{ rememberConfirmed=true; lockRemember.checked=true; lockConfirm.classList.remove('show'); };
document.getElementById('rememberCancel').onclick=()=>{ rememberConfirmed=false; lockRemember.checked=false; lockConfirm.classList.remove('show'); };
/* tarayici onizlemesi icin mock (pywebview yokken) */
const MOCK_MEM={ _init:false, _unlocked:false,
  async memory_state(){ return {enabled:true, initialized:this._init, unlocked:this._unlocked}; },
  async memory_unlock(p){ if(p&&p.length>=4){ this._init=true; this._unlocked=true; return {ok:true}; } return {ok:false}; } };

/* tarayici onizlemesi icin genis mock (menu/modal/tts) */
const MOCK_API={
  _auto:false,_speaking:false,_spkT:null,
  _params:{speed:1.1,denoise_prop:0.75,exaggeration:0.5},
  _facts:[{id:1,type:'identity',text:'Kullanıcının adı Deniz',importance:9},
          {id:2,type:'preference',text:'Sade espresso seviyor',importance:6},
          {id:3,type:'milestone',text:'Birlikte bir masaüstü uygulaması bitirdiler',importance:7}],
  _prompts:{ system:{system_prompt:'Sen Wisteria\'sın...\n\n(mock sistem promptu)'},
             character:{wisteria:'Wisteria otuzlarında, sıcak, oyunbaz...\n\n(mock)'},
             persona:{persona1:''} },
  _active:{character:'wisteria',persona:'persona1'},
  async tts_status(){ return {enabled:this._auto,auto:this._auto,loaded:true,state:'ready',detail:'hazir (mock)',speaking:this._speaking}; },
  async set_tts_enabled(on){ this._auto=!!on; return this.tts_status(); },
  async tts_get_params(){ return {ok:true,auto:this._auto,state:'ready',...this._params}; },
  async tts_set_params(p){ Object.assign(this._params,{speed:p.speed??this._params.speed,
      denoise_prop:p.denoise_prop??this._params.denoise_prop, exaggeration:p.exaggeration??this._params.exaggeration});
    if(p.auto!==undefined) this._auto=!!p.auto; return this.tts_get_params(); },
  async speak_message(t){ if(!t) return {ok:false}; this._speaking=true;
    clearTimeout(this._spkT); this._spkT=setTimeout(()=>{ this._speaking=false; },2000); return {ok:true}; },
  async stop_speaking(){ this._speaking=false; clearTimeout(this._spkT); return {ok:true}; },
  async memory_overview(){ return {ok:true,
    recap:'Deniz ve Wisteria uygulama üzerinde çalıştılar; ses ve hafıza eklendi.',
    facts:this._facts, episodes:[{text:'Sesin ilk çalıştığı gün.',ts:Date.now()/1000-86400},
      {text:'Kahve tercihi üzerine şakalaştılar.',ts:Date.now()/1000-172800}],
    message_count:42}; },
  async memory_delete_fact(id){ this._facts=this._facts.filter(f=>f.id!==id); return {ok:true}; },
  async memory_add_fact(t,x,imp){ const id=Math.max(0,...this._facts.map(f=>f.id))+1;
    this._facts.push({id,type:t||'bilgi',text:x,importance:Math.max(1,Math.min(10,+imp||7))}); return {ok:true,id}; },
  async memory_update_fact(id,x,imp){ const f=this._facts.find(f=>f.id===+id);
    if(!f) return {ok:false,error:'update'}; f.text=x; f.importance=Math.max(1,Math.min(10,+imp)); return {ok:true}; },
  async export_chat(){ return {ok:false,error:'cancelled'}; },
  async prompts_list(){ const k={}; for(const kind in this._prompts) k[kind]=Object.keys(this._prompts[kind]).sort();
    return {ok:true,kinds:k,active:{system:'system_prompt',...this._active}}; },
  async prompts_get(kind,name){ return {ok:true,text:(this._prompts[kind]&&this._prompts[kind][name])||''}; },
  async prompts_save(kind,name,text){ (this._prompts[kind]=this._prompts[kind]||{})[name]=text; return {ok:true}; },
  async prompts_create(kind,name){ const slug=(name||'').toLowerCase();
    if(this._prompts[kind]&&this._prompts[kind][slug]!==undefined) return {ok:false,error:'exists'};
    (this._prompts[kind]=this._prompts[kind]||{})[slug]=''; return {ok:true,name:slug}; },
  async prompts_set_active(kind,name){ this._active[kind]=name; return {ok:true}; },
  async prompts_rename(kind,old,nw){ const t=(this._prompts[kind]||{})[old]; if(t===undefined) return {ok:false,error:'not_found'};
    const slug=(nw||'').toLowerCase().replace(/ /g,'_').replace(/[^a-z0-9_-]/g,''); if(!slug) return {ok:false,error:'name'};
    if(this._prompts[kind][slug]!==undefined) return {ok:false,error:'exists'};
    this._prompts[kind][slug]=t; delete this._prompts[kind][old];
    if(this._active[kind]===old) this._active[kind]=slug; return {ok:true,name:slug}; },
  async prompts_delete(kind,name){ const ks=Object.keys(this._prompts[kind]||{});
    if(!ks.includes(name)) return {ok:false,error:'not_found'};
    if(ks.length<=1) return {ok:false,error:'last'};
    delete this._prompts[kind][name];
    if(this._active[kind]===name) this._active[kind]=Object.keys(this._prompts[kind]).sort()[0];
    return {ok:true,active:this._active[kind]}; },
  async prompts_export(){ return {ok:false,error:'cancelled'}; },
  async ui_text_get(){ return {ok:true,font_px:this._uiFp||15.5,line_height:this._uiLh||1.62}; },
  async ui_text_set(fp,lh){ this._uiFp=+fp; this._uiLh=+lh; return {ok:true,font_px:+fp,line_height:+lh}; },
  _bg:{has:false,dataurl:'',lum:0.5,contrast:0.35,tint:'auto',rect:[0,0,1,1]},
  async ui_bg_get(){ return {ok:true,has:this._bg.has,dataurl:this._bg.dataurl,
    lum:this._bg.lum,contrast:this._bg.contrast,tint:this._bg.tint,rect:this._bg.rect}; },
  async ui_bg_set(du,lum,rect){ this._bg.has=true; this._bg.dataurl=du; this._bg.lum=+lum||0.5;
    this._bg.rect=(Array.isArray(rect)&&rect.length===4)?rect:[0,0,1,1]; return {ok:true}; },
  async ui_bg_clear(){ this._bg.has=false; this._bg.dataurl=''; return {ok:true}; },
  async ui_bg_prefs(c,t){ this._bg.contrast=+c; this._bg.tint=t; return {ok:true,contrast:+c,tint:t}; },
};

/* ===== boot ===== */
/* Iki mod: gercek pywebview API'si ya da tarayici onizleme (mock). Yavas bir
   WebView2 soguk acilisinda API 1.5s'den gec enjekte olursa mock boot olabilir;
   pywebviewready gelince GERCEK boot bir kez daha kosar (bootedReal ile tek sefer)
   ve initLock/polling gercek duruma gore kendini duzeltir. */
let bootedReal=false, bootedMock=false;
function boot(){
  const real=!!(window.pywebview&&window.pywebview.api);
  if(real){ if(bootedReal) return; bootedReal=true; }
  else { if(bootedMock||bootedReal) return; bootedMock=true; }
  initLock(); startPolling(); startTtsPoll();
  loadTextPrefs();  // kayitli punto/satir araligi acilista uygulanir
  loadBg();         // kayitli sohbet arka plani + kontrast/ton acilista uygulanir
}
/* Tetikleme dayanikli: tek seferlik pywebviewready olayina guvenme (soguk aciliste
   kacabiliyor) - kullanilabilir api icin kisa araliklarla bak. 8s'de mock'a dus
   (tarayici onizleme), ama 60s'ye kadar izlemeye devam et: gec gelen gercek kopru
   mock'un ustune gercek boot kosar (bootedReal buna izin verir). */
window.addEventListener('pywebviewready', boot);
const apiUsable=()=>!!(window.pywebview&&window.pywebview.api&&typeof window.pywebview.api.status==='function');
if(apiUsable()){ boot(); }
else{
  let waited=0;
  const bootWatch=setInterval(()=>{
    waited+=300;
    if(apiUsable()){ clearInterval(bootWatch); boot(); return; }
    if(waited===8100) boot();                       // mock fallback - izlemeyi birakma
    if(waited>=60000) clearInterval(bootWatch);
  },300);
}

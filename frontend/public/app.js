"use strict";
const $ = (id) => document.getElementById(id);
let history = [], activeJob = null, activeState = null, busy = false, pollGeneration = 0;
let sessionPhase = 'checking';
let sourceGeneration = 0, sourceController = null;
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const STORAGE_KEY = 'wiki-active-job';
class ApiError extends Error {
  constructor(message, status = 0) { super(message); this.status = status; }
  get retryable() { return this.status === 0 || this.status === 408 || this.status >= 500; }
}
async function api(path, options = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) abort();
  options.signal?.addEventListener('abort', abort, {once:true});
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    // Access redirects leave this origin. Inspect the redirect before login CORS
    // turns it into an indistinguishable network failure.
    const response = await fetch(path, {credentials:'same-origin', cache:'no-store', ...options, redirect:'manual', signal:controller.signal});
    if (response.type === 'opaqueredirect' || response.status === 401 || response.redirected) throw new ApiError('로그인이 만료됐어요. 아래 버튼으로 다시 로그인해주세요.', 401);
    if (response.status === 413) throw new ApiError('대화 내용이 너무 큽니다. 새 질문으로 시작해주세요.', 413);
    const isJson = response.headers.get('content-type')?.includes('application/json');
    if (!isJson) {
      if (response.ok) throw new ApiError('로그인 상태를 확인해주세요. 페이지를 새로고침해주세요.', 401);
      throw new ApiError(`서버 응답을 확인하지 못했어요. (${response.status})`, response.status);
    }
    const data = await response.json();
    if (!response.ok) throw new ApiError(data.error || '요청을 처리하지 못했어요.', response.status);
    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError('연결을 확인하지 못했어요. 잠시 후 다시 시도해주세요.');
  } finally { clearTimeout(timeout); options.signal?.removeEventListener('abort', abort); }
}
function post(path, data) { return api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)}); }
function notice(text, error = false) { $('notice').textContent = text; $('notice').classList.toggle('error', error); }
function interactionPermissions() {
  const ready=sessionPhase==='ready', idle=!activeState;
  return {compose:ready && idle, reset:ready && idle, source:ready, request:ready,
    // Withdrawal is local intent first. It remains available while login is
    // required, and reaches the server only after authentication resumes.
    cancel:!idle && !activeState.cancelRequested, authenticate:sessionPhase==='required'};
}
function canInteract(action) { return interactionPermissions()[action]===true; }
function renderInteraction() {
  const allowed=interactionPermissions();
  busy=!!activeState;
  $('send').hidden=busy; $('send').disabled=!allowed.compose;
  $('cancel').hidden=!busy; $('cancel').disabled=!allowed.cancel;
  $('question').disabled=!allowed.compose; $('reset').disabled=!allowed.reset;
  document.querySelectorAll('[data-question]').forEach(button => button.disabled=!allowed.compose);
  document.querySelectorAll('.sources button').forEach(button => button.disabled=!allowed.source);
  $('reauthenticate').hidden=!allowed.authenticate;
  $('reauthenticate').textContent=activeState?.cancelRequested ? '다시 로그인하고 중단하기' : busy ? '다시 로그인하고 질문 이어받기' : '다시 로그인하기';
  $('source-reauthenticate').hidden=!allowed.authenticate || !$('source-dialog').open;
}
function authenticationNotice(error) {
  const message=activeState?.cancelRequested ? '중단 요청을 저장했어요. 다시 로그인하면 중단을 요청할게요.'
    : activeState ? '로그인이 만료됐어요. 다시 로그인하면 진행 중인 질문을 이어서 확인할 수 있어요.'
    : error?.message || '로그인이 만료됐어요. 아래 버튼으로 다시 로그인해주세요.';
  notice(message,true);
}
function requireAuthentication(error) {
  sessionPhase='required'; ++pollGeneration;
  authenticationNotice(error); renderInteraction();
}
document.querySelectorAll('[data-reauthenticate]').forEach(button => button.addEventListener('click', () => { if(canInteract('authenticate')) location.reload(); }));
function persistJob() {
  try {
    if (activeState) {
      const {id,accepted,history,cancelRequested,request}=activeState;
      // Accepted jobs retain references and intent, never question/answer text.
      const stored={version:3,id,accepted,history,cancelRequested};
      if (!accepted) stored.request=request;
      sessionStorage.setItem(STORAGE_KEY,JSON.stringify(stored));
    } else sessionStorage.removeItem(STORAGE_KEY);
  } catch {}
  renderInteraction();
}
function saveJob(id, request = null) {
  if (!id) activeState=null;
  else if (activeState?.id!==id) activeState={id,accepted:!request,history:request?.history || [],request,cancelRequested:false};
  else if (!request) { activeState.accepted=true; activeState.request=null; }
  activeJob=id;
  persistJob();
}
function restoreJob(value) {
  const validId=id=>typeof id==='string' && /^[a-f0-9]{32}$/.test(id);
  const context=items=>{
    if (!Array.isArray(items) || items.length>3 || items.some(m=>!validId(m?.job_id))) throw new Error('Invalid pending history');
    return items.map(m=>({job_id:m.job_id}));
  };
  if (validId(value)) return {id:value,accepted:true,history:[],request:null,cancelRequested:false};
  const pending=JSON.parse(value);
  if (!validId(pending?.id) || ![1,2,3].includes(pending.version)) throw new Error('Invalid pending job');
  if (pending.version===3 && (typeof pending.accepted!=='boolean' || typeof pending.cancelRequested!=='boolean')) throw new Error('Invalid pending state');
  const prior=pending.version===3 ? context(pending.history) : pending.version===1 ? [] : context(pending.request?.history);
  if (pending.version===3 && pending.accepted) return {id:pending.id,accepted:true,history:prior,request:null,cancelRequested:pending.cancelRequested};
  const request=pending.request;
  if (request?.request_id!==pending.id || typeof request.question!=='string' || !request.question.trim() || request.question.length>4000) throw new Error('Invalid pending request');
  // Legacy rendered history is discarded; only normalized job references survive.
  return {id:pending.id,accepted:false,history:prior,cancelRequested:pending.version===3 && pending.cancelRequested,
    request:{question:request.question,history:prior,request_id:pending.id}};
}
async function sendCancellation(state) {
  if (!state || activeState!==state || !state.cancelRequested || state.cancelInFlight || state.cancelAcknowledged || !canInteract('request') || Date.now()<(state.cancelRetryAt || 0)) return;
  state.cancelInFlight=true;
  try {
    // An unconfirmed POST can still arrive after a reload or a failed lookup.
    // The server records this intent before admission, preventing late work.
    const data=await post('/api/jobs/'+state.id+'/cancel',state.accepted ? {} : {pending:true});
    if (activeState!==state) return;
    saveJob(state.id);
    state.cancelAcknowledged=true;
    if (!canInteract('request')) return;
    if (data.status==='cancelled') { notice('질문을 중단했어요.'); finishJob(); return; }
    notice('질문을 중단하고 있어요…');
  } catch (error) {
    if (activeState!==state || !canInteract('request')) return;
    if (error.status===401) requireAuthentication(error);
    else {
      // Keep intent even when the pending-cancellation store is full (429).
      // Retry with bounded backoff; never resubmit a withdrawn question.
      state.cancelFailures=(state.cancelFailures || 0)+1;
      state.cancelRetryAt=Date.now()+Math.min(1000*2**Math.min(state.cancelFailures,4),15000);
      notice(error.message,true);
    }
  } finally { state.cancelInFlight=false; }
}
function queryPayload(question, requestId) {
  // Prior questions and rendered answers stay out of browser request history.
  // The server resolves these references to its own protected conversation.
  return {question,history:history.filter(id=>typeof id==='string' && /^[a-f0-9]{32}$/.test(id)).slice(-3).map(job_id=>({job_id})),request_id:requestId};
}
function appendProtectedText(parent, text, protectedValues) {
  if (protectedValues===null) { parent.append(document.createTextNode(text)); return; }
  for (const part of text.split(/(\[\[PRIVATE_[a-f0-9]{32}\]\])/g)) {
    const isToken=/^\[\[PRIVATE_[a-f0-9]{32}\]\]$/.test(part);
    const value=isToken ? (Object.prototype.hasOwnProperty.call(protectedValues,part) && typeof protectedValues[part]==='string' ? protectedValues[part] : '[보호된 값]') : part;
    if (isToken) {
      const literal=document.createElement('span'); literal.style.whiteSpace='pre-wrap';
      literal.append(document.createTextNode(value)); parent.append(literal);
    } else parent.append(document.createTextNode(value));
  }
}
function inline(parent, text, protectedValues) {
  // Render a small Markdown subset exclusively with DOM text nodes.
  const parts = text.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/g);
  for (const part of parts) {
    if (part.startsWith('`') && part.endsWith('`')) { const el = document.createElement('code'); appendProtectedText(el,part.slice(1,-1),protectedValues); parent.append(el); }
    else if (part.startsWith('**') && part.endsWith('**')) { const el = document.createElement('strong'); appendProtectedText(el,part.slice(2,-2),protectedValues); parent.append(el); }
    else appendProtectedText(parent,part,protectedValues);
  }
}
function render(parent, text, protectedValues = null) {
  let code = null;
  for (const line of text.split('\n')) {
    if (line.startsWith('```')) { if (code) code = null; else { const pre = document.createElement('pre'); code = document.createElement('code'); pre.append(code); parent.append(pre); } continue; }
    if (code) { appendProtectedText(code,line + '\n',protectedValues); continue; }
    if (!line.trim()) continue;
    const el = document.createElement(/^#{1,6} /.test(line) ? 'h3' : 'p');
    inline(el, line.replace(/^#{1,6} /, '').replace(/^[-*] /, '• '),protectedValues); parent.append(el);
  }
}
function addMessage(role, text, sources = [], jobId = null, protectedValues = null) {
  const article = document.createElement('article'); article.className = 'message ' + role;
  const label = document.createElement('div'); label.className = 'message-label'; label.textContent = role === 'user' ? 'YOU' : 'YOUR NOTES'; article.append(label);
  const body = document.createElement('div'); body.className = 'message-body'; render(body, text, protectedValues); article.append(body);
  if (sources.length) {
    const wrap = document.createElement('div'); wrap.className = 'sources';
    const label = document.createElement('span'); label.className = 'sources-label'; label.textContent = '참고한 문서'; wrap.append(label);
    for (const source of sources) { const button = document.createElement('button'); button.type='button'; button.textContent = source.path.replace(/\.md$/, '') + ' · ' + source.line + '행'; button.addEventListener('click', () => openSource(source, jobId)); wrap.append(button); }
    article.append(wrap);
  }
  $('messages').append(article); return article;
}
async function openSource(source, jobId) {
  if (!canInteract('source')) return;
  invalidateSource();
  const generation=sourceGeneration, controller=sourceController=new AbortController();
  $('source-reauthenticate').hidden=true;
  $('source-title').textContent = source.path; $('source-content').textContent = '문서를 읽고 있어요…'; $('source-dialog').showModal();
  try {
    const data = await api('/api/source?path=' + encodeURIComponent(source.path) + (jobId ? '&job=' + encodeURIComponent(jobId) : ''), {signal:controller.signal});
    if (generation===sourceGeneration && $('source-dialog').open && canInteract('source')) $('source-content').textContent = data.text.split('\n').map((line,i) => `${i+1}  ${line}`).join('\n');
  } catch (error) {
    if (generation!==sourceGeneration || !$('source-dialog').open) return;
    $('source-content').textContent = error.message;
    if (error.status===401) { requireAuthentication(error); $('source-reauthenticate').hidden=false; }
  }
}
function invalidateSource() { ++sourceGeneration; sourceController?.abort(); sourceController=null; }
function clearSource() { invalidateSource(); $('source-content').textContent=''; $('source-title').textContent=''; $('source-reauthenticate').hidden=true; }
function closeSource() { clearSource(); $('source-dialog').close(); }
$('close-source').addEventListener('click', closeSource);
$('source-dialog').addEventListener('cancel', event => { event.preventDefault(); closeSource(); });
$('source-dialog').addEventListener('close', () => { if (!$('source-dialog').open) clearSource(); });
$('source-dialog').addEventListener('click', event => { if (event.target === $('source-dialog')) { const r = event.target.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) closeSource(); } });
function finishJob() { saveJob(null); }
function continueAfterSubmissionError(error, id, question) {
  if (activeJob!==id || !canInteract('request')) return false;
  if (error.status===401) { requireAuthentication(error); return false; }
  // Admission errors do not prove an earlier copy of this POST cannot arrive.
  // Every submission path must preserve withdrawal until cancellation is known.
  if (!error.retryable && !activeState.cancelRequested) {
    notice(error.message,true); $('question').value=question; finishJob(); return false;
  }
  notice(error.message,true); return true;
}
async function watchJob(id, request = null, resumed = false, question = '') {
  if (!canInteract('request')) return;
  const generation = ++pollGeneration, started = Date.now();
  let failures=0, questionShown=!resumed;
  question=request?.question || question;
  if (activeState?.accepted) request=null;
  if (resumed) {
    history=activeState.history.map(m=>m.job_id);
    if (request) { $('welcome').hidden=true; addMessage('user',question); questionShown=true; }
  }
  while (activeJob===id && generation===pollGeneration) {
    sendCancellation(activeState);
    try {
      const data = await api('/api/jobs/'+id);
      if (activeJob!==id) return;
      // Any successful lookup confirms acceptance. Never recreate an expired job.
      if (request) saveJob(id);
      request = null;
      if (data.status==='cancelling') { activeState.cancelRequested=true; activeState.cancelAcknowledged=true; persistJob(); }
      if (generation!==pollGeneration) return;
      if (data.status==='running') sendCancellation(activeState);
      question=data.question || question;
      if (!questionShown && data.question) { $('welcome').hidden=true; addMessage('user',data.question); questionShown=true; }
      if (data.status==='completed') {
        const display=typeof data.display_answer==='string';
        const values=display && data.protected_values && typeof data.protected_values==='object' && !Array.isArray(data.protected_values) ? data.protected_values : {};
        addMessage('assistant',display ? data.display_answer : data.answer,data.sources,id,display ? values : null);
        history.push(id);
        history=history.slice(-3); notice('답변을 찾았어요. 참고 문서를 눌러 원문을 확인할 수 있어요.'); finishJob(); return;
      }
      if (data.status==='failed') { notice(data.error,true); if(question) $('question').value=question; finishJob(); return; }
      if (data.status==='cancelled') { notice('질문을 중단했어요.'); finishJob(); return; }
      failures=0;
      const stage=activeState.cancelRequested || data.status==='cancelling'?'질문을 중단하고 있어요…':data.stage==='selecting'?'관련 문서를 찾고 있어요…':'답변을 준비하고 있어요…';
      notice(`${stage} ${Math.floor((Date.now()-started)/1000)}초`);
      await wait(1800);
    } catch (error) {
      if (activeJob!==id || generation!==pollGeneration) return;
      if (activeState.accepted) request=null;
      if (error.status===404 && request && activeState.cancelRequested) {
        // A missing job does not prove the original POST cannot arrive later.
        // Keep polling until the server records cancellation or finds the job.
        sendCancellation(activeState);
        notice('취소 요청을 다시 확인하고 있어요…');
        await wait(1800); continue;
      }
      if (error.status===404 && request) {
        // Submission may not have reached the server. Reuse its idempotency key.
        try {
          await post('/api/query',request);
          if (activeJob!==id) return;
          saveJob(id); request=null;
          if (generation!==pollGeneration) return;
          failures=0; continue;
        }
        catch (submitError) {
          if (generation!==pollGeneration || !continueAfterSubmissionError(submitError,id,request?.question || question)) return;
          // Cancellation may have been requested while this retry was in flight.
          // Keep its state and polling loop even on a terminal admission error.
          if (activeState.cancelRequested) {
            sendCancellation(activeState);
            await wait(1800); continue;
          }
          error=submitError;
        }
      }
      if (activeJob!==id || generation!==pollGeneration) return;
      if (error.status===401) { requireAuthentication(error); return; }
      if (error.status===404 || !error.retryable) {
        if (request) $('question').value=request.question;
        notice(error.message,true); finishJob(); return;
      }
      failures++;
      notice('연결을 다시 확인하고 있어요. 진행 중인 질문은 유지됩니다.',true);
      await wait(Math.min(1000*2**Math.min(failures,4),15000));
    }
  }
}
$('question-form').addEventListener('submit', async event => {
  event.preventDefault(); if(!canInteract('compose')) return;
  const question=$('question').value.trim(); if(!question) return;
  const id=crypto.randomUUID().replaceAll('-',''), payload=queryPayload(question,id);
  $('welcome').hidden=true; addMessage('user',question); $('question').value=''; saveJob(id,payload);
  notice('관련 문서를 찾고 있어요…');
  let pendingRequest=payload;
  try { await post('/api/query',payload); if(activeJob!==id) return; saveJob(id); pendingRequest=null; }
  catch(error) {
    if(!continueAfterSubmissionError(error,id,question)) return;
  }
  await watchJob(id,pendingRequest,false,question);
});
$('question').addEventListener('keydown', event => { if(event.key==='Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if(canInteract('compose')) $('question-form').requestSubmit(); } });
$('cancel').addEventListener('click', () => {
  if(!canInteract('cancel')) return;
  activeState.cancelRequested=true; persistJob();
  if(sessionPhase==='required') { authenticationNotice(); return; }
  if(sessionPhase==='checking') { notice('중단 요청을 저장했어요. 로그인 상태를 확인하고 있어요…'); return; }
  notice('질문을 중단하고 있어요…');
  // Record intent server-side even if the original POST has not arrived.
  sendCancellation(activeState);
});
$('reset').addEventListener('click', () => { if(!canInteract('reset')) return; closeSource(); history=[]; $('messages').replaceChildren(); $('welcome').hidden=false; notice(''); $('question').value=''; $('question').focus(); });
document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => { if(!canInteract('compose')) return; $('question').value=button.dataset.question; $('question').focus(); }));
try {
  const pending=sessionStorage.getItem(STORAGE_KEY);
  if(pending) { activeState=restoreJob(pending); activeJob=activeState.id; persistJob(); }
} catch { try { sessionStorage.removeItem(STORAGE_KEY); } catch {} }
async function initializeSession() {
  renderInteraction();
  try {
    const data=await api('/api/status');
    $('note-count').textContent=`${data.notes}개의 Markdown 노트`;
  } catch(error) {
    $('note-count').textContent='연결을 확인해주세요';
    if(error.status===401) { requireAuthentication(error); return; }
    // The status endpoint is only an initial session check. A metadata/network
    // failure must still allow queries and pending recovery to try reconnecting.
    if(!activeState) notice('노트 정보를 확인하지 못했어요. 질문을 보내 다시 연결할 수 있어요.',true);
  }
  sessionPhase='ready'; renderInteraction();
  if(activeState) {
    notice(activeState.cancelRequested ? '중단 요청을 다시 확인하고 있어요…' : '이전 질문의 답변을 다시 연결하고 있어요…');
    watchJob(activeJob,activeState.request,true);
  }
}
initializeSession();

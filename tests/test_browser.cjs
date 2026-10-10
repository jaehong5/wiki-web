const fs=require('fs'),assert=require('assert'),http=require('http'),path=require('path');
// Override these paths to run against an isolated frontend copy or another
// existing Playwright installation; this suite never installs dependencies.
let playwright;
if(process.env.WIKI_TEST_PLAYWRIGHT)playwright=require(process.env.WIKI_TEST_PLAYWRIGHT);
else playwright=require('playwright');
const {chromium}=playwright;
const root=path.resolve(process.env.WIKI_TEST_PUBLIC_ROOT || path.join(__dirname,'../frontend/public'))+path.sep;
const serverSource=process.env.WIKI_TEST_SERVER_SOURCE || path.join(__dirname,'../backend/server.py');
const bodyLimitMatch=fs.readFileSync(serverSource,'utf8').match(/^BODY_LIMIT\s*=\s*(\d+)\s*$/m);
assert(bodyLimitMatch,'read the actual server request-body limit');
const bodyLimit=Number(bodyLimitMatch[1]);
const contentType=name=>name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':name.endsWith('.svg')?'image/svg+xml':name.endsWith('.png')?'image/png':name.endsWith('.woff2')?'font/woff2':'text/html';
const fileName=path=>path==='/'?'index.html':path==='/app/'?'app/index.html':path.slice(1);
async function setup(browser, handler, {statusHandler,beforeLoad}={}){
 const page=await browser.newPage({viewport:{width:390,height:844}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 if(beforeLoad)await beforeLoad(page);
 await page.route('**/*',route=>{
  const url=new URL(route.request().url());
  if(url.pathname==='/api/status')return statusHandler ? statusHandler(route,page) : route.fulfill({json:{notes:35}});
  if(url.pathname.startsWith('/api/'))return handler(route,url,page);
  const name=fileName(url.pathname);
  if(!fs.existsSync(root+name))return route.fulfill({status:404,body:''});
  return route.fulfill({body:fs.readFileSync(root+name),contentType:contentType(name)});
 });
 await page.goto('https://wiki.example.com/app/');
 return {page,errors};
}
async function send(page,question='테스트 질문'){
 await page.locator('#question').fill(question);await page.locator('#send').click();
}
async function waitForSignal(signal,label){
 let timeout;
 try{await Promise.race([signal,new Promise((_,reject)=>{timeout=setTimeout(()=>reject(new Error(label)),5000);})]);}
 finally{clearTimeout(timeout);}
}
async function listen(server){
 await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
 return 'http://127.0.0.1:'+server.address().port;
}
async function closeServer(server){
 await new Promise(resolve=>{server.close(resolve);server.closeAllConnections();});
}
async function checkLanding(browser){
 const state={mode:'anonymous',statusHits:0,loginHits:0,otherApiHits:0};
 let siteOrigin;
 const login=http.createServer((req,res)=>{
  state.loginHits++;
  res.writeHead(200,{'Content-Type':'text/html'});
  res.end('<!doctype html><a id="login" href="'+siteOrigin+'/resume">Sign in</a>');
 });
 const loginOrigin=await listen(login);
 const site=http.createServer((req,res)=>{
  const path=new URL(req.url,siteOrigin).pathname;
  const redirect=()=>{res.writeHead(302,{Location:loginOrigin+'/login'});res.end();};
  const json=(code,data)=>{res.writeHead(code,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
  if(path==='/resume'){
   state.mode='valid';res.writeHead(302,{Location:'/app/'});res.end();return;
  }
  if(path==='/api/status'){
   state.statusHits++;
   if(state.mode==='anonymous')return redirect();
   if(state.mode==='unauthorized')return json(401,{error:'unauthorized'});
   if(state.mode==='server-error')return json(503,{error:'unavailable'});
   if(state.mode==='network')return res.destroy();
   if(state.mode==='timeout')return;
   if(state.mode==='wrong-shape')return json(200,{notes:'35'});
   if(state.mode==='invalid-json'){res.writeHead(200,{'Content-Type':'application/json'});res.end('{');return;}
   if(state.mode==='html'){res.writeHead(200,{'Content-Type':'text/html'});res.end('<h1>Sign in</h1>');return;}
   return json(200,{notes:35});
  }
  if(path.startsWith('/api/')){state.otherApiHits++;return json(404,{error:'not used in landing tests'});}
  const publicPath=path==='/' || path.startsWith('/assets/branding/');
  if(!publicPath && state.mode!=='valid')return redirect();
  const name=fileName(path);
  if(!['index.html','app/index.html','app.js','style.css','glass.css'].includes(name) && !name.startsWith('assets/branding/')){res.writeHead(404);res.end();return;}
  if(!fs.existsSync(root+name)){res.writeHead(404);res.end();return;}
  res.writeHead(200,{'Content-Type':contentType(name)});res.end(fs.readFileSync(root+name));
 });
 let page;
 const errors=[];
 const open=async(javaScriptEnabled=true)=>{
  page=await browser.newPage({viewport:{width:390,height:844},javaScriptEnabled});
  page.on('pageerror',error=>errors.push(error.message));
  if(javaScriptEnabled){
   await page.clock.install();
   await page.addInitScript(()=>{
    const originalFetch=window.fetch;
    window.probeRequests=[];window.probeSettled=false;
    window.addEventListener('pagehide',()=>{
     if(location.pathname==='/')sessionStorage.setItem('landing-history-length',String(window.history.length));
    });
    window.fetch=(url,options)=>{
     window.probeRequests.push({url,redirect:options?.redirect,credentials:options?.credentials,cache:options?.cache});
     return originalFetch(url,options).finally(()=>{window.probeSettled=true;});
    };
   });
  }
  await page.goto(siteOrigin+'/');
 };
 try{
  siteOrigin=await listen(site);
  await open();
  await page.waitForFunction(()=>window.probeSettled);
  await page.clock.fastForward(16000);
  assert.equal(page.url(),siteOrigin+'/');assert.equal(state.loginHits,0);assert.equal(state.statusHits,1);
  assert.deepEqual(await page.evaluate(()=>probeRequests),[{url:'/api/status',redirect:'manual',credentials:'same-origin',cache:'no-store'}]);
  assert.equal(await page.locator('title').textContent(),'Personal Wiki');
  const ogImage=new URL(await page.locator('meta[property="og:image"]').getAttribute('content'));
  assert.equal(ogImage.origin,'https://wiki.example.com');assert(ogImage.pathname.startsWith('/assets/branding/'));
  const ogResponse=await page.request.get(siteOrigin+ogImage.pathname+ogImage.search,{maxRedirects:0});
  assert.equal(ogResponse.status(),200,'the referenced OG image must be publicly served');
  assert(ogResponse.headers()['content-type'].includes('image/png'));
  const ogBytes=await ogResponse.body();
  assert.deepEqual(ogBytes.subarray(0,8),Buffer.from([137,80,78,71,13,10,26,10]),'OG asset must be PNG bytes');
  assert.equal(ogBytes.toString('ascii',12,16),'IHDR');
  const ogDimensions=[ogBytes.readUInt32BE(16),ogBytes.readUInt32BE(20)];
  assert.deepEqual(ogDimensions,[1200,630],'verify actual PNG dimensions, not only metadata strings');
  assert.equal(await page.locator('meta[property="og:image:width"]').getAttribute('content'),String(ogDimensions[0]));
  assert.equal(await page.locator('meta[property="og:image:height"]').getAttribute('content'),String(ogDimensions[1]));
  assert.equal(await page.locator('meta[name="twitter:image"]').getAttribute('content'),ogImage.href);
  for(const href of await page.locator('link[rel="icon"],link[rel="apple-touch-icon"]').evaluateAll(nodes=>nodes.map(node=>node.getAttribute('href')))){
   const iconUrl=new URL(href,siteOrigin);
   assert.equal(iconUrl.origin,siteOrigin);assert(iconUrl.pathname.startsWith('/assets/branding/'));
   const iconResponse=await page.request.get(iconUrl.href,{maxRedirects:0});
   assert.equal(iconResponse.status(),200,'the referenced favicon must be publicly served: '+iconUrl.pathname);
   assert((await iconResponse.body()).length>0);
  }
  assert.equal(state.loginHits,0,'branding must not visit the login origin');
  assert(await page.locator('.enter-wiki').isVisible());assert.equal(await page.locator('.enter-wiki').getAttribute('href'),'/app/');
  assert.equal(await page.locator('#question').count(),0,'the public page must not contain the private question UI');
  assert(await page.locator('script[src],link[rel="stylesheet"],img[src]').evaluateAll(nodes=>nodes.every(node=>(node.getAttribute('src')||node.getAttribute('href')).startsWith('/assets/branding/'))));
  await page.emulateMedia({colorScheme:'light'});
  assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).backgroundColor),'rgb(24, 35, 31)');
  for(const width of [320,390,768,1440]){
   await page.setViewportSize({width,height:900});
   assert(!(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)),'landing must fit '+width+'px');
  }
  // Optional artifacts for manual inspection, not visual-regression assertions.
  if(process.env.WIKI_TEST_SCREENSHOTS){fs.mkdirSync(process.env.WIKI_TEST_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.WIKI_TEST_SCREENSHOTS,'wiki-landing-desktop.png'),fullPage:true});}
  await page.setViewportSize({width:390,height:844});
  if(process.env.WIKI_TEST_SCREENSHOTS)await page.screenshot({path:path.join(process.env.WIKI_TEST_SCREENSHOTS,'wiki-landing-mobile.png'),fullPage:true});
  console.log('Public landing: one protected manual probe; Access redirect stays public. Dark layout bounds, publicly served branding, and actual OG PNG dimensions verified.');
  await page.locator('.enter-wiki').click();await page.waitForURL(loginOrigin+'/login');
  assert.equal(state.loginHits,1);assert.equal(page.context().pages().length,1);
  await page.locator('#login').click();await page.waitForURL(siteOrigin+'/app/');
  await page.locator('#question').waitFor();
  console.log('Landing entry button follows private /app/ authentication in the same tab and arrives at the Wiki.');
  await page.close();page=null;

  const loginsBefore=state.loginHits;
  const statusBefore=state.statusHits;
  await open();
  await page.waitForURL(siteOrigin+'/app/');
  await page.waitForFunction(()=>document.getElementById('note-count')?.textContent==='35개의 Markdown 노트');
  assert.equal(state.loginHits,loginsBefore);assert.equal(state.statusHits,statusBefore+2);
  assert(await page.evaluate(()=>window.history.length===Number(sessionStorage.getItem('landing-history-length'))),'automatic entry replaces the landing history entry');
  console.log('Existing valid Wiki session automatically replaces the landing with /app/ and never visits login.');
  await page.close();page=null;

  for(const mode of ['unauthorized','server-error','network','html','invalid-json','wrong-shape','timeout']){
   state.mode=mode;const countBefore=state.statusHits;
   await open();
   if(mode==='timeout')await page.clock.fastForward(6000);
   await page.waitForFunction(()=>window.probeSettled);
   await page.clock.fastForward(16000);
   assert.equal(page.url(),siteOrigin+'/');
   assert.equal(await page.evaluate(()=>probeRequests.length),1);
   // Chromium may retry a dropped TCP connection internally; the page must
   // still issue exactly one fetch and never schedule its own retry.
   if(mode!=='network')assert.equal(state.statusHits,countBefore+1);
   assert.equal(state.loginHits,loginsBefore);assert(await page.locator('.enter-wiki').isVisible());
   await page.close();page=null;
  }
  console.log('401, 503, network failure, HTML, malformed JSON, invalid status shape, and timeout all keep the landing usable without retry/login loops.');
  state.mode='anonymous';const noJsCount=state.statusHits;
  await open(false);
  assert.equal(state.statusHits,noJsCount);assert(await page.locator('.enter-wiki').isVisible());
  await page.locator('.enter-wiki').click();await page.waitForURL(loginOrigin+'/login');
  assert.equal(state.loginHits,loginsBefore+1);assert.equal(state.otherApiHits,0);
  assert.deepEqual(errors,[]);
  console.log('With JavaScript disabled the public page makes no auth probe and its native entry link still reaches Access login.');
 }finally{
  if(page)await page.close();await closeServer(site);await closeServer(login);
 }
}
async function checkAccessRedirects(browser){
 // Two real origins are necessary: a mocked TypeError or mocked opaqueredirect
 // cannot establish Chromium's CORS/redirect behavior for an Access login page.
 const state={authenticated:true,requireLogin:false,known:null,completed:true,submissions:[],polls:0,loginHits:0};
 let siteOrigin;
 const login=http.createServer((req,res)=>{
  state.loginHits++;
  res.writeHead(200,{'Content-Type':'text/html'});
  res.end('<!doctype html><a id="login" href="'+siteOrigin+'/resume">Sign in</a>');
 });
 const loginOrigin=await listen(login);
 const site=http.createServer(async(req,res)=>{
  const path=new URL(req.url,siteOrigin).pathname;
  const redirect=()=>{res.writeHead(302,{Location:loginOrigin+'/login'});res.end();};
  const json=(code,data)=>{res.writeHead(code,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
  if(path==='/probe')return redirect();
  if(path==='/resume'){
   state.authenticated=true;res.writeHead(302,{Location:'/app/'});res.end();return;
  }
  if(path==='/api/query'){
   let body='';for await(const chunk of req)body+=chunk;
   const request=JSON.parse(body);state.submissions.push(request);
   if(!state.authenticated)return redirect();
   state.known=request;return json(202,{id:request.request_id});
  }
  if(path.startsWith('/api/jobs/')){
   state.polls++;
   if(!state.authenticated)return redirect();
   if(!state.known)return json(404,{error:'missing'});
   return json(200,state.completed?{status:'completed',question:state.known.question,answer:'resumed after login',sources:[]}:{status:'running',question:state.known.question});
  }
  if(path==='/api/status')return json(200,{notes:35});
  if(path==='/app/' && state.requireLogin && !state.authenticated)return redirect();
  const name=fileName(path);
  if(!['index.html','app/index.html','app.js','style.css','glass.css'].includes(name) && !name.startsWith('assets/branding/')){res.writeHead(404);res.end();return;}
  if(!fs.existsSync(root+name)){res.writeHead(404);res.end();return;}
  res.writeHead(200,{'Content-Type':contentType(name)});
  res.end(fs.readFileSync(root+name));
 });
 let page;
 try{
  siteOrigin=await listen(site);page=await browser.newPage();
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(siteOrigin+'/app/');
  const followed=await page.evaluate(async()=>{try{await fetch('/probe');return 'unexpected success';}catch(error){return error.name;}});
  assert.equal(followed,'TypeError');assert.equal(state.loginHits,1);
  const manual=await page.evaluate(async()=>{const r=await fetch('/probe',{redirect:'manual'});return {type:r.type,status:r.status,redirected:r.redirected};});
  assert.deepEqual(manual,{type:'opaqueredirect',status:0,redirected:false});
  assert.equal(state.loginHits,1,'manual mode must not fetch the cross-origin login page');
  console.log('Real two-origin redirect: default fetch fails CORS; manual fetch returns opaqueredirect without following.');

  await page.clock.install();
  await page.evaluate(()=>{history=['a'.repeat(32)];});
  state.authenticated=false;state.requireLogin=true;
  await send(page,'로그인 후 복구할 질문');await page.locator('#reauthenticate').waitFor();
  const stored=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')));
  assert.equal(stored.version,3);assert.equal(stored.accepted,false);
  assert.deepEqual(stored.request,state.submissions[0]);assert.deepEqual(stored.request.history,[{job_id:'a'.repeat(32)}]);
  assert(await page.evaluate(()=>busy && activeJob!==null));
  assert((await page.locator('#notice').textContent()).includes('로그인'));
  await page.clock.fastForward(16000);
  assert.equal(state.submissions.length,1);assert.equal(state.polls,0);assert.equal(state.loginHits,1);
  const previousPage=page;
  await page.locator('#reauthenticate').click();await page.waitForURL(loginOrigin+'/login');
  assert.equal(page,previousPage);assert.equal(page.context().pages().length,1);
  await page.locator('#login').click();await page.waitForURL(siteOrigin+'/app/');
  await page.getByText('resumed after login',{exact:true}).waitFor();
  assert.deepEqual(state.submissions[1],stored.request);
  assert.equal(await page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  console.log('Expired initial submission stops retries; same-tab login preserves and resends the original ID/question/job references.');

  state.completed=false;state.submissions=[];state.polls=0;state.known=null;
  await page.evaluate(()=>{history=['b'.repeat(32),'c'.repeat(32)];});
  await send(page,'진행 중 로그인 만료');
  await page.waitForFunction(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')||'null')?.accepted===true);
  const confirmedId=await page.evaluate(()=>sessionStorage.getItem('wiki-active-job'));
  state.authenticated=false;
  await page.clock.fastForward(1800);await page.locator('#reauthenticate').waitFor();
  const stoppedPolls=state.polls;await page.clock.fastForward(16000);
  assert.equal(state.polls,stoppedPolls);assert.equal(state.submissions.length,1);
  assert.equal(await page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),confirmedId);
  state.completed=true;
  await page.locator('#reauthenticate').click();await page.waitForURL(loginOrigin+'/login');
  await page.locator('#login').click();await page.waitForURL(siteOrigin+'/app/');
  await page.getByText('resumed after login',{exact:true}).waitFor();
  assert.equal(state.submissions.length,1,'an accepted request must not be submitted again after login');
  const resumedId=JSON.parse(confirmedId).id;
  await send(page,'LOGIN-CONTEXT-FOLLOW-UP');await page.waitForFunction(()=>!busy);
  assert.deepEqual(state.submissions[1].history,['b'.repeat(32),'c'.repeat(32),resumedId].map(job_id=>({job_id})), 'same-tab Access login preserves prior accepted conversation references');
  assert.equal(await page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  assert.deepEqual(errors,[]);
  console.log('Expired polling stops retries and preserves accepted reference-only state; login resumes the accepted job without resubmission.');
 }finally{
  if(page)await page.close();await closeServer(site);await closeServer(login);
 }
}
async function checkPrivateHistory(browser){
 const token='[[PRIVATE_'+ '1'.repeat(32)+']]';
 const unknown='[[PRIVATE_'+ '2'.repeat(32)+']]';
 const secret='SYNTHETIC-SECRET-`code`-**bold**\n<script>window.secretExecuted=true</script>';
 const display=['plain '+token,'`'+token+'`','**'+token+'**','```',token,'```',unknown].join('\n');
 const submissions=[];
 let holdSubmission;
 const heldReady=new Promise(resolve=>{holdSubmission=resolve;});
 const session=await setup(browser,(route,url)=>{
  if(url.pathname==='/api/query'){
   submissions.push(JSON.parse(route.request().postData()));
   if(submissions.length===2){holdSubmission(route);return;}
   return route.fulfill({status:202,json:{id:submissions.at(-1).request_id}});
  }
  return route.fulfill({json:{status:'completed',question:submissions.at(-1).question,
   answer:'This legacy answer must not be rendered',display_answer:display,protected_values:{[token]:secret},sources:[]}});
 });
 try{
  await send(session.page,'FIRST-QUESTION-ONLY');await session.page.waitForFunction(()=>!busy);
  const body=session.page.locator('.message.assistant .message-body');
  assert.equal(await body.locator('p').nth(0).textContent(),'plain '+secret);
  assert.equal(await body.locator('p > code').textContent(),secret);
  assert.equal(await body.locator('p > strong').textContent(),secret);
  assert.equal(await body.locator('pre > code').textContent(),secret+'\n');
  assert.equal(await body.locator('p').last().textContent(),'[보호된 값]');
  assert.equal(await body.locator('script').count(),0);
  assert.equal(await session.page.evaluate(()=>window.secretExecuted),undefined);
  assert(!(await body.textContent()).includes('This legacy answer'));
  assert.deepEqual(await session.page.evaluate(()=>history),[submissions[0].request_id]);
  await send(session.page,'SECOND-QUESTION-ONLY');
  const heldSubmission=await heldReady;
  await session.page.waitForFunction(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')||'null')?.version===3);
  const storedText=await session.page.evaluate(()=>sessionStorage.getItem('wiki-active-job'));
  const expected=[{job_id:submissions[0].request_id}];
  assert.deepEqual(submissions[1].history,expected);
  assert.deepEqual(JSON.parse(storedText).request.history,expected);
  for(const forbidden of [secret,'SYNTHETIC-SECRET','FIRST-QUESTION-ONLY',token,display]){
   assert(!JSON.stringify(submissions[1]).includes(forbidden),'follow-up must contain only job references');
   assert(!storedText.includes(forbidden),'pending storage must not persist prior answer/question');
  }
  await heldSubmission.fulfill({status:202,json:{id:submissions[1].request_id}});
  await session.page.waitForFunction(()=>!busy);
  assert.deepEqual(session.errors,[]);
  console.log('Protected values render literally after Markdown parsing in text/code/bold/fences; unknown values stay hidden. Follow-up POST and pending storage contain only job references.');
 }finally{await session.page.close();}

 const legacyId='d'.repeat(32),legacySubmissions=[];
 let legacyAccepted=false,holdLegacy;
 const legacyReady=new Promise(resolve=>{holdLegacy=resolve;});
 const legacy=await setup(browser,(route,url)=>{
  if(url.pathname==='/api/query'){
   legacySubmissions.push(JSON.parse(route.request().postData()));holdLegacy(route);return;
  }
  if(!legacyAccepted)return route.fulfill({status:404,json:{error:'not accepted'}});
  return route.fulfill({json:{status:'completed',question:'LEGACY-CURRENT-QUESTION',answer:'legacy recovered',sources:[]}});
 });
 try{
  await legacy.page.evaluate(({id,secret})=>sessionStorage.setItem('wiki-active-job',JSON.stringify({version:1,id,request:{request_id:id,question:'LEGACY-CURRENT-QUESTION',history:[{role:'user',content:'LEGACY-PRIOR-QUESTION'},{role:'assistant',content:secret}]}})),{id:legacyId,secret});
  await legacy.page.reload();
  await legacy.page.waitForFunction(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')||'null')?.version===3);
  await legacy.page.waitForFunction(()=>document.getElementById('messages').textContent.includes('LEGACY-CURRENT-QUESTION'));
  const legacyRoute=await legacyReady;
  const stored=await legacy.page.evaluate(()=>sessionStorage.getItem('wiki-active-job'));
  const cleanRequest={question:'LEGACY-CURRENT-QUESTION',history:[],request_id:legacyId};
  assert.deepEqual(legacySubmissions,[cleanRequest]);
  assert.deepEqual(JSON.parse(stored),{version:3,id:legacyId,accepted:false,history:[],cancelRequested:false,request:cleanRequest});
  assert(!stored.includes('SYNTHETIC-SECRET'));assert(!stored.includes('LEGACY-PRIOR-QUESTION'));
  legacyAccepted=true;await legacyRoute.fulfill({status:202,json:{id:legacyId}});
  await legacy.page.getByText('legacy recovered',{exact:true}).waitFor();
  assert.equal(await legacy.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  assert.deepEqual(await legacy.page.evaluate(()=>history),[legacyId]);
  assert.deepEqual(legacy.errors,[]);
  console.log('Version 1 pending storage migrates to version 3 before retry: ID/current question preserved, all raw prior history discarded.');
 }finally{await legacy.page.close();}
}
async function checkAcceptedHistoryRecovery(browser){
 const submissions=[];
 let completed=true;
 const session=await setup(browser,(route,url)=>{
  if(url.pathname==='/api/query'){
   const request=JSON.parse(route.request().postData());submissions.push(request);
   return route.fulfill({status:202,json:{id:request.request_id}});
  }
  return route.fulfill({json:{status:completed?'completed':'running',question:submissions.at(-1).question,answer:'SYNTHETIC-RESTORED-ANSWER',sources:[]}});
 });
 try{
  for(const question of ['FIRST-PRIVATE-QUESTION','SECOND-PRIVATE-QUESTION']){
   await send(session.page,question);await session.page.waitForFunction(()=>!busy);
  }
  completed=false;await send(session.page,'THIRD-PRIVATE-QUESTION');
  await session.page.waitForFunction(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')||'null')?.accepted===true);
  const storedText=await session.page.evaluate(()=>sessionStorage.getItem('wiki-active-job'));
  const stored=JSON.parse(storedText);
  assert.deepEqual(stored.history,submissions.slice(0,2).map(request=>({job_id:request.request_id})));
  assert.equal(stored.request,undefined);
  for(const privateText of ['SYNTHETIC-RESTORED-ANSWER','FIRST-PRIVATE-QUESTION','SECOND-PRIVATE-QUESTION','THIRD-PRIVATE-QUESTION'])assert(!storedText.includes(privateText));
  completed=true;await session.page.reload();await session.page.waitForFunction(()=>!busy);
  assert.equal(submissions.length,3,'reload must only look up an accepted request');
  await send(session.page,'FOURTH-QUESTION');await session.page.waitForFunction(()=>!busy);
  assert.deepEqual(submissions[3].history,submissions.slice(0,3).map(request=>({job_id:request.request_id})),'reload preserves all three prior job references for the next real POST');
  assert.deepEqual(session.errors,[]);
  console.log('Accepted follow-up reload preserves prior references without persisting any question or restored answer, and never replays the accepted POST.');
 }finally{await session.page.close();}
}
async function checkCancellationRaces(browser){
 // Original POST admission can outlive a reload. Keep it unregistered even
 // after the restored page's first GET 404; only a server cancellation ACK is
 // enough to clear intent. The backend suite checks the real admission lock.
 for(const reload of [false,true]){
  let heldSubmission,heldCancellation,marked=false,missingLookups=0,cancelSeen;
  const cancellationReady=new Promise(resolve=>{cancelSeen=resolve;});
  const submissions=[],cancellations=[];
  const session=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){
    submissions.push(JSON.parse(route.request().postData()));heldSubmission=route;return;
   }
   if(url.pathname.endsWith('/cancel')){
    cancellations.push(JSON.parse(route.request().postData()));
    if(reload && cancellations.length===1)return route.abort('failed');
    if(reload){heldCancellation=route;cancelSeen();return;}
    marked=true;return route.fulfill({json:{status:'cancelled'}});
   }
   if(marked)return route.fulfill({json:{status:'cancelled'}});
   missingLookups++;return route.fulfill({status:404,json:{error:'original POST still not registered'}});
  });
  try{
   await send(session.page,'CANCEL-BEFORE-ACCEPT');
   await session.page.locator('#cancel').click();
   if(reload){
    await session.page.waitForFunction(()=>document.getElementById('notice').classList.contains('error'));
    const stored=JSON.parse(await session.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')));
    assert.equal(stored.cancelRequested,true);assert.equal(stored.accepted,false);
    await session.page.reload();await cancellationReady;
    await session.page.waitForFunction(()=>document.getElementById('notice').textContent.includes('취소 요청을 다시 확인'));
    assert(missingLookups>=1,'restored GET must encounter the original POST before admission');
    assert(await session.page.evaluate(()=>busy));
    assert.equal(await session.page.evaluate(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')).cancelRequested),true,'404 must not discard cancellation intent');
    marked=true;await heldCancellation.fulfill({json:{status:'cancelled'}});
   }
   await session.page.waitForFunction(()=>!busy);
   assert.deepEqual(cancellations,Array(reload?2:1).fill({pending:true}));
   assert.equal(submissions.length,1,'cancellation must never replay the original query');
   assert((await session.page.locator('#notice').textContent()).includes('중단했어요'));
   assert.equal(await session.page.locator('.message.assistant').count(),0);
   assert.equal(await session.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
   // Model the delayed original HTTP handler returning the marker recorded
   // above, rather than admitting a worker after the browser cleared state.
   assert(marked);
   await heldSubmission.fulfill({status:200,json:{id:submissions[0].request_id,status:'cancelled'}}).catch(()=>{});
   await session.page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,0)));
   assert.equal(await session.page.evaluate(()=>activeJob),null);
   assert.equal(await session.page.locator('.message.assistant').count(),0);
   assert.deepEqual(session.errors,[]);
  }finally{await session.page.close();}
 }
 console.log('Pre-admission cancellation uses pending:true; reload + initial GET 404 retains intent until the server records cancellation, and a late original POST cannot resurrect the UI.');

 // Capacity rejection must preserve pending intent; successful retry creates
 // the marker without submitting any question to the server.
 let withdrawnPosts=0,withdrawnCancels=0;
 const withdrawn=await setup(browser,(route,url)=>{
  if(url.pathname==='/api/query'){withdrawnPosts++;return route.fulfill({status:202,json:{id:'e'.repeat(32)}});}
  if(url.pathname.endsWith('/cancel')){
   withdrawnCancels++;
   return route.fulfill(withdrawnCancels===1?{status:429,json:{error:'pending cancellation capacity'}}:{json:{status:'cancelled'}});
  }
  return route.fulfill({status:404,json:{error:'original POST not registered'}});
 });
 try{
  await withdrawn.page.clock.install();
  await withdrawn.page.evaluate(()=>sessionStorage.setItem('wiki-active-job',JSON.stringify({version:3,id:'e'.repeat(32),accepted:false,history:[],cancelRequested:true,request:{request_id:'e'.repeat(32),question:'WITHDRAWN-QUESTION',history:[]}})));
  await withdrawn.page.reload();
  await withdrawn.page.waitForFunction(()=>document.getElementById('notice').textContent.includes('취소 요청을 다시 확인'));
  assert.equal(withdrawnCancels,1);assert.equal(withdrawnPosts,0);assert(await withdrawn.page.evaluate(()=>busy));
  assert.equal(await withdrawn.page.evaluate(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')).cancelRequested),true);
  await withdrawn.page.clock.fastForward(2500);await withdrawn.page.waitForFunction(()=>!busy);
  assert.equal(withdrawnCancels,2);assert.equal(withdrawnPosts,0);assert.equal(await withdrawn.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  assert((await withdrawn.page.locator('#notice').textContent()).includes('중단했어요'));assert.deepEqual(withdrawn.errors,[]);
 }finally{await withdrawn.page.close();}
 console.log('Pending-cancellation capacity 429 plus GET 404 retains intent and retries cancellation without starting new AI work.');

 // A user can withdraw while either the initial POST or an idempotent retry is
 // in flight. Rejecting that POST does not rule out another delayed copy, so a
 // failed cancellation still needs its own retry and server acknowledgement.
 for(const submission of ['initial','retry'])for(const cancelFailure of ['503','network'])for(const admissionStatus of [409,429]){
  const queries=[],cancellations=[];
  let heldAdmission,heldCancellation,marked=false,admissionSeen,cancellationSeen;
  const admissionReady=new Promise(resolve=>{admissionSeen=resolve;});
  const cancellationReady=new Promise(resolve=>{cancellationSeen=resolve;});
  const race=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){
    queries.push(JSON.parse(route.request().postData()));
    if(submission==='retry' && queries.length===1)return route.abort('failed');
    heldAdmission=route;admissionSeen();return;
   }
   if(url.pathname.endsWith('/cancel')){
    cancellations.push(JSON.parse(route.request().postData()));
    if(cancellations.length===1)return cancelFailure==='network'?route.abort('failed'):route.fulfill({status:503,json:{error:'temporary cancellation failure'}});
    heldCancellation=route;cancellationSeen();return;
   }
   return route.fulfill(marked?{json:{status:'cancelled'}}:{status:404,json:{error:'original POST not registered'}});
  });
  try{
   await race.page.clock.install();await send(race.page,'WITHDRAWN-DURING-'+submission.toUpperCase());await waitForSignal(admissionReady,'expected the held '+submission+' query');
   await race.page.locator('#cancel').click();
   await race.page.waitForFunction(()=>activeState?.cancelFailures===1);
   const before=JSON.parse(await race.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')));
   assert.equal(before.cancelRequested,true);assert.equal(before.accepted,false);
   await heldAdmission.fulfill({status:admissionStatus,json:{error:'ADMISSION-REJECTED'}});
   await race.page.waitForFunction(()=>document.getElementById('notice').textContent==='ADMISSION-REJECTED' || document.getElementById('notice').textContent.includes('취소 요청을 다시 확인'));
   assert(await race.page.evaluate(()=>busy),'admission rejection must not release a withdrawn job before cancellation ACK');
   assert.deepEqual(JSON.parse(await race.page.evaluate(()=>sessionStorage.getItem('wiki-active-job'))),before);
   assert.equal(await race.page.locator('#question').inputValue(),'','a withdrawn question must not return to the composer');
   await race.page.clock.fastForward(2500);await waitForSignal(cancellationReady,'withdrawal must retry cancellation after admission rejection');
   assert.deepEqual(cancellations,[{pending:true},{pending:true}]);
   const expectedPosts=submission==='retry'?2:1;
   assert.equal(queries.length,expectedPosts);
   assert(queries.every(query=>JSON.stringify(query)===JSON.stringify(queries[0])),'any retry before withdrawal must use the same payload and idempotency key');
   await race.page.clock.fastForward(6000);
   assert.equal(queries.length,expectedPosts,'no query may be replayed after withdrawal');
   assert.equal(cancellations.length,2,'an in-flight cancellation must not be duplicated');
   assert.equal(await race.page.evaluate(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')).cancelRequested),true);
   assert(await race.page.locator('#cancel').isDisabled());
   marked=true;await heldCancellation.fulfill({json:{status:'cancelled'}});
   await race.page.waitForFunction(()=>!busy);
   assert.equal(await race.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
   assert.equal(await race.page.locator('.message.assistant').count(),0);
   assert((await race.page.locator('#notice').textContent()).includes('중단했어요'));
   await race.page.clock.fastForward(30000);
   assert.equal(queries.length,expectedPosts);assert.equal(cancellations.length,2);assert.deepEqual(race.errors,[]);
  }finally{await race.page.close();}
 }
 console.log('Withdrawal during initial/retried POST survives cancellation 503/network failure plus admission 409/429; persisted intent retries cancellation only and clears after ACK.');

 // Keep A's cancellation response pending while A completes and B starts.
 // A late success, failure, or auth error must have no effects on B.
 for(const status of [200,500,401]){
  let heldCancel,phase='running',cancelSeen;
  const ready=new Promise(resolve=>{cancelSeen=resolve;});
  const submissions=[];
  const session=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){
    submissions.push(JSON.parse(route.request().postData()));return route.fulfill({status:202,json:{id:submissions.at(-1).request_id}});
   }
   if(url.pathname.endsWith('/cancel')){heldCancel=route;cancelSeen();return;}
   return route.fulfill({json:{status:phase,question:submissions.at(-1).question,answer:'COMPLETED-A',sources:[]}});
  });
  try{
   await session.page.clock.install();await send(session.page,'OLD-JOB');
   await session.page.waitForFunction(()=>document.getElementById('notice').textContent.includes('답변을 준비'));
   await session.page.locator('#cancel').click();await ready;
   phase='completed';await session.page.clock.fastForward(1800);await session.page.waitForFunction(()=>!busy);
   await session.page.locator('#reset').click();phase='running';await send(session.page,'NEW-JOB');
   await session.page.waitForFunction(()=>document.getElementById('notice').textContent.includes('답변을 준비'));
   assert(await session.page.locator('#cancel').isEnabled(),'a previous in-flight cancellation must not disable the new job');
   const noticeBefore=await session.page.locator('#notice').textContent();
   const delivered=session.page.waitForResponse(response=>new URL(response.url()).pathname.endsWith('/cancel'));
   await heldCancel.fulfill({status,json:status===200?{status:'cancelling'}:{error:'OLD-CANCEL-ERROR'}});
   await delivered;
   await session.page.waitForFunction(()=>activeState && !activeState.cancelRequested);
   // Flush the fetch continuation before checking stale UI effects.
   await session.page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,0)));
   assert.equal(await session.page.locator('#notice').textContent(),noticeBefore);
   assert(await session.page.locator('#cancel').isEnabled());assert(await session.page.locator('#reauthenticate').isHidden());
   assert.equal(await session.page.evaluate(()=>activeJob),submissions[1].request_id);
   assert.equal(await session.page.evaluate(()=>sessionPhase),'ready');assert.deepEqual(session.errors,[]);
  }finally{await session.page.close();}
 }
 console.log('Late cancellation success, 500, and 401 from an old job cannot disable, relabel, or force login on the next job.');

 // Accepted cancellation is retained through an expired session, but the
 // original question is never submitted again after authentication resumes.
 let authenticated=true,cancelled=false,cancelCalls=0,posts=0,jobId;
 const recovery=await setup(browser,(route,url)=>{
  if(url.pathname==='/api/query'){posts++;jobId=JSON.parse(route.request().postData()).request_id;return route.fulfill({status:202,json:{id:jobId}});}
  if(url.pathname.endsWith('/cancel')){
   cancelCalls++;
   if(!authenticated)return route.fulfill({status:401,json:{error:'expired cancel session'}});
   cancelled=true;return route.fulfill({json:{status:'cancelling'}});
  }
  return route.fulfill({json:{status:cancelled?'cancelled':'running',question:'CANCEL-AFTER-LOGIN'}});
 });
 try{
  await send(recovery.page,'CANCEL-AFTER-LOGIN');
  await recovery.page.waitForFunction(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')||'null')?.accepted===true);
  authenticated=false;await recovery.page.locator('#cancel').click();await recovery.page.locator('#reauthenticate').waitFor();
  const stored=JSON.parse(await recovery.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')));
  assert.equal(stored.accepted,true);assert.equal(stored.cancelRequested,true);assert.equal(stored.request,undefined);
  authenticated=true;await recovery.page.reload();await recovery.page.waitForFunction(()=>!busy);
  assert.equal(posts,1);assert.equal(cancelCalls,2);assert((await recovery.page.locator('#notice').textContent()).includes('중단했어요'));assert.deepEqual(recovery.errors,[]);
 }finally{await recovery.page.close();}
 console.log('Expired cancellation authentication retains intent and retries only cancellation after recovery, without replaying the accepted query.');
}
async function assertComposerBlocked(page){
 for(const selector of ['#question','#send','#reset','[data-question]']){
  for(const control of await page.locator(selector).all())assert(await control.isDisabled(),selector+' must follow the session/job interaction gate');
 }
}
async function attemptBlockedInteractions(page){
 // DOM events exercise the handlers as well as disabled-button rendering.
 // requestSubmit/keyboard/preset/reset must all obey the same permission gate.
 const before=await page.evaluate(()=>({draft:document.getElementById('question').value,messages:document.getElementById('messages').textContent,
  notice:document.getElementById('notice').textContent,stored:sessionStorage.getItem('wiki-active-job'),history:[...history]}));
 await page.evaluate(()=>{
  document.getElementById('question-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
  document.getElementById('question-form').requestSubmit();
  document.getElementById('question').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
  document.querySelector('[data-question]').dispatchEvent(new MouseEvent('click',{bubbles:true}));
  document.getElementById('reset').dispatchEvent(new MouseEvent('click',{bubbles:true}));
 });
 assert.deepEqual(await page.evaluate(()=>({draft:document.getElementById('question').value,messages:document.getElementById('messages').textContent,
  notice:document.getElementById('notice').textContent,stored:sessionStorage.getItem('wiki-active-job'),history:[...history]})),before,
  'blocked events must preserve draft, conversation references, notice, and pending intent');
}
async function checkSessionTransitions(browser){
 // Regression: a source 401 used to leave an idle composer active. A query
 // accepted after another tab restored auth would then have no polling/cancel.
 let authenticated=true,posts=0,polls=0,sources=0,cancels=0;
 const source=await setup(browser,(route,url)=>{
  if(url.pathname==='/api/query'){posts++;return route.fulfill({status:202,json:{id:JSON.parse(route.request().postData()).request_id}});}
  if(url.pathname==='/api/source'){sources++;return route.fulfill(authenticated?{json:{text:'SOURCE'}}:{status:401,json:{error:'expired'}});}
  if(url.pathname.endsWith('/cancel')){cancels++;return route.fulfill({json:{status:'cancelled'}});}
  polls++;return route.fulfill({json:{status:'completed',question:'SOURCE-AUTH-QUESTION',answer:'SOURCE-AUTH-ANSWER',sources:[{path:'A.md',line:1}]}});
 });
 try{
  await source.page.clock.install();await send(source.page,'SOURCE-AUTH-QUESTION');await source.page.waitForFunction(()=>!busy);
  await source.page.locator('#question').fill('PRESERVE-UNSUBMITTED-DRAFT');
  authenticated=false;await source.page.getByRole('button',{name:'A · 1행',exact:true}).click();
  await source.page.locator('#source-reauthenticate').waitFor();await source.page.locator('#close-source').click();
  assert.equal(await source.page.locator('#source-content').textContent(),'');
  await assertComposerBlocked(source.page);
  assert(await source.page.locator('.sources button').isDisabled());
  assert.equal(await source.page.locator('#reauthenticate').textContent(),'다시 로그인하기');
  assert(await source.page.locator('#cancel').isHidden());
  const savedHistory=await source.page.evaluate(()=>[...history]);
  authenticated=true; // Recovery elsewhere must not silently bypass local state.
  await attemptBlockedInteractions(source.page);await source.page.clock.fastForward(30000);
  assert.equal(posts,1);assert.equal(polls,1);assert.equal(sources,1);assert.equal(cancels,0);
  assert.deepEqual(await source.page.evaluate(()=>[...history]),savedHistory);
  assert.equal(await source.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  await source.page.locator('#reauthenticate').click();await source.page.waitForFunction(()=>sessionPhase==='ready');
  assert(await source.page.locator('#reauthenticate').isHidden());
  await send(source.page,'AFTER-EXPLICIT-LOGIN');await source.page.waitForFunction(()=>!busy);
  assert.equal(posts,2);assert.equal(polls,2);assert.deepEqual(source.errors,[]);
 }finally{await source.page.close();}
 console.log('Source 401 blocks send, Enter, presets, reset, and source reads while preserving the current draft/history; external auth recovery cannot create a stuck query, and explicit login restores usable controls.');

 // A slow initial status result cannot race a new query. Non-auth failures
 // unlock useful retry paths; a definite 401 keeps all entry paths gated.
 for(const outcome of ['ready','unauthorized','unavailable','network']){
  let statusRoute,statusCalls=0,postCalls=0,jobCalls=0,lastQuestion;
  const session=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){postCalls++;lastQuestion=JSON.parse(route.request().postData()).question;return route.fulfill({status:202,json:{id:JSON.parse(route.request().postData()).request_id}});}
   jobCalls++;return route.fulfill({json:{status:'completed',question:lastQuestion,answer:'SESSION-READY',sources:[]}});
  },{statusHandler:route=>{statusCalls++;if(statusCalls===1){statusRoute=route;return;}return route.fulfill({json:{notes:35}});}});
  try{
   await session.page.waitForFunction(()=>sessionPhase==='checking');
   await assertComposerBlocked(session.page);
   await session.page.evaluate(()=>{document.getElementById('question').value='DRAFT-DURING-STATUS';});
   await attemptBlockedInteractions(session.page);
   assert.equal(postCalls,0);assert.equal(jobCalls,0);
   if(outcome==='network')await statusRoute.abort('failed');
   else await statusRoute.fulfill(outcome==='ready'?{json:{notes:35}}:outcome==='unauthorized'?{status:401,json:{error:'initial session expired'}}:{status:503,json:{error:'status temporarily unavailable'}});
   if(outcome==='unauthorized'){
    await session.page.locator('#reauthenticate').waitFor();await assertComposerBlocked(session.page);await attemptBlockedInteractions(session.page);
    assert.equal(postCalls,0);assert.equal(jobCalls,0);
    await session.page.locator('#reauthenticate').click();
   }
   await session.page.waitForFunction(()=>sessionPhase==='ready');
   if(['unavailable','network'].includes(outcome))assert((await session.page.locator('#notice').textContent()).includes('질문을 보내 다시 연결'));
   await session.page.locator('[data-question]').first().click();
   const preset=await session.page.locator('#question').inputValue();assert(preset.length>0);
   await session.page.locator('#question').press('Shift+Enter');
   await session.page.locator('#question').dispatchEvent('keydown',{key:'Enter',isComposing:true});
   assert.equal(postCalls,0,'Shift+Enter and IME composition must not submit');
   await session.page.locator('#question').press('Enter');await session.page.waitForFunction(()=>!busy);
   assert.equal(postCalls,1);assert.equal(jobCalls,1);assert.equal(lastQuestion,preset);
   await session.page.locator('#reset').click();
   assert.equal(await session.page.locator('.message').count(),0);assert.equal(await session.page.locator('#question').inputValue(),'');
   assert.deepEqual(session.errors,[]);
  }finally{await session.page.close();}
 }
 console.log('Delayed initial status gates all submission paths; 401 requires login, 503/network failures permit reconnection, and ready-state presets, Enter, Shift+Enter, IME, and reset retain their intended behavior.');

 // Both accepted and unconfirmed work are restored before the status check.
 // Withdrawal while checking/expired must persist without making an API call,
 // then retry cancellation only after login, without replaying the question.
 for(const accepted of [false,true])for(const withdrawAt of ['checking','required']){
  const id=(accepted?'a':'b').repeat(32),prior=[{job_id:'c'.repeat(32)}];
  const pending={version:3,id,accepted,history:prior,cancelRequested:false};
  if(!accepted)pending.request={request_id:id,question:'PENDING-PRIVATE-QUESTION',history:prior};
  let statusRoute,statusCalls=0,postCalls=0,jobCalls=0;
  const cancelBodies=[];
  const session=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){postCalls++;return route.fulfill({status:500,json:{error:'must never replay a withdrawn question'}});}
   if(url.pathname.endsWith('/cancel')){cancelBodies.push(JSON.parse(route.request().postData()));return route.fulfill({json:{status:'cancelled'}});}
   jobCalls++;return route.fulfill({status:404,json:{error:'pending admission'}});
  },{
   statusHandler:route=>{statusCalls++;if(statusCalls===1){statusRoute=route;return;}return route.fulfill({json:{notes:35}});},
   beforeLoad:page=>page.addInitScript(pending=>{if(!sessionStorage.getItem('fixture-seeded')){sessionStorage.setItem('fixture-seeded','1');sessionStorage.setItem('wiki-active-job',JSON.stringify(pending));}},pending)
  });
  try{
   await session.page.waitForFunction(()=>sessionPhase==='checking' && busy);
   await assertComposerBlocked(session.page);assert(await session.page.locator('#cancel').isEnabled());
   assert.deepEqual(JSON.parse(await session.page.evaluate(()=>sessionStorage.getItem('wiki-active-job'))),pending);
   assert.equal(postCalls,0);assert.equal(jobCalls,0);assert.equal(cancelBodies.length,0);
   if(withdrawAt==='checking')await session.page.locator('#cancel').click();
   await statusRoute.fulfill({status:401,json:{error:'initial session expired'}});await session.page.locator('#reauthenticate').waitFor();
   if(withdrawAt==='required')await session.page.locator('#cancel').click();
   assert(await session.page.locator('#cancel').isDisabled());
   assert((await session.page.locator('#notice').textContent()).includes('다시 로그인하면 중단을 요청'));
   assert.equal(await session.page.locator('#reauthenticate').textContent(),'다시 로그인하고 중단하기');
   const stored=JSON.parse(await session.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')));
   assert.deepEqual(stored,{...pending,cancelRequested:true});
   await attemptBlockedInteractions(session.page);
   assert.equal(postCalls,0);assert.equal(jobCalls,0);assert.equal(cancelBodies.length,0);
   await session.page.locator('#reauthenticate').click();await session.page.waitForFunction(()=>sessionPhase==='ready' && !busy);
   assert.equal(postCalls,0);assert.deepEqual(cancelBodies,[accepted?{}:{pending:true}]);
   assert.equal(await session.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
   assert((await session.page.locator('#notice').textContent()).includes('중단했어요'));assert.deepEqual(session.errors,[]);
  }finally{await session.page.close();}
 }
 console.log('Restored accepted/unconfirmed jobs wait for initial authentication; cancellation during checking or expired login preserves exact intent and resumes cancellation only after login, without any query replay.');
}
async function checkStreaming(browser){
 let job,stream,submits=0,connections=0,deny=false;
 const token='[[PRIVATE_'+'a'.repeat(32)+']]';
 const secret='SYNTHETIC <img src=x onerror=alert(1)> VALUE';
 const site=http.createServer((req,res)=>{
  const url=new URL(req.url,'http://localhost');
  const json=(status,data)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
  if(url.pathname==='/api/status')return json(200,{notes:2});
  if(url.pathname==='/api/query'){
   let body='';req.on('data',b=>body+=b);req.on('end',()=>{
    const data=JSON.parse(body);submits++;job={id:data.request_id,question:data.question,status:'running',stage:'answering'};
    json(202,{id:job.id});
   });return;
  }
  if(url.pathname.endsWith('/cancel')){
   job.status='cancelled';if(stream&&!stream.destroyed)stream.end(JSON.stringify(job)+'\n');
   return json(200,job);
  }
  if(url.pathname.startsWith('/api/jobs/')){
   connections++;
   if(deny)return json(401,{error:'synthetic expired auth'});
   assert.equal(url.searchParams.get('stream'),'1');stream=res;
   res.writeHead(200,{'Content-Type':'application/x-ndjson'});
   res.write(JSON.stringify(job)+'\n');
   const bytes=Buffer.from(JSON.stringify({...job,preview:'생성 중인 문장 [보호된 값]'})+'\n');
   // Split within a multibyte Korean code point, not just a JSON boundary.
   const cut=bytes.indexOf(Buffer.from('생'))+1;
   res.write(bytes.subarray(0,cut));setTimeout(()=>{if(!res.destroyed)res.write(bytes.subarray(cut));},30);
   return;
  }
  const name=fileName(url.pathname);
  if(!fs.existsSync(root+name))return json(404,{});
  res.writeHead(200,{'Content-Type':contentType(name)});res.end(fs.readFileSync(root+name));
 });
 const origin=await listen(site),page=await browser.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const preview=()=>page.waitForFunction(()=>document.querySelector('.streaming .message-body')?.textContent.includes('생성 중'));
 try{
  await page.goto(origin+'/app/');await send(page);await preview();
  assert.equal(await page.locator('.sources').count(),0);
  assert.equal(await page.locator('#send').isDisabled(),true);
  assert(!(await page.locator('#messages').innerText()).includes(secret));
  assert(!(await page.evaluate(()=>sessionStorage.getItem('wiki-active-job'))).includes('생성 중'));
  job={...job,status:'completed',display_answer:'결과 '+token,answer:'결과 '+secret,protected_values:{[token]:secret},sources:[{path:'Example.md',line:1}]};
  stream.end(JSON.stringify(job)+'\n');
  await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  assert.equal(await page.locator('.assistant').count(),1);assert.equal(await page.locator('.streaming').count(),0);
  assert((await page.locator('.assistant').innerText()).includes(secret));assert.equal(await page.locator('.assistant img').count(),0);
  assert.equal(await page.locator('.sources button').count(),1);
  await send(page,'failed stream');await preview();
  stream.end(JSON.stringify({...job,status:'failed',error:'synthetic stream failure'})+'\n');
  await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('synthetic stream failure'));
  assert.equal(await page.locator('.streaming').count(),0);assert.equal(await page.locator('.assistant').count(),1);
  await send(page,'disconnect');await preview();
  const before=connections;stream.destroy();
  await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('연결을 다시'));
  while(connections===before)await new Promise(resolve=>setTimeout(resolve,50));
  await preview();assert.equal(submits,3,'stream reconnect must never re-submit an accepted query');
  await page.locator('#cancel').click();await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  assert.equal(await page.locator('.streaming').count(),0);
  await send(page,'auth expires');await preview();deny=true;stream.end();
  await page.waitForFunction(()=>!document.querySelector('#reauthenticate').hidden);
  assert.equal(await page.locator('.streaming').count(),0);
  assert.equal(JSON.parse(await page.evaluate(()=>sessionStorage.getItem('wiki-active-job'))).id,job.id);
  assert.equal(submits,4);assert.deepEqual(errors,[]);
  console.log('Real chunked streams render before completion, decode split UTF-8, restore values only after validation, and recover without duplicate requests.');
 }finally{await page.close();await closeServer(site);}
}
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  await checkLanding(browser);
  await checkStreaming(browser);
  await checkPrivateHistory(browser);
  await checkAcceptedHistoryRecovery(browser);
  await checkCancellationRaces(browser);
  await checkSessionTransitions(browser);
  let maxBytes=0,lastPayload,lastBody;
  const completedIds=[];
  const long=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){
    const body=route.request().postData(),data=JSON.parse(body);
    lastPayload=data;lastBody=body;
    maxBytes=Math.max(maxBytes,Buffer.byteLength(body));
    completedIds.push(data.request_id);
    return route.fulfill({status:202,json:{id:data.request_id}});
   }
   return route.fulfill({json:{status:'completed',question:'후속 질문',answer:'가'.repeat(8000),sources:[]}});
  });
  const sendConversation=async question=>{
   const expectedHistory=completedIds.slice(-3).map(job_id=>({job_id}));
   await send(long.page,question);await long.page.waitForFunction(()=>!busy);
   // Assert on the main test promise so failures always close the browser.
   assert.deepEqual(lastPayload.history,expectedHistory,'the outgoing request must contain only the expected completed job references');
   assert(Buffer.byteLength(lastBody)<=bodyLimit,'serialized UTF-8 request must fit the actual server body limit');
  };
  for(let i=0;i<5;i++)await sendConversation('후속 질문');
  const maximumQuestion='한'.repeat(4000);
  await sendConversation(maximumQuestion);
  assert.equal(lastPayload.question,maximumQuestion,'the maximum permitted Korean question must not be truncated');
  assert.equal(Buffer.byteLength(lastPayload.question,'utf8'),12000);
  assert.equal(lastPayload.history.length,3);
  assert(Buffer.byteLength(lastBody,'utf8')>lastBody.length+7999,'measure UTF-8 bytes, not JavaScript string length');
  assert(Buffer.byteLength(lastBody,'utf8')<=bodyLimit);
  assert(!(await long.page.locator('#notice').textContent()).includes('로그인'));
  console.log('Long conversation and 4,000-character Korean question: only three job references; actual serialized UTF-8 maximum='+maxBytes+' bytes, server limit='+bodyLimit+'.');
  await long.page.locator('#reset').click();
  assert.equal(await long.page.locator('.message').count(),0);
  assert(await long.page.locator('#welcome').isVisible());
  assert.equal(await long.page.locator('#question').inputValue(),'');
  completedIds.length=0;
  await sendConversation('새 대화의 첫 질문');
  assert.deepEqual(lastPayload.history,[],'Reset must clear outgoing conversation context as well as visible messages');
  assert.equal(await long.page.locator('.message.user').count(),1);
  console.log('Reset clears visible messages and the next real POST has no prior job references.');
  await long.page.close();

  const large=await setup(browser,route=>route.fulfill({status:413,contentType:'text/html',body:'too large'}));
  await send(large.page);await large.page.waitForFunction(()=>!busy);
  assert((await large.page.locator('#notice').textContent()).includes('너무 큽니다'));
  assert(!(await large.page.locator('#notice').textContent()).includes('로그인'));
  console.log('HTML 413 classified correctly.');await large.page.close();

  let polls=0;
  const reconnect=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query')return route.fulfill({status:202,json:{id:JSON.parse(route.request().postData()).request_id}});
   if(++polls===1)return route.abort('failed');
   return route.fulfill({json:{status:'completed',question:'테스트 질문',answer:'reconnected',sources:[]}});
  });
  await send(reconnect.page);
  await reconnect.page.waitForFunction(()=>document.getElementById('notice').textContent.includes('진행 중인 질문은 유지'));
  assert(await reconnect.page.evaluate(()=>activeJob!==null && busy));
  assert(await reconnect.page.locator('#cancel').isVisible());
  await reconnect.page.getByText('reconnected',{exact:true}).waitFor();
  console.log('Polling failure retains job and cancel button; response recovered.');await reconnect.page.close();

  let known=false,submits=0;
  const lost=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){
    submits++;if(submits===1)return route.abort('failed');known=true;
    return route.fulfill({status:202,json:{id:JSON.parse(route.request().postData()).request_id}});
   }
   if(!known)return route.fulfill({status:404,json:{error:'missing'}});
   return route.fulfill({json:{status:'completed',question:'테스트 질문',answer:'retried safely',sources:[]}});
  });
  await send(lost.page);await lost.page.getByText('retried safely',{exact:true}).waitFor();
  assert.equal(submits,2);console.log('Unknown submission retried with same request ID.');await lost.page.close();

  let completed=false;
  const reload=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query')return route.fulfill({status:202,json:{id:JSON.parse(route.request().postData()).request_id}});
   return route.fulfill({json:completed?{status:'completed',question:'복구 질문',answer:'restored after reload',sources:[]}:{status:'running',stage:'answering',question:'복구 질문'}});
  });
  await send(reload.page,'복구 질문');
  assert(await reload.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')));
  await reload.page.reload();completed=true;
  await reload.page.getByText('restored after reload',{exact:true}).waitFor();
  assert.equal(await reload.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  console.log('Reload restores active job; clears only after terminal status.');
  assert(!(await reload.page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)));
  assert.deepEqual(reload.errors,[]);await reload.page.close();

  let cancelAccepted=false,cancelTerminal=false,cancelId,cancelPath,cancelSubmits=0,cancellingPolls=0,nextCancellationPoll;
  const cancel=await setup(browser,async(route,url)=>{
   if(url.pathname==='/api/query'){
    cancelSubmits++;cancelId=JSON.parse(route.request().postData()).request_id;
    return route.fulfill({status:202,json:{id:cancelId}});
   }
   if(url.pathname.endsWith('/cancel')){
    cancelPath=url.pathname;cancelAccepted=true;
    return route.fulfill({json:{status:'cancelling'}});
   }
   const status=cancelTerminal?'cancelled':cancelAccepted?'cancelling':'running';
   await route.fulfill({json:{status,question:'테스트 질문'}});
   if(status==='cancelling'){cancellingPolls++;const resolve=nextCancellationPoll;nextCancellationPoll=null;resolve?.();}
  });
  await cancel.page.clock.install();
  await send(cancel.page);
  await cancel.page.waitForFunction(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')||'null')?.accepted===true);
  const cancelAcknowledged=cancel.page.waitForResponse(response=>new URL(response.url()).pathname.endsWith('/cancel'));
  await cancel.page.locator('#cancel').click();
  assert.deepEqual(await (await cancelAcknowledged).json(),{status:'cancelling'});
  assert.equal(cancelPath,'/api/jobs/'+cancelId+'/cancel');
  assert(await cancel.page.locator('#cancel').isDisabled(),'accepted cancellation remains disabled until terminal status');
  const assertStillCancelling=async()=>{
   assert(await cancel.page.evaluate(()=>busy),'a cancellation ACK is not terminal completion');
   assert.equal(await cancel.page.evaluate(()=>activeJob),cancelId);
   assert.equal(await cancel.page.evaluate(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')).id),cancelId);
   assert.equal(await cancel.page.evaluate(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')).cancelRequested),true);
   assert(await cancel.page.locator('#question').isDisabled());
   assert(await cancel.page.locator('#send').isHidden());
   assert(await cancel.page.locator('#cancel').isVisible());
  };
  await assertStillCancelling();
  for(let i=0;i<3;i++){
   const polled=new Promise((resolve,reject)=>{
    const timeout=setTimeout(()=>reject(new Error('cancelling job must continue polling')),5000);
    nextCancellationPoll=()=>{clearTimeout(timeout);resolve();};
   });
   await cancel.page.clock.fastForward(1800);await polled;
   await assertStillCancelling();
  }
  assert(cancellingPolls>=3);
  await cancel.page.locator('#question-form').evaluate(form=>form.requestSubmit());
  assert.equal(cancelSubmits,1,'a pending cancellation must not allow another submission');
  cancelTerminal=true;await cancel.page.clock.fastForward(1800);
  await cancel.page.waitForFunction(()=>!busy);
  assert.equal(await cancel.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  assert.equal(await cancel.page.evaluate(()=>activeJob),null);
  assert(await cancel.page.locator('#question').isEnabled());
  assert((await cancel.page.locator('#notice').textContent()).includes('중단했어요'));
  assert.deepEqual(cancel.errors,[]);
  console.log('Cancellation ACK and three cancelling polls retain busy state and job ID; only terminal cancelled releases the form.');await cancel.page.close();

  let sourceJob;
  const sourceRequests=[];
  const sourceSnapshots={'A.md':'A at answer time','B.md':'B at answer time'};
  const changedDocuments={'A.md':'A changed after answer','B.md':'B changed after answer'};
  const source=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){
    sourceJob=JSON.parse(route.request().postData()).request_id;
    return route.fulfill({status:202,json:{id:sourceJob}});
   }
   if(url.pathname==='/api/source'){
    const requested=Object.fromEntries(url.searchParams);sourceRequests.push(requested);
    // Match the server contract: a job selects the immutable answer snapshot;
    // omitting it reads the independently changed current document.
    const text=requested.job===sourceJob?sourceSnapshots[requested.path]:changedDocuments[requested.path];
    return route.fulfill({json:{path:requested.path,text}});
   }
   return route.fulfill({json:{status:'completed',question:'문서 질문',answer:'참고 문서',sources:[{path:'A.md',line:1},{path:'B.md',line:1}]}});
  });
  await send(source.page);await source.page.waitForFunction(()=>!busy);
  for(const name of ['A','B']){
   await source.page.getByRole('button',{name:name+' · 1행',exact:true}).click();
   await source.page.waitForFunction(()=>document.getElementById('source-content').textContent!=='문서를 읽고 있어요…');
   assert.equal(await source.page.locator('#source-content').textContent(),'1  '+sourceSnapshots[name+'.md']);
   assert.deepEqual(sourceRequests.at(-1),{path:name+'.md',job:sourceJob},'each source request must identify the exact answer snapshot');
   await source.page.getByRole('button',{name:'문서 닫기'}).click();
   assert.equal(await source.page.locator('#source-content').textContent(),'','closing source removes hidden raw text');
   assert.equal(await source.page.locator('#source-title').textContent(),'');
  }
  console.log('Source links send the exact path and completed job ID, rendering answer-time snapshots even when current documents differ.');
  await source.page.evaluate(()=>{
   const realFetch=window.fetch;window.pendingSources=[];
   window.fetch=(url,options)=>url.startsWith('/api/source?')?new Promise((resolve,reject)=>{
    // Deliberately ignore cancellation, modeling a response already queued for
    // processing. Generation checks must still protect the next open dialog.
    window.pendingSources.push({resolve,reject,signal:options.signal});
   }):realFetch(url,options);
  });
  for(const [closeMethod,lateFailure] of [['button',false],['escape',true],['backdrop',false]]){
   await source.page.evaluate(()=>{window.pendingSources=[];});
   await source.page.getByRole('button',{name:'A · 1행',exact:true}).click();
   await source.page.waitForFunction(()=>pendingSources.length===1);
   if(closeMethod==='button')await source.page.getByRole('button',{name:'문서 닫기'}).click();
   else if(closeMethod==='escape')await source.page.keyboard.press('Escape');
   else await source.page.mouse.click(1,1);
   await source.page.waitForFunction(()=>!document.getElementById('source-dialog').open);
   assert(await source.page.evaluate(()=>pendingSources[0].signal.aborted));
   await source.page.getByRole('button',{name:'B · 1행',exact:true}).click();
   await source.page.waitForFunction(()=>pendingSources.length===2);
   await source.page.evaluate(()=>pendingSources[1].resolve(new Response(JSON.stringify({text:'B 본문'}),{headers:{'Content-Type':'application/json'}})));
   await source.page.waitForFunction(()=>document.getElementById('source-content').textContent==='1  B 본문');
   await source.page.evaluate(async failure=>{
    if(failure)pendingSources[0].reject(new TypeError('late network failure'));
    else pendingSources[0].resolve(new Response(JSON.stringify({text:'A 본문'}),{headers:{'Content-Type':'application/json'}}));
    await new Promise(resolve=>setTimeout(resolve,0));
   },lateFailure);
   assert.equal(await source.page.locator('#source-content').textContent(),'1  B 본문');
   assert(await source.page.getByRole('dialog',{name:'B.md',exact:true}).isVisible());
   await source.page.getByRole('button',{name:'문서 닫기'}).click();
   assert.equal(await source.page.locator('#source-content').textContent(),'','closing source removes hidden raw text');
   assert.equal(await source.page.locator('#source-title').textContent(),'');
  }
  assert.deepEqual(source.errors,[]);
  console.log('Source dialog ignores late success/failure after button, Escape, or backdrop close; accessible name follows B.md.');await source.page.close();

  let afterReload=false,accepted=false;
  const replayed=[];
  const unconfirmed=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){
    replayed.push(JSON.parse(route.request().postData()));
    if(replayed.length===1)return route.abort('failed');
    accepted=true;return route.fulfill({status:202,json:{id:replayed[1].request_id}});
   }
   if(!afterReload)return route.abort('failed');
   if(!accepted)return route.fulfill({status:404,json:{error:'missing'}});
   return route.fulfill({json:{status:'completed',question:replayed[1].question,answer:'replayed after reload',sources:[]}});
  });
  await unconfirmed.page.evaluate(()=>{history=['b'.repeat(32)];});
  await send(unconfirmed.page,'재전송할 질문');
  await unconfirmed.page.waitForFunction(()=>document.getElementById('notice').textContent.includes('진행 중인 질문은 유지'));
  const pendingRequest=await unconfirmed.page.evaluate(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')));
  assert.equal(pendingRequest.version,3);assert.equal(pendingRequest.accepted,false);
  assert.deepEqual(pendingRequest.request,replayed[0]);assert.deepEqual(pendingRequest.request.history,[{job_id:'b'.repeat(32)}]);
  afterReload=true;await unconfirmed.page.reload();
  await unconfirmed.page.getByText('replayed after reload',{exact:true}).waitFor();
  assert.equal(replayed.length,2);assert.deepEqual(replayed[1],replayed[0]);
  assert.equal(await unconfirmed.page.locator('.message.user').count(),1);
  assert.equal(await unconfirmed.page.evaluate(()=>history[0]),'b'.repeat(32));
  assert.equal(await unconfirmed.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  assert.deepEqual(unconfirmed.errors,[]);
  console.log('Lost initial POST survives reload and GET 404: exact ID/question/job references replayed, terminal state cleared.');await unconfirmed.page.close();

  let expiredSubmits=0;
  const expired=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){expiredSubmits++;return route.fulfill({status:202,json:{id:JSON.parse(route.request().postData()).request_id}});}
   return route.fulfill({status:404,json:{error:'expired confirmed job'}});
  });
  await send(expired.page);await expired.page.waitForFunction(()=>!busy);
  assert.equal(expiredSubmits,1,'POST-confirmed jobs must not be recreated on their first 404');
  assert.equal(await expired.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  await expired.page.evaluate(()=>sessionStorage.setItem('wiki-active-job','c'.repeat(32)));
  await expired.page.reload();await expired.page.waitForFunction(()=>!busy);
  assert.equal(expiredSubmits,1,'legacy ID-only jobs must never be recreated');
  assert.equal(await expired.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  console.log('POST-confirmed and legacy ID-only expired jobs clear on 404 without resubmission.');await expired.page.close();

  let getConfirmedSubmits=0,getExpired=false;
  const getConfirmed=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){getConfirmedSubmits++;return route.abort('failed');}
   if(getExpired)return route.fulfill({status:404,json:{error:'expired after lookup'}});
   return route.fulfill({json:{status:'running',question:'조회로 확인한 질문'}});
  });
  await send(getConfirmed.page,'조회로 확인한 질문');
  await getConfirmed.page.waitForFunction(()=>JSON.parse(sessionStorage.getItem('wiki-active-job')||'null')?.accepted===true);
  getExpired=true;await getConfirmed.page.reload();await getConfirmed.page.waitForFunction(()=>!busy);
  assert.equal(getConfirmedSubmits,1);assert.equal(await getConfirmed.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  console.log('Successful GET strips the pending question payload; a later 404 never reruns that job.');await getConfirmed.page.close();

  const failed=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query')return route.fulfill({status:202,json:{id:JSON.parse(route.request().postData()).request_id}});
   return route.fulfill({json:{status:'failed',question:'다시 고칠 질문',error:'synthetic job failure'}});
  });
  await send(failed.page,'다시 고칠 질문');await failed.page.waitForFunction(()=>!busy);
  assert.equal(await failed.page.locator('#question').inputValue(),'다시 고칠 질문');
  assert.equal(await failed.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  console.log('Failed accepted job restores its question and clears pending storage.');await failed.page.close();

  let acceptanceRoute,acceptanceSubmits=0,acceptancePolls=0;
  const acceptanceRace=await setup(browser,(route,url)=>{
   if(url.pathname==='/api/query'){acceptanceSubmits++;acceptanceRoute=route;return;}
   if(url.pathname==='/api/source')return route.fulfill({status:401,json:{error:'synthetic concurrent auth expiry'}});
   acceptancePolls++;return route.fulfill({status:404,json:{error:'expired confirmed job'}});
  });
  await send(acceptanceRace.page,'인증과 접수 응답 순서');
  await acceptanceRace.page.evaluate(()=>openSource({path:'synthetic.md'},null));
  assert(await acceptanceRace.page.locator('#source-reauthenticate').isVisible());
  const acceptanceId=JSON.parse(acceptanceRoute.request().postData()).request_id;
  await acceptanceRoute.fulfill({status:202,json:{id:acceptanceId}});
  await acceptanceRace.page.waitForFunction(id=>{const state=JSON.parse(sessionStorage.getItem('wiki-active-job')||'null');return state?.id===id && state.accepted;},acceptanceId);
  assert.equal(acceptancePolls,0,'a concurrent auth failure must keep polling paused');
  await acceptanceRace.page.reload();await acceptanceRace.page.waitForFunction(()=>!busy);
  assert.equal(acceptanceSubmits,1,'a late acceptance still prevents replay after expiry');
  assert.equal(await acceptanceRace.page.evaluate(()=>sessionStorage.getItem('wiki-active-job')),null);
  assert.deepEqual(acceptanceRace.errors,[]);
  console.log('Concurrent auth failure and late POST acceptance keep polling paused, retain accepted reference-only state, and prevent replay.');await acceptanceRace.page.close();

  await checkAccessRedirects(browser);
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});

// Packaged-app acceptance, synthetic fixtures only; never mocks ctx.rest.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { _electron: electron } = require(process.env.AC_PLAYWRIGHT || 'C:/w/hermes-desktop-test-build/node_modules/playwright');
const repo = path.resolve(__dirname, '../..');
const root = path.resolve(process.env.AC_TEST_ROOT || 'C:/Users/jerry/AppData/Local/HermesInlineMainTestBuild');
const run = path.join(root, 'ac-acceptance-' + Date.now());
const home = path.join(run, 'agent-home');
const core = process.env.AC_HERMES_ROOT || 'C:/w/baseline-main';
const python = process.env.AC_PYTHON || 'C:/w/hermes-agent-inbox/.inbox-work/.venv/Scripts/python.exe';
const exe = process.env.AC_EXE || 'C:/w/hermes-desktop-test-build/apps/desktop/release/versions/50503c8fb9/win-unpacked/Hermes.exe';
fs.mkdirSync(home, { recursive: true });
fs.writeFileSync(path.join(home,'ACTION_CENTER_SYNTHETIC_TEST'),'Synthetic test fixtures only. No credentials.');
fs.mkdirSync(path.join(home,'desktop-plugins/action-center'), {recursive:true});
fs.copyFileSync(process.env.AC_DESKTOP_SOURCE || path.join(repo,'desktop/plugin.js'),path.join(home,'desktop-plugins/action-center/plugin.js'));
fs.cpSync(process.env.AC_BACKEND_SOURCE || path.join(repo,'dashboard'),path.join(home,'plugins/action-center/dashboard'),{recursive:true});
fs.cpSync(path.join(__dirname,'fixtures-plugin'),path.join(home,'plugins/ac-fixtures'),{recursive:true});
fs.writeFileSync(path.join(home,'config.yaml'),'plugins:\n  enabled: [action-center, ac-fixtures]\nmodel: anthropic/claude-sonnet-4\n');
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([k]) => /^(PATH|PATHEXT|SYSTEMROOT|SYSTEMDRIVE|ALLUSERSPROFILE|PROGRAMDATA|WINDIR|COMSPEC|TEMP|TMP|USERPROFILE|LOCALAPPDATA|APPDATA|PROGRAMFILES|PROGRAMFILES\(X86\)|COMMONPROGRAMFILES|HOMEDRIVE|HOMEPATH|NUMBER_OF_PROCESSORS|PROCESSOR_ARCHITECTURE)$/i.test(k)));
const crypto = require('node:crypto');
const sourceHashes = Object.fromEntries([['desktop/plugin.js',path.join(home,'desktop-plugins/action-center/plugin.js')],['dashboard/plugin_api.py',path.join(home,'plugins/action-center/dashboard/plugin_api.py')]].map(([name,file])=>[name,crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')]));
const receipt = { run, core, sourceHashes, synthetic:true, checks:[], errors:[] };
let app, page;
function pass(name,data={}) { receipt.checks.push({name,...data}); console.log('PASS',name,JSON.stringify(data)); }
async function api(route,body) { return page.evaluate(async ({route,body}) => window.hermesDesktop.api({path:'/api/plugins/'+route,method:body===undefined?'GET':'POST',...(body===undefined?{}:{body})}),{route,body}); }
async function observed(kind, predicate) {
  const deadline=Date.now()+15000; let value;
  do { value=await api('ac-fixtures/inspect?kind='+kind); if(predicate(value)) return value; await page.waitForTimeout(100); } while(Date.now()<deadline);
  assert.fail('State did not converge: '+JSON.stringify(value));
}
async function select(kind) {
  const seed = await api('ac-fixtures/seed',{kind});
  const panel = page.locator('[data-action-center="page"]');
  await panel.getByRole('button',{name:'Refresh Action Center',exact:true}).click();
  await panel.getByRole('button',{name:/All sessions/}).click();
  await panel.getByRole('textbox',{name:'Search sessions',exact:true}).fill('SYNTHETIC '+kind);
  await panel.getByText('SYNTHETIC '+kind,{exact:true}).first().click();
  await panel.locator('[data-session-detail]').waitFor();
  return {seed,panel};
}
(async()=>{
 try {
  app = await electron.launch({executablePath:exe,env:{...cleanEnv,HERMES_HOME:home,AC_TEST_ROOT:root,PYTHONDONTWRITEBYTECODE:'1',HERMES_DESKTOP_USER_DATA_DIR:path.join(run,'desktop-state'),HERMES_DESKTOP_APP_NAME:'HermesActionCenterAcceptance',HERMES_DESKTOP_IGNORE_EXISTING:'1',HERMES_DESKTOP_SKIP_QUIT_CONFIRM:'1',HERMES_DESKTOP_HERMES_ROOT:core,HERMES_DESKTOP_PYTHON:python,HERMES_PYTHON:python},timeout:40000});
  page = await app.firstWindow({timeout:30000});
  page.setDefaultTimeout(20000);
  page.on('pageerror',e=>receipt.errors.push(e.message));
  await page.waitForFunction(()=>window.hermesDesktop?.api);
  console.log('PLUGIN_ROOT',await page.evaluate(()=>window.hermesDesktop.desktopPluginsRoot()));
  page.on('console',m=>{ if(/plugin|action.center/i.test(m.text())) console.log('RENDER_CONSOLE',m.text().slice(0,500)); });
  // Wait for native bootstrap before reload: reloading mid-boot loses its ready event.
  const later = page.getByText(/choose a provider later/i);
  await later.waitFor({timeout:60000});
  await later.first().click();
  await page.evaluate(()=>localStorage.setItem('hermes.desktop.pluginDecisions.v2',JSON.stringify({'action-center':true})));
  await page.reload();
  // Native reference chip is icon-only; the disk plugin has a visible label.
  const chip = page.locator('button[title^="Action Center —"]').filter({hasText:'Action Center'});
  await chip.waitFor({state:'visible',timeout:30000});
  await chip.click();
  const panel = page.locator('[data-action-center="page"]');
  await panel.waitFor({state:'visible'});
  await app.evaluate(({BrowserWindow})=>{const w=BrowserWindow.getAllWindows().find(w=>w.isVisible());w.setFullScreen(false);w.unmaximize();w.setSize(1280,900);});
  const {seed}=await select('once');
  await panel.locator('[data-approval-request]').getByRole('button',{name:/Approve once/i}).click();
  const state=await observed('once',s=>s.settled);
  assert.equal(state.settled,true); assert.equal(state.result,'once'); assert.equal(state.approvals.length,0);
  assert.equal(path.normalize(state.core_file),path.normalize(path.join(core,'tui_gateway/server.py')));
  pass('approve-once-real-queue-unpatched-core',state);
  for(const kind of ['deny','restricted']) {
    await select(kind);
    const card=panel.locator('[data-approval-request]');
    if(kind==='restricted') { assert.equal(await card.getByRole('button',{name:'Always allow',exact:true}).count(),0); assert.equal(await card.getByRole('button',{name:'Approve for session',exact:true}).count(),0); }
    await card.getByRole('button',{name:kind==='deny'?'Deny':'Approve once',exact:true}).click();
    const s=await observed(kind,s=>s.settled); assert.equal(s.result,kind==='deny'?'deny':'once'); assert.equal(s.approvals.length,0); pass(kind+'-real-queue',s);
  }
  for(const kind of ['single','multi','batch']) {
    await select(kind);
    await panel.getByRole('button',{name:/Red/}).click();
    if(kind==='multi') await panel.getByRole('button',{name:/Blue/}).click();
    if(kind==='batch') await panel.getByRole('button',{name:/Large/}).click();
    // Staging is local: real server request remains pending until Submit.
    assert.equal((await api('ac-fixtures/inspect?kind='+kind)).settled,false);
    await panel.getByRole('button',{name:kind==='batch'?'Submit answers':'Submit',exact:true}).click();
    const s=await observed(kind,s=>s.settled);
    if(kind==='single') assert.equal(s.result.answer,'Red');
    if(kind==='multi') assert.deepEqual(JSON.parse(s.result.answer),['Red','Blue']);
    if(kind==='batch') assert.deepEqual(s.result.answers,{color:'Red',size:'Large'});
    pass(kind+'-real-server-request',s);
  }
  for(const kind of ['goal','loop','heartbeat']) {
    await select(kind);
    await panel.getByRole('button',{name:'Pause '+kind,exact:true}).click();
    await observed(kind,s=>s.status==='paused');
    await panel.getByRole('button',{name:'Resume '+kind,exact:true}).click();
    const s=await observed(kind,s=>s.status==='active');
    pass('stored-'+kind+'-pause-resume',s);
  }
  await select('expired');
  const expired=panel.locator('[data-expired-request]');
  await expired.getByRole('button',{name:'Redo',exact:true}).click();
  await expired.getByRole('alert').waitFor();
  assert.match(await expired.getByRole('alert').innerText(),/running|live session/i);
  pass('redo-stored-session-refusal-surfaced',{message:await expired.getByRole('alert').innerText()});
  await expired.getByRole('button',{name:'Dismiss',exact:true}).click();
  await expired.waitFor({state:'detached'});
  const details=await api('action-center/details?session_key=ac-live-expired');
  assert.equal(details.sessions[0].expired_requests.length,0);
  pass('dismiss-persisted-record',{seeded:true});
  await select('redo');
  await panel.locator('[data-expired-request]').getByRole('button',{name:'Redo',exact:true}).click();
  const redone=await observed('redo',s=>s.submitted.length===1);
  assert.equal(redone.submitted[0].session_id,'ac-runtime-redo');
  assert.match(redone.submitted[0].text,/echo SYNTHETIC/);
  assert.equal(redone.submitted[0].display_kind,'hidden');
  await panel.locator('[data-expired-request]').waitFor({state:'detached'});
  assert.equal((await api('action-center/details?session_key=ac-live-redo')).sessions[0].expired_requests.length,0);
  pass('redo-dispatch-and-clear',{boundary:'prompt.submit intercepted for synthetic session only; no LLM execution'});
  await select('capture');
  await panel.locator('[data-expired-request]').waitFor();
  const captured=(await api('action-center/details?session_key=ac-live-capture')).sessions[0].expired_requests;
  assert.equal(captured.length,1); assert.equal(captured[0].outcome,'notify_failed');
  pass('automatic-real-notify-failure-capture',{captured});
  await page.screenshot({path:path.join(run,'captured-expiry.png')});
  try { await api('action-center/summary?profile=does-not-exist'); assert.fail('Unknown profile unexpectedly accepted'); } catch(e) { assert.match(e.message,/404|unknown profile|not found/i); pass('unknown-profile-refused'); }
  for(const width of [1600,1280,700,500]) {
    await app.evaluate(({BrowserWindow},width)=>{const w=BrowserWindow.getAllWindows().find(w=>w.isVisible());w.setMinimumSize(400,400);w.setSize(width,900);},width);
    await page.waitForTimeout(300);
    const geometry=await panel.evaluate(n=>({width:n.clientWidth,scrollWidth:n.scrollWidth,height:n.clientHeight,notice:!!n.querySelector('[data-ac-too-narrow]'),toggle:!!n.querySelector('[data-ac-rail-toggle]'),bodyDirection:getComputedStyle(n.querySelector('[data-ac-split]')).flexDirection}));
    assert(geometry.width>0 && geometry.height>0); assert(geometry.scrollWidth<=geometry.width+1);
    if(geometry.width<160) assert(geometry.notice); else if(geometry.width<=420) assert(geometry.toggle);
    await page.screenshot({path:path.join(run,'native-window-'+width+'.png')});
    pass('geometry-'+width,geometry);
  }
  // Controlled real container widths: native window minimums can hide compact bands.
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.isVisible()).setSize(1600,900));
  for(const width of [900,678,214,88]) {
    await panel.evaluate((n,width)=>{n.style.width=width+'px';n.style.maxWidth=width+'px';},width);
    await page.waitForTimeout(200);
    const geometry=await panel.evaluate(n=>({width:n.clientWidth,scrollWidth:n.scrollWidth,notice:!!n.querySelector('[data-ac-too-narrow]'),toggle:!!n.querySelector('[data-ac-rail-toggle]'),bodyDirection:getComputedStyle(n.querySelector('[data-ac-split]')).flexDirection}));
    assert.equal(geometry.width,width); assert(geometry.scrollWidth<=width+1);
    if(width<160) { assert(geometry.notice); const expand=panel.locator('[data-ac-expand]'); assert.equal((await expand.innerText()).trim(),'Expand'); const r=await expand.boundingBox(); assert(r.width>=40 && r.height>=20); } else if(width<=420) { assert(geometry.toggle); await panel.locator('[data-ac-rail-toggle]').click(); assert.equal(await panel.locator('[data-ac-rail-toggle]').getAttribute('aria-expanded'),'true'); await panel.locator('[data-ac-rail-toggle]').click(); }
    if(width<760) assert.equal(geometry.bodyDirection,'column');
    geometry.positions = await panel.evaluate(n=>{const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,scrollLeft:e.scrollLeft}};return {root:rect(n),zoom:devicePixelRatio,viewport:innerWidth,first:rect(n.querySelector('[data-ac-too-narrow]') || n.querySelector('[data-ac-compact-header]') || n),button:n.querySelector('[data-ac-expand]')?rect(n.querySelector('[data-ac-expand]')):null,ancestors:(()=>{let p=n.parentElement,a=[];while(p&&a.length<6){a.push({tag:p.tagName,...rect(p),overflow:getComputedStyle(p).overflow});p=p.parentElement}return a})()}});
    await page.screenshot({path:path.join(run,'full-container-'+width+'.png')});
    pass('controlled-container-'+width,geometry);
  }
  assert.equal(receipt.errors.length,0,'Uncaught renderer errors');
  receipt.complete=true;
 } catch(e) {
  receipt.failure=e.stack; console.error('FAIL',e.stack);
  if(page) { console.log('DOM', (await page.locator('body').innerText().catch(()=>'' )).slice(-9000)); await page.screenshot({path:path.join(run,'failure.png')}).catch(()=>{}); }
  process.exitCode=1;
 } finally {
  if(app) await app.close();
  fs.writeFileSync(path.join(run,'receipt.json'),JSON.stringify(receipt,null,2));
  console.log('RECEIPT',path.join(run,'receipt.json'));
 }
})();

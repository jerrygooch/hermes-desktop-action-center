// Isolated packaged-app probe. Never targets the daily app or user data.
const { _electron: electron } = require('C:/w/hermes-desktop-test-build/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
const home = 'C:/Users/jerry/AppData/Local/HermesInlineMainTestBuild';
(async () => {
  let app;
  try {
    app = await electron.launch({
      executablePath: 'C:/w/hermes-desktop-test-build/apps/desktop/release/versions/50503c8fb9/win-unpacked/Hermes.exe',
      env: { ...process.env, HERMES_HOME: home + '/agent-home', HERMES_DESKTOP_USER_DATA_DIR: home + '/desktop-state', HERMES_DESKTOP_APP_NAME: 'HermesInlineMainTestBuild', HERMES_DESKTOP_IGNORE_EXISTING: '1', HERMES_DESKTOP_SKIP_QUIT_CONFIRM: '1', HERMES_DESKTOP_HERMES_ROOT: 'C:/w/hermes-desktop-test-build', HERMES_DESKTOP_PYTHON: 'C:/w/hermes-agent-inbox/.inbox-work/.venv/Scripts/python.exe', HERMES_PYTHON: 'C:/w/hermes-agent-inbox/.inbox-work/.venv/Scripts/python.exe' },
      timeout: 30000
    });
    const page = await app.firstWindow({ timeout: 30000 });
    page.on('pageerror', e => console.log('RENDER_ERROR', e.message.slice(0,300)));
    await page.waitForTimeout(12000);
    await page.evaluate(() => { const key='hermes.desktop.pluginDecisions.v2'; const decisions=JSON.parse(localStorage.getItem(key)||'{}'); localStorage.setItem(key,JSON.stringify({...decisions,'action-center':true})); });
    await page.reload();
    await page.waitForTimeout(8000);
    console.log(JSON.stringify(await page.evaluate(() => ({ actionCenterEnabled: JSON.parse(localStorage.getItem('hermes.desktop.pluginDecisions.v2')||'{}')['action-center'] }))));
    console.log(JSON.stringify({ phase: 'window', title: await page.title(), url: page.url(), actionCenterLabels: await page.getByText('Action Center', { exact: true }).count() }));
    const candidates = page.getByRole('button', { name: /Action Center/ });
    if (await candidates.count()) await candidates.first().click();
    else await page.evaluate(() => { location.hash = '/action-center'; });
    await page.waitForTimeout(6000);
    const panel = page.locator('[data-action-center="page"]');
    await panel.waitFor({ state: 'visible', timeout: 15000 });
    await panel.locator('input').fill('action-center-visual-probe-no-session-match');
    await page.waitForTimeout(300);
    await app.evaluate(({ BrowserWindow }) => { const win=BrowserWindow.getAllWindows().find(w=>w.isVisible() && w.webContents.getURL().includes('index.html')); if(win) { win.setFullScreen(false); win.unmaximize(); } });
    await page.waitForTimeout(800);
    for (const width of [1280, 700, 500]) {
      await app.evaluate(({ BrowserWindow }, width) => { const win = BrowserWindow.getAllWindows().find(w => !w.isDestroyed() && w.isVisible()); if (win) { win.setMinimumSize(400, 400); win.setSize(width, 800); } }, width);
      await page.waitForTimeout(500);
      await panel.screenshot({ path: `tests/live/action-center-empty-${width}.png` });
      console.log(JSON.stringify(await panel.evaluate((n, width) => { const r=n.getBoundingClientRect(); const body=n.children[1]; const rail=body?.children[0]; return { phase:'geometry', requestedWidth:width, windowWidth:innerWidth, panelWidth:r.width, panelHeight:r.height, clientWidth:n.clientWidth, scrollWidth:n.scrollWidth, bodyDirection:body && getComputedStyle(body).flexDirection, railWidth:rail?.getBoundingClientRect().width, header:n.querySelector('header')?.textContent, emptyVisible:n.textContent.includes('No results') }; }, width)));
    }
    await panel.screenshot({ path: 'tests/live/action-center-empty-narrow.png' });
    console.log(JSON.stringify(await page.evaluate(() => ({
      phase: 'panel', url: location.href,
      actionCenterNodes: [...document.querySelectorAll('button,a')].filter(n => /Action Center/.test(n.textContent || '')).map(n => ({ text: n.textContent.trim().slice(0,100), width: n.getBoundingClientRect().width, height: n.getBoundingClientRect().height })),
      searchFields: [...document.querySelectorAll('input')].map(n => ({ label:n.getAttribute('aria-label'), placeholder:n.placeholder })).filter(n => /Search sessions/.test(n.label || n.placeholder)),
      errorAlerts: [...document.querySelectorAll('[role=alert]')].map(n => n.textContent.slice(0,160))
    }))));
  } catch (e) { console.error('LIVE_PROBE_BLOCKED:', e.message); process.exitCode = 1; }
  finally { if (app) await app.close(); }
})();

/* Isolated smoke test of the actual Ervisio sandbox/broker and runtime.
 * Uses a fake Java executable: no game downloads or real EULA acceptance.
 * Build the plugin, Ervisio web, and .verification/bin/{ervisiod,ervisio-bridge}
 * first. Set MC_PLAYWRIGHT_MODULE and MC_CHROMIUM to your local installations, and ERVISIO_CORE to a core checkout.
 */
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const net = require('node:net');
const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.MC_PLAYWRIGHT_MODULE || 'playwright-core');
const project = path.resolve(__dirname, '..');
// The Ervisio core checkout (for web/dist): ERVISIO_CORE, else the folder two levels up.
const core = path.resolve(process.env.ERVISIO_CORE || path.resolve(project, '../..'));
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const scratch = await fs.mkdtemp(path.join(os.tmpdir(), 'ervisio-minecraft-e2e-'));
  const socketPath = path.join(scratch, 'control.sock');
  const data = path.join(scratch, 'data');
  const run = path.join(scratch, 'run');
  const children = [];
  let browser, page;
  const log = [];
  const boot = (command, args, options = {}) => {
    const child = spawn(command, args, {stdio:['ignore','pipe','pipe'], ...options});
    children.push(child);
    child.stdout.on('data', chunk => log.push(chunk.toString()));
    child.stderr.on('data', chunk => log.push(chunk.toString()));
    return child;
  };
  const api = (method, route, body) => new Promise((resolve, reject) => {
    const text = body === undefined ? '' : JSON.stringify(body);
    const req = http.request({socketPath, path:route, method, headers:{'Content-Type':'application/json','Content-Length':Buffer.byteLength(text)}}, res => {
      let answer='';res.on('data',chunk=>answer+=chunk);res.on('end',()=>{
        try {const json=JSON.parse(answer);if(res.statusCode>=400)reject(new Error(JSON.stringify(json)));else resolve(json)}catch(error){reject(error)}
      });
    });req.on('error',reject);req.end(text);
  });
  async function eventually(fn, timeout=15000) {
    const end=Date.now()+timeout;let last;
    while(Date.now()<end){try{const result=await fn();if(result)return result}catch(error){last=error}await wait(200)}
    throw last || new Error('Timed out');
  }
  try {
    await fs.mkdir(path.join(run,'plugins'),{recursive:true});
    await fs.cp(path.join(project,'dist/minecraft'),path.join(run,'plugins/minecraft'),{recursive:true});
    const manifestPath=path.join(run,'plugins/minecraft/manifest.json');
    const manifest=JSON.parse(await fs.readFile(manifestPath,'utf8'));
    manifest.capabilities.http[0].socket=socketPath;
    delete manifest.capabilities.http[0].admin;
    delete manifest.capabilities.http[0].adminUnlessGroup;
    manifest.visibleTo={groups:[]};
    await fs.writeFile(manifestPath,JSON.stringify(manifest));
    const fakeJava=path.join(scratch,'fake-java.py');
    await fs.writeFile(fakeJava,`#!/usr/bin/env python3
import sys
if '-version' in sys.argv:
 print('openjdk version "21-test-fixture"',file=sys.stderr);sys.exit(0)
print('[Server thread/INFO]: Done (0.1s)! For help, type "help"',flush=True)
print('[Server thread/INFO]: Alex joined the game',flush=True)
for command in sys.stdin:
 command=command.strip()
 if command=='stop':
  print('[Server thread/INFO]: Stopping server',flush=True);break
 print('[Server thread/INFO]: fixture received '+command,flush=True)
`,{mode:0o700});
    boot('python3',[path.join(project,'plugin/runtime/minecraft_runtime.py'),'--data',data,'--socket',socketPath,'--java',fakeJava],{env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
    await eventually(()=>api('GET','/v1/health'));
    const created=[];
    for(const [name,port] of [['Validation Survival',25581],['Validation Fabric',25582]]){
      const server=await api('POST','/v1/servers',{name,engine:'custom',version:'1.21.1',port,memoryMB:512,eula:true,autostart:false});
      created.push(server);
      await api('PUT',`/v1/servers/${server.id}/file`,{path:'server.jar',offset:0,total:7,data:Buffer.from('fixture').toString('base64'),final:true});
      await api('PATCH',`/v1/servers/${server.id}/file`,{path:'readme.txt',text:'initial fixture'}).catch(()=>{});
      const bytes=Buffer.from('Minecraft file transfer smoke test\n');
      await api('PUT',`/v1/servers/${server.id}/file`,{path:'readme.txt',offset:0,total:bytes.length,data:bytes.toString('base64'),final:true});
      await api('POST',`/v1/servers/${server.id}/power`,{action:'start'});
    }
    await eventually(async()=>{const s=await api('GET','/v1/servers');return s.servers.every(x=>x.state==='online')});
    const reserving=net.createServer();await new Promise(resolve=>reserving.listen(0,'127.0.0.1',resolve));
    const port=reserving.address().port;await new Promise(resolve=>reserving.close(resolve));
    const base=`http://127.0.0.1:${port}`;
    boot(path.join(project,'.verification/bin/ervisiod'),['--dev','--dev-insecure-noauth','--listen',`127.0.0.1:${port}`,'--web',path.join(core,'web/dist'),'--bridge',path.join(project,'.verification/bin/ervisio-bridge'),'--config',path.join(scratch,'missing.conf')],{cwd:run});
    const signIn=await eventually(()=>log.join('').match(/http:\/\/127\.0\.0\.1:\d+\/api\/dev\/noauth\?token=[^\s]+/)?.[0]);
    browser=await chromium.launch({headless:true,executablePath:process.env.MC_CHROMIUM,args:['--no-sandbox']});
    page=await browser.newPage({viewport:{width:1280,height:800},acceptDownloads:true});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto(signIn);await page.goto(base+'/p/minecraft/minecraft');
    const frame=page.frameLocator('iframe[src*="/plugin-frame/minecraft"]');
    await frame.locator('.mc-server-card').first().waitFor({timeout:20000});
    assert.equal(await frame.locator('.mc-server-card').count(),2);
    await fs.mkdir(path.join(project,'.verification/screenshots'),{recursive:true});
    await page.screenshot({path:path.join(project,'.verification/screenshots/desktop.png')});
    await frame.locator('.mc-card-select').first().click();
    await frame.locator('form.mc-command input').fill('say integration works');
    await frame.locator('form.mc-command button').click();
    await frame.locator('.mc-console').filter({hasText:'fixture received say integration works'}).waitFor({timeout:12000});
    // Exercise every detail area in the actual opaque-origin frame.
    for(const label of [/File|Files/,/Configur|Config/,/Software/,/Mod|Add-on/,/Giocatori|Players/,/Mondi|Worlds/,/Backup/,/Pianific|Schedule/]){
      await frame.locator('.mc-tabs button').filter({hasText:label}).first().click();
      await wait(300);
    }
    await frame.locator('.mc-tabs button').filter({hasText:/^File(s)?$/}).click();
    const fileRow=frame.locator('tr').filter({hasText:'readme.txt'});
    const downloadWait=page.waitForEvent('download');
    await fileRow.getByRole('button',{name:/^(Scarica|Download) readme\.txt$/}).click();
    const saved=await downloadWait;
    const downloaded=await fs.readFile(await saved.path(),'utf8');
    assert.equal(downloaded,'Minecraft file transfer smoke test\n');
    // Edit a text file in the editor and save it.
    await fileRow.getByRole('button',{name:/readme\.txt/}).first().click();
    await frame.locator('textarea.mc-editor').fill('edited in the browser');
    await frame.getByRole('button',{name:/Save file|Salva file/}).click();
    await eventually(async()=>(await fs.readFile(path.join(data,'servers',created[0].id,'readme.txt'),'utf8'))==='edited in the browser');
    // Typed confirmation dialog: delete the file.
    await frame.locator('tr').filter({hasText:'readme.txt'}).getByRole('button',{name:/^(Delete|Elimina) readme\.txt$/}).click();
    const confirmBox=frame.getByRole('alertdialog');
    await confirmBox.locator('input').fill('readme.txt');
    await confirmBox.getByRole('button',{name:/^(Delete|Elimina)$/}).click();
    await eventually(async()=>!(await fs.stat(path.join(data,'servers',created[0].id,'readme.txt')).catch(()=>null)));
    // Structured configuration edits must reach the text that is saved.
    await frame.locator('.mc-tabs button').filter({hasText:/Configur/}).click();
    await frame.getByLabel(/^(Description|Descrizione)/).fill('Edited MOTD');
    await frame.locator('.mc-tab-body .mc-title-row').getByRole('button',{name:/^(Save|Salva)$/}).click();
    await eventually(async()=>(await fs.readFile(path.join(data,'servers',created[0].id,'server.properties'),'utf8')).includes('motd=Edited MOTD'));
    // Power: stop from the drawer and wait for the job; then back up from the Backups tab.
    await frame.locator('.mc-detail-head').getByRole('button',{name:/^(Stop|Arresta)$/}).click();
    await eventually(async()=>(await api('GET',`/v1/servers/${created[0].id}`)).state==='offline');
    await frame.locator('.mc-tabs button').filter({hasText:/Backup/}).click();
    await frame.getByRole('button',{name:/Back up now|Backup ora/}).click();
    await frame.locator('.mc-list-row').first().waitFor({timeout:15000});
    assert.equal((await api('GET',`/v1/servers/${created[0].id}/backups`)).backups.length,1);
    await frame.locator('.mc-detail-head').getByRole('button',{name:/^(Start|Avvia)$/}).click();
    await eventually(async()=>(await api('GET',`/v1/servers/${created[0].id}`)).state==='online');
    await page.screenshot({path:path.join(project,'.verification/screenshots/drawer.png')});
    await page.keyboard.press('Escape');
    await frame.locator('.mc-detail-panel').waitFor({state:'detached',timeout:5000});
    await page.setViewportSize({width:390,height:844});
    await wait(1200);
    await page.screenshot({path:path.join(project,'.verification/screenshots/mobile.png')});
    const pluginFrame=page.frames().find(f=>f.url().includes('/plugin-frame/minecraft'));
    await frame.locator('.mc-card-select').first().click();
    await wait(500);
    await page.screenshot({path:path.join(project,'.verification/screenshots/mobile-detail.png')});
    await page.keyboard.press('Escape');
    assert.equal(await pluginFrame.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'Mobile frame overflow');
    await page.goto(base+'/overview');
    await wait(800);
    assert.equal((await api('GET','/v1/servers')).servers.filter(s=>s.state==='online').length,2,'Closing plugin page stopped servers');
    assert.deepEqual(errors,[],'Browser runtime errors');
    console.log(JSON.stringify({sandbox:true,cards:2,console:true,tabs:9,download:true,editor:true,typedConfirm:true,config:true,power:true,backup:true,mobile:true,persistentAfterNavigation:true,browserErrors:errors}));
  } catch(error) {
    await fs.mkdir(path.join(project,'.verification'),{recursive:true});
    await fs.writeFile(path.join(project,'.verification/integration.log'),log.join(''));
    if(page)await page.screenshot({path:path.join(project,'.verification/failure.png')}).catch(()=>{});
    throw error;
  } finally {
    if(browser)await browser.close();
    for(const child of children.reverse()){
      if(child.exitCode===null){child.kill('SIGTERM');await Promise.race([new Promise(resolve=>child.once('exit',resolve)),wait(12000)]);if(child.exitCode===null)child.kill('SIGKILL')}
    }
    await fs.rm(scratch,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error);process.exitCode=1});

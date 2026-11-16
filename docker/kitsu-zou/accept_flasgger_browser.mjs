// Cliente CDP finito. Importarlo o invocarlo sin --run no crea procesos ni sockets.
import {createHash} from 'node:crypto';
import {Buffer} from 'node:buffer';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import process from 'node:process';
import {setTimeout, clearTimeout} from 'node:timers';
import {spawn} from 'node:child_process';
import {pathToFileURL, URL} from 'node:url';

export const CALLBACK_FRAGMENT = `    const priorOnComplete = uiOptions.onComplete;
    uiOptions.onComplete = function() {
        try {
            if (typeof priorOnComplete === "function") {
                return priorOnComplete.apply(this, arguments);
            }
        } finally {
            updateTitle();
        }
    };
`;
export const CALLBACK_SHA256 = '33fab8e4252025d7f3f9a9a1e6dea1ff46a501ae2d8d3f8aeddc3cd45b155a68';
const FIXTURE_JWT = 'fixture-only-no-operational-credential';
const MAX_CONTROL = 1024 * 1024;

function requireValue(value, message) { if (!value) throw new Error(message); }
function digest(bytes) { return createHash('sha256').update(bytes).digest('hex'); }
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(k=>`${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  return JSON.stringify(value);
}

export function allowedRequest(url, origin, paths) {
  try {
    const parsed = new URL(url);
    const oauthHashes = ['fixture-state','fixture-wrong-state'].map(state=>`#access_token=${FIXTURE_JWT}&state=${state}`);
    const fragmentAllowed = !parsed.hash || (parsed.pathname==='/oauth2-redirect.html' && oauthHashes.includes(parsed.hash));
    return parsed.origin === origin && !parsed.username && !parsed.password && !parsed.search && fragmentAllowed && paths.has(parsed.pathname);
  } catch { return false; }
}

export function commandArguments(profile) {
  requireValue(typeof profile === 'string' && profile.length > 0 && !profile.includes('\0'), 'Perfil inválido.');
  return ['--headless=new','--remote-debugging-pipe','--disable-gpu','--disable-extensions',
    '--disable-background-networking','--disable-component-update','--disable-sync',
    '--no-first-run','--no-default-browser-check',`--user-data-dir=${profile}`,'about:blank'];
}

export function validateBrowserResult(value) {
  for (const name of ['rendered','spec_equal','title_safe','try_it_out','jwt_fixture','oauth_redirect']) requireValue(value[name] === true, `Aceptación incompleta: ${name}`);
  requireValue(Number.isSafeInteger(value.callback_invocations) && value.callback_invocations >= 1, 'Callback no invocado.');
  for (const name of ['errors','external_requests']) requireValue(Array.isArray(value[name]) && value[name].length === 0, `Diagnósticos de navegador: ${name}`);
  return value;
}

export function oauthShim() {
  // El redirect original se ejecutará intacto. El opener y el cierre son dobles explícitos del fixture.
  return `window.__fixtureOAuth={called:0,closed:false,isValid:null,token_matches:false,errors:[]};
    Object.defineProperty(window,'opener',{configurable:true,value:{swaggerUIRedirectOauth2:{state:'fixture-state',redirectUrl:location.origin+'/oauth2-redirect.html',auth:{name:'fixture-oauth',schema:{get:()=> 'implicit'}},callback:function(value){window.__fixtureOAuth.called++;window.__fixtureOAuth.isValid=value.isValid;window.__fixtureOAuth.token_matches=value.token.access_token===${JSON.stringify(FIXTURE_JWT)};},errCb:function(){window.__fixtureOAuth.errors.push('error OAuth sintético');}}}});
    window.close=function(){window.__fixtureOAuth.closed=true;};`;
}

function verifiedFile(filename, maximum, pin) {
  const absolute = path.resolve(filename);
  requireValue(absolute === fs.realpathSync.native(absolute), 'Ruta no canónica.');
  for (let item = absolute; ; item = path.dirname(item)) {
    requireValue(!fs.lstatSync(item).isSymbolicLink(), 'Ruta enlazada.');
    if (path.dirname(item) === item) break;
  }
  const before = fs.lstatSync(absolute, {bigint:true});
  const descriptor = fs.openSync(absolute, fs.constants.O_RDONLY);
  try {
    const opened = fs.fstatSync(descriptor, {bigint:true});
    requireValue(opened.isFile() && opened.nlink === 1n && opened.size <= BigInt(maximum), 'Archivo no regular o excesivo.');
    const bytes = fs.readFileSync(descriptor); const final = fs.fstatSync(descriptor, {bigint:true}); const after = fs.lstatSync(absolute, {bigint:true});
    const common = row=>[row.dev,row.ino,row.size,row.mtimeNs,row.nlink].map(String).join(':');
    requireValue(common(before)===common(opened) && common(opened)===common(final) && common(final)===common(after) && before.ctimeNs===after.ctimeNs && opened.ctimeNs===final.ctimeNs, 'Archivo cambiado.');
    requireValue(bytes.length===Number(opened.size) && digest(bytes)===pin,'SHA no corresponde al pin.');
    return bytes;
  } finally { fs.closeSync(descriptor); }
}

export class Cdp {
  constructor(child) {
    this.child=child;this.next=0;this.pending=new Map();this.buffer=Buffer.alloc(0);this.events=0;this.handler=()=>{};
    child.stdio[4].on('data',bytes=>{
      try {
        this.buffer=Buffer.concat([this.buffer,bytes]);requireValue(this.buffer.length<=MAX_CONTROL,'Frame CDP excesivo.');
        let end;
        while ((end=this.buffer.indexOf(0))>=0) {
          const row=JSON.parse(this.buffer.subarray(0,end).toString('utf8'));this.buffer=this.buffer.subarray(end+1);
          if (row.id) { const request=this.pending.get(row.id);if(request){this.pending.delete(row.id);clearTimeout(request.timer);if(row.error)request.reject(new Error('Error CDP.'));else request.resolve(row.result);} }
          else {requireValue(++this.events<=10000,'Demasiados eventos CDP.');Promise.resolve(this.handler(row)).catch(error=>this.abort(error));}
        }
      } catch(error) {this.abort(error);}
    });
    child.once('error',error=>this.abort(error));child.once('exit',()=>this.abort(new Error('Navegador finalizado.')));
  }
  abort(error) {this.failure=error;for(const item of this.pending.values()){clearTimeout(item.timer);item.reject(error);}this.pending.clear();}
  call(method,params={},sessionId) {
    if(this.failure)return Promise.reject(this.failure);
    const id=++this.next;requireValue(id<=1000,'Demasiados comandos CDP.');
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error('Comando CDP agotado.'));},5000);
      this.pending.set(id,{resolve,reject,timer});
      this.child.stdio[3].write(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})})+'\0',error=>{if(error)this.abort(error);});
    });
  }
  async evaluate(expression,session) {
    const result=await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true},session);
    requireValue(!result.exceptionDetails,'Excepción al evaluar el contrato UI.');return result.result.value;
  }
}

async function until(check, deadline) {
  while(Date.now()<deadline){if(await check())return;await new Promise(resolve=>setTimeout(resolve,50));}
  throw new Error('Condición UI no alcanzada.');
}

async function run(input) {
  requireValue(process.platform==='win32' && process.versions.node==='24.13.0','Runtime Node/Windows distinto del fijado.');
  requireValue(input.schema_version===1 && input.cases.length===4 && input.application_accepted===false,'Perfil de otro alcance.');
  const output=path.resolve(input.output);requireValue(output===fs.realpathSync.native(output),'Salida no canónica.');
  const profile=path.join(output,'edge-profile');requireValue(!fs.existsSync(profile),'No se adopta perfil existente.');fs.mkdirSync(profile,{mode:0o700});
  verifiedFile(input.edge_path,512*1024*1024,input.edge_sha256);
  let active;const observations=new Map();let child;let cdp;let server;
  try {
    const payloads=new Map();
    for(const [pin,encoded] of Object.entries(input.payloads)) {
      const data=Buffer.from(encoded,'base64');requireValue(digest(data)===pin && data.length<=5*1024*1024,'Payload falso/excesivo.');payloads.set(pin,data);
    }
    for(const item of input.cases) {
      requireValue(['baseline','derived'].includes(item.subject) && typeof item.hide_top_bar==='boolean','Caso no cerrado.');
      const routes=new Map();for(const row of item.routes){requireValue(typeof row.path==='string' && row.path.startsWith('/') && !row.path.startsWith('//') && !/[?#\\]/.test(row.path) && !row.path.split('/').some(p=>p==='.'||p==='..') && !routes.has(row.path) && payloads.has(row.sha256),'Ruta/payload ambiguos.');routes.set(row.path,row);}
      item.routeMap=routes;
    }
    server=http.createServer((request,response)=>{
      try {
        requireValue(active && ['GET','HEAD'].includes(request.method) && typeof request.url==='string' && active.routeMap.has(request.url),'Ruta o método no permitido.');
        const row=active.routeMap.get(request.url);const body=payloads.get(row.sha256);
        if(request.url==='/fixture/echo') {
          const observed=observations.get(active.id);const fixtureResponse=JSON.parse(body.toString('utf8'));
          observed.try_it_out=row.status===200 && row.content_type.startsWith('application/json') && fixtureResponse.ok===true && fixtureResponse.fixture===true;
          observed.jwt_fixture=request.headers.authorization===`Bearer ${FIXTURE_JWT}`;
        }
        response.writeHead(row.status,{'Content-Type':row.content_type,'Content-Length':body.length,'Cache-Control':'no-store','Connection':'close'});response.end(request.method==='HEAD'?undefined:body);
      } catch {response.writeHead(404,{'Content-Length':'0','Connection':'close'});response.end();}
    });
    server.requestTimeout=5000;server.headersTimeout=5000;server.maxConnections=16;
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
    const origin=`http://127.0.0.1:${server.address().port}`;
    child=spawn(input.edge_path,commandArguments(profile),{cwd:output,env:{...input.browser_environment},windowsHide:true,stdio:['ignore','ignore','pipe','pipe','pipe']});
    let stderrBytes=0;child.stderr.on('data',data=>{stderrBytes+=data.length;if(stderrBytes>262144)child.kill();});
    cdp=new Cdp(child);const version=await cdp.call('Browser.getVersion');requireValue(version.product.split('/').at(-1)===input.edge_product_version,'Versión CDP distinta de la versión Edge respaldada.');const results=[];
    for(const item of input.cases) {
      active=item;const status={try_it_out:false,jwt_fixture:false,errors:[],external_requests:[]};observations.set(item.id,status);
      const {targetId}=await cdp.call('Target.createTarget',{url:'about:blank'});const {sessionId}=await cdp.call('Target.attachToTarget',{targetId,flatten:true});
      const allowed=new Set(item.routeMap.keys());
      cdp.handler=async event=>{
        if(event.sessionId!==sessionId)return;
        if(event.method==='Fetch.requestPaused') {
          const {requestId,request}=event.params;
          if(allowedRequest(request.url,origin,allowed))await cdp.call('Fetch.continueRequest',{requestId},sessionId);
          else {status.external_requests.push('solicitud fuera del mapa cerrado');await cdp.call('Fetch.failRequest',{requestId,errorReason:'BlockedByClient'},sessionId);}
        }
        if(event.method==='Runtime.exceptionThrown')status.errors.push('excepción de JavaScript');
        if(event.method==='Runtime.consoleAPICalled' && event.params.type==='error')status.errors.push('console.error');
      };
      await cdp.call('Page.enable',{},sessionId);await cdp.call('Runtime.enable',{},sessionId);await cdp.call('Network.enable',{},sessionId);
      await cdp.call('Network.setCacheDisabled',{cacheDisabled:true},sessionId);await cdp.call('Network.setBypassServiceWorker',{bypass:true},sessionId);
      await cdp.call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]},sessionId);
      await cdp.call('Page.navigate',{url:origin+'/apidocs/'},sessionId);
      await until(()=>cdp.evaluate('Boolean(window.ui && window.ui.specSelectors && window.ui.specSelectors.specJson().size && window.__fixtureCallbackCount>=1 && document.querySelectorAll(".opblock").length)',sessionId),Date.now()+10000);
      const state=await cdp.evaluate(`({spec:window.ui.specSelectors.specJson().toJS(),callback_invocations:window.__fixtureCallbackCount||0,title:document.title,bar:[...document.querySelectorAll('.topbar-wrapper .link span')].map(x=>x.textContent),title_execution_flag:window.__fixtureTitleExecuted||0,injected:Boolean(document.querySelector('#fixture-title-injection'))})`,sessionId);
      requireValue(canonical(state.spec)===canonical(item.expected_spec),'Spec del navegador diferente del capturado.');
      await cdp.evaluate(`window.ui.getConfigs().responseInterceptor({headers:{'jwt-token':${JSON.stringify(FIXTURE_JWT)}}});true`,sessionId);
      await cdp.evaluate(`(()=>{const block=[...document.querySelectorAll('.opblock')].find(x=>x.querySelector('.opblock-summary-path')?.textContent.trim()==='/fixture/echo');if(!block)throw Error('endpoint ausente');block.querySelector('.opblock-summary').click();return true;})()`,sessionId);
      await until(()=>cdp.evaluate(`Boolean([...document.querySelectorAll('.opblock button')].find(x=>x.textContent.trim()==='Try it out'))`,sessionId),Date.now()+5000);
      await cdp.evaluate(`[...document.querySelectorAll('.opblock button')].find(x=>x.textContent.trim()==='Try it out').click();true`,sessionId);
      await until(()=>cdp.evaluate(`Boolean(document.querySelector('.opblock .execute'))`,sessionId),Date.now()+5000);
      await cdp.evaluate(`document.querySelector('.opblock .execute').click();true`,sessionId);
      await until(()=>status.try_it_out,Date.now()+5000);
      const oauth=await cdp.evaluate(`fetch('/oauth2-redirect.html').then(async r=>({ok:r.status===200,text:await r.text()}))`,sessionId);
      const oauthSourceValid=oauth.ok && digest(Buffer.from(oauth.text))===item.routeMap.get('/oauth2-redirect.html').sha256;
      await cdp.call('Page.addScriptToEvaluateOnNewDocument',{source:oauthShim()},sessionId);
      for(const state of ['fixture-state','fixture-wrong-state']) {
        await cdp.call('Page.navigate',{url:'about:blank'},sessionId);
        await until(()=>cdp.evaluate('location.href === "about:blank"',sessionId),Date.now()+5000);
        await cdp.call('Page.navigate',{url:origin+'/oauth2-redirect.html#access_token='+FIXTURE_JWT+'&state='+state},sessionId);
        await until(()=>cdp.evaluate('Boolean(window.__fixtureOAuth?.called===1 && window.__fixtureOAuth.closed)',sessionId),Date.now()+5000);
        const redirect=await cdp.evaluate('window.__fixtureOAuth',sessionId);
        requireValue(redirect.token_matches===true && redirect.isValid===(state==='fixture-state') && redirect.errors.length===0,'OAuth redirect alteró su contrato sintético.');
      }
      const safeTitle=state.title===input.fixture_title && !state.title_execution_flag && !state.injected && (item.hide_top_bar?state.bar.length===0:state.bar.length>=1 && state.bar.every(x=>x===input.fixture_title));
      const value={id:item.id,subject:item.subject,hide_top_bar:item.hide_top_bar,rendered:true,spec_equal:true,callback_invocations:state.callback_invocations,title_safe:safeTitle,
        title_execution_flag:state.title_execution_flag,...status,oauth_redirect:oauthSourceValid};
      // La baseline es un control histórico vulnerable. La derivada debe superar todas las condiciones.
      if(item.subject==='derived')validateBrowserResult(value);
      else validateBrowserResult({...value,title_safe:true});
      results.push(value);await cdp.call('Target.closeTarget',{targetId});
    }
    requireValue(stderrBytes<=262144,'Diagnósticos Edge excesivos.');
    return {success:true,browser:version,results,baseline_xss_control_observed:results.some(r=>r.subject==='baseline'&&!r.hide_top_bar&&r.title_execution_flag===1),
      renderer_network_requests_observed:true,background_network_isolation_proven:false,fixture_executed:true,browser_executed:true,application_accepted:false,sca_approved:false};
  } finally {
    if(cdp){try{await cdp.call('Browser.close');}catch{ /* El Job del padre garantiza el cierre de descendientes. */ }}
    if(child && child.exitCode===null){child.kill();await Promise.race([new Promise(resolve=>child.once('exit',resolve)),new Promise(resolve=>setTimeout(resolve,2000))]);}
    if(server){server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
  }
}

async function main() {
  if(process.argv.length===2){process.stdout.write(JSON.stringify({plan_only:true,browser_executed:false})+'\n');return;}
  requireValue(process.argv.length===5 && process.argv[2]==='--run' && /^[a-f0-9]{64}$/.test(process.argv[4]),'Argumentos cerrados requeridos.');
  const raw=verifiedFile(process.argv[3],16*1024*1024,process.argv[4]);const result=await run(JSON.parse(raw.toString('utf8')));
  const encoded=Buffer.from(JSON.stringify(result));requireValue(encoded.length<=MAX_CONTROL,'Recibo excesivo.');process.stdout.write(encoded);process.stdout.write('\n');
}

if(process.argv[1] && import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)main().catch(error=>{process.stderr.write(JSON.stringify({success:false,error:'FLASGGER_BROWSER_REJECTED',error_type:error.name,detail:String(error.message).slice(0,200)})+'\n');process.exitCode=1;});

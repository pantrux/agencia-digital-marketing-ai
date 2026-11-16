// Pruebas puras del contrato CDP; no se inicia navegador, servidor ni procesos.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {EventEmitter} from 'node:events';
import {Buffer} from 'node:buffer';
import {test} from 'node:test';
import vm from 'node:vm';
import {CALLBACK_FRAGMENT, CALLBACK_SHA256, allowedRequest, commandArguments, validateBrowserResult, oauthShim, Cdp} from '../docker/kitsu-zou/accept_flasgger_browser.mjs';

test('fragmento auténtico conserva retorno, this y argumentos', () => {
  assert.equal(createHash('sha256').update(CALLBACK_FRAGMENT).digest('hex'), CALLBACK_SHA256);
  let observed; let titles = 0;
  const context = {uiOptions:{onComplete:function(...args){observed={receiver:this,args};return 17;}}, updateTitle:()=>{titles++;}};
  vm.runInNewContext(CALLBACK_FRAGMENT, context);
  const receiver = {marker:true};
  assert.equal(context.uiOptions.onComplete.call(receiver, 'first', 2),17);
  assert.equal(observed.receiver,receiver);assert.deepEqual(observed.args,['first',2]);assert.equal(titles,1);
});

test('excepción original se conserva y finally actualiza título una vez', () => {
  const error = new Error('fixture');let titles=0;
  const context={uiOptions:{onComplete:()=>{throw error;}}, updateTitle:()=>{titles++;}};
  vm.runInNewContext(CALLBACK_FRAGMENT,context);
  assert.throws(()=>context.uiOptions.onComplete(),value=>value===error);assert.equal(titles,1);
});

test('ausencia de callback sigue permitida', () => {
  let titles=0;const context={uiOptions:{},updateTitle:()=>{titles++;}};
  vm.runInNewContext(CALLBACK_FRAGMENT,context);assert.equal(context.uiOptions.onComplete(),undefined);assert.equal(titles,1);
});

test('frontera URL exige origen exacto y ruta cerrada',()=>{
  const origin='http://127.0.0.1:12345';const paths=new Set(['/derived/visible/apidocs/']);
  assert.equal(allowedRequest(origin+'/derived/visible/apidocs/',origin,paths),true);
  for(const url of ['http://localhost:12345/derived/visible/apidocs/',origin+'/other','https://example.com/',origin+'@example.com/'])assert.equal(allowedRequest(url,origin,paths),false);
});

test('argv no reutiliza perfil ni desactiva seguridad de navegador',()=>{
  const args=commandArguments('C:/private/new-profile','C:/private/edge');
  assert(args.includes('--remote-debugging-pipe'));assert(args.includes('--headless=new'));
  assert(!args.includes('--no-sandbox'));assert(!args.includes('--disable-web-security'));
});

test('opener sintético conserva sólo banderas y exige state correcto',()=>{
  const window={};const context={window,location:{origin:'http://127.0.0.1:1'}};
  vm.runInNewContext(oauthShim(),context);
  window.opener.swaggerUIRedirectOauth2.callback({isValid:false,token:{access_token:'fixture-only-no-operational-credential'}});
  window.close();
  assert.equal(window.__fixtureOAuth.called,1);assert.equal(window.__fixtureOAuth.closed,true);
  assert.equal(window.__fixtureOAuth.isValid,false);assert.equal(window.__fixtureOAuth.token_matches,true);
  assert.equal(Object.hasOwn(window.__fixtureOAuth,'token'),false);
});

test('resultado incompleto, callback ausente, XSS o errores no pasan',()=>{
  const good={rendered:true,spec_equal:true,callback_invocations:1,title_safe:true,try_it_out:true,jwt_fixture:true,oauth_redirect:true,errors:[],external_requests:[]};
  validateBrowserResult(good);
  for(const changed of [{callback_invocations:0},{title_safe:false},{spec_equal:false},{errors:['bad']},{external_requests:['blocked']}])assert.throws(()=>validateBrowserResult({...good,...changed}));
});

function fakeChild(){
  const child=new EventEmitter();const writes=[];
  child.stdio=[null,null,null,{write:(value,callback)=>{writes.push(value);callback?.();}},new EventEmitter()];
  return {child,writes};
}

test('CDP real reconstruye frames partidos sin ejecutar procesos',async()=>{
  const {child,writes}=fakeChild();const client=new Cdp(child);const request=client.call('Browser.getVersion');
  assert.equal(JSON.parse(writes[0].slice(0,-1)).method,'Browser.getVersion');
  child.stdio[4].emit('data',Buffer.from('{"id":1,"result":'));child.stdio[4].emit('data',Buffer.from('{"product":"fixture"}}\0'));
  assert.deepEqual(await request,{product:'fixture'});
});

test('CDP rechaza frame excesivo y proceso cerrado antes de responder',async()=>{
  for(const variant of ['overflow','exit']){
    const {child}=fakeChild();const client=new Cdp(child);const request=client.call('Browser.getVersion');
    if(variant==='overflow')child.stdio[4].emit('data',Buffer.alloc(1024*1024+1));else child.emit('exit',1);
    await assert.rejects(request);assert.equal(client.pending.size,0);
  }
});

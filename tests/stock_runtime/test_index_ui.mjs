import fs from 'node:fs';
import assert from 'node:assert/strict';
import { Window } from './.qa_node/node_modules/happy-dom/lib/index.js';
const sourceURL=fs.existsSync(new URL('./index.html',import.meta.url))?new URL('./index.html',import.meta.url):new URL('../../index.html',import.meta.url);
const html=fs.readFileSync(sourceURL,'utf8');
const scripts=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
const window=new Window({url:'http://index-test.local',settings:{disableJavaScriptFileLoading:true,disableCSSFileLoading:true,disableIframePageLoading:true}});
window.document.write(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,''));
const requests=[];
let response=JSON.parse(fs.readFileSync(new URL('./index_smoke_state.json',import.meta.url),'utf8'));
window.fetch=async (url,options={})=>{
 requests.push({url,options});
 if(url.includes('/api/desk-risk/'))return new window.Response(JSON.stringify({error:'Risk worker not running in offline fixture'}),{status:503});
 return new window.Response(JSON.stringify(response));
};
const selected=[];
for(const token of ['// AI Desk v2. All displayed trading values','async function recoverStockOrder','function renderTradeReviewControls','const sharedRiskPanels','/* Presentation helpers.','function renderStockRuntime','let ixState=null']){
 const script=scripts.find(s=>s.includes(token));assert.ok(script,token);if(!selected.includes(script))selected.push(script);
}
window.eval(selected.join('\n')+'\nwindow.__stopIndexUI=()=>clearInterval(ixTimer);');
let checks=0;function check(label,fn){fn();checks++;console.log('PASS',label);}
window.eval('initIndexTradeAIUI()');await new Promise(r=>setTimeout(r,100));
check('independent Index build and cold response render',()=>assert.equal(window.document.getElementById('ixBuild').textContent,'2026.10.24-index-independent.1'));
check('cash pool is labelled separately from daily loss',()=>{const t=window.document.getElementById('ixFields').textContent;assert.match(t,/Index cash budget/);assert.match(t,/Index daily loss limit/);assert.doesNotMatch(t,/premium budget per entry/)});
response={...response,connected:true,market_open:true,model:{id:'fixture'},halt:null,block:null,config:{...response.config,armed:true,mode:'paper'},signals:[{
 symbol:'NIFTY',decision:'READY',direction:'UP',confidence:.67,qty:50,lot:50,contract:'TESTCE',entry:200.1,stop:190.095,target:217,spread:.05,
 risk_plan:{sizing_basis:'trade-independent-v2',planned_loss:625},reason:'Setup qualified; final entry check pending'
}],execution:{status:'WAIT',reason:'Fresh final option quote unavailable; retrying'},entry_diagnostics:{workers:{},candidate_reasons:{}}};
await window.eval('refreshIndexTradeAI()');
check('trade plan explains its independent stop and quantity inputs',()=>{const t=window.document.getElementById('ixPlan').textContent;assert.match(t,/Trade volatility, delta and confidence/);assert.match(t,/Available Index cash/);assert.match(t,/does not set stop, target or quantity/)});
check('failed entry reason remains visible with a qualifying setup',()=>assert.match(window.document.getElementById('ixNotice').textContent,/Fresh final option quote unavailable/));
check('risk policy is explicit in model eligibility panel',()=>assert.match(window.document.getElementById('ixEligibility').textContent,/Independent of daily loss allowance/));
response.database={status:'RETRYING',message:'Index database busy; order/position checks will retry automatically.'};response.block='Index database busy · automatic order/position recovery pending';
response.signals[0]={...response.signals[0],decision:'WAIT',setup_qualified:true,reason:'Setup qualified · '+response.block};
await window.eval('refreshIndexTradeAI()');
check('database recovery is shown and row does not imply an order',()=>{assert.match(window.document.getElementById('ixNotice').textContent,/retry automatically/);assert.match(window.document.getElementById('ixSignals').textContent,/database busy/);assert.equal(window.document.getElementById('ixReady').textContent,'0')});
response.database={status:'OK'};response.block=null;response.signals=[];response.execution={status:'NO READY CANDIDATE',reason:'Waiting for a fresh qualifying setup'};
await window.eval('refreshIndexTradeAI()');
check('recovered state clears old database warning',()=>assert.doesNotMatch(window.document.getElementById('ixNotice').textContent,/database busy/));
response.execution={status:'WAIT',reason:'<img src=x onerror=alert(1)>'};await window.eval('refreshIndexTradeAI()');
check('execution errors are escaped as text',()=>{const n=window.document.getElementById('ixNotice');assert.equal(n.querySelectorAll('img').length,0);assert.match(n.textContent,/<img/)});
check('loading Index never arms or submits an order',()=>assert.ok(requests.every(r=>r.options.method!=='POST'&&!r.url.includes('/orders'))));
check('Index state reads use its own endpoint',()=>{assert.ok(requests.some(r=>r.url.includes('/api/index-ai/state')));assert.ok(requests.every(r=>!r.url.includes('/api/stock-ai/')))});
window.__stopIndexUI();await window.happyDOM.close();console.log(checks+' Index UI checks passed');

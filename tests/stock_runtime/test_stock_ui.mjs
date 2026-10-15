import fs from 'node:fs';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import { Window } from './.qa_node/node_modules/happy-dom/lib/index.js';
const sourceURL=fs.existsSync(new URL('./index.html',import.meta.url))?new URL('./index.html',import.meta.url):new URL('../../index.html',import.meta.url);
const html=fs.readFileSync(sourceURL,'utf8');
const scripts=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
const window=new Window({url:'http://stock-test.local',settings:{disableJavaScriptFileLoading:true,disableCSSFileLoading:true,disableIframePageLoading:true}});
window.document.write(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,''));
const requests=[];
let response=JSON.parse(fs.readFileSync(new URL('./smoke_state.json',import.meta.url),'utf8'));
window.fetch=async (url,options={})=>{
 requests.push({url,options});
 if(url.includes('/api/desk-risk/'))return new window.Response(JSON.stringify({error:'Test waiting for risk worker'}),{status:503});
 if(options.method==='POST')return new window.Response(JSON.stringify({ok:true,queued:true,command_id:'test-command'}));
 return new window.Response(JSON.stringify(response));
};
const selectedScripts=[];
for(const token of ['// AI Desk v2. All displayed trading values','async function recoverStockOrder','function renderTradeReviewControls','const sharedRiskPanels','/* Presentation helpers.','function renderStockRuntime']){
 const script=scripts.find(code=>code.includes(token));assert.ok(script,token);selectedScripts.push(script);
}
window.eval(selectedScripts.join('\n')+'\nwindow.__stopStockUI=()=>clearInterval(stockdeskTimer);');
let checks=0;function check(label,fn){fn();checks++;console.log('PASS',label);}
check('single Stock and Index roots with no duplicate IDs',()=>{
 const ids=[...window.document.querySelectorAll('[id]')].map(e=>e.id);const stock=[...window.document.querySelectorAll('#stockdesk [id]')].map(e=>e.id);
 assert.equal(new Set(stock).size,stock.length);assert.equal(ids.filter(x=>x==='stockdesk').length,1);assert.equal(ids.filter(x=>x==='indexdesk').length,1);
});
check('default tab opens only after presentation helpers',()=>{
 assert.ok(html.indexOf('id="stock-default-tab"')>html.indexOf('function renderStockRuntime'));
 assert.ok(!scripts.slice(0,-1).some(x=>/^\s*switchTopTab\('stockTradeAI'\);\s*$/.test(x)));
});
window.eval('initStockTradeAIUI()');await new Promise(r=>setTimeout(r,100));
check('cold backend snapshot renders without JavaScript error',()=>assert.match(window.document.getElementById('skConnectionState').textContent,/connected/));
check('incomplete historical analytics are visible without a false disconnect',()=>{assert.equal(window.document.getElementById('skDisplayWarnings').hidden,false);assert.match(window.document.getElementById('skDisplayWarnings').textContent,/Paper history/);assert.doesNotMatch(window.document.getElementById('skConnectionState').textContent,/failed|interrupted/)});
response.dashboard={status:'ERROR',stale:true,has_snapshot:false,error:'The saved ledger is busy; retrying automatically. Reference TEST',failure:{code:'DATABASE_BUSY',phase:'position and order ledger'}};
await window.eval('refreshStockTradeAI()');
check('display error identifies the failed read and keeps Disarm available',()=>{assert.match(window.document.getElementById('skConnectionState').textContent,/read failed/);assert.match(window.document.getElementById('skDataAge').textContent,/DATABASE_BUSY/);assert.equal(window.document.getElementById('skArm').disabled,true);assert.equal(window.document.getElementById('skDisarm').disabled,false)});
response={...response,positions:[],display_warnings:[],connected:true,market_open:true,model:{id:'test'},last_cycle:new Date().toISOString(),config:{...response.config,armed:false,mode:'paper'},dashboard:{status:'CURRENT',stale:false,age_seconds:0},signals:[
 {symbol:'READYTEST',rank:1,decision:'READY',confidence:.7,rank_score:70,reason:'Eligible',market_regime:{name:'TREND_UP'}},
 {symbol:'WAITTEST',rank:2,decision:'WAIT',confidence:.51,reason:'Direction below confidence threshold'}
]};
await window.eval('refreshStockTradeAI()');
check('candidate ready count and blocking reasons render',()=>{assert.equal(window.document.getElementById('skReadyCount').textContent,'1');assert.match(window.document.getElementById('skBlockerSummary').textContent,/Direction/)});
window.document.querySelector('[data-stock-filter="READY"]').click();
check('Ready filter hides waiting candidates',()=>{const text=window.document.getElementById('skRanking').textContent;assert.match(text,/READYTEST/);assert.doesNotMatch(text,/WAITTEST/)});
response.dashboard={status:'STALE',stale:true,age_seconds:25};await window.eval('refreshStockTradeAI()');
check('stale data disables entry controls while Disarm remains available',()=>{assert.equal(window.document.getElementById('skArm').disabled,true);assert.equal(window.document.getElementById('skMode').disabled,true);assert.equal(window.document.getElementById('skDisarm').disabled,false)});
response.dashboard={status:'CURRENT',stale:false,age_seconds:1};await window.eval('refreshStockTradeAI()');
check('recovered connection restores mode controls',()=>assert.equal(window.document.getElementById('skMode').disabled,false));
check('recovered optional statistics warning is cleared',()=>assert.equal(window.document.getElementById('skDisplayWarnings').hidden,true));
response.command={action:'arm',status:'QUEUED'};await window.eval('refreshStockTradeAI()');
check('queued arm is never displayed as completed',()=>{assert.match(window.document.getElementById('skNotice').textContent,/Queued/);assert.equal(window.document.getElementById('skArm').disabled,true)});
response.command={action:'arm',status:'FAILED',message:'Daily loss limit reached'};await window.eval('refreshStockTradeAI()');
check('failed command is explicit',()=>assert.match(window.document.getElementById('skNotice').textContent,/Daily loss limit/));
response.forward_review={drift:true,recent:{samples:300,brier:.3,auc:.48},observations:{excluded:17}};await window.eval('refreshStockTradeAI()');
check('forward drift and excluded outcomes are visible',()=>{assert.match(window.document.getElementById('skLearningReview').textContent,/drift detected/);assert.match(window.document.getElementById('skLearningReview').textContent,/17/)});
check('no order placement endpoint is called by the UI load',()=>assert.ok(requests.every(r=>!r.url.includes('/orders'))));
const expectedIndexHash='af545d3d29c494d52f5c7cee1c637fc8380a18d61bab3d0f2a221da0bdafee85';
const indexScript=(s)=>[...s.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]).find(c=>c.includes('let ixState=null'));
check('Index script is byte-identical',()=>assert.equal(createHash('sha256').update(indexScript(html)).digest('hex'),expectedIndexHash));
window.__stopStockUI();await window.happyDOM.close();console.log(checks+' Stock UI checks passed');

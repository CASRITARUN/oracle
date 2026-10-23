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
 {symbol:'READYTEST',rank:1,decision:'READY',confidence:.7,rank_score:70,entry:3.61,stop:3.30,target:4.22,reason:'Eligible',market_regime:{name:'TREND_UP'}},
 {symbol:'WAITTEST',rank:2,decision:'WAIT',confidence:.51,reason:'Direction below confidence threshold'}
]};
await window.eval('refreshStockTradeAI()');
check('candidate ready count and blocking reasons render',()=>{assert.equal(window.document.getElementById('skReadyCount').textContent,'1');assert.match(window.document.getElementById('skBlockerSummary').textContent,/Direction/)});
check('paper pause explains how to start simulation',()=>assert.match(window.document.getElementById('skOperationalDetail').textContent,/Paper entries are paused.*Arm entries/));
check('small option prices retain paise precision',()=>{const text=window.document.getElementById('skRanking').textContent;for(const price of ['₹3.61','₹3.30','₹4.22'])assert.ok(text.includes(price),text)});
check('scanner READY total matches visible rows even if old scan counters disagree',()=>assert.match(window.document.getElementById('skScanStatus').textContent,/READY 1/));
window.document.querySelector('[data-stock-filter="READY"]').click();
check('Ready filter hides waiting candidates',()=>{const text=window.document.getElementById('skRanking').textContent;assert.match(text,/READYTEST/);assert.doesNotMatch(text,/WAITTEST/)});
response.dashboard={status:'STALE',stale:true,age_seconds:25};await window.eval('refreshStockTradeAI()');
check('stale data disables entry controls while Disarm remains available',()=>{assert.equal(window.document.getElementById('skArm').disabled,true);assert.equal(window.document.getElementById('skMode').disabled,true);assert.equal(window.document.getElementById('skDisarm').disabled,false)});
response.dashboard={status:'CURRENT',stale:false,age_seconds:1};await window.eval('refreshStockTradeAI()');
check('recovered connection restores mode controls',()=>assert.equal(window.document.getElementById('skMode').disabled,false));
check('recovered optional statistics warning is cleared',()=>assert.equal(window.document.getElementById('skDisplayWarnings').hidden,true));
response.database={status:'RETRYING',message:'Stock database busy; order/position checks will retry automatically.'};await window.eval('refreshStockTradeAI()');
check('database recovery is visible separately from dashboard connectivity',()=>{assert.match(window.document.getElementById('skOperationalTitle').textContent,/Recovering Stock database/);assert.match(window.document.getElementById('skOperationalDetail').textContent,/retry automatically/);assert.match(window.document.getElementById('skConnectionState').textContent,/Dashboard connected/)});
response.database={status:'OK'};
response.command={action:'arm',status:'QUEUED'};await window.eval('refreshStockTradeAI()');
check('queued arm is never displayed as completed',()=>{assert.match(window.document.getElementById('skNotice').textContent,/Queued/);assert.equal(window.document.getElementById('skArm').disabled,true)});
response.command={action:'arm',status:'FAILED',message:'Daily loss limit reached'};await window.eval('refreshStockTradeAI()');
check('failed command is explicit',()=>assert.match(window.document.getElementById('skNotice').textContent,/Daily loss limit/));
response.forward_review={drift:true,recent:{samples:300,brier:.3,auc:.48},observations:{excluded:17}};await window.eval('refreshStockTradeAI()');
check('forward drift and excluded outcomes are visible',()=>{assert.match(window.document.getElementById('skLearningReview').textContent,/drift detected/);assert.match(window.document.getElementById('skLearningReview').textContent,/17/)});
response.forward_review.trade_review={updated:new Date().toISOString(),window:'Latest 500 closed positions',modes:{paper:{trades:2,net:180,average_net:90,cost_turned_losses:0,excluded:0,groups:[{dimension:'exit',label:'Target reached',trades:2,net:180,fees:20}]},live:{trades:1,net:-50,average_net:-50,cost_turned_losses:1,excluded:1,groups:[]}}};
await window.eval('refreshStockTradeAI()');
check('paper outcome breakdown includes costs and sample count',()=>{const t=window.document.getElementById('skTradeOutcomes').textContent;assert.match(t,/Paper trade outcomes/);assert.match(t,/Target reached/);assert.match(t,/180/)});
response.config.mode='live';await window.eval('refreshStockTradeAI()');
check('live outcome panel never mixes paper results',()=>{const t=window.document.getElementById('skTradeOutcomes').textContent;assert.match(t,/Live trade outcomes/);assert.match(t,/50/);assert.doesNotMatch(t,/180/);assert.match(t,/1 incomplete records excluded/)});
response.config.mode='paper';await window.eval('refreshStockTradeAI()');
response.execution_traces=[
 {id:'paper',mode:'paper',symbol:'<img src=x onerror=alert(1)>',contract:'TESTCE',stage:'FILLED',reason:'Paper simulation',qualified_at:'2026-10-21T10:00:00+05:30',updated:'2026-10-21T10:00:02+05:30',first_fill_at:'2026-10-21T10:00:02+05:30',events:[{stage:'FILLED',ts:'2026-10-21T10:00:02+05:30',details:{execution_measurement:{slippage_vs_ask_bps:1.2,confirmation_seconds:.5}}}]},
 {id:'live',mode:'live',symbol:'LIVEONLY',stage:'UNKNOWN',reason:'No unique broker match',events:[]}
];
response.option_model_review={status:'CHAMPION RETAINED',reason:'More later outcomes required',samples:120,validation_samples:20,validation_sessions:3};
response.option_rollback_available=true;
response.forward_review.exit_research={modes:{paper:{path_records:2,mean_giveback:250,paths_with_gaps:1,policies:[{name:'Net breakeven after 1R',triggered:0,censored:1,incomplete:1}]}}};
await window.eval('refreshStockTradeAI()');
check('paper trace distinguishes a simulated fill and excludes live orders',()=>{const t=window.document.getElementById('skExecutionTrace').textContent;assert.match(t,/Paper filled/);assert.doesNotMatch(t,/LIVEONLY/);assert.match(t,/1.20 bps/)});
check('execution trace escapes instrument text',()=>{const node=window.document.getElementById('skExecutionTrace');assert.equal(node.querySelectorAll('img').length,0);assert.match(node.textContent,/<img src=x/)});
check('model-review panel shows new evidence counts and optional rollback',()=>{const n=window.document.getElementById('skOptionReview');assert.match(n.textContent,/CHAMPION RETAINED/);assert.match(n.textContent,/120/);assert.equal(window.document.getElementById('skOptionRollback').disabled,false)});
check('exit research clearly separates censored paths from actual live exits',()=>{const t=window.document.getElementById('skExitResearch').textContent;assert.match(t,/Censored/);assert.match(t,/Incomplete/);assert.match(t,/live exits are unchanged/)});
response.config.armed=true;await window.eval('refreshStockTradeAI()');
check('model rollback is disabled while armed',()=>assert.equal(window.document.getElementById('skOptionRollback').disabled,true));
response.config.armed=false;response.config.mode='live';await window.eval('refreshStockTradeAI()');
check('uncertain live order is never displayed as a fill',()=>{const t=window.document.getElementById('skExecutionTrace').textContent;assert.match(t,/Awaiting broker evidence/);assert.doesNotMatch(t,/Paper filled|<img/)});
response.config.mode='paper';await window.eval('refreshStockTradeAI()');
check('rendering model evidence never sends a rollback or arm request',()=>assert.ok(requests.every(r=>r.options.method!=='POST')));
window.eval("window.__savedStockRender=renderStockCandidates;renderStockCandidates=()=>{throw Error('Test render error')}");
await window.eval('refreshStockTradeAI()');
check('rendering exceptions are not mislabelled as network disconnects',()=>{assert.match(window.document.getElementById('skConnectionState').textContent,/Display rendering failed/);assert.match(window.document.getElementById('skNotice').textContent,/Test render error/)});
window.eval('renderStockCandidates=window.__savedStockRender');await window.eval('refreshStockTradeAI()');
check('successful render recovers the display status',()=>assert.match(window.document.getElementById('skConnectionState').textContent,/Dashboard connected/));
check('no order placement endpoint is called by the UI load',()=>assert.ok(requests.every(r=>!r.url.includes('/orders'))));
const expectedIndexHash='af545d3d29c494d52f5c7cee1c637fc8380a18d61bab3d0f2a221da0bdafee85';
const indexScript=(s)=>[...s.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]).find(c=>c.includes('let ixState=null'));
check('Index script is byte-identical',()=>assert.equal(createHash('sha256').update(indexScript(html)).digest('hex'),expectedIndexHash));
let authAvailable=false;
window.fetch=async()=>new window.Response(JSON.stringify(authAvailable?{authenticated:true,csrf:'offline-csrf'}:{error:'busy'}),{status:authAvailable?200:503});
window.eval(scripts.find(code=>code.includes('const websiteFetch =')));await new Promise(r=>setTimeout(r,20));
check('access-check failure is distinct from Stock market-data staleness',()=>{assert.ok(window.document.documentElement.classList.contains('website-connection-warning'));assert.match(window.document.documentElement.dataset.websiteMessage,/Website access check delayed/);assert.match(window.document.getElementById('skConnectionState').textContent,/Dashboard connected/)});
authAvailable=true;await window.eval('websiteCheck(true)');
check('access-check banner clears automatically on recovery',()=>assert.ok(!window.document.documentElement.classList.contains('website-connection-warning')));
window.__stopStockUI();await window.happyDOM.close();console.log(checks+' Stock UI checks passed');

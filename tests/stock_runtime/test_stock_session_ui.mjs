import fs from 'node:fs';
import assert from 'node:assert/strict';
import {Window} from './.qa_node/node_modules/happy-dom/lib/index.js';
const source=fs.existsSync(new URL('./index.html',import.meta.url))?new URL('./index.html',import.meta.url):new URL('../../index.html',import.meta.url);
const html=fs.readFileSync(source,'utf8');
const scripts=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
const window=new Window({url:'http://session-test.local',settings:{disableJavaScriptFileLoading:true,disableCSSFileLoading:true}});
window.document.body.innerHTML='<div id="loginArea"></div>';
let website={status:200,body:{authenticated:true,csrf:'test'}},broker={status:200,body:{logged_in:true}},calls=0,release=null,hold=false;
window.fetch=async(url)=>{
 if(url==='/api/website-status')return new window.Response(typeof website.body==='string'?website.body:JSON.stringify(website.body),{status:website.status});
 assert.equal(url,'/api/session-status');calls++;
 if(hold)await new Promise(resolve=>{release=resolve});
 if(broker.error)throw broker.error;
 return new window.Response(typeof broker.body==='string'?broker.body:JSON.stringify(broker.body),{status:broker.status});
};
window.eval(scripts.find(s=>s.includes('const websiteFetch =')));
await new Promise(r=>setTimeout(r,10));
window.eval('window.signouts=0;websiteSignOut=()=>{window.signouts++;websiteRevoked=true};');
const session=scripts.find(s=>s.includes('async function refreshKiteSession()'));
window.eval(session.slice(session.indexOf('let kiteSessionCheck='),session.indexOf('// Proactively re-check')));
let checks=0;function check(name,action){action();checks++;console.log('PASS',name)}
await window.eval('checkSession()');
check('confirmed broker session displays connected controls',()=>{assert.ok(window.document.getElementById('logoutBtn'));assert.match(window.document.getElementById('loginArea').textContent,/Connected to Kite/)});
for(const reply of [{status:503,body:{error:'busy'}},{status:502,body:'<html>proxy error</html>'},{status:401,body:'proxy login'},{status:200,body:{error:'malformed'}},{error:new Error('Network disconnected')},{error:new window.DOMException('Aborted','AbortError')}]){
 broker=reply;await window.eval('checkSession()');
 check('transient '+(reply.status||reply.error.name)+' preserves login and retries',()=>{assert.ok(window.document.getElementById('logoutBtn'));assert.equal(window.document.getElementById('loginBtn'),null);assert.match(window.document.getElementById('kiteSessionWarning').textContent,/retrying/);assert.equal(window.signouts,0)});
}
broker={status:200,body:{logged_in:true}};await window.eval('checkSession()');
check('successful retry clears connection warning',()=>assert.equal(window.document.getElementById('kiteSessionWarning'),null));
hold=true;const before=calls;
const burst=Array.from({length:12},()=>window.eval('checkSession()'));
check('simultaneous status checks share one request',()=>assert.equal(calls-before,1));
release();await Promise.all(burst);hold=false;
broker={status:200,body:{logged_in:false,session_expired:true}};await window.eval('checkSession()');
check('confirmed broker expiry still asks for reconnection',()=>{assert.ok(window.document.getElementById('loginBtn'));assert.match(window.document.getElementById('loginArea').textContent,/Session expired/)});
for(const reply of [{status:401,body:'<html>gateway</html>'},{status:401,body:{session_expired:true}},{status:503,body:{error:'website_access_unavailable'}}]){
 website=reply;await window.eval('websiteCheck(true)');
 check('ambiguous auth '+reply.status+' is retried without website logout',()=>{assert.equal(window.signouts,0);assert.ok(window.document.documentElement.classList.contains('website-connection-warning'))});
}
website={status:200,body:{authenticated:true,csrf:'fresh'}};await window.eval('websiteCheck(true)');
check('website warning clears after recovery',()=>assert.ok(!window.document.documentElement.classList.contains('website-connection-warning')));
website={status:401,body:{website_login_required:true}};await window.eval('websiteCheck(true)');
check('explicit website revocation still signs out',()=>assert.equal(window.signouts,1));
await window.happyDOM.close();console.log(checks+' session UI checks passed');

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
function page(name) {
 const elements=new Map(), alerts=[], requests=[], blobs=[];
 const el=id=> {if(!elements.has(id)) elements.set(id,{dataset:{canManage:'true'},value:'',textContent:'',innerHTML:'',disabled:false,checked:false,scrollTop:0,addEventListener(k,fn){this[k]=fn;},setAttribute(){},reportValidity(){return true;}});return elements.get(id);};
 const c=vm.createContext({document:{getElementById:el,addEventListener(){},createElement(){return {click(){}};}},window:{addEventListener(){}},console,confirm:()=>true,setInterval:()=>1,clearInterval(){},setTimeout(){},location:{reload(){}},URL:{createObjectURL:()=>'',revokeObjectURL(){}},Blob:class{constructor(parts){blobs.push(parts.join(''));}},showAlert:(...a)=>alerts.push(a),apiCall:async(...args)=>{requests.push(args);return {ok:true,data:{success:true,data:{logs:[]}}};},esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')});
 const html=fs.readFileSync(`InferenceNode/templates/${name}.html`,'utf8');
 vm.runInContext([...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n'),c);
 return {c,el,alerts,requests,blobs};
}
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
test('node refresh preserves unsaved configuration',()=>{const {c,el}=page('node_info');c.updateConfiguration({node_name:'saved'});el('nodeName').value='draft';el('configForm').input();c.updateConfiguration({node_name:'old',log_level:'INFO'});assert.equal(el('nodeName').value,'draft');});
test('node hardware text is escaped and missing numeric fields do not crash',()=>{const {c,el}=page('node_info');c.updateGPUInfo([{name:'<img src=x>',type:'<script>',driver_version:'<b>'}]);c.updateStorageInfo([{device:'<img>',mountpoint:'<script>',total:0,used:0}]);assert.doesNotMatch(el('gpuInfo').innerHTML,/<img|<script|<b>/);assert.doesNotMatch(el('storageInfo').innerHTML,/<img|<script|NaN/);});
test('node shows unavailable instead of healthy when request fails',async()=>{const {c,el}=page('node_info');c.apiCall=async()=>({ok:false,error:'offline'});await c.loadNodeInfo();assert.equal(el('nodeStatusDisplay').textContent,'Unavailable');assert.match(el('nodeSaveStatus').textContent,/unavailable/);});
test('node rejects false success and prevents duplicate saves',async()=>{const {c,el}=page('node_info');vm.runInContext('nodeReady=true',c);el('nodeName').value='Node';el('logLevel').value='INFO';const d=deferred();let calls=0;c.apiCall=()=>{calls++;return d.promise;};const first=el('configForm').submit({preventDefault(){}});await el('configForm').submit({preventDefault(){}});assert.equal(calls,1);d.resolve({ok:true,data:{success:false}});await first;assert.match(el('nodeSaveStatus').textContent,/Save failed/);assert.equal(el('nodeFields').disabled,false);});
test('logs include critical errors and system entries in counts',()=>{const {c,el}=page('logs');vm.runInContext("allLogs=[{level:'CRITICAL',component:'system',message:'bad'}]",c);c.updateLogStatistics();assert.equal(el('errorCount').textContent,1);assert.equal(el('systemLogCount').textContent,1);});
test('log download contains raw text and exception details, display escapes it',()=>{const {c,el,blobs}=page('logs');vm.runInContext("allLogs=[{timestamp:'2026-09-23',level:'ERROR',component:'web',message:'a < b & c',exception:'trace <x>'}]",c);c.filterLogs();assert.match(el('logContainer').innerHTML,/a &lt; b &amp; c/);c.downloadLogs();assert.match(blobs[0],/a < b & c/);assert.match(blobs[0],/trace <x>/);});
test('log refresh cannot restore entries after clear begins',async()=>{const {c,el}=page('logs');const d=deferred();c.apiCall=url=>url==='/api/logs'?d.promise:Promise.resolve({ok:true,data:{success:true}});const loading=c.loadLogs();await c.clearLogs();d.resolve({ok:true,data:{success:true,data:{logs:[{level:'ERROR',component:'system',message:'old'}]}}});await loading;assert.doesNotMatch(el('logContainer').innerHTML,/old/);});
test('failed clear never claims success',async()=>{const {c,alerts}=page('logs');c.apiCall=async()=>({ok:false,error:'denied'});await c.clearLogs();assert.equal(alerts[0][0],'error');});
test('both configuration forms send JSON with the required content type',async()=>{
 for(const name of ['node_info','logs']) {
  const {c,el,requests}=page(name);
  vm.runInContext(name==='logs'?'logSettingsReady=true':'nodeReady=true',c);
  el('nodeName').value='Node';el('logLevel').value='INFO';el('globalLogLevel').value='INFO';el('maxLogSize').value='10';el('logRetention').value='7';
  await el(name==='logs'?'logSettingsForm':'configForm').submit({preventDefault(){}});
  assert.equal(requests[0][1].headers['Content-Type'],'application/json');
  assert.doesNotThrow(()=>JSON.parse(requests[0][1].body));
 }
});

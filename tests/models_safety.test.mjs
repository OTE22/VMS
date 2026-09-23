import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const source = fs.readFileSync(new URL('../InferenceNode/templates/models.html', import.meta.url), 'utf8').replace(/\r/g, '');
const helpers = source.slice(source.indexOf('let modelOperationPending'), source.indexOf('let currentView'));
function fn(name) {
  let start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, name);
  if (source.slice(start - 6, start) === 'async ') start -= 6;
  return source.slice(start, source.indexOf('\n}\n', start) + 3);
}
function setup(extra = {}) {
  const controls = [{disabled:false}, {disabled:true}, {disabled:false}];
  const ctx = vm.createContext({document:{querySelectorAll:()=>controls}, esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;'), showAlert(){}, console, ...extra});
  vm.runInContext(helpers, ctx);
  return {ctx, controls};
}
test('one upload or download locks all form fields and restores original disabled states', () => {
  const {ctx,controls} = setup();
  assert.equal(ctx.beginModelOperation(), true);
  assert.ok(controls.every(c=>c.disabled));
  assert.equal(ctx.beginModelOperation(), false);
  ctx.endModelOperation();
  assert.deepEqual(controls.map(c=>c.disabled), [false,true,false]);
  assert.equal(ctx.beginModelOperation(), true);
});
test('older model GET cannot replace the latest response', async () => {
  const pending=[], displayed=[], list={};
  const {ctx}=setup({fetch:()=>new Promise(r=>pending.push(r)),document:{getElementById:()=>list},updateStorageStats(){},displayModels:models=>displayed.push(models)});
  vm.runInContext(fn('refreshModels'),ctx);
  const a=ctx.refreshModels(), b=ctx.refreshModels();
  pending[1]({ok:true,json:async()=>({models:{newer:{}},stats:{}})});await b;
  pending[0]({ok:true,json:async()=>({models:{older:{}},stats:{}})});await a;
  assert.equal(displayed.length,1);assert.ok(displayed[0].newer);
});
test('delete safely encodes identifiers, suppresses duplicate requests, and allows retry', async()=>{
  let resolve;const urls=[];
  const {ctx}=setup({confirm:()=>true,fetch:url=>{urls.push(url);return new Promise(r=>resolve=r)},refreshModels(){}});
  vm.runInContext(fn('deleteModel'),ctx);
  const id="model's #1";
  const first=ctx.deleteModel(id);await ctx.deleteModel(id);
  assert.deepEqual(urls,['/api/models/'+encodeURIComponent(id)]);
  resolve({ok:false,json:async()=>({error:'temporary failure'})});await first;
  const retry=ctx.deleteModel(id);assert.equal(urls.length,2);
  resolve({ok:true,json:async()=>({})});await retry;
  assert.equal((source.match(/onclick="deleteModel\(this.dataset.modelId\)"/g)||[]).length,2);
  assert.ok(!source.includes("deleteModel('${esc(modelId)}')"));
});
test('storage badge does not claim runtime compatibility',()=>{
  const {ctx}=setup();vm.runInContext(fn('modelStatusBadge'),ctx);
  const html=ctx.modelStatusBadge({status:'AVAILABLE',validation_status:'PASSED'});
  assert.ok(html.includes('Stored'));assert.ok(html.includes('Runtime compatibility is checked when starting a pipeline'));
});
test('download shares upload guard and stale cleanup cannot hide a newer operation',async()=>{
  const controls=[{disabled:false}];const elements={};const timers=[];const pending=[];
  for(const id of ['ultralyticsModel','description','modelName','downloadUltralyticsBtn','downloadProgress','downloadStatus','downloadDetails']) elements[id]={value:'',style:{},disabled:false};
  const bar={style:{}};elements.downloadProgress.querySelector=()=>bar;
  const {ctx}=setup({document:{querySelectorAll:()=>controls,getElementById:id=>elements[id]},fetch:()=>new Promise(r=>pending.push(r)),refreshModels(){},setTimeout:f=>timers.push(f)});
  vm.runInContext(fn('downloadUltralyticsModel'),ctx);
  elements.ultralyticsModel.value='yolov8n.pt';const first=ctx.downloadUltralyticsModel();
  await ctx.downloadUltralyticsModel();assert.equal(pending.length,1);assert.equal(ctx.beginModelOperation(),false);assert.equal(controls[0].disabled,true);
  pending[0]({ok:true,json:async()=>({model_id:'one'})});await first;assert.equal(controls[0].disabled,false);
  elements.ultralyticsModel.value='yolov8s.pt';const second=ctx.downloadUltralyticsModel();timers[0]();
  assert.equal(elements.downloadProgress.style.display,'block');
  pending[1]({ok:false,json:async()=>({error:'download unavailable'})});await second;assert.equal(controls[0].disabled,false);
});
test('server error messages are escaped before shared HTML notifications',()=>{
  const alerts=[];const {ctx}=setup({showAlert:(...args)=>alerts.push(args)});
  ctx.showModelAlert('error','<img src=x>');assert.equal(alerts[0][1],'&lt;img src=x&gt;');
});

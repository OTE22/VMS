import fs from 'node:fs'; import vm from 'node:vm'; import test from 'node:test'; import assert from 'node:assert/strict';
const source=fs.readFileSync(new URL('../InferenceNode/templates/pipeline_management.html',import.meta.url),'utf8').replace(/\r/g,'');
const helpers=source.slice(source.indexOf('const pendingPipelineControls'),source.indexOf('// Preview toggle switch functionality'));
function fn(name){let a=source.indexOf(`function ${name}(`);assert.ok(a>=0,name);if(source.slice(a-6,a)==='async ')a-=6;return source.slice(a,source.indexOf('\n}\n',a)+3)}
function setup(extra={}) {
 const alerts=[];const pipeline={id:'p',name:'Camera',status:'stopped',inference_enabled:false,destinations:[{id:'d',enabled:false}]};
 const ctx=vm.createContext({allPipelines:[pipeline],filteredPipelines:[pipeline],activePreviews:new Set(),MY_NODE_ID:'node',document:{querySelectorAll:()=>[]},showAlert:(...a)=>alerts.push(a),refreshPipelines:async()=>{},console:{log(){},error(){},warn(){}},...extra});
 vm.runInContext(helpers,ctx);return {ctx,alerts,pipeline};
}
test('newer refresh wins and older request cannot replace it',async()=>{
 const pending=[];const {ctx}=setup({fetch:()=>new Promise(r=>pending.push(r)),updatePipelineData(){}});vm.runInContext(fn('refreshPipelines'),ctx);
 const a=ctx.refreshPipelines(true),b=ctx.refreshPipelines(true);pending[1]({ok:true,json:async()=>({pipelines:[{id:'p',status:'running'}]})});await b;
 pending[0]({ok:true,json:async()=>({pipelines:[{id:'p',status:'stopped'}]})});await a;assert.equal(ctx.allPipelines[0].status,'running');
});
test('publisher poll started before a save cannot undo the saved state',async()=>{
 const pending=[];const {ctx,pipeline}=setup({fetch:()=>new Promise(r=>pending.push(r)),updatePipelinePublisherUI(){}});vm.runInContext(fn('updatePublisherStates'),ctx);
 const poll=ctx.updatePublisherStates('p');const save=ctx.savePipelineControl('p','d',true);
 pending[1]({ok:true,json:async()=>({})});await save;assert.equal(pipeline.destinations[0].enabled,true);
 pending[0]({ok:true,json:async()=>({publishers:{d:{enabled:false}}})});await poll;assert.equal(pipeline.destinations[0].enabled,true);
});
test('publisher polling does not overlap for the same pipeline',async()=>{
 let resolve,calls=0;const {ctx}=setup({fetch:()=>{calls++;return new Promise(r=>resolve=r)},updatePipelinePublisherUI(){}});vm.runInContext(fn('updatePublisherStates'),ctx);
 const first=ctx.updatePublisherStates('p');await ctx.updatePublisherStates('p');assert.equal(calls,1);resolve({ok:true,json:async()=>({publishers:{}})});await first;
});
test('bulk inference includes stopped pipelines and skips busy controls',async()=>{
 const calls=[];const {ctx,pipeline}=setup({fetch:async url=>{calls.push(url);return {ok:true,json:async()=>({})}}});
 ctx.allPipelines.push({id:'busy',status:'running',inference_enabled:false});vm.runInContext("pendingPipelineControls.add('busy');"+fn('setAllInference'),ctx);
 await ctx.setAllInference(true);assert.deepEqual(calls,['/api/pipeline/p/inference/enable']);assert.equal(pipeline.inference_enabled,true);
});
test('bulk inference cannot submit twice while pending',async()=>{
 let resolve,calls=0;const {ctx}=setup({fetch:()=>{calls++;return new Promise(r=>resolve=r)}});vm.runInContext(fn('setAllInference'),ctx);const first=ctx.setAllInference(true);await ctx.setAllInference(true);assert.equal(calls,1);resolve({ok:true,json:async()=>({})});await first;
});
test('saved runtime failure shows warning and keeps durable state',async()=>{
 const {ctx,pipeline,alerts}=setup({fetch:async()=>({ok:false,json:async()=>({saved:true,runtime_applied:false,error:'Setting saved; runtime failed'})})});
 const ok=await ctx.savePipelineControl('p',null,true);assert.equal(ok,false);assert.equal(pipeline.inference_enabled,true);assert.equal(alerts.at(-1)[0],'warning');
});
test('Duplicate has one in-flight request and becomes retryable after failure',async()=>{
 let resolve,calls=0;const {ctx}=setup({fetch:()=>{calls++;return new Promise(r=>resolve=r)}});vm.runInContext(fn('duplicatePipeline'),ctx);
 const a=ctx.duplicatePipeline('p');await ctx.duplicatePipeline('p');assert.equal(calls,1);resolve({ok:false,status:500,json:async()=>({error:'Database unavailable'})});await a;
 const b=ctx.duplicatePipeline('p');assert.equal(calls,2);resolve({ok:true,json:async()=>({pipeline_id:'copy'})});await b;
});
test('details escape stored names and JSON without changing raw config',()=>{
 const content={};const {ctx}=setup({document:{getElementById:()=>content},getStatusColor:()=>'',capitalizeStatus:s=>s});vm.runInContext(fn('renderPipelineDetails'),ctx);
 const pipeline={name:'<b>Test</b>',status:'stopped',frame_source:{config:{source:'</pre><b>URL</b>'}}};ctx.renderPipelineDetails(pipeline);
 assert.ok(content.innerHTML.includes('&lt;b&gt;Test&lt;/b&gt;'));assert.ok(!content.innerHTML.includes('</pre><b>URL</b>'));assert.equal(pipeline.name,'<b>Test</b>');
});
test('fullscreen handler no longer interpolates pipeline name',()=>{
 assert.ok(!source.includes("openFullPreview('${pipeline.id}', '${pipeline.name}')"));assert.ok(source.includes("openFullPreview('${pipeline.id}')"));
});
test('both filter implementations match canonical IP cameras and CUDA GPUs',()=>{
 const fields={searchPipelines:{value:''},pipelinesContainer:{style:{}},emptyState:{style:{}}};
 const {ctx,pipeline}=setup({document:{getElementById:id=>fields[id],querySelectorAll:()=>[]},activeQuickFilters:new Set(),availableEngineTypes:[],currentViewMode:'card',updateQuickFilterBadges(){},updatePipelineUI(){},renderCardView(){},setTimeout:f=>f()});
 pipeline.frame_source={capture_type:'ip_camera'};pipeline.model={device:'cuda:0'};
 vm.runInContext(fn('matchesSourceCategory')+fn('matchesHardwareCategory')+fn('filterPipelines')+fn('updatePipelineData'),ctx);
 for(const category of ['rtsp','gpu']) {ctx.activeQuickFilters=new Set([category]);ctx.filterPipelines();assert.equal(ctx.filteredPipelines.length,1);ctx.updatePipelineData(new Set());assert.equal(ctx.filteredPipelines.length,1);}
});

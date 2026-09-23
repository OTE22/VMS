import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../InferenceNode/templates/pipeline_management.html',import.meta.url),'utf8').replace(/\r/g,'');
const helpers=source.slice(source.indexOf('const pendingPipelineControls'),source.indexOf('// Preview toggle switch functionality'));
function setup(fetch){
 const pipeline={id:'p',status:'stopped',inference_enabled:true,destinations:[{id:'w',enabled:false,auto_disabled:true}]};
 const inference={dataset:{}},publisher={dataset:{publisherId:'w'}};
 const element={querySelectorAll:s=>s.startsWith('.inference')?[inference]:[publisher]};
 const ctx=vm.createContext({allPipelines:[pipeline],fetch,showAlert:()=>{},refreshPipelines:async()=>{},document:{querySelectorAll:()=>[element]}});
 vm.runInContext(helpers,ctx);
 return {ctx,pipeline,inference,publisher};
}
test('stopped settings remain checked and editable; starting settings lock',()=>{
 const {ctx,pipeline}=setup();
 assert.match(ctx.inferenceControl(pipeline),/checked/);
 assert.doesNotMatch(ctx.inferenceControl(pipeline),/disabled/);
 pipeline.status='starting'; assert.match(ctx.inferenceControl(pipeline),/disabled/);
 assert.doesNotMatch(source,/pipeline.inference_enabled = newStatus/);
});
test('in-flight saves lock both controls and cannot start pipeline or submit twice',async()=>{
 let resolve; const calls=[];
 const {ctx,pipeline,inference,publisher}=setup(url=>{calls.push(url);return new Promise(r=>resolve=r)});
 const saved=ctx.toggleInference('p',false);
 assert.equal(inference.disabled,true); assert.equal(publisher.disabled,true);
 await ctx.toggleInference('p',true); assert.equal(calls.length,1);
 resolve({ok:true,json:async()=>({})}); await saved;
 assert.equal(pipeline.status,'stopped');assert.equal(pipeline.inference_enabled,false);
 assert.equal(inference.checked,false);assert.equal(inference.disabled,false);
 assert.deepEqual(calls,['/api/pipeline/p/inference/disable']);
});
test('failed saves restore saved checkbox states',async()=>{
 const {ctx,pipeline,inference,publisher}=setup(async()=>({ok:false,json:async()=>({error:'denied'})}));
 await ctx.savePipelineControl('p','w',true);
 assert.equal(pipeline.destinations[0].enabled,false);assert.equal(publisher.checked,false);
 await ctx.toggleInference('p',false);assert.equal(inference.checked,true);
});
test('manual publisher recovery clears local failure state after success',async()=>{
 const {ctx,pipeline,publisher}=setup(async()=>({ok:true,json:async()=>({})}));
 await ctx.savePipelineControl('p','w',true);
 assert.equal(pipeline.destinations[0].auto_disabled,false);assert.equal(publisher.checked,true);
 assert.match(source,/Retry \/ Re-enable/);
});
test('all template scripts remain valid JavaScript',()=>{
 for(const match of source.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)){
  if(match[1].trim()) new vm.Script(match[1]);
 }
});

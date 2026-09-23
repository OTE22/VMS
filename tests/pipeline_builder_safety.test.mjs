import fs from 'node:fs'; import vm from 'node:vm'; import test from 'node:test'; import assert from 'node:assert/strict';
const source=fs.readFileSync(new URL('../InferenceNode/templates/pipeline_builder.html',import.meta.url),'utf8').replace(/\r/g,'');
function fn(name){let a=source.indexOf(`function ${name}(`);if(source.slice(a-6,a)==='async ')a-=6;return source.slice(a,source.indexOf('\n}\n',a)+3)}
function saveHarness(){
 let handler,resolve;const sent=[],alerts=[];
 const controls={pipelineForm:{dataset:{},addEventListener:(e,f)=>handler=f},pipelineName:{value:'A'},pipelineDescription:{value:''},inferenceEngine:{value:'pass'},inferenceEnabled:{checked:false},frameSourceType:{value:'webcam'},selectedModel:{value:''},inferenceDevice:{value:'cpu'},destinationType:{value:''},submitButton:{disabled:false},resetButton:{disabled:false}};
 const ctx=vm.createContext({document:{getElementById:id=>controls[id],querySelectorAll:()=>[controls.submitButton,controls.resetButton]},pipelineSavePending:false,currentDestinations:[{type:'null',config:{}}],editingDestinationId:null,editingPipelineId:'A',getFrameSourceConfig:()=>({source:0}),collectFrameSourceConfigFromSchema:()=>({isValid:true,missingFields:[]}),showAlert:(...a)=>alerts.push(a),refreshPipelines:async()=>{},resetForm:()=>{},fetch:(url,opts)=>{sent.push({url,...opts});return new Promise(r=>resolve=r)}});
 const a=source.indexOf("document.getElementById('pipelineForm').addEventListener('submit'");vm.runInContext(source.slice(a,source.indexOf('\n});',a)+4),ctx);
 return {ctx,controls,sent,alerts,submit:()=>handler({preventDefault(){}}),finish:ok=>resolve({ok,json:async()=>({pipeline_id:'A',error:'DB unavailable'})})};
}
test('repeated Save issues one request and failures restore retry controls',async()=>{
 const h=saveHarness();const pending=h.submit();await h.submit();assert.equal(h.sent.length,1);assert.equal(h.controls.resetButton.disabled,true);h.finish(false);await pending;assert.equal(h.ctx.pipelineSavePending,false);assert.equal(h.controls.submitButton.disabled,false);assert.equal(h.alerts.at(-1)[0],'error');
});
test('pending save prevents reset and edit from replacing its draft',async()=>{
 const h=saveHarness();const pending=h.submit();vm.runInContext(fn('resetForm')+fn('editPipeline')+fn('cancelEdit'),h.ctx);h.ctx.resetForm();h.ctx.cancelEdit();await h.ctx.editPipeline('B');assert.equal(h.ctx.editingPipelineId,'A');h.finish(false);await pending;
});
test('unapplied destination edits block Save',async()=>{
 const h=saveHarness();h.ctx.editingDestinationId='d';await h.submit();assert.equal(h.sent.length,0);assert.equal(h.alerts[0][0],'warning');
});
test('required source element missing blocks validation',()=>{
 const ctx=vm.createContext({availableFrameSourceTypes:[{type:'ip_camera',config_schema:{fields:[{name:'source',required:true,type:'text'}]}}],document:{getElementById:()=>null},console:{warn(){}},isLocalMediaSourceType:()=>false});vm.runInContext(fn('collectFrameSourceConfigFromSchema'),ctx);assert.equal(ctx.collectFrameSourceConfigFromSchema('ip_camera').isValid,false);
});
test('failed cleanup retains test id; retry deletes it',async()=>{
 let ok=false;const alerts=[];
 const ctx=vm.createContext({activeTestSourceId:'temporary',document:{getElementById:()=>null},bootstrap:{Modal:{getInstance:()=>null}},showAlert:(...a)=>alerts.push(a),fetch:async()=>({ok,status:ok?200:500})});
 vm.runInContext(fn('cleanupTestPipeline')+fn('stopTestSource'),ctx);await ctx.stopTestSource();assert.equal(ctx.activeTestSourceId,'temporary');assert.equal(alerts.length,1);ok=true;await ctx.stopTestSource();assert.equal(ctx.activeTestSourceId,null);
});
test('pipeline names are text, not injected markup',()=>{
 const list={};const ctx=vm.createContext({document:{getElementById:()=>list},allPipelines:{}});vm.runInContext(fn('escapeBuilderText')+fn('updatePipelinesList'),ctx);ctx.updatePipelinesList([{id:'id',name:'<b>Test</b>',status:'stopped'}]);assert.ok(list.innerHTML.includes('&lt;b&gt;Test&lt;/b&gt;'));assert.ok(!list.innerHTML.includes('<b>Test</b>'));
});
test('older upload cannot release Save while another upload is pending',async()=>{
 const nodes={pipelineForm:{dataset:{}}};const resolves=[];
 const ctx=vm.createContext({document:{getElementById:id=>nodes[id]},FormData:class{append(){}},fetch:()=>new Promise(r=>resolves.push(r)),showAlert(){}});vm.runInContext(fn('handleFileUpload'),ctx);
 async function start(id){nodes[id+'_btn']={};nodes[id+'_status']={};await ctx.handleFileUpload({id,files:[{name:'test.mp4'}],getAttribute:k=>k==='data-target-field'?'source':'/upload'});return {pending:nodes[id+'_btn'].onclick()}}
 const a=await start('a'),b=await start('b');resolves[0]({ok:true,json:async()=>({relative_source:'a.mp4'})});await a.pending;assert.equal(nodes.pipelineForm.dataset.uploadInProgress,'true');resolves[1]({ok:true,json:async()=>({relative_source:'b.mp4'})});await b.pending;assert.equal(nodes.pipelineForm.dataset.uploadInProgress,undefined);
});

test('favorite selection sends its reference for server-side resolution',()=>{
 const elements={destinationType:{value:'mqtt'},favoriteConfigSelect:{value:'favorite-id'},destinationTypeSelector:{querySelectorAll:()=>[]}};
 const ctx=vm.createContext({document:{getElementById:id=>elements[id]},editingDestinationId:null,currentDestinations:[],collectDestinationConfigFromSchema:()=>({config:{password:'***'},isValid:true}),showAlert(){},updateDestinationsList(){},updateDestinationConfig(){}});
 vm.runInContext(fn('addDestination'),ctx);ctx.addDestination();assert.equal(ctx.currentDestinations[0].favorite_id,'favorite-id');assert.equal(elements.favoriteConfigSelect.value,'');
});
test('camera test submits existing source reference and canonical inference flag',async()=>{
 let body;const elements={frameSourceType:{value:'ip_camera'},testSourceBtn:{innerHTML:'Test',disabled:false}};
 const ctx=vm.createContext({activeTestSourceId:null,editingPipelineId:'existing-camera',document:{getElementById:id=>elements[id]},collectFrameSourceConfigFromSchema:()=>({config:{password:'***'},isValid:true}),console:{error(){}},showAlert(){},fetch:async(url,opts)=>{body=JSON.parse(opts.body);return {ok:false,json:async()=>({error:'Isolated test: stop before starting'})}}});
 vm.runInContext(fn('testFrameSource'),ctx);await ctx.testFrameSource();assert.equal(body.source_pipeline_id,'existing-camera');assert.equal(body.inference_enabled,false);assert.equal(body.model.device,'cpu');assert.equal(body.inference,undefined);
});

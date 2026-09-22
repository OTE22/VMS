import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../InferenceNode/templates/pipeline_builder.html',import.meta.url),'utf8').replace(/\r/g,'');
const upload=html.slice(html.indexOf('async function handleFileUpload('),html.indexOf('// Initialize page',html.indexOf('async function handleFileUpload(')));
function harness(response={ok:true,relative_source:'registered/new.mp4'}) {
    let value='';const options=[];
    const source={tagName:'SELECT',options,dataset:{loadVersion:'1'},classList:{add(){}},
        add(option){options.push(option);},set value(v){value=options.some(o=>o.value===v)?v:'';},get value(){return value;},dispatchEvent(){}};
    const input={id:'upload_file',files:[{name:'new.mp4'}],getAttribute:k=>k==='data-target-field'?'source':'/api/media/upload-video'};
    const elements={source,pipelineForm:{dataset:{}},upload_file_btn:{},upload_file_status:{appendChild(){}},mediaPickerWrap:{hidden:false}};
    const ctx=vm.createContext({document:{getElementById:id=>elements[id],createElement:()=>({})},
        FormData:class{append(){}},Option:class{constructor(text,value){this.text=text;this.value=value;}},Event:class{},
        fetch:async()=>({ok:response.ok,json:async()=>response}),showAlert(){},loadMediaSources(){}});
    vm.runInContext(upload,ctx);
    return {ctx,input,elements};
}
test('upload inserts and selects canonical reference without asking for a path',async()=>{
    const h=harness();await h.ctx.handleFileUpload(h.input);await h.elements.upload_file_btn.onclick();
    assert.equal(h.elements.source.value,'registered/new.mp4');
    assert.equal(h.elements.source.dataset.selected,'registered/new.mp4');
    assert.equal(h.elements.source.dataset.loadVersion,'2');
    assert.equal(h.elements.mediaPickerWrap.hidden,true);
    assert.equal(h.elements.pipelineForm.dataset.uploadInProgress,undefined);
    assert.equal(h.input.disabled,false);
});
test('failed upload stays retryable and does not select a nonexistent path',async()=>{
    const h=harness({ok:false,error:'Storage unavailable'});await h.ctx.handleFileUpload(h.input);await h.elements.upload_file_btn.onclick();
    assert.equal(h.elements.source.value,'');assert.equal(h.elements.mediaPickerWrap.hidden,false);
    assert.equal(h.elements.upload_file_btn.disabled,false);assert.match(h.elements.upload_file_status.textContent,/Storage unavailable/);
});
test('missing registry reference is not reported as selected',async()=>{
    const h=harness({ok:true,filename:'old-name.mp4'});await h.ctx.handleFileUpload(h.input);await h.elements.upload_file_btn.onclick();
    assert.equal(h.elements.source.value,'');assert.equal(h.elements.upload_file_btn.textContent,'Retry');
});
test('late media listing cannot overwrite an uploaded selection',async()=>{
    const h=harness();let resolve;
    h.ctx.fetch=()=>new Promise(r=>{resolve=r;});
    const start=html.indexOf('async function loadMediaSources(');
    vm.runInContext(html.slice(start,html.indexOf('\n}\n',start)+3),h.ctx);
    const pending=h.ctx.loadMediaSources();
    h.elements.source.add({value:'registered/new.mp4'});h.elements.source.value='registered/new.mp4';
    h.elements.source.dataset.loadVersion='3';
    resolve({ok:true,json:async()=>({sources:[]})});await pending;
    assert.equal(h.elements.source.value,'registered/new.mp4');
});
test('file picker local path never becomes persisted source configuration',()=>{
    const start=html.indexOf('function collectFrameSourceConfigFromSchema(');
    const script=html.slice(start,html.indexOf('\n}\n',start)+3);
    const ctx=vm.createContext({document:{getElementById:()=>({value:'C:\\fakepath\\new.mp4'})},console,
        isLocalMediaSourceType:()=>false});
    vm.runInContext('var availableFrameSourceTypes=[{type:"video_file",config_schema:{fields:[{name:"upload_file",type:"file"}]}}];'+script,ctx);
    assert.equal(Object.hasOwn(ctx.collectFrameSourceConfigFromSchema('video_file').config,'upload_file'),false);
});

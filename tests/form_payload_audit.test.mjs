// Field-level audit of shipped JavaScript collectors and submit handlers.
// Regression checks for the deployed form contracts.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const base = new URL('../InferenceNode/templates/', import.meta.url);
const page = n => fs.readFileSync(new URL(n + '.html', base), 'utf8').replace(/\r/g, '');
function fn(source, name) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start >= 0, name);
    return source.slice(start, source.indexOf('\n}\n', start) + 3);
}
function collector(values, schema, target='publisher') {
    const source = page(target === 'publisher' ? 'publisher' : 'pipeline_builder');
    const name = target === 'publisher' ? 'collectConfigFromSchema' : 'collectDestinationConfigFromSchema';
    const ctx = vm.createContext({console: {warn(){}}, document:{getElementById: id => values[id] || null}});
    vm.runInContext('var availableDestinationTypes = ' + JSON.stringify([schema]) + ';' + fn(source, name), ctx);
    return ctx[name](schema.type);
}
const schema = {type:'mqtt',config_schema:{fields:[
    {name:'server',type:'text',required:true,label:'Server'},
    {name:'port',type:'number'}, {name:'password',type:'password'},
    {name:'include_image_data',type:'checkbox'}, {name:'rate_limit',type:'number'},
]}};
test('publisher collector sends numbers and false booleans without losing zero', () => {
    const r=collector({server:{value:' broker '},port:{value:'1883'},password:{value:'***'},
        include_image_data:{checked:false},rate_limit:{value:'0'}},schema);
    assert.deepEqual(JSON.parse(JSON.stringify(r.config)), {server:'broker',port:1883,password:'***',include_image_data:false,rate_limit:0});
    assert.equal(r.isValid,true);
});
test('blank credentials preserve stored values and explicit clear is available', () => {
    const r=collector({server:{value:'broker'}, password:{value:''}},schema);
    assert.equal(Object.hasOwn(r.config,'password'),false);
    assert.equal(page('publisher').includes('Clear stored credential'),true);
});
test('Publisher password collector preserves significant whitespace', () => {
    const r=collector({server:{value:'broker'},password:{value:' secret with spaces '}},schema);
    assert.equal(r.config.password,' secret with spaces ');
});
for (const target of ['publisher','builder']) {
    test(`${target} submits header text for server normalization`, () => {
        const prefix=target==='publisher'?'':'dest_';
        const r=collector({[prefix+'headers']:{value:'X-Audit: value'}},
            {type:'webhook',config_schema:{fields:[{name:'headers',type:'textarea'}]}},target);
        assert.equal(typeof r.config.headers,'string');
    });
}
test('Publisher test-message submit handler calls the real endpoint', () => {
    const html=page('publisher');
    assert.match(html,/<form id="testPublishForm">/);
    const scripts=[...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
    assert.match(scripts,/testPublishForm/);
    assert.match(scripts,/\/api\/publisher\/test-favorites/);
});
test('missing Publisher schema returns a complete invalid result', () => {
    const result=collector({}, {type:'unknown'});
    assert.equal(result.isValid,false);
    assert.equal(result.missingFields.length,1);
});

function handler(pageName, form, values) {
    const source=page(pageName);
    const marker=`document.getElementById('${form}').addEventListener('submit', async function(e) {`;
    const start=source.indexOf(marker); assert.ok(start>=0);
    const script=source.slice(start,source.indexOf('\n});',start)+4);
    let submit;
    const sent=[];
    const elements=Object.fromEntries(Object.entries(values).map(([k,v])=>[k,typeof v==='boolean'?{checked:v}:{value:v}]));
    elements[form]={addEventListener:(_,f)=>{submit=f;}};
    const ctx=vm.createContext({document:{getElementById:id=>elements[id]},
        fetch:async(url,options)=>{sent.push({url,...options});return {ok:true,json:async()=>({pipeline_id:'new-id'})};},
        showAlert(){},startTelemetryUpdates(){}, resetForm(){},refreshPipelines:async()=>{},
        getFrameSourceConfig:()=>({source:'rtsp://camera.invalid/live',buffer_size:0}),
        collectFrameSourceConfigFromSchema:()=>({isValid:true,missingFields:[]}),
    });
    vm.runInContext('var editingPipelineId=null; var currentDestinations=[{type:"null",config:{},enabled:false}];',ctx);
    vm.runInContext(script,ctx);
    return {submit:()=>submit({preventDefault(){}}),sent,ctx};
}
test('node form sends exactly name, selected log level and numeric deployment port',async()=>{
    const h=handler('node_info','configForm',{nodeName:'New node',logLevel:'WARNING',webPort:'5555'});
    await h.submit();assert.equal(h.sent[0].url,'/api/node/config');
    assert.deepEqual(JSON.parse(h.sent[0].body),{node_name:'New node',log_level:'WARNING',web_port:5555});
});
test('log form preserves disabled file logging and integer preferences',async()=>{
    const h=handler('logs','logSettingsForm',{globalLogLevel:'ERROR',maxLogSize:'21',logRetention:'12',enableFileLogging:false});
    await h.submit();assert.deepEqual(JSON.parse(h.sent[0].body),{log_level:'ERROR',max_log_size_mb:21,retention_days:12,enable_file_logging:false});
});
test('telemetry form sends blank broker and edited topic/port rather than omitting them',async()=>{
    const h=handler('telemetry','telemetryConfigForm',{telemetryEnabled:false,publishInterval:'17',mqttServer:'',mqttPort:'1885',telemetryTopic:'new/topic'});
    await h.submit();assert.deepEqual(JSON.parse(h.sent[0].body),{enabled:false,publish_interval:17,mqtt_server:'',mqtt_port:1885,mqtt_topic:'new/topic'});
});
test('Pass pipeline sends an empty model reference for canonical server normalization',async()=>{
    const h=handler('pipeline_builder','pipelineForm',{pipelineName:'Pass test',pipelineDescription:'d',inferenceEngine:'pass',inferenceEnabled:false,frameSourceType:'ip_camera',selectedModel:'',inferenceDevice:'cpu'});
    await h.submit();assert.equal(h.sent[0].url,'/api/pipeline/create');
    const payload=JSON.parse(h.sent[0].body);
    assert.deepEqual(payload.model,{id:'',engine_type:'pass',device:'cpu'});
    assert.equal(payload.inference_enabled,false);
    assert.equal(payload.destinations[0].enabled,false);
});

test('Publisher explicit credential clear emits null', () => {
    const r=collector({server:{value:'broker'},password:{value:'***'},password__clear:{checked:true}},schema);
    assert.equal(r.config.password,null);
});

test('Publisher test form posts parsed JSON and selected favorite IDs', async () => {
    const source=page('publisher');
    const start=source.indexOf("document.getElementById('testPublishForm').addEventListener");
    const script=source.slice(start,source.indexOf('\n});',start)+4);
    let submit; let sent;
    const ctx=vm.createContext({document:{getElementById:id=>id==='testPublishForm'?{addEventListener:(_,fn)=>{submit=fn;}}:{value:'{"value":42}'},
        querySelectorAll:()=>[{value:'favorite-1'},{value:'favorite-2'}]},
        fetch:async(url,options)=>{sent={url,...options};return {ok:true,json:async()=>({message:'Test completed',results:{}})};},showAlert(){}});
    vm.runInContext(script,ctx);
    await submit({preventDefault(){}});
    assert.equal(sent.url,'/api/publisher/test-favorites');
    assert.deepEqual(JSON.parse(sent.body),{message:{value:42},favorite_ids:['favorite-1','favorite-2']});
});

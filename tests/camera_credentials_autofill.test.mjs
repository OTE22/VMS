import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../InferenceNode/templates/pipeline_builder.html',import.meta.url),'utf8').replace(/\r/g,'');
function extract(name){const a=source.indexOf(`function ${name}(`);return source.slice(a,source.indexOf('\n}\n',a)+3);}
const schema={fields:[{name:'username',type:'text',label:'Username',default:'must-not-prefill'}, {name:'password',type:'password',label:'Password',default:'must-not-prefill'}]};
test('new camera fields are blank and do not invite reuse of VMS login credentials',()=>{
    const ctx=vm.createContext({window:{currentFrameSourceType:'ip_camera'},isLocalMediaSourceType:()=>false});
    vm.runInContext(extract('generateFormFieldsWithDiscoveredDevices'),ctx);
    const html=ctx.generateFormFieldsWithDiscoveredDevices(schema);
    assert.match(source,/<form id="pipelineForm" autocomplete="off">/);
    assert.match(html,/name="camera-source-username" autocomplete="off"/);
    assert.match(html,/name="camera-source-password"\s+autocomplete="new-password"/);
    assert.doesNotMatch(html,/must-not-prefill|value="(?:admin|\*\*\*)"/);
    assert.match(html,/data-lpignore="true"/);
});
test('editing retains saved camera username and redacted password sentinel',()=>{
    const controls={username:{value:''},password:{value:''}};
    const ctx=vm.createContext({console,document:{getElementById:id=>controls[id]},isLocalMediaSourceType:()=>false});
    vm.runInContext('var availableFrameSourceTypes='+JSON.stringify([{type:'ip_camera',config_schema:schema}])+';'+extract('populateFrameSourceConfig'),ctx);
    ctx.populateFrameSourceConfig({capture_type:'ip_camera',config:{username:'camera-operator',password:'***'}});
    assert.equal(controls.username.value,'camera-operator');assert.equal(controls.password.value,'***');
});

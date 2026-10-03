// Behavioral checks using small ComfyUI/DOM mocks; no third-party dependencies.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor(tagName) { this.tagName = tagName; this.children = []; this.style = {}; this.value = ''; }
  append(...elements) { this.children.push(...elements); }
  replaceChildren(...elements) { this.children = elements; }
  setAttribute() {}
  removeAttribute(name) { delete this[name]; }
  click() {}
  focus() {}
}
const find = (el, text) => el.children.flatMap(child => [child, ...all(child)]).find(e => e.textContent === text);
const all = el => el.children.flatMap(c => [c, ...all(c)]);
(async () => {
  for (const [fail, clearBetweenShots] of [[false, false], [false, true], [true, true]]) {
    let extension, root, domWidget, submits = [], polls = {}, failed = false, uploadedFilename, freeCalls = 0;
    const created = [];
    const shots = [{start_frame:'a.png',end_frame:'b.png',prompt:'first',duration:5},{start_frame:'c.png',end_frame:'d.png',prompt:'second',duration:7}];
    const app = {
      registerExtension(e) { extension = e; },
      async graphToPrompt() { return {output:{'7':{class_type:'ShotQueue',inputs:{}},'8':{class_type:'SaveVideo',inputs:{}}},workflow:{nodes:[{id:7,widgets_values:[]} ]}}; }
    };
    const api = {
      apiURL(path) { return path; },
      async queuePrompt(_, job) {
        if (submits.length && clearBetweenShots) assert.equal(polls['p1'], 2, 'cleanup mode must wait for complete history');
        if (submits.length) assert.equal(freeCalls, clearBetweenShots ? 1 : 0, 'cleanup must be armed before next prompt');
        submits.push(job); return {prompt_id:`p${submits.length}`,node_errors:{}};
      },
      async fetchApi(url, options) {
        if (url === '/free') {
          assert.equal(options.method, 'POST');
          assert.deepEqual(JSON.parse(options.body), {free_memory:true});
          freeCalls++;
          return {ok:true};
        }
        if (url === '/upload/image') {
          uploadedFilename = options.body.fields.find(([name]) => name === 'image')[2];
          return {ok:true,async json() { return {name:uploadedFilename,subfolder:'shot_queue',type:'input'}; }};
        }
        if (url === '/shot_queue/images') {
          return {ok:true,async json() { return {files:['existing.png','shot_queue/previous.png']}; }};
        }
        assert(clearBetweenShots, 'prequeued shots must not wait for browser-side history polling');
        const id = url.split('/').pop(); polls[id] = (polls[id] || 0) + 1;
        if (id === 'p1') assert.equal(freeCalls, clearBetweenShots ? 1 : 0, 'cleanup must be armed before completion');
        return {ok:true,async json() {
          if (polls[id] === 1) return {};
          if (fail) { failed = true; return {[id]:{status:{completed:false,status_str:'error'}}}; }
          return {[id]:{status:{completed:true,status_str:'success'}}};
        }};
      }
    };
    const context = {app,api,document:{createElement:tag=>{const el=new Element(tag);created.push(el);return el;}},setTimeout:cb=>{cb();},structuredClone,console,crypto:require('node:crypto').webcrypto,URLSearchParams,FormData:class {constructor(){this.fields=[];}append(...field){this.fields.push(field);}}};
    vm.createContext(context);
    const source = fs.readFileSync(require('node:path').join(__dirname,'../web/shot_queue.js'),'utf8').replace(/^import .*;\n/gm,'');
    vm.runInContext(source,context);
    function Node() {}
    await extension.beforeRegisterNodeDef(Node,{name:'ShotQueue'});
    const n = new Node(); n.id=7;
    n.widgets=[{name:'shots_json',value:JSON.stringify(shots)},{name:'shot_index',value:0}];
    n.graph={setDirtyCanvas(){}}; n.setSize=()=>{}; n.addDOMWidget=(_,__,el,options)=>{root=el;domWidget={options,computeSize:()=>[460,540]};return domWidget;};
    n.onNodeCreated();
    assert.equal(domWidget.computeSize,undefined);
    assert.equal(domWidget.computeLayoutSize().minWidth,300);
    assert.equal(domWidget.computeLayoutSize().minHeight,180);
    assert.equal(domWidget.options.getMinHeight(),180);
    assert.deepEqual(root.children.map(e=>e.className),['sq-toolbar','sq-status','sq-list']);
    assert.match(root.innerHTML,/\.sq-list\{[^}]*overflow-y:auto/);
    assert(find(root,'Start Frame')); assert(find(root,'End Frame')); assert(find(root,'Add Shot'));
    const cleanup = all(root).find(e=>e.type==='checkbox');
    assert(cleanup);
    cleanup.checked=clearBetweenShots; cleanup.onchange();
    assert.equal(n.properties.clearBetweenShots,clearBetweenShots);
    await find(root,'Run Queue').onclick();
    assert.equal(submits.length,fail ? 1 : 2);
    assert.equal(freeCalls,clearBetweenShots ? 1 : 0);
    if (!clearBetweenShots) {
      assert.equal(Object.keys(polls).length,0);
      assert.match(root.children[1].textContent,/Queued 2 shots in ComfyUI/);
      find(root,'Stop Queue').onclick();
      assert.match(root.children[1].textContent,/ComfyUI's queue controls/);
    }
    assert.equal(submits[0].output['7'].inputs.shot_index,0);
    assert.equal(submits[0].output['7'].inputs.shots_json,JSON.stringify(shots));
    if (!fail) assert.equal(submits[1].output['7'].inputs.shot_index,1);
    else assert(failed);
    find(root,'Add Shot').onclick(); assert.equal(JSON.parse(n.widgets[0].value).length,3);
    n.widgets[0].value=JSON.stringify(shots); n.onConfigure();
    assert.equal(all(root).filter(e=>e.textContent==='Start Frame').length,2);
    cleanup.checked=!clearBetweenShots; n.onConfigure();
    assert.equal(cleanup.checked,clearBetweenShots);
    await find(root,'Select Image').onclick();
    const existing = all(root).find(e=>e.tagName==='select');
    const preview = all(root).find(e=>e.tagName==='img');
    assert.equal(existing.children[2].value,'shot_queue/previous.png');
    existing.value='existing.png'; existing.onchange();
    assert.equal(new URL(`http://localhost${preview.src}`).searchParams.get('filename'),'existing.png');
    assert.equal(new URL(`http://localhost${preview.src}`).searchParams.get('subfolder'),'');
    assert.equal(JSON.parse(n.widgets[0].value)[0].start_frame,'a.png');
    find(root,'Cancel').onclick();
    assert.equal(preview.src,undefined);
    await find(root,'Select Image').onclick();
    existing.value='shot_queue/previous.png'; existing.onchange();
    assert.equal(new URL(`http://localhost${preview.src}`).searchParams.get('subfolder'),'shot_queue');
    assert.equal(JSON.parse(n.widgets[0].value)[0].start_frame,'a.png');
    find(root,'Use Image').onclick();
    assert.equal(JSON.parse(n.widgets[0].value)[0].start_frame,'shot_queue/previous.png');
    assert.equal(preview.src,undefined);
    context.crypto = {getRandomValues(bytes) { bytes.fill(7); return bytes; }};
    find(root,'Browse').onclick();
    const picker = created.at(-1); picker.files = [{name:'frame.png'}];
    await picker.onchange();
    assert.equal(uploadedFilename, `${'07'.repeat(16)}_frame.png`);
    assert.equal(JSON.parse(n.widgets[0].value)[0].start_frame, `shot_queue/${uploadedFilename}`);
  }
  console.log('Frontend checks passed: prequeue, serial cleanup, error stop, add, restore, image preview and selection, insecure-context upload.');
})().catch(e=>{console.error(e);process.exitCode=1;});

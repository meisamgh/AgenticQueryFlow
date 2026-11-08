// Real Cytoscape headless + DOM interactions, not a substitute for visual browser QA.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const cytoscapeSource=fs.readFileSync(`${__dirname}/../web/vendor/cytoscape-3.30.4.min.js`,'utf8');
const source=fs.readFileSync(`${__dirname}/../web/semantic_explorer.js`,'utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.attributes={};this.dataset={};this.style={};this.hidden=false;this.value='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return (this._text||'')+this.children.map(child=>child.textContent||'').join('');}
  set innerHTML(_){throw new Error('Unsafe metadata rendering');}
  setAttribute(key,value){this.attributes[key]=String(value);}
  addEventListener(key,fn){(this.listeners[key]||=[]).push(fn);}
  querySelectorAll(tag){return this.children.flatMap(child=>[...(child.tagName===tag?[child]:[]),...child.querySelectorAll(tag)]);}
}
function harness(api){
  const graphs=[];const body=new Element('body');body.classList={add(){},remove(){}};
  const context=vm.createContext({window:{},_graphs:graphs,
    document:{body,head:new Element('head'),documentElement:new Element('html'),createElement:tag=>new Element(tag)},
    URLSearchParams,MutationObserver:class{observe(){}disconnect(){}},
    getComputedStyle:()=>({getPropertyValue:()=>''}),console});
  vm.runInContext(cytoscapeSource,context);
  vm.runInContext(`window.cytoscape=opts=>{
    const cy=cytoscape({...opts,container:undefined,headless:true,styleEnabled:false});
    _graphs.push(cy);return cy;
  };`,context);
  vm.runInContext(source,context);
  const mount=new Element('section');mount.hidden=true;
  const controller=context.window.QueryflowSemanticExplorer.create({mount,api,defaultDb:()=> 'shop'});
  const findButton=text=>mount.querySelectorAll('button').find(button=>button.textContent===text);
  return {module:context.window.QueryflowSemanticExplorer,mount,controller,graphs,findButton,
    click:async text=>{const button=findButton(text);assert.ok(button,`Missing ${text}`);await button.listeners.click[0]();}};
}
const table=id=>({id:`table:${id}`,kind:'TABLE',label:id,status:'observed',metadata:{relation:id}});
const metric={id:'metric:net',kind:'METRIC',label:'Net',status:'approved',metadata:{formula:'SUM(amount)'}};
const column={id:'column:orders.amount',kind:'COLUMN',label:'amount',status:'observed',metadata:{type:'REAL'}};
const link={id:'join',source:'table:orders',target:'table:customers',kind:'REFERENCES',status:'authoritative'};
const schema={nodes:[table('orders'),table('customers')],edges:[link],truncated:false,warnings:[]};
function metadataApi(log,override){return async path=>{
  log.push(path);assert.ok(path.startsWith('/api/semantic-graph'));
  if(override){const value=override(path);if(value!==undefined)return value;}
  if(path.endsWith('/sources'))return ['shop','other'];
  if(path.includes('/nodes/'))return {node:{...table('orders'),label:'<img src=x onerror=alert(1)>'},columns:[],relationships:[],associated_kpis:[]};
  if(path.includes('/neighborhood'))return {...schema,nodes:[...schema.nodes,column]};
  const query=new URL(path,'http://localhost').searchParams;
  return query.get('view')==='kpi'?{nodes:[metric],edges:[],truncated:false}:schema;
};}

test('bounded merge preserves IDs, endpoints and raw metadata without aggregation',()=>{
  const h=harness(async()=>[]);
  const result=h.module.mergeGraph(schema,{nodes:[column],edges:[{...link,id:'outside',target:'missing'}]},2);
  assert.equal(result.nodes.length,2);assert.equal(result.edges.length,1);assert.ok(result.truncated);
  assert.equal(JSON.stringify(schema.nodes),JSON.stringify([table('orders'),table('customers')]));h.controller.dispose();
});
test('three views render through Cytoscape with zero chat/SQL/model routes',async()=>{
  const log=[],h=harness(metadataApi(log));await h.controller.open();
  assert.equal(h.graphs.at(-1).nodes().length,2);
  await h.click('KPI dependencies');assert.equal(h.graphs.at(-1).nodes()[0].id(),'metric:net');
  await h.click('Combined');assert.ok(log.some(path=>path.includes('view=combined')));
  assert.ok(log.every(path=>path.startsWith('/api/semantic-graph')));h.controller.dispose();
});
test('node details are safe text, expansion and collapse retain original tables',async()=>{
  const h=harness(metadataApi([]));await h.controller.open();
  h.graphs.at(-1).getElementById('table:orders').emit('tap');await flush();
  assert.ok(h.mount.textContent.includes('<img src=x onerror=alert(1)>'));
  await h.click('Expand neighbors');assert.equal(h.graphs.at(-1).nodes().length,3);
  await h.click('Collapse expansion');await flush();assert.equal(h.graphs.at(-1).nodes().length,2);
  h.controller.dispose();
});
test('layout positions survive view changes and reopen',async()=>{
  const h=harness(metadataApi([]));await h.controller.open();
  const position={x:120,y:180};
  h.graphs.at(-1).getElementById('table:orders').position('x',120).position('y',180);
  await h.click('KPI dependencies');await h.click('Schema');
  assert.equal(JSON.stringify(h.graphs.at(-1).getElementById('table:orders').position()),JSON.stringify(position));
  h.controller.close();await h.controller.open();
  assert.equal(JSON.stringify(h.graphs.at(-1).getElementById('table:orders').position()),JSON.stringify(position));h.controller.dispose();
});
test('source switch is isolated and stale request cannot replace the new view',async()=>{
  let release;const delayed=new Promise(resolve=>{release=resolve;});let delayedOnce=false;
  const h=harness(metadataApi([],path=>{
    if(path.includes('view=schema')&&!delayedOnce){delayedOnce=true;return delayed;}
  }));
  const first=h.controller.open();await flush();await h.click('KPI dependencies');
  release(schema);await first;assert.equal(h.graphs.at(-1).nodes()[0].id(),'metric:net');
  const select=h.mount.querySelectorAll('select')[0];select.value='other';select.listeners.change[0]();await flush();
  assert.equal(select.value,'other');h.controller.dispose();
});
test('empty/error states and cancellation of a closed view stay visible and safe',async()=>{
  const h=harness(async path=>{if(path.endsWith('sources'))return ['shop'];throw new Error('Metadata unavailable');});
  await h.controller.open();assert.ok(h.mount.textContent.includes('Metadata unavailable'));
  h.controller.close();assert.ok(h.mount.hidden);h.controller.dispose();
});
test('scoped responsive/theme/reduced-motion rules and navigation are shipped',()=>{
  const css=fs.readFileSync(`${__dirname}/../web/semantic_explorer.css`,'utf8');
  const html=fs.readFileSync(`${__dirname}/../web/index.html`,'utf8');
  const app=fs.readFileSync(`${__dirname}/../web/app.js`,'utf8');
  for(const token of ['max-width:760px','prefers-reduced-motion','data-theme="dark"','var(--surface)'])assert.ok(css.includes(token));
  assert.ok(html.includes('id="semanticExplorerButton"'));assert.ok(html.includes('/static/semantic_explorer.js'));
  assert.ok(app.includes('View KPI lineage'));assert.ok(app.includes('Show dependencies'));
  assert.ok(!source.includes('innerHTML'));assert.ok(!source.includes('/api/chat'));
});

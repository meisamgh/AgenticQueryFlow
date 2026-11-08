const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(`${__dirname}/../web/app.js`,'utf8');
function harness({busy=false,fail=false}={}) {
  let handler,closed=0,requests=0,focus=0,cleared=0;
  const nodes=new Map();
  const state={busy,lastBody:{accepted:true},activeRunId:'previous',branchFromRunId:'branch',restorePending:true};
  const $=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{
      addEventListener(_,fn){handler=fn;},replaceChildren(){cleared++;},focus(){focus++;},hidden:false,
    });
    return nodes.get(selector);
  };
  const errors=[];
  vm.runInNewContext(source.slice(source.indexOf('$("#resetButton").addEventListener'),source.indexOf('\ninitialize();')),
    {$,state,sessionId:'scoped-session',encodeURIComponent,semanticExplorerView:{close(){closed++;}},
      api:async(url,options)=>{requests++;assert.equal(options.method,'POST');assert.ok(url.includes('/scoped-session/reset'));if(fail)throw Error('Reset failed');},
      showError:message=>errors.push(message)});
  return {run:()=>handler(),state,nodes,errors,counts:()=>({closed,requests,focus,cleared})};
}
test('New conversation closes semantic explorer and focuses a clean composer after reset',async()=>{
  const h=harness();await h.run();
  assert.deepEqual(h.counts(),{closed:1,requests:1,focus:1,cleared:1});
  assert.equal(h.state.lastBody,null);assert.equal(h.state.activeRunId,null);
  assert.equal(h.nodes.get('#emptyState').hidden,false);
});
test('failed reset leaves explorer and previous accepted conversation intact',async()=>{
  const h=harness({fail:true});await h.run();
  assert.equal(h.counts().closed,0);assert.equal(h.counts().cleared,0);
  assert.equal(h.state.activeRunId,'previous');assert.deepEqual(h.errors,['Reset failed']);
});
test('busy conversation cannot be reset through explorer navigation',async()=>{
  const h=harness({busy:true});await h.run();
  assert.deepEqual(h.counts(),{closed:0,requests:0,focus:0,cleared:0});
});

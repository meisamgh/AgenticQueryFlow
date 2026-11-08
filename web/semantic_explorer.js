/* Read-only metadata exploration. No chat, result mutation, SQL or LLM calls. */
window.QueryflowSemanticExplorer = (() => {
  const colors = {TABLE:'#2563eb',COLUMN:'#0f766e',METRIC:'#7c3aed',DIMENSION:'#15803d',
    BUSINESS_RULE:'#b45309',BUSINESS_CONCEPT:'#64748b'};
  const relations = ['REFERENCES','DEPENDS_ON','USES_COLUMN','GROUPABLE_BY','FILTERED_BY','HAS_COLUMN','TIME_BY','SOURCE_TABLE'];
  const el = (tag, cls, text) => {
    const node = document.createElement(tag); node.className = cls || '';
    if (text != null) node.textContent = text;
    return node;
  };
  const button = (label, run) => {
    const node = el('button','secondary-button',label); node.type = 'button';
    node.addEventListener('click', run); return node;
  };
  const elements = graph => [
    ...graph.nodes.map(node => ({group:'nodes',data:{id:node.id,label:node.label,
      kind:node.kind,status:node.status}})),
    ...graph.edges.map(edge => ({group:'edges',data:{id:edge.id,source:edge.source,target:edge.target,
      label:edge.kind.replaceAll('_',' ').toLowerCase(),kind:edge.kind,status:edge.status}})),
  ];
  function mergeGraph(previous, next, limit=250) {
    const nodes = new Map(previous.nodes.map(node => [node.id,node]));
    next.nodes.forEach(node => { if (nodes.has(node.id) || nodes.size < limit) nodes.set(node.id,node); });
    const edges = new Map([...previous.edges,...next.edges].filter(edge => nodes.has(edge.source)
      && nodes.has(edge.target)).map(edge => [edge.id,edge]));
    return {...next,nodes:[...nodes.values()],edges:[...edges.values()].slice(0,500),
      truncated:previous.truncated || next.truncated || nodes.size < new Set(
        [...previous.nodes,...next.nodes].map(node=>node.id)).size || edges.size > 500};
  }
  let rendererPromise;
  function renderer() {
    if (window.cytoscape) return Promise.resolve(window.cytoscape);
    if (!rendererPromise) rendererPromise = new Promise((resolve,reject) => {
      const script = el('script'); script.src='/static/vendor/cytoscape-3.30.4.min.js';
      script.onload=()=>resolve(window.cytoscape);
      script.onerror=()=>{rendererPromise=null; script.remove(); reject(new Error('Graph renderer unavailable. Reload to retry.'));};
      document.head.append(script);
    });
    return rendererPromise;
  }
  function styles() {
    const theme = getComputedStyle(document.documentElement);
    const text = theme.getPropertyValue('--text').trim() || '#0f172a';
    return [
      {selector:'node',style:{label:'data(label)','background-color':'#64748b',color:'#fff',
        'text-valign':'center','text-halign':'center','text-wrap':'wrap','text-max-width':110,
        'font-size':12,'font-family':'Inter, sans-serif',width:128,height:48,
        shape:'round-rectangle','border-width':1.5,'border-color':'#cbd5e1'}},
      ...Object.entries(colors).map(([kind,color])=>({selector:`node[kind="${kind}"]`,
        style:{'background-color':color,shape:kind==='METRIC'?'hexagon':kind==='COLUMN'?'ellipse':'round-rectangle'}})),
      {selector:'edge',style:{label:'data(label)','font-size':10,color:text,'text-background-color':theme.getPropertyValue('--surface').trim()||'#fff',
        'text-background-opacity':.9,'text-background-padding':3,'curve-style':'bezier',width:1.5,
        'target-arrow-shape':'triangle','line-color':'#94a3b8','target-arrow-color':'#94a3b8'}},
      {selector:'edge[status="suggested"]',style:{'line-style':'dashed'}},
      {selector:'edge[status="definition"]',style:{'line-style':'dotted'}},
      {selector:'edge[status="forbidden"]',style:{'line-color':'#dc2626','target-arrow-color':'#dc2626','line-style':'dashed'}},
      {selector:'node:selected',style:{'border-width':4,'border-color':text}},
      {selector:'edge:selected',style:{'line-color':'#168447','target-arrow-color':'#168447',width:3}},
      {selector:'.dimmed',style:{opacity:.2}},
    ];
  }
  function create({mount,api,defaultDb,onOpen=()=>{},onClose=()=>{}}) {
    let cy=null, graph={nodes:[],edges:[]}, dbId='', view='schema', epoch=0, detailEpoch=0;
    let focus=null, nodeKinds=new Set(Object.keys(colors)), edgeKinds=new Set(relations);
    const memories = new Map(), expansions = new Map(); let initial=new Set();
    let selectedTab=null;
    const heading=el('header','semantic-heading'), title=el('div');
    title.append(el('p','intro-eyebrow','METADATA · READ ONLY'),el('h1','','Semantic Explorer'));
    heading.append(title,button('Back to conversation',close));
    const controls=el('div','semantic-toolbar');
    const sourceLabel=el('label','','Datasource'), source=el('select');
    sourceLabel.append(source); source.setAttribute('aria-label','Explorer datasource');
    const searchLabel=el('label','','Find tables, columns or KPIs'), search=el('input');
    search.type='search'; search.placeholder='Search metadata…'; search.setAttribute('aria-label','Search semantic graph');
    searchLabel.append(search);
    const tabs=el('nav','semantic-view-tabs'); tabs.setAttribute('aria-label','Graph views');
    for(const [value,label] of [['schema','Schema'],['kpi','KPI dependencies'],['combined','Combined']]) {
      const tab=button(label,()=>switchView(value)); tab.dataset.view=value; tabs.append(tab);
    }
    controls.append(sourceLabel,tabs,searchLabel,button('Search',()=>load('search')),
      button('Refresh',()=>load('root')),button('Fit graph',()=>cy?.fit(undefined,36)),
      button('Check selected joins',connect));
    const filters=el('details','semantic-filters'), summary=el('summary','','Node and relationship filters');
    filters.append(summary);
    function filterGroup(label, values, selected, update) {
      const group=el('fieldset'); group.append(el('legend','',label));
      for(const value of values) {
        const item=el('label'), box=el('input'); box.type='checkbox'; box.checked=selected.has(value);
        box.addEventListener('change',()=>{box.checked?selected.add(value):selected.delete(value); update();});
        item.append(box,el('span','',value.replaceAll('_',' ').toLowerCase())); group.append(item);
      }
      filters.append(group);
    }
    filterGroup('Nodes',Object.keys(colors),nodeKinds,()=>load('root'));
    filterGroup('Relationships',relations,edgeKinds,()=>load('root'));
    const status=el('p','semantic-status'); status.setAttribute('role','status'); status.setAttribute('aria-live','polite');
    const body=el('div','semantic-body'), canvas=el('div','semantic-canvas');
    canvas.setAttribute('role','img'); canvas.setAttribute('aria-label','Interactive metadata graph. Use the node list for keyboard navigation.');
    const panel=el('aside','semantic-detail'); panel.setAttribute('aria-label','Selected metadata');
    const list=el('details','semantic-node-list'); list.append(el('summary','','Accessible node list'));
    const listBody=el('div'); list.append(listBody);
    body.append(canvas,panel);
    const legend=el('div','semantic-legend');
    Object.entries(colors).forEach(([kind,color])=>{
      const item=el('span'), dot=el('i'); dot.style.backgroundColor=color;
      item.append(dot,el('span','',kind.toLowerCase().replaceAll('_',' '))); legend.append(item);
    });
    legend.append(el('span','','Solid: authoritative · dotted: catalog definition · dashed: suggested · red: forbidden'));
    mount.replaceChildren(heading,controls,filters,status,body,list,legend);
    source.addEventListener('change',()=>{remember();cy?.destroy();cy=null; dbId=source.value; focus=null; restore(); load('root');});
    search.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault(); load('search');}});
    function key(){return `${dbId}:${view}`;}
    function remember() {
      if(!cy)return;
      memories.set(key(),{positions:cy?Object.fromEntries(cy.nodes().map(n=>[n.id(),n.position()])):{},
        zoom:cy?.zoom(),pan:cy?.pan(),kinds:[...nodeKinds],relations:[...edgeKinds]});
    }
    function restore() {
      const saved=memories.get(key());
      nodeKinds.clear(); (saved?.kinds||Object.keys(colors)).forEach(kind=>nodeKinds.add(kind));
      edgeKinds.clear(); (saved?.relations||relations).forEach(kind=>edgeKinds.add(kind));
      filters.querySelectorAll('fieldset').forEach((group,index)=>group.querySelectorAll('input').forEach((box,i)=>{
        box.checked=(index===0?nodeKinds:edgeKinds).has((index===0?Object.keys(colors):relations)[i]);
      }));
    }
    function params(extra={}) {
      const query=new URLSearchParams({db_id:dbId,view,node_limit:'120',edge_limit:'300',...extra});
      [...nodeKinds].forEach(kind=>query.append('kinds',kind));
      [...edgeKinds].forEach(kind=>query.append('relations',kind));
      return query;
    }
    async function draw(next,{merge=false}={}) {
      const token=epoch;
      const factory=await renderer();
      if(token!==epoch || mount.hidden)return;
      const positions=cy?Object.fromEntries(cy.nodes().map(n=>[n.id(),n.position()])):
        (memories.get(key())?.positions||{});
      graph=merge?mergeGraph(graph,next):next;
      if(!cy) {
        cy=factory({container:canvas,elements:[],style:styles(),minZoom:.12,maxZoom:3,
          selectionType:'additive'});
        cy.on('tap','node',event=>inspect(event.target.id()));
        cy.on('tap','edge',event=>inspectEdge(event.target.id()));
      }
      cy.batch(()=>{cy.elements().remove(); cy.add(elements(graph));});
      cy.layout({name:'breadthfirst',directed:view!=='schema',padding:40,spacingFactor:1.4,
        animate:false,fit:true}).run();
      cy.nodes().forEach(node=>{if(positions[node.id()]) node.position(positions[node.id()]);});
      if(merge) cy.fit(undefined,36);
      else if(memories.get(key())?.zoom) {cy.zoom(memories.get(key()).zoom);cy.pan(memories.get(key()).pan);}
      listBody.replaceChildren(...graph.nodes.map(node=>button(`${node.label} · ${node.kind.toLowerCase()}`,()=>inspect(node.id))));
      status.textContent=graph.nodes.length?`${graph.nodes.length} nodes · ${graph.edges.length} links${graph.truncated?' · bounded view; expand a node or search to explore more':''}. ${(graph.warnings||[]).join(' ')}`:
        `No ${view==='kpi'?'KPI definitions':'matching metadata'} in this view. ${(graph.warnings||[]).join(' ')}`;
      if(graph.nodes.some(node=>node.kind==='METRIC' && node.status==='suggested'))
        status.textContent+=' Candidate definitions are not human-approved business contracts.';
    }
    async function load(mode='root',nodeId=null) {
      const token=++epoch; ++detailEpoch;
      status.textContent='Loading verified metadata…';
      if(mode!=='expand')panel.replaceChildren(el('p','','Select a node or relationship to inspect.'));
      try {
        if(!nodeKinds.size) {await draw({nodes:[],edges:[],truncated:false});return;}
        const lineage=mode==='upstream'||mode==='downstream';
        const query=params(lineage?{node_id:nodeId,direction:mode,depth:'6'}:
          mode==='expand'?{node_id:nodeId,depth:'1'}:mode==='search'?{search:search.value,depth:'0'}:{depth:'0'});
        const route=lineage?'lineage':mode==='expand'?'neighborhood':'';
        const next=await api(`/api/semantic-graph${route?'/'+route:''}?${query}`);
        if(token!==epoch || mount.hidden) return;
        if(!edgeKinds.size)next.edges=[];
        if(mode==='expand') expansions.set(nodeId,new Set(next.nodes.filter(n=>!graph.nodes.some(old=>old.id===n.id)).map(n=>n.id)));
        else {expansions.clear(); initial=new Set(next.nodes.map(n=>n.id));}
        await draw(next,{merge:mode==='expand'});
        if(lineage) status.textContent=(mode==='downstream'?'Potential impact, not proof of breakage. ':'Verified upstream dependency paths. ')+status.textContent;
      } catch(error) {if(token===epoch){status.textContent=error.message||'Metadata unavailable.';}}
    }
    async function inspect(id) {
      focus=id; const token=++detailEpoch, graphToken=epoch;
      panel.replaceChildren(el('p','','Loading node details…'));
      try {
        const data=await api(`/api/semantic-graph/nodes/${encodeURIComponent(id)}?${new URLSearchParams({db_id:dbId})}`);
        if(token!==detailEpoch || graphToken!==epoch || mount.hidden) return;
        panel.replaceChildren(el('h2','',data.node.label),el('p','semantic-provenance',`${data.node.kind} · ${data.node.status} · ${data.node.provenance}`));
        const actions=el('div','semantic-detail-actions');
        actions.append(button('Expand neighbors',()=>load('expand',id)),button('Collapse expansion',()=>collapse(id)),
          button('Upstream lineage',()=>load('upstream',id)),button('Potential downstream impact',()=>load('downstream',id)));
        panel.append(actions); fields(data.node.metadata,panel);
        if(data.columns) {
          panel.append(el('h3','',`Columns (${data.column_count})`));
          data.columns.forEach(column=>panel.append(button(`${column.label} · ${column.metadata.type}${column.metadata.primary_key?' · PK':''}`,()=>inspect(column.id))));
          if(data.columns_truncated)panel.append(el('p','','Column list bounded to 100. Search for another column.'));
        }
        if(data.associated_kpis?.length) {
          panel.append(el('h3','','Approved KPIs'));
          data.associated_kpis.forEach(node=>panel.append(button(node.label,()=>{switchView('kpi',node.id);})));}
        panel.append(el('h3','','Connected relationships'));
        data.relationships.forEach(edge=>{const b=button(`${edge.kind} · ${edge.source} → ${edge.target}`,()=>edgeDetails(edge));panel.append(b);});
      } catch(error){if(token===detailEpoch)panel.replaceChildren(el('p','',error.message));}
    }
    function fields(metadata,target) {
      const dl=el('dl');
      Object.entries(metadata).forEach(([name,value])=>{
        dl.append(el('dt','',name.replaceAll('_',' ')),el('dd','',typeof value==='object'?JSON.stringify(value,null,2):String(value??'Not available')));
      }); target.append(dl);
    }
    function edgeDetails(edge) {
      ++detailEpoch;
      panel.replaceChildren(el('h2','',edge.kind.replaceAll('_',' ')));
      fields(edge,panel);
      panel.append(button('Inspect source',()=>inspect(edge.source)),button('Inspect target',()=>inspect(edge.target)));
    }
    function inspectEdge(id){const edge=graph.edges.find(e=>e.id===id);if(edge)edgeDetails(edge);}
    function collapse(id) {
      const removed=expansions.get(id); if(!removed)return;
      expansions.delete(id);
      const retained=new Set([...initial,...expansions.keys(),...[...expansions.values()].flatMap(ids=>[...ids])]);
      const nodes=graph.nodes.filter(node=>!removed.has(node.id)||retained.has(node.id)||node.id===id);
      const ids=new Set(nodes.map(node=>node.id));
      draw({...graph,nodes,edges:graph.edges.filter(e=>ids.has(e.source)&&ids.has(e.target))});
    }
    async function connect() {
      const tables=cy?cy.nodes(':selected').filter(n=>n.data('kind')==='TABLE').map(n=>n.id().slice(6)):[];
      if(tables.length<2 || tables.length>8){status.textContent='Select 2–8 tables (tap nodes or box-select) to inspect a bounded join path.';return;}
      const token=epoch, query=new URLSearchParams({db_id:dbId});tables.forEach(t=>query.append('tables',t));
      try {
        const result=await api(`/api/semantic-graph/connect?${query}`);if(token!==epoch)return;
        panel.replaceChildren(el('h2','','Selected join paths'));
        fields(result,panel);
        if(result.status==='CONNECTED')await draw({nodes:result.nodes,edges:result.edges,truncated:false});
        status.textContent=`${result.status}: ${result.explanation}${result.bridges.length?' Bridge tables: '+result.bridges.join(', '):''}`;
      }catch(error){status.textContent=error.message;}
    }
    async function switchView(value,nodeId=null) {
      remember(); cy?.destroy(); cy=null; view=value; focus=nodeId; search.value='';restore();
      selectedTab=value; tabs.querySelectorAll('button').forEach(tab=>tab.setAttribute('aria-pressed',String(tab.dataset.view===value)));
      await load(nodeId?(value==='schema'?'expand':'upstream'):'root',nodeId);
    }
    async function open(options={}) {
      if(cy){remember();cy.destroy();cy=null;}
      mount.hidden=false; document.body.classList.add('semantic-explorer-open');onOpen();
      const token=++epoch; status.textContent='Loading configured datasources…';
      try {
        const values=await api('/api/semantic-graph/sources');if(token!==epoch)return;
        source.replaceChildren(...values.map(value=>{const option=el('option','',value);option.value=value;return option;}));
        dbId=options.dbId||dbId||defaultDb();
        if(!values.includes(dbId))dbId=values[0]||''; source.value=dbId;
        if(!dbId){status.textContent='No configured datasource is available.';return;}
        await switchView(options.view||selectedTab||'schema',options.nodeId||null);
      }catch(error){status.textContent=error.message;}
    }
    function close() {remember();++epoch;++detailEpoch;cy?.destroy();cy=null;mount.hidden=true;document.body.classList.remove('semantic-explorer-open');onClose();}
    const observer=new MutationObserver(()=>{if(cy)cy.style(styles());});
    observer.observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
    return {open,close,dispose(){close();observer.disconnect();mount.replaceChildren();}};
  }
  return {create,elements,mergeGraph,colors};
})();

/* Read-only metadata exploration. No chat, result mutation, SQL or LLM calls. */
window.QueryflowSemanticExplorer = (() => {
  const colors = {TABLE:'#2563eb',COLUMN:'#0f766e',METRIC:'#8b5cf6',DIMENSION:'#15803d',
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
  function connectionLabel(edge) {
    if(edge.kind!=='REFERENCES')return edge.kind.replaceAll('_',' ').toLowerCase();
    const cardinality=String(edge.cardinality||'unknown').toLowerCase().replaceAll('_','-');
    const known=['one-to-one','one-to-many','many-to-one','many-to-many'];
    return 'foreign key · '+(known.includes(cardinality)?cardinality:'cardinality unknown');
  }
  function cardBackground(kind,dark=false) {
    // Decorative, locally generated SVG only. Metadata remains plain canvas text.
    const color=colors[kind]||colors.BUSINESS_CONCEPT;
    const glyph=kind==='METRIC'?'<path d="M192 10l5 5-5 5-5-5z"/>':
      kind==='TABLE'?'<path fill="none" stroke="currentColor" d="M187 10h10v10h-10zM187 14h10M192 14v6"/>':
      kind==='BUSINESS_RULE'?'<path d="M188 10h8l-2 4 2 4h-7v4h-1z"/>':
      '<rect x="188" y="11" width="9" height="9" rx="2"/>';
    const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="210" height="78" viewBox="0 0 210 78"><defs><clipPath id="card"><rect width="210" height="78" rx="10"/></clipPath></defs><g clip-path="url(#card)"><rect width="210" height="78" fill="${dark?'#18232f':'#ffffff'}"/><rect width="6" height="78" fill="${color}"/><rect x="181" y="5" width="22" height="22" rx="6" fill="${color}" fill-opacity=".12"/><g fill="${color}" color="${color}">${glyph}</g></g></svg>`;
    return 'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg);
  }
  const elements = graph => [
    ...graph.nodes.map(node => ({group:'nodes',data:{id:node.id,label:node.label+'\n'+(node.kind==='METRIC'?(node.status==='approved'?'Approved KPI':'KPI')+' · v'+(node.metadata?.version||1):node.kind.toLowerCase().replaceAll('_',' ')),
      kind:node.kind,status:node.status}})),
    ...graph.edges.map(edge => ({group:'edges',data:{id:edge.id,source:edge.source,target:edge.target,
      label:connectionLabel(edge),kind:edge.kind,status:edge.status}})),
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
  function lineageAuthority(graph) {
    const proof=graph.edges.filter(edge=>edge.kind!=='HAS_COLUMN');
    const approved=proof.some(edge=>edge.authoritative||edge.status==='definition');
    const suggested=proof.some(edge=>!edge.authoritative&&edge.status!=='definition');
    return !proof.length?'Lineage unavailable':approved&&suggested?'Mixed lineage':suggested?'Suggested lineage':'Approved lineage';
  }
  function layeredPositions(graph) {
    const nodes=[...graph.nodes].sort((a,b)=>a.id.localeCompare(b.id));
    const byId=new Map(nodes.map(node=>[node.id,node])), adjacent=new Map(nodes.map(n=>[n.id,[]]));
    for(const edge of graph.edges)if(edge.kind!=='REFERENCES'&&byId.has(edge.source)&&byId.has(edge.target)){
      adjacent.get(edge.source).push(edge.target);adjacent.get(edge.target).push(edge.source);
    }
    const seen=new Set(), components=[],result={};
    for(const node of nodes)if(!seen.has(node.id)){
      const component=[],queue=[node.id];seen.add(node.id);
      for(let i=0;i<queue.length;i++){const id=queue[i];component.push(byId.get(id));
        for(const next of adjacent.get(id))if(!seen.has(next)){seen.add(next);queue.push(next);}}
      components.push(component);
    }
    let xOffset=0,yOffset=0,rowHeight=0;
    for(const component of components){
      const metrics=component.filter(n=>n.kind==='METRIC'),rank=new Map(metrics.map(n=>[n.id,0]));
      for(let pass=0;pass<Math.min(metrics.length,16);pass++)for(const edge of graph.edges)
        if(edge.kind==='DEPENDS_ON'&&rank.has(edge.source)&&rank.has(edge.target))
          rank.set(edge.target,Math.max(rank.get(edge.target),rank.get(edge.source)+1));
      const base=Math.max(0,...rank.values()),levels=new Map();
      for(const node of component){const level=rank.get(node.id)??(base+(node.kind==='BUSINESS_RULE'?1:node.kind==='TABLE'?3:2));
        if(!levels.has(level))levels.set(level,[]);levels.get(level).push(node);}
      const perRow=component.length>30?4:3;
      const width=Math.max(...[...levels.values()].map(row=>Math.min(row.length,perRow)))*246;
      if(xOffset&&xOffset+width>1500){xOffset=0;yOffset+=rowHeight+90;rowHeight=0;}
      let y=0;
      for(const [,row] of [...levels].sort((a,b)=>a[0]-b[0])){
        row.sort((a,b)=>a.id.localeCompare(b.id)).forEach((node,i)=>{
          const count=Math.min(perRow,row.length-Math.floor(i/perRow)*perRow);
          result[node.id]={x:xOffset+width/2+(i%perRow-(count-1)/2)*246,y:yOffset+y+Math.floor(i/perRow)*105};
        });y+=Math.ceil(row.length/perRow)*105+25;
      }
      xOffset+=width+90;rowHeight=Math.max(rowHeight,y);
    }
    return result;
  }
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
    const dark=document.documentElement.dataset.theme==='dark';
    const tints=dark?{TABLE:'#18243a',COLUMN:'#152d2d',METRIC:'#29213b',DIMENSION:'#192e22',BUSINESS_RULE:'#302719',BUSINESS_CONCEPT:'#252b35'}:
      {TABLE:'#eff6ff',COLUMN:'#eff9f7',METRIC:'#f3edff',DIMENSION:'#f0f9f2',BUSINESS_RULE:'#fff8e9',BUSINESS_CONCEPT:'#f5f6f8'};
    return [
      {selector:'node',style:{label:'data(label)','background-color':theme.getPropertyValue('--surface').trim()||'#fff',color:text,
        'text-valign':'center','text-halign':'center','text-wrap':'wrap','text-max-width':155,
        'text-justification':'left','text-margin-x':-6,'text-margin-y':4,
        'font-size':13,'font-weight':600,'font-family':'Inter, sans-serif',width:210,height:78,
        shape:'round-rectangle','border-width':2,'border-color':'#cbd5e1',
        'background-image':node=>cardBackground(node.data('kind'),dark),'background-fit':'contain',
        'underlay-color':'#103d32','underlay-opacity':.045,'underlay-padding':4,'underlay-shape':'round-rectangle'}},
      ...Object.entries(colors).map(([kind,color])=>({selector:`node[kind="${kind}"]`,
        style:{'border-color':color,'background-color':tints[kind],'background-opacity':1}})),
      {selector:'edge',style:{label:'data(label)','font-size':11,color:text,'text-background-color':theme.getPropertyValue('--surface').trim()||'#fff',
        'text-background-opacity':.9,'text-background-padding':3,'curve-style':'bezier',width:1.5,
        'target-arrow-shape':'triangle','line-color':'#94a3b8','target-arrow-color':'#94a3b8'}},
      {selector:'edge[status="suggested"]',style:{'line-style':'dashed'}},
      {selector:'edge[status="definition"]',style:{'line-style':'dotted'}},
      {selector:'edge[status="forbidden"]',style:{'line-color':'#dc2626','target-arrow-color':'#dc2626','line-style':'dashed'}},
      {selector:'node:selected',style:{'border-width':3,'border-color':'#0f766e','underlay-opacity':.12}},
      {selector:'edge:selected',style:{'line-color':'#168447','target-arrow-color':'#168447',width:3}},
      {selector:'edge.hovered, edge:selected',style:{label:'data(label)'}},
      {selector:'.lineage',style:{'line-color':'#168447','target-arrow-color':'#168447',width:2.5}},
      {selector:'.matched',style:{'border-width':4,'border-color':'#168447'}},
      {selector:'.dimmed',style:{opacity:.6}},
    ];
  }
  function create({mount,api,defaultDb,onOpen=()=>{},onClose=()=>{}}) {
    let cy=null, graph={nodes:[],edges:[]}, dbId='', view='schema', epoch=0, detailEpoch=0;
    let focus=null, nodeKinds=new Set(Object.keys(colors)), edgeKinds=new Set(relations), dragged=new Set();
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
    for(const [value,label] of [['kpi','KPI Lineage'],['schema','Database Schema'],['combined','Combined Graph']]) {
      const tab=button(label,()=>switchView(value)); tab.dataset.view=value; tabs.append(tab);
    }
    controls.append(sourceLabel,tabs,searchLabel,button('Search',()=>load('search')));
    const status=el('p','semantic-status'); status.setAttribute('role','status'); status.setAttribute('aria-live','polite');
    const body=el('div','semantic-body'), canvas=el('div','semantic-canvas');
    canvas.setAttribute('role','img'); canvas.setAttribute('aria-label','Interactive metadata graph. Use the node list for keyboard navigation.');
    const panel=el('aside','semantic-detail'); panel.setAttribute('aria-label','Node inspector');
    const toggleInspector=button('Node inspector',()=>{panel.hidden=!panel.hidden;toggleInspector.setAttribute('aria-expanded',String(!panel.hidden));});
    toggleInspector.className='semantic-inspector-toggle';toggleInspector.setAttribute('aria-expanded','true');controls.append(toggleInspector);
    const list=el('details','semantic-node-list'); list.append(el('summary','','Accessible node list'));
    const listBody=el('div'); list.append(listBody);
    body.append(canvas,panel);
    const legend=el('div','semantic-legend');
    Object.entries(colors).forEach(([kind,color])=>{
      const item=el('span'), dot=el('i'); dot.style.backgroundColor=color;
      item.append(dot,el('span','',kind.toLowerCase().replaceAll('_',' '))); legend.append(item);
    });
    legend.append(el('span','','Solid: authoritative · dotted: catalog definition · dashed: suggested · red: forbidden'));
    mount.replaceChildren(heading,controls,status,body,list,legend);
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
      const positions=cy?Object.fromEntries(cy.nodes().filter(n=>dragged.has(n.id())).map(n=>[n.id(),n.position()])):
        (memories.get(key())?.positions||{});
      graph=merge?mergeGraph(graph,next):next;
      if(!cy) {
        cy=factory({container:canvas,elements:[],style:styles(),minZoom:.12,maxZoom:3,
          selectionType:'additive'});
        cy.on('tap','node',event=>selectNode(event.target.id()));
        cy.on('tap','edge',event=>inspectEdge(event.target.id()));
        cy.on('mouseover','edge',event=>event.target.addClass('hovered'));
        cy.on('mouseout','edge',event=>event.target.removeClass('hovered'));
        cy.on('dragfree','node',event=>dragged.add(event.target.id()));
      }
      cy.batch(()=>{cy.elements().remove(); cy.add(elements(graph));});
      const layers=layeredPositions(graph);
      cy.layout(view==='schema'?{name:'breadthfirst',directed:false,padding:40,spacingFactor:1.4,animate:false,fit:true}:
        {name:'preset',positions:node=>layers[node.id()],padding:45,animate:false,fit:true}).run();
      cy.nodes().forEach(node=>{if(positions[node.id()]) node.position(positions[node.id()]);});
      if(merge) cy.fit(undefined,36);
      else if(memories.get(key())?.zoom) {cy.zoom(memories.get(key()).zoom);cy.pan(memories.get(key()).pan);}
      else cy.zoom(Math.min(cy.zoom(),1.25));
      listBody.replaceChildren(...graph.nodes.map(node=>button(`${node.label} · ${node.kind.toLowerCase()}`,()=>selectNode(node.id))));
      status.textContent=graph.nodes.length?`${graph.nodes.length} nodes · ${graph.edges.length} links${graph.truncated?' · expandable':''}. ${(graph.warnings||[]).join(' ')}`:
        `No ${view==='kpi'?'KPI definitions':'matching metadata'} in this view. ${(graph.warnings||[]).join(' ')}`;
    }
    async function load(mode='root',nodeId=null) {
      const token=++epoch; ++detailEpoch;
      status.textContent='Loading metadata…';
      if(mode!=='expand')panel.replaceChildren(el('p','semantic-inspector-label','NODE INSPECTOR'),el('p','','Select a node or relationship to inspect.'));
      try {
        if(!nodeKinds.size) {await draw({nodes:[],edges:[],truncated:false});return;}
        const lineage=mode==='upstream'||mode==='downstream';
        const query=params(lineage?{node_id:nodeId,direction:mode,depth:'6'}:
          mode==='expand'?{node_id:nodeId,depth:'1'}:mode==='search'?{search:search.value,depth:'0'}:{depth:'0'});
        const route=lineage?'lineage':mode==='expand'?'neighborhood':'';
        const next=await api(`/api/semantic-graph${route?'/'+route:''}?${query}`);
        if(token!==epoch || mount.hidden) return;
        if(!edgeKinds.size)next.edges=[];
        if(mode==='expand') expansions.set(nodeId,new Set([...(expansions.get(nodeId)||[]),...next.nodes.filter(n=>!graph.nodes.some(old=>old.id===n.id)).map(n=>n.id)]));
        else {expansions.clear(); initial=new Set(next.nodes.map(n=>n.id));}
        await draw(next,{merge:mode==='expand'});
        if(mode==='search')cy?.nodes().addClass('matched');
        if(lineage) {
          cy?.edges().addClass('lineage');
          const authority=lineageAuthority(next);
          status.textContent=authority+'. '+(mode==='downstream'?'Potential impact, not proof of breakage. ':'')+status.textContent;
          if(nodeId)await inspect(nodeId);
        } else if(view==='kpi' && mode==='root') {
          const selected=graph.nodes.find(node=>node.id===focus&&node.kind==='METRIC')||graph.nodes.find(node=>node.kind==='METRIC');
          if(selected)await selectNode(selected.id);
        }
      } catch(error) {if(token===epoch){status.textContent=error.message||'Metadata unavailable.';}}
    }
    async function selectNode(id) {
      const shouldExpand=graph.nodes.some(item=>item.id===id&&(item.kind==='METRIC'||view==='schema'&&item.kind==='TABLE'))&&!expansions.has(id);
      const sourceDb=dbId,expected=epoch+(shouldExpand?1:0);
      const node=graph.nodes.find(item=>item.id===id);
      if(node&&shouldExpand)await load('expand',id);
      if(mount.hidden||sourceDb!==dbId||epoch!==expected||!graph.nodes.some(item=>item.id===id))return;
      await inspect(id);
    }
    async function inspect(id) {
      panel.hidden=false;toggleInspector.setAttribute('aria-expanded','true');
      focus=id; const token=++detailEpoch, graphToken=epoch;
      panel.replaceChildren(el('p','','Loading node details…'));
      try {
        const data=await api(`/api/semantic-graph/nodes/${encodeURIComponent(id)}?${new URLSearchParams({db_id:dbId})}`);
        if(token!==detailEpoch || graphToken!==epoch || mount.hidden) return;
        cy?.nodes().unselect();cy?.getElementById(id).select();
        cy?.elements().removeClass('dimmed');
        const selected=cy?.getElementById(id);
        if(selected?.length){cy.elements().addClass('dimmed');selected.closedNeighborhood().removeClass('dimmed');}
        panel.replaceChildren(el('p','semantic-inspector-label','NODE INSPECTOR'),el('p','semantic-node-badge',`${data.node.status==='approved'?'APPROVED':data.node.status==='suggested'?'NOT REVIEWED':'OBSERVED'} ${data.node.kind}`),el('h2','',data.node.label),el('p','semantic-provenance',String(data.node.provenance||'Metadata').replaceAll('candidate','unreviewed')));
        const actions=el('div','semantic-detail-actions');
        actions.append(button('Expand neighbors',()=>load('expand',id)),button('Collapse expansion',()=>collapse(id)),
          button('Upstream lineage',()=>load('upstream',id)),button('Potential downstream impact',()=>load('downstream',id)));
        panel.append(actions);
        const metadata=data.node.metadata;
        fields(Object.fromEntries(['description','formula','source_relation','required_fields','unit','aggregation']
          .filter(name=>metadata[name]!=null).map(name=>[name,metadata[name]])),panel);
        const extra=el('details','semantic-extra');extra.append(el('summary','','Dependencies, filters & metadata'));
        fields(Object.fromEntries(Object.entries(metadata).filter(([name])=>!['description','formula','source_relation','required_fields','unit','aggregation'].includes(name))),extra);panel.append(extra);
        if(data.columns) {
          panel.append(el('h3','',`Columns (${data.column_count})`));
          data.columns.forEach(column=>panel.append(button(`${column.label} · ${column.metadata.type}${column.metadata.primary_key?' · PK':''}${column.metadata.foreign_key?' · FK':''}`,()=>inspect(column.id))));
          if(data.columns_truncated)panel.append(el('p','','Column list bounded to 100. Search for another column.'));
        }
        if(data.associated_kpis?.length) {
          panel.append(el('h3','','Associated KPI definitions'));
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
    async function switchView(value,nodeId=null) {
      const previousFocus=nodeId||focus;
      remember(); cy?.destroy(); cy=null; view=value; focus=previousFocus; search.value='';restore();
      selectedTab=value; tabs.querySelectorAll('button').forEach(tab=>tab.setAttribute('aria-pressed',String(tab.dataset.view===value)));
      await load(nodeId?(value==='schema'?'expand':'upstream'):'root',nodeId);
      if(previousFocus && graph.nodes.some(node=>node.id===previousFocus))await inspect(previousFocus);
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
        let destination=options.view||selectedTab;
        if(!destination){const overview=await api(`/api/semantic-graph?${new URLSearchParams({db_id:dbId,view:'kpi',depth:'0'})}`);if(token!==epoch)return;destination=overview.nodes.some(node=>node.kind==='METRIC')?'kpi':'schema';}
        await switchView(destination,options.nodeId||null);
      }catch(error){status.textContent=error.message;}
    }
    function close() {remember();++epoch;++detailEpoch;cy?.destroy();cy=null;mount.hidden=true;document.body.classList.remove('semantic-explorer-open');onClose();}
    const observer=new MutationObserver(()=>{if(cy)cy.style(styles());});
    observer.observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
    return {open,close,dispose(){close();observer.disconnect();mount.replaceChildren();}};
  }
  return {create,elements,mergeGraph,colors,layeredPositions,lineageAuthority,connectionLabel,cardBackground};
})();

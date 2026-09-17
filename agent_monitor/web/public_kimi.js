(()=>{
  'use strict';

  const $=id=>document.getElementById(id);
  const configuredBase=window.__PK_BASE__??document.querySelector('meta[name="pk-base"]')?.content??'';
  const APP_BASE=String(configuredBase).replace(/\/+$/,'');
  const PUBLIC_ENGINE='plain';
  const TERMINAL_STATES=new Set(['done','finished','completed','failed','stopped','cancelled','canceled']);
  const STORAGE_KEY='proving-console-public:last-run';
  const state={
    config:null,
    plainAvailable:false,
    selectedHarness:PUBLIC_ENGINE,
    runId:'',
    run:null,
    pollGeneration:0,
    pollTimer:null,
    busy:false,
    activeTab:'proof'
  };

  function endpoint(path){
    const value=String(path||'');
    const suffix=value.startsWith('/')?value:'/'+value;
    return APP_BASE+suffix;
  }

  function normalizedStatus(value){
    const status=String(value||'queued').trim().toLowerCase();
    if(['complete','success','succeeded','done'].includes(status))return'finished';
    if(status==='canceled')return'cancelled';
    return status||'queued';
  }

  function isTerminal(value){
    return TERMINAL_STATES.has(String(value||'').trim().toLowerCase())||
      TERMINAL_STATES.has(normalizedStatus(value));
  }

  function safeTime(value){
    if(!value)return'';
    const parsed=new Date(value);
    if(Number.isNaN(parsed.getTime()))return'';
    return parsed.toLocaleString([],{dateStyle:'medium',timeStyle:'short'});
  }

  function requestError(payload,fallback){
    if(payload&&typeof payload==='object'){
      const message=payload.error||payload.message||payload.detail;
      if(typeof message==='string'&&message.trim())return message.trim();
    }
    return fallback;
  }

  async function requestJson(path,options={}){
    const response=await fetch(endpoint(path),{
      credentials:'same-origin',
      cache:'no-store',
      ...options,
      headers:{
        ...(options.body?{'Content-Type':'application/json'}:{}),
        ...(options.headers||{})
      }
    });
    const raw=await response.text();
    let payload={};
    if(raw){
      try{payload=JSON.parse(raw)}
      catch(_){payload={error:raw.slice(0,500)}}
    }
    if(!response.ok){
      const error=new Error(requestError(payload,`Request failed (HTTP ${response.status})`));
      error.status=response.status;
      throw error;
    }
    return payload;
  }

  function setValidation(message){
    $('validation').textContent=String(message||'');
  }

  function setBusy(value){
    state.busy=Boolean(value);
    $('run-button').classList.toggle('busy',state.busy);
    $('run-button-label').textContent=state.busy?'Starting…':'Prove';
    syncControls();
  }

  function problemLimit(){
    const limits=state.config&&state.config.limits;
    const raw=limits&&(limits.problem_characters??limits.max_problem_chars??limits.problem_max_chars);
    const limit=Number(raw);
    return Number.isFinite(limit)&&limit>0?Math.floor(limit):12000;
  }

  function syncCounter(){
    const size=$('problem').value.length;
    const limit=problemLimit();
    $('problem').maxLength=limit;
    $('counter').textContent=`${size.toLocaleString()} / ${limit.toLocaleString()}`;
    $('counter').classList.toggle('over',size>limit);
    syncControls();
  }

  function syncControls(){
    const text=$('problem').value.trim();
    const valid=text.length>=8&&text.length<=problemLimit();
    $('run-button').disabled=state.busy||!state.plainAvailable||!valid;
    const status=normalizedStatus(state.run&&state.run.status);
    $('stop-run').disabled=!state.runId||isTerminal(status)||!['queued','running','starting'].includes(status);
    $('new-run').disabled=!state.runId||!isTerminal(status);
  }

  function configurePlain(items){
    const list=Array.isArray(items)?items:[];
    const plain=list.find(item=>item&&String(item.id||'').trim()===PUBLIC_ENGINE);
    state.plainAvailable=Boolean(plain);
    state.selectedHarness=PUBLIC_ENGINE;
    $('engine-lock').textContent=plain?String(plain.label||'Plain'):'Plain unavailable';
    if(!plain)setValidation('The Plain public prover is unavailable right now. Please try again later.');
    syncControls();
  }

  function humanLimitLabel(key){
    return({
      problem_characters:'characters per problem',
      request_bytes:'request bytes',
      max_iterations:'iterations per run',
      active_runs:'active run per visitor',
      global_active_runs:'concurrent public runs',
      requests_per_hour:'starts per hour per visitor',
      global_requests_per_hour:'global starts per hour',
      artifact_ttl_hours:'hour artifact retention',
      max_output_tokens:'output tokens per model call'
    })[key]||String(key).replaceAll('_',' ');
  }

  function renderLimits(limits){
    const list=$('limit-list');
    list.replaceChildren();
    const entries=Object.entries(limits&&typeof limits==='object'?limits:{})
      .filter(([,value])=>['string','number'].includes(typeof value)&&String(value).trim());
    if(!entries.length){
      const item=document.createElement('span');
      item.textContent='fair-use limits apply';
      list.append(item);
      return;
    }
    for(const[key,value]of entries){
      const item=document.createElement('span');
      item.textContent=`${value} ${humanLimitLabel(key)}`;
      list.append(item);
    }
  }

  function eventView(event,index){
    if(typeof event==='string')return{title:`Update ${index+1}`,message:event,time:''};
    const item=event&&typeof event==='object'?event:{};
    const title=item.title||item.stage||item.type||item.kind||item.status||`Update ${index+1}`;
    const message=item.message||item.text||item.output||item.detail||item.summary||'';
    const time=item.timestamp||item.created_at||item.time||'';
    return{title:String(title),message:String(message),time:safeTime(time)};
  }

  function renderEvents(events){
    const values=Array.isArray(events)?events:[];
    const list=$('event-list');
    list.replaceChildren();
    $('event-count').textContent=values.length?`(${values.length})`:'';
    if(!values.length){
      const empty=document.createElement('p');
      empty.className='events-empty';
      empty.textContent='No activity has been reported yet. This view refreshes while Plain runs.';
      list.append(empty);
      return;
    }
    values.forEach((value,index)=>{
      const view=eventView(value,index);
      const row=document.createElement('div');
      row.className='event';
      const dot=document.createElement('span');
      dot.className='event-dot';
      dot.setAttribute('aria-hidden','true');
      const title=document.createElement('div');
      title.className='event-title';
      title.textContent=view.title;
      row.append(dot,title);
      if(view.message){
        const message=document.createElement('div');
        message.className='event-message';
        message.textContent=view.message;
        row.append(message);
      }
      if(view.time){
        const time=document.createElement('div');
        time.className='event-time';
        time.textContent=view.time;
        row.append(time);
      }
      list.append(row);
    });
  }

  function detailRows(run){
    const configuredModel=state.config&&state.config.model;
    return[
      ['Run ID',run.run_id||state.runId||'—'],
      ['Status',normalizedStatus(run.status)],
      ['Model',run.model||(configuredModel&&(configuredModel.label||configuredModel.id))||'Kimi K3'],
      ['Harness','Plain'],
      ['Started',safeTime(run.created_at)||'—'],
      ['Last update',safeTime(run.updated_at)||'—']
    ];
  }

  function renderDetails(run){
    const root=$('details');
    root.replaceChildren();
    for(const[labelText,valueText]of detailRows(run)){
      const row=document.createElement('div');
      row.className='detail-row';
      const label=document.createElement('div');
      label.className='detail-label';
      label.textContent=labelText;
      const value=document.createElement('div');
      value.className='detail-value';
      value.textContent=String(valueText);
      row.append(label,value);
      root.append(row);
    }
  }

  function showTab(name){
    const selected=['proof','activity','details'].includes(name)?name:'proof';
    state.activeTab=selected;
    for(const tab of document.querySelectorAll('.tab[data-tab]')){
      const active=tab.dataset.tab===selected;
      tab.setAttribute('aria-selected',active?'true':'false');
      tab.tabIndex=active?0:-1;
    }
    for(const panel of['proof','activity','details']){
      $(`panel-${panel}`).hidden=panel!==selected;
    }
  }

  function showRunSurface(){
    $('empty-state').hidden=true;
    showTab(state.activeTab);
  }

  function renderRun(run){
    if(!run||typeof run!=='object')return;
    state.run=run;
    state.runId=String(run.run_id||state.runId||'');
    const status=normalizedStatus(run.status);
    $('run-name').textContent=state.runId||'Current run';
    $('status').dataset.state=status;
    $('status').textContent=status;
    const updated=safeTime(run.updated_at);
    $('updated').textContent=updated?`Updated ${updated}`:'Waiting for an update';
    const error=typeof run.error==='string'?run.error.trim():'';
    $('error-box').hidden=!error;
    $('error-box').textContent=error;
    const proof=typeof run.proof==='string'?run.proof.trim():'';
    $('proof').textContent=proof||(isTerminal(status)
      ?'This run ended without a proof artifact.'
      :'Waiting for Plain to produce a proof…');
    $('proof').classList.toggle('placeholder',!proof);
    renderEvents(run.events);
    renderDetails(run);
    showRunSurface();
    syncControls();
  }

  function cancelPolling(){
    state.pollGeneration+=1;
    if(state.pollTimer)clearTimeout(state.pollTimer);
    state.pollTimer=null;
  }

  function schedulePoll(runId,generation,delay=1600){
    if(generation!==state.pollGeneration||!runId)return;
    state.pollTimer=setTimeout(()=>pollRun(runId,generation),delay);
  }

  function forgetStoredRun(){
    try{sessionStorage.removeItem(STORAGE_KEY)}catch(_){}
  }

  async function pollRun(runId,generation=state.pollGeneration){
    if(!runId||generation!==state.pollGeneration)return;
    try{
      const run=await requestJson(`/api/runs/${encodeURIComponent(runId)}`);
      if(generation!==state.pollGeneration||runId!==state.runId)return;
      renderRun(run);
      if(!isTerminal(run.status))schedulePoll(runId,generation);
    }catch(error){
      if(generation!==state.pollGeneration)return;
      if(error.status===404){
        forgetStoredRun();
        renderRun({
          run_id:runId,
          status:'failed',
          error:'This run is no longer available. Public artifacts expire automatically.',
          events:[]
        });
        cancelPolling();
        return;
      }
      $('updated').textContent=`Update paused: ${String(error.message||error)}`;
      schedulePoll(runId,generation,3500);
    }
  }

  async function startRun(event){
    event.preventDefault();
    setValidation('');
    const problem=$('problem').value.trim();
    if(!state.plainAvailable){
      setValidation('The Plain public prover is unavailable right now.');
      return;
    }
    if(problem.length<8){
      setValidation('Enter a complete math problem (at least 8 characters).');
      $('problem').focus();
      return;
    }
    if(problem.length>problemLimit()){
      setValidation(`The problem is longer than the ${problemLimit().toLocaleString()} character public limit.`);
      return;
    }
    cancelPolling();
    setBusy(true);
    try{
      const run=await requestJson('/api/runs',{
        method:'POST',
        body:JSON.stringify({engine:PUBLIC_ENGINE,problem})
      });
      if(!run||typeof run.run_id!=='string'||!run.run_id.trim()){
        throw new Error('The server did not return a run ID.');
      }
      state.runId=run.run_id.trim();
      try{sessionStorage.setItem(STORAGE_KEY,state.runId)}catch(_){}
      renderRun({...run,events:Array.isArray(run.events)?run.events:[]});
      state.pollGeneration+=1;
      const generation=state.pollGeneration;
      schedulePoll(state.runId,generation,250);
    }catch(error){
      setValidation(String(error.message||error));
    }finally{
      setBusy(false);
    }
  }

  async function stopRun(){
    const runId=state.runId;
    if(!runId||isTerminal(state.run&&state.run.status))return;
    cancelPolling();
    const generation=state.pollGeneration;
    $('stop-run').disabled=true;
    try{
      const run=await requestJson(`/api/runs/${encodeURIComponent(runId)}/stop`,{
        method:'POST',
        body:'{}'
      });
      renderRun({...(state.run||{}),...run,run_id:run.run_id||runId});
      if(!isTerminal(run.status))schedulePoll(runId,generation,500);
    }catch(error){
      $('updated').textContent=`Could not stop run: ${String(error.message||error)}`;
      syncControls();
    }
  }

  function resetComposer(){
    cancelPolling();
    state.runId='';
    state.run=null;
    forgetStoredRun();
    $('problem').value='';
    $('run-name').textContent='No run started';
    $('status').dataset.state='idle';
    $('status').textContent='Ready';
    $('updated').textContent='Not started';
    $('empty-state').hidden=false;
    for(const panel of['proof','activity','details'])$(`panel-${panel}`).hidden=true;
    $('error-box').hidden=true;
    $('error-box').textContent='';
    $('proof').textContent='Waiting for Plain to produce a proof…';
    $('proof').classList.add('placeholder');
    renderEvents([]);
    renderDetails({});
    setValidation('');
    syncCounter();
    $('problem').focus();
  }

  async function loadConfig(){
    try{
      const config=await requestJson('/api/config');
      state.config=config&&typeof config==='object'?config:{};
      const model=state.config.model&&(state.config.model.label||state.config.model.id);
      $('model-lock').textContent=String(model||'Kimi K3');
      configurePlain(state.config.harnesses);
      renderLimits(state.config.limits);
      syncCounter();
      let previous='';
      try{previous=sessionStorage.getItem(STORAGE_KEY)||''}catch(_){}
      if(previous){
        state.runId=previous;
        state.pollGeneration+=1;
        await pollRun(previous,state.pollGeneration);
      }
    }catch(error){
      state.plainAvailable=false;
      renderLimits(null);
      setValidation(`The public Kimi service is unavailable: ${String(error.message||error)}`);
      syncControls();
    }
  }

  $('run-form').addEventListener('submit',startRun);
  $('problem').addEventListener('input',syncCounter);
  $('stop-run').addEventListener('click',stopRun);
  $('new-run').addEventListener('click',resetComposer);

  for(const tab of document.querySelectorAll('.tab[data-tab]')){
    tab.addEventListener('click',()=>showTab(tab.dataset.tab));
    tab.addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight'].includes(event.key))return;
      event.preventDefault();
      const tabs=[...document.querySelectorAll('.tab[data-tab]')];
      const index=tabs.indexOf(tab);
      const next=(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
      showTab(tabs[next].dataset.tab);
      tabs[next].focus();
    });
  }

  document.addEventListener('visibilitychange',()=>{
    if(!document.hidden&&state.runId&&!isTerminal(state.run&&state.run.status)){
      cancelPolling();
      state.pollGeneration+=1;
      pollRun(state.runId,state.pollGeneration);
    }
  });

  window.PublicKimiApp=Object.freeze({
    endpoint,
    isTerminal,
    normalizedStatus,
    eventView,
    humanLimitLabel
  });

  loadConfig();
})();

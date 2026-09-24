(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let current = null;
  let candidate = null;
  const status = (message, error=false) => { $('settingsStatus').textContent=message; $('settingsStatus').dataset.error=String(error); };
  async function operation(name, payload={}) {
    const response = await fetch(`/api/admin/operations/${name}`, {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}, body:JSON.stringify(payload)});
    const data=await response.json();
    if(!response.ok || data.error) throw new Error(data.error || 'This operation could not finish.');
    return data;
  }
  function memberRow(member={login:'',name:'',role:'household'}) {
    const row=document.createElement('div');row.className='settings-member';
    for(const [field,title] of [['login','Tailscale login'],['name','Display name']]) {
      const label=document.createElement('label');label.textContent=title;const input=document.createElement('input');input.dataset.memberField=field;input.value=member[field];input.required=true;input.maxLength=field==='login'?320:100;label.append(input);row.append(label);
    }
    const label=document.createElement('label');label.textContent='Role';const select=document.createElement('select');select.dataset.memberField='role';
    for(const [value,text] of [['household','Member'],['admin','Administrator']]) {const option=document.createElement('option');option.value=value;option.textContent=text;select.append(option);}
    select.value=member.role;label.append(select);row.append(label);
    const remove=document.createElement('button');remove.type='button';remove.className='secondary-button';remove.textContent='Remove';remove.addEventListener('click',()=>row.remove());row.append(remove);$('members').append(row);
  }
  function jobs(items=[]) {
    $('jobs').replaceChildren();
    if(!items.length){$('jobs').textContent='No recent server operations.';return;}
    items.forEach(job=>{const row=document.createElement('div');row.className='settings-job';row.textContent=`${job.operation || job.kind || 'Server operation'} · ${job.state || job.status || 'pending'}${job.error || job.phase?' · '+(job.error || job.phase):''}`;$('jobs').append(row);});
  }
  async function load() {
    try {
      const data=await operation('status');current=data.configuration;
      if(!current) throw new Error('Finish the installation wizard before managing this server.');
      $('displayName').value=current.display_name;$('timezone').value=current.timezone;$('country').value=current.country;
      $('serverAddress').textContent=`Private address: ${current.public_url}`;$('dataLocation').textContent=`Application data: ${current.storage.data_root}`;
      $('backupRoot').value=current.storage.backup_root || '';$('backupState').textContent=current.storage.backup_root?'Independent backup configured. Run a backup and restoration test to verify recovery.':'Backups are not configured. A lost data disk could lose your saved content.';
      $('updateRecoveryRoot').value=current.storage.update_snapshot_root || '';
      const recovery=data.update_recovery;
      $('updateRecoveryState').textContent=recovery?`Update recovery: ${recovery.path}. ${recovery.error || (Number.isFinite(recovery.free_bytes)?`${(recovery.free_bytes/1024**3).toFixed(1)} GiB free. `:'')}${recovery.snapshots?.filter(item=>item.complete).length || 0} complete snapshot(s), ${recovery.snapshots?.filter(item=>!item.complete).length || 0} incomplete attempt(s).`:'Update recovery uses a separate folder alongside the library.';
      $('members').replaceChildren();current.members.forEach(memberRow);
      document.querySelectorAll('[data-module]').forEach(select=>select.value=current.modules[select.dataset.module]);
      $('webPush').checked=Boolean(current.integrations.web_push);$('releaseState').textContent=`Installed version: ${data.release?.version || 'local development'}`;
      jobs(data.jobs);$('settingsForm').hidden=false;status('Settings loaded.');
    } catch(error) {status(error.message,true);}
  }
  $('addMember').addEventListener('click',()=>memberRow());
  $('settingsForm').addEventListener('submit', async event=>{
    event.preventDefault();$('saveSettings').disabled=true;
    try {
      const members=[...document.querySelectorAll('.settings-member')].map(row=>Object.fromEntries([...row.querySelectorAll('[data-member-field]')].map(input=>[input.dataset.memberField,input.value.trim()])));
      const modules=Object.fromEntries([...document.querySelectorAll('select[data-module]')].map(select=>[select.dataset.module,select.value]));
      if(modules.device_backup!=='disabled' && modules.media==='disabled') throw new Error('Phone backup requires Media. Enable Media or turn off Phone backup.');
      const secrets={};if($('tmdbToken').value.trim())secrets.TMDB_API_READ_TOKEN=$('tmdbToken').value.trim();if($('mealdbKey').value.trim())secrets.THEMEALDB_API_KEY=$('mealdbKey').value.trim();
      const result=await operation('settings',{configuration:{display_name:$('displayName').value.trim(),timezone:$('timezone').value.trim(),country:$('country').value.trim().toUpperCase(),members,modules,integrations:{web_push:$('webPush').checked},storage:{backup_root:$('backupRoot').value.trim()||null,update_snapshot_root:$('updateRecoveryRoot').value.trim()||null}},secrets});
      $('tmdbToken').value='';$('mealdbKey').value='';status('Settings accepted. The server may reconnect while selected apps restart.');watch(result);
    }catch(error){status(error.message,true);}finally{$('saveSettings').disabled=false;}
  });
  document.querySelectorAll('[data-test-provider]').forEach(button=>button.addEventListener('click',async()=>{
    const name=button.dataset.testProvider;const target=document.querySelector(`[data-provider-result="${name}"]`);button.disabled=true;target.textContent='Checking connection…';
    try {const credential=$(name==='movies'?'tmdbToken':'mealdbKey').value.trim();const result=await operation('test_integration',{name,...(credential?{credential}:{})});target.textContent=result.message || (result.ok===false?'The provider could not be reached.':'Connection verified. Save settings to apply changes.');}
    catch(error){target.textContent=error.message;}finally{button.disabled=false;}
  }));
  async function watch(result) {
    const id=result.job_id || result.id || result.job?.id;if(!id)return;
    for(let count=0;count<120;count++) {
      await new Promise(resolve=>setTimeout(resolve,3000));
      try {const job=await operation('job',{id});jobs([job]);if(['complete','failed','interrupted','needs_attention'].includes(job.state || job.status)){status(job.error || job.message || `Operation ${job.state || job.status}.`,(job.state || job.status)!=='complete');return;}}
      catch(_){status('The server is reconnecting. Reload Settings to check the saved operation status.');return;}
    }
    status('The operation is still running. You can return to Settings later to check it.');
  }
  document.querySelectorAll('[data-operation]').forEach(button=>button.addEventListener('click',async()=>{button.disabled=true;try{const result=await operation(button.dataset.operation);status('Operation started. You can leave this page while the server works.');watch(result);}catch(error){status(error.message,true);}finally{button.disabled=false;}}));
  $('checkUpdates').addEventListener('click',async()=>{candidate=null;$('installUpdate').hidden=true;try{const result=await operation('update_check');candidate=result.update || result.release || result;const available=result.update_available===true;$('updateState').textContent=result.message || (available?`Version ${candidate.available || candidate.version || ''} is available.`:'You are using the current stable release.');$('installUpdate').hidden=!available;}catch(error){$('updateState').textContent=error.message;}});
  $('installUpdate').addEventListener('click',async()=>{if(!candidate)return;$('installUpdate').disabled=true;try{const result=await operation('update',{version:candidate.available || candidate.version});status('Preparing a verified update and recovery snapshot…');watch(result);}catch(error){status(error.message,true);}finally{$('installUpdate').disabled=false;}});
  load();
})();

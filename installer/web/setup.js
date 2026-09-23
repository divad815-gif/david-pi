'use strict';
let state;
const $ = id => document.getElementById(id);
const labels = {media:'Media library',files:'Files',notes:'Notes',movies:'Movie Night',recipes:'Recipes',places:'Places',audiobooks:'Audiobooks',mytube:'MyTube',chat:'Household chat',games:'Games',assistant:'Local assistant',device_backup:'Phone Backup'};
const error = message => {$('error').textContent=message;$('error').hidden=false;};
async function api(path, body){
 const options = body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state?.csrf || ''},body:JSON.stringify(body)};
 const response=await fetch(path,options); const data=await response.json();
 if(!response.ok)throw new Error(data.error || 'This step could not finish.');return data;
}
async function load(){state=await api('/api/setup');$('claim').hidden=true;$('wizard').hidden=false;$('timezone').value=state.timezone;$('address').textContent=`Private address: ${state.origin} · Administrator: ${state.admin}`;
 const container=$('modules');container.replaceChildren();Object.entries(labels).forEach(([key,label])=>{const item=document.createElement('label');item.className='check';const input=document.createElement('input');input.type='checkbox';input.id=`module-${key}`;input.checked=true;item.append(input,document.createTextNode(label));container.append(item);});
 $('module-chat').addEventListener('change',()=>{if(!$('module-chat').checked)$('web-push').checked=false;});$('web-push').addEventListener('change',()=>{if($('web-push').checked)$('module-chat').checked=true;});
 $('module-media').addEventListener('change',()=>{if(!$('module-media').checked)$('module-device_backup').checked=false;});$('module-device_backup').addEventListener('change',()=>{if($('module-device_backup').checked)$('module-media').checked=true;});
}
$('claim-form').addEventListener('submit',async event=>{event.preventDefault();try{await api('/api/claim',{token:$('token').value.trim()});$('token').value='';await load();$('error').hidden=true;}catch(e){error(e.message);}});
document.querySelectorAll('[data-skip]').forEach(button=>button.addEventListener('click',()=>{$(button.dataset.skip).value='';button.textContent='Skipped — configure later in settings';}));
document.querySelectorAll('[data-test]').forEach(button=>button.addEventListener('click',async()=>{button.disabled=true;try{await api('/api/test-integration',{name:button.dataset.test,credential:$(button.dataset.test==='movies'?'tmdb':'mealdb').value.trim()});button.textContent='Connection verified';}catch(e){error(e.message);}finally{button.disabled=false;}}));
$('wizard').addEventListener('submit',async event=>{event.preventDefault();$('error').hidden=true;$('install').disabled=true;try{
 const modules={};Object.keys(labels).forEach(key=>{modules[key]=$(`module-${key}`).checked?'enabled':'disabled';});['movies','recipes'].forEach(key=>{if(modules[key]!=='disabled')modules[key]=$(key==='movies'?'tmdb':'mealdb').value.trim()?'connected':'manual';});modules.pihole='disabled';
 const parent=$('storage-parent').value.trim().replace(/\/$/,'');const backup=$('backup-parent').value.trim().replace(/\/$/,'');
 const configuration={schema_version:1,instance_id:state.instance_id,display_name:$('display-name').value.trim(),hostname:state.hostname,public_url:state.origin,timezone:$('timezone').value.trim(),country:$('country').value.toUpperCase(),members:[{login:state.admin,name:$('owner-name').value.trim(),role:'admin'}],storage:{mode:$('storage-mode').value,data_root:`${parent}/david-pi-data`,backup_root:backup?`${backup}/david-pi-backups`:null},modules,integrations:{web_push:$('web-push').checked}};
 const result=await api('/api/install',{configuration,secrets:{TMDB_API_READ_TOKEN:$('tmdb').value.trim(),THEMEALDB_API_KEY:$('mealdb').value.trim()}});$('wizard').hidden=true;$('progress').hidden=false;poll(result.job_id);
 }catch(e){error(e.message);$('install').disabled=false;}});
async function poll(id,failures=0){try{const job=await api(`/api/job?id=${encodeURIComponent(id)}`);failures=0;$('phase').textContent=job.phase;if(job.state==='complete'){$('phase').textContent='Your server is ready.';$('open').href=job.result.public_url;$('open').textContent='Open your home server';$('open').hidden=false;return;}if(['failed','interrupted'].includes(job.state)){error(job.error);$('phase').textContent='Setup needs attention. Your existing data was preserved.';return;}}catch(e){failures++;$('phase').textContent='Reconnecting as the portal starts…';try{const response=await fetch('/api/installation');if(response.ok){$('open').href=state.origin;$('open').textContent='Open your home server';$('open').hidden=false;return;}}catch{}}
 if(failures>=30){$('phase').textContent='Setup is no longer reachable in this session. Reopen your private address, or run sudo david-pi status on the server to see the saved operation.';$('open').href=state.origin;$('open').textContent='Open your private address';$('open').hidden=false;return;}
 setTimeout(()=>poll(id,failures),2000);
}
load().catch(()=>{});

(() => {
  'use strict';
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const videoId = document.querySelector('meta[name="mytube-video-id"]')?.content || '';
  const $ = selector => document.querySelector(selector);
  let view = 'library', offset = 0, loading = false, activeVideo = null, hls = null, progressTimer = null;
  let listGeneration = 0, listController = null, collection = null, seen = new Set(), uploadController = null;
  let progressSaving = false, progressPending = null, playerDeadline = null;
  let watchGeneration=0, watchController=null, choiceGeneration=0, choiceController=null;
  const initialCollection = new URL(location.href).searchParams.get('collection');
  if (initialCollection && /^[0-9a-f]{32}$/.test(initialCollection)) collection = {id: initialCollection};

  async function api(url, options = {}) {
    options.headers = {...options.headers, 'X-CSRF-Token': csrf};
    const controller=new AbortController(), abort=()=>controller.abort();
    if(options.signal?.aborted) abort(); else options.signal?.addEventListener('abort',abort,{once:true});
    const deadline=setTimeout(abort,options.timeout||30000);
    try {
      const response=await fetch(url,{...options,signal:controller.signal}), data=await response.json().catch(()=>({}));
      if(!response.ok){const error=new Error(data.error||'Something went wrong.');error.status=response.status;error.data=data;throw error;}
      return data;
    } finally {clearTimeout(deadline);options.signal?.removeEventListener('abort',abort);}
  }
  function time(seconds) { const total=Math.max(0,Math.floor(Number(seconds)||0)),h=Math.floor(total/3600),m=Math.floor(total%3600/60),s=total%60;return h?`${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`:`${m}:${String(s).padStart(2,'0')}`; }
  function size(bytes) { const units=['B','KB','MB','GB','TB'];let value=Number(bytes)||0,index=0;while(value>=1024&&index<units.length-1){value/=1024;index++;}return `${index&&value<10?value.toFixed(1):Math.round(value)} ${units[index]}`; }
  function notice(text) { const toast=$('#platformToast');if(!toast)return;toast.textContent=text;toast.hidden=false;clearTimeout(notice.timer);notice.timer=setTimeout(()=>toast.hidden=true,3200); }
  function card(video, compact=false) {
    const article=document.createElement('article');article.className='mytube-card';
    const link=document.createElement('a');link.href=video.watch_url + (collection ? `?collection=${encodeURIComponent(collection.id)}` : '');link.setAttribute('aria-label',`Watch ${video.title}`);
    const thumb=document.createElement('div');thumb.className='mytube-thumb';
    if(video.poster_url){const image=new Image();image.src=video.poster_url;image.alt='';image.loading='lazy';image.decoding='async';thumb.append(image);}
    else{const mark=document.createElement('span');mark.className='mytube-thumb-placeholder';mark.textContent='▶';thumb.append(mark);}
    const duration=document.createElement('span');duration.className='mytube-duration';duration.textContent=time(video.duration_seconds);thumb.append(duration);
    if(video.playback_state!=='ready'&&video.playback_state!=='direct'){const state=document.createElement('span');state.className='mytube-preparing';state.textContent=video.playback_state==='failed'?'Direct play':'Preparing';thumb.append(state);}
    if(video.position_seconds>0&&video.duration_seconds>0){const progress=document.createElement('span');progress.className='mytube-progress';const fill=document.createElement('span');fill.style.width=`${Math.min(100,video.position_seconds/video.duration_seconds*100)}%`;progress.append(fill);thumb.append(progress);}
    const copy=document.createElement('div');copy.className='mytube-card-copy';const title=document.createElement('h3');title.textContent=video.title;const meta=document.createElement('p');meta.textContent=`${video.visibility==='private'?'Only me':'Shared'} · ${size(video.byte_size)}`;copy.append(title,meta);link.append(thumb,copy);article.append(link);
    if(!compact){const tools=document.createElement('div');tools.className='mytube-card-tools';if(view!=='trash'){const collect=document.createElement('button');collect.type='button';collect.textContent='Add to collection';collect.addEventListener('click',()=>chooseCollection(video));tools.append(collect);}if(video.is_mine){const action=document.createElement('button');action.type='button';action.textContent=view==='trash'?'Restore':video.media_id?'Remove from MyTube':'Move to trash';action.addEventListener('click',()=>mutateVideo(video,view==='trash'));tools.append(action);}if(collection?.is_mine){const remove=document.createElement('button');remove.type='button';remove.textContent='Remove from collection';remove.addEventListener('click',async()=>{remove.disabled=true;try{await api(`/api/mytube/collections/${collection.id}/videos/${video.id}`,{method:'DELETE'});notice('Removed from this collection. The video is unchanged.');await loadVideos(true);}catch(error){notice(error.message);remove.disabled=false;}});tools.append(remove);}article.append(tools);}
    return article;
  }
  async function mutateVideo(video, restore) {
    if (!restore && !await confirmAction(video.media_id ? 'Remove this MyTube link? The Media original will stay unchanged.' : 'Move this video to recoverable trash?')) return;
    try{const linked=Boolean(video.media_id&&!restore);await api(linked?`/api/mytube/media-links/${video.media_id}`:`/api/mytube/videos/${video.id}${restore?'/restore':''}`,{method:restore?'POST':'DELETE'});notice(restore?'Video restored.':linked?'Removed from MyTube. The Media original is unchanged.':'Video moved to trash.');loadVideos(true);}catch(error){notice(error.message);}
  }
  function confirmAction(text) {
    return new Promise(resolve => {
      const dialog = $('#mytubeConfirm'); $('#mytubeConfirmCopy').textContent = text;
      const finish = value => { dialog.close(); resolve(value); };
      $('#mytubeConfirmCancel').onclick = () => finish(false);
      $('#mytubeConfirmAccept').onclick = () => finish(true);
      dialog.oncancel = event => { event.preventDefault(); finish(false); };
      dialog.showModal();
    });
  }
  async function chooseCollection(video) {
    choiceController?.abort();choiceController=new AbortController();const generation=++choiceGeneration;
    const choices=$('#mytubeCollectionChoices'),message=$('#mytubeAddCollectionMessage'),more=$('#mytubeMoreCollectionChoices'),dialog=$('#mytubeAddCollectionSheet');
    let next=0;const choiceIds=new Set();choices.replaceChildren();message.textContent='Loading collections…';more.hidden=true;dialog.showModal();
    const load=async()=>{
      more.disabled=true;
      try{
        const data=await api(`/api/mytube/collections?owner=mine&limit=48&offset=${next}`,{signal:choiceController.signal});
        if(generation!==choiceGeneration)return;
        next=data.next_offset;message.textContent=data.total?'':'Create a collection first, then add this video.';
        for(const entry of data.collections){if(choiceIds.has(entry.id))continue;choiceIds.add(entry.id);
          const button=document.createElement('button'),title=document.createElement('strong'),meta=document.createElement('span');button.type='button';title.textContent=entry.name;meta.textContent=entry.visibility==='private'?'Only me':'Shared';button.append(title,meta);
          button.addEventListener('click',async()=>{button.disabled=true;try{await api(`/api/mytube/collections/${entry.id}/videos/${video.id}`,{method:'PUT'});if(generation!==choiceGeneration)return;dialog.close();notice(`Added to ${entry.name}.`);}catch(error){if(generation===choiceGeneration)message.textContent=error.message;}finally{button.disabled=false;}});choices.append(button);
        }
        more.hidden=!data.has_more;more.textContent='More collections';
      }catch(error){if(generation===choiceGeneration&&error.name!=='AbortError'){message.textContent=error.message;more.hidden=false;more.textContent='Retry loading collections';}}
      finally{if(generation===choiceGeneration)more.disabled=false;}
    };
    more.onclick=load;await load();
  }
  function setState(name) { ['Loading','Grid','Empty','Error'].forEach(key=>{const node=$(`#mytube${key}`);if(node)node.hidden=key.toLowerCase()!==name;}); }
  async function loadVideos(reset=false) {
    if (loading && !reset) return;
    if (reset) { listController?.abort(); offset=0; seen=new Set(); $('#mytubeGrid').replaceChildren(); setState('loading'); }
    const generation=++listGeneration; listController=new AbortController(); loading=true;
    $('#mytubePanel').setAttribute('aria-busy','true'); $('#mytubeMore').disabled=true;
    $('#mytubeCollectionBack').hidden=!collection;
    try {
      if(view==='collections' && !collection) {
        const data=await api(`/api/mytube/collections?offset=${offset}`, {signal:listController.signal});
        if (generation!==listGeneration) return;
        data.collections.forEach(entry=>{
          if (seen.has(entry.id)) return; seen.add(entry.id);
          const item=document.createElement('button'); item.type='button'; item.className='mytube-collection-card';
          const title=document.createElement('h3'); title.textContent=entry.name;
          const meta=document.createElement('p'); meta.textContent=`${entry.video_count} videos · ${entry.visibility==='private'?'Only me':'Shared'}`;
          item.append(title,meta); item.addEventListener('click',()=>openCollection(entry)); $('#mytubeGrid').append(item);
        });
        offset=data.next_offset; $('#mytubeCount').textContent=`${data.total ?? data.collections.length} collections`;
        $('#mytubeMore').hidden=!data.has_more; setState(seen.size?'grid':'empty');
      } else {
        const query=new URLSearchParams({view,limit:'48',offset:String(offset)});
        if(collection) query.set('collection',collection.id);
        const data=await api(`/api/mytube/videos?${query}`,{signal:listController.signal});
        if(generation!==listGeneration) return;
        if(data.collection){collection=data.collection;$('#mytubeHeading').textContent=collection.name;$('#mytubeKicker').textContent='Collection';}
        data.videos.forEach(video=>{if(seen.has(video.id))return;seen.add(video.id);$('#mytubeGrid').append(card(video));});
        offset=data.next_offset; $('#mytubeCount').textContent=`${data.total} ${data.total===1?'video':'videos'}`;
        $('#mytubeMore').hidden=!data.has_more; setState(seen.size?'grid':'empty');
      }
    } catch(error) {if(generation===listGeneration && error.name!=='AbortError') {setState('error');$('#mytubeErrorCopy').textContent=error.message||'Check the connection and retry. Your videos are unchanged.';}}
    finally {if(generation===listGeneration){loading=false;$('#mytubeMore').disabled=false;$('#mytubePanel').setAttribute('aria-busy','false');}}
  }
  function openCollection(entry) {
    collection=entry; view='library'; $('#newMytubeCollection').hidden=true;
    history.replaceState(null,'',`/mytube?collection=${encodeURIComponent(entry.id)}`); loadVideos(true);
  }
  function openUpload() { $('#mytubeUploadSheet')?.showModal(); }
  async function uploadIdentity(file, contentSha256) {
    const scope=document.querySelector('meta[name="audiobook-progress-scope"]')?.content;
    if(!scope)throw new Error('Account verification is unavailable. Reload before uploading.');
    const storageKey=`david-pi:mytube-upload:v3:${scope}:${contentSha256}`;
    let key=localStorage.getItem(storageKey);
    if(!key){key=`mytube-${crypto.randomUUID?.()||`${Date.now()}-${Math.random()}`}`;localStorage.setItem(storageKey,key);}
    return {key,storageKey};
  }
  function hashUpload(file, signal, progress) {
    return new Promise((resolve,reject)=>{
      const worker=new Worker('/static/mytube-upload-worker.js?v=1');
      const finish=(error,value)=>{worker.terminate();signal.removeEventListener('abort',abort);error?reject(error):resolve(value);};
      const abort=()=>finish(new DOMException('Upload paused. Select the same video to resume.','AbortError'));
      if(signal.aborted){abort();return;}
      signal.addEventListener('abort',abort,{once:true});
      worker.onmessage=({data})=>{if(data.error)finish(new Error(data.error));else if(data.sha256)finish(null,data.sha256);else progress(data.progress);};
      worker.onerror=()=>finish(new Error('Video verification could not start. Reload and try again.'));
      worker.postMessage({file});
    });
  }
  async function upload() {
    const file=$('#mytubeInput').files[0],button=$('#startMytubeUpload'),message=$('#mytubeUploadMessage'),bar=$('#mytubeUploadBar'),track=$('#mytubeUploadProgress');
    if(!file){message.textContent='Choose a video first.';return;}button.disabled=true;track.hidden=false;
    uploadController=new AbortController(); const signal=uploadController.signal; $('#pauseMytubeUpload').hidden=false;
    const title=$('#mytubeTitle').value.trim()||file.name.replace(/\.[^.]+$/,'');
    let uploadRecord=null;
    try{
      message.textContent='Checking video integrity…';const contentSha256=await hashUpload(file,signal,value=>{
        message.textContent=`Checking video integrity ${Math.round(value*100)}%`;bar.style.width=`${Math.round(value*100)}%`;
      });
      uploadRecord=await uploadIdentity(file,contentSha256);const key=uploadRecord.key;
      const metadataKey=`${uploadRecord.storageKey}:metadata`;
      let metadata;try{metadata=JSON.parse(localStorage.getItem(metadataKey));}catch(_error){}
      metadata=metadata||{filename:file.name,size:file.size,sha256:contentSha256,title,visibility:$('#mytubeVisibility').value};
      localStorage.setItem(metadataKey,JSON.stringify(metadata));
      const reservation=await api('/api/mytube/uploads',{method:'POST',signal,headers:{'Content-Type':'application/json','Idempotency-Key':key},body:JSON.stringify(metadata)});
      let sent=Number(reservation.offset)||0,chunkSize=8*1024*1024;
      let failures=0;
      while(sent<file.size){
        const previous=sent, chunk=file.slice(sent,Math.min(file.size,sent+chunkSize));
        try {
          const result=await api(`/api/mytube/uploads/${reservation.upload_id}`,{method:'PATCH',signal,timeout:90000,headers:{'Content-Type':'application/offset+octet-stream','Upload-Offset':String(sent)},body:chunk});
          sent=Number(result.offset); failures=0;
        }catch(error){
          if(signal.aborted||(![409,408,500,502,503,504].includes(error.status)&&error.status))throw error;
          if(++failures>3)throw new Error('Upload paused after connection trouble. Choose the same video to resume safely.');
          message.textContent='Checking the saved upload position…';
          const status=await api(`/api/mytube/uploads/${reservation.upload_id}`,{signal});
          if(status.expired)throw Object.assign(new Error('Upload reservation expired. Try the upload again.'),{status:410});
          if(!['open','complete','finalizing'].includes(status.state))throw new Error('The upload is no longer active. Start a new upload.');
          sent=Number(status.offset);
        }
        if(!Number.isInteger(sent)||sent<previous||sent>file.size||(!failures&&sent===previous))throw new Error('The saved upload position is invalid. Nothing else was sent.');
        bar.style.width=`${Math.round(sent/file.size*100)}%`;message.textContent=`Uploading “${metadata.title}” ${Math.round(sent/file.size*100)}%`;
      }
      message.textContent='Verifying video…';await api(`/api/mytube/uploads/${reservation.upload_id}/finalize`,{method:'POST',signal,timeout:180000});
      localStorage.removeItem(uploadRecord.storageKey);localStorage.removeItem(metadataKey);message.textContent='Video added and ready to play.';notice('Video added to MyTube.');await loadVideos(true);
    }catch(error){if(error.status===410&&uploadRecord){localStorage.removeItem(uploadRecord.storageKey);localStorage.removeItem(`${uploadRecord.storageKey}:metadata`);}message.textContent=signal.aborted?'Upload paused. Choose the same video to resume; completed chunks are kept.':error.name==='AbortError'?'The request timed out. Retry with the same video to check its saved position.':error.message;}
    finally{button.disabled=false;uploadController=null;$('#pauseMytubeUpload').hidden=true;}
  }
  async function saveProgress(force=false) {
    const player=$('#mytubePlayer'); if(!activeVideo||!player||!Number.isFinite(player.currentTime))return;
    progressPending={id:activeVideo.id,position_seconds:player.currentTime,completed:Number.isFinite(player.duration)&&player.duration>0&&player.currentTime>=player.duration-3};
    if(progressSaving)return;progressSaving=true;
    try{while(progressPending){const event=progressPending;progressPending=null;try{await api(`/api/mytube/videos/${event.id}/progress`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(event)});}catch(_error){progressPending=progressPending||event;if(force)notice('Playback progress will retry when the connection returns.');break;}}}
    finally{progressSaving=false;}
  }
  function playerState(message,{retry=false}={}) {
    clearTimeout(playerDeadline); const state=$('#mytubePlayerState'); state.replaceChildren(); state.hidden=false;
    $('#mytubeWatchPanel').setAttribute('aria-busy','false');
    const text=document.createElement('p');text.textContent=message;state.append(text);
    if(retry){const button=document.createElement('button');button.type='button';button.className='secondary-button';button.textContent='Retry playback';button.onclick=()=>loadWatch();state.append(button);}
  }
  function startPlayer(video) {
    const player=$('#mytubePlayer');activeVideo=null;player.onpause=null;$('#mytubeWatchTitle').textContent=video.title;document.title=`${video.title} · MyTube · ${window.davidPiServerName || "Home server"}`;$('#mytubeWatchDescription').textContent=video.description||'';$('#mytubeWatchMeta').textContent=`${video.visibility==='private'?'Only me':'Shared'} · ${size(video.byte_size)} · ${time(video.duration_seconds)}`;
    if(video.poster_url)player.poster=video.poster_url;
    hls?.destroy();hls=null;player.pause();player.removeAttribute('src');
    activeVideo=video;$('#mytubeDownload').href=video.stream_url;$('#mytubeDownload').hidden=false;
    if(video.hls_url&&window.Hls?.isSupported()){
      hls=new window.Hls({capLevelToPlayerSize:true,startLevel:-1,maxBufferLength:30,maxMaxBufferLength:60});let recoveries=0;
      hls.on(window.Hls.Events.ERROR,(_event,data)=>{
        if(!data.fatal)return;
        if(recoveries++<2&&data.type===window.Hls.ErrorTypes.NETWORK_ERROR){hls.startLoad();return;}
        if(recoveries<=2&&data.type===window.Hls.ErrorTypes.MEDIA_ERROR){hls.recoverMediaError();return;}
        hls.destroy();hls=null;player.src=video.stream_url;player.load();playerState('Prepared playback failed. Trying the original video…');
        playerDeadline=setTimeout(()=>playerState('The video could not be played. Check the connection or try the original download.',{retry:true}),15000);
      });
      hls.loadSource(video.hls_url);hls.attachMedia(player);
    }
    else player.src=video.hls_url||video.stream_url;
    player.onloadedmetadata=()=>{if(video.position_seconds>0&&video.position_seconds<player.duration-3)player.currentTime=video.position_seconds;clearTimeout(playerDeadline);$('#mytubePlayerState').hidden=true;$('#mytubeWatchPanel').setAttribute('aria-busy','false');};
    player.onplaying=()=>{clearTimeout(playerDeadline);$('#mytubePlayerState').hidden=true;};
    player.onwaiting=player.onstalled=()=>{clearTimeout(playerDeadline);playerDeadline=setTimeout(()=>playerState('Buffering is taking longer than expected. You can retry playback.',{retry:true}),15000);};
    player.onerror=()=>playerState(player.error?.code===3||player.error?.code===4?'This video format could not be decoded. Try its original download.':'The video could not load. Check the connection and retry.',{retry:true});
    player.ontimeupdate=()=>{if(!progressTimer)progressTimer=setTimeout(()=>{progressTimer=null;saveProgress();},12000);};player.onpause=()=>saveProgress();player.onended=()=>saveProgress();
    clearTimeout(playerDeadline);playerDeadline=setTimeout(()=>playerState('The video is taking too long to load. Check the connection and retry.',{retry:true}),15000);
  }
  async function loadWatch() {
    watchController?.abort();watchController=new AbortController();const generation=++watchGeneration,signal=watchController.signal;
    if(!videoId){$('#mytubeWatchTitle').textContent='Video unavailable';playerState('This video is missing or is not shared with this account.');return;}
    playerState('Getting the video ready…');
    try{const {video}=await api(`/api/mytube/videos/${videoId}`,{signal});if(generation!==watchGeneration)return;startPlayer(video);}
    catch(error){if(generation===watchGeneration&&error.name!=='AbortError'){$('#mytubeWatchTitle').textContent='Video unavailable';playerState(error.message||'Video is unavailable.',{retry:error.status!==404});}return;}
    try{const list=await api('/api/mytube/videos?view=library&limit=8',{signal});if(generation===watchGeneration)$('#mytubeUpNext').replaceChildren(...list.videos.filter(item=>item.id!==videoId).slice(0,6).map(item=>card(item,true)));}
    catch(error){if(generation===watchGeneration&&error.name!=='AbortError')$('#mytubeUpNext').textContent='Other videos are temporarily unavailable. This video can still play.';}
  }
  if($('#mytubePlayer')){
    if(collection)$('.back').href=`/mytube?collection=${encodeURIComponent(collection.id)}`;
    document.addEventListener('visibilitychange',()=>{if(document.hidden)saveProgress(true);});
    window.addEventListener('online',()=>saveProgress());loadWatch();return;
  }
  $('#mytubeViews')?.addEventListener('click',event=>{const button=event.target.closest('button[data-view]');if(!button)return;collection=null;history.replaceState(null,'','/mytube');view=button.dataset.view;document.querySelectorAll('#mytubeViews button').forEach(item=>{const selected=item===button;item.classList.toggle('selected',selected);item.setAttribute('aria-pressed',String(selected));});const labels={library:['Library','Ready to watch'],continue:['Pick up where you left off','Continue watching'],recent:['New at home','Recently added'],collections:['Organize your screen','Collections'],mine:['Your uploads','My videos'],trash:['Recoverable for 30 days','Trash']};const selectedLabels=labels[view];$('#mytubeKicker').textContent=selectedLabels[0];$('#mytubeHeading').textContent=selectedLabels[1];$('#newMytubeCollection').hidden=view!=='collections';loadVideos(true);});
  $('#mytubeCollectionBack').addEventListener('click',()=>$('#mytubeViews [data-view="collections"]').click());
  $('#pauseMytubeUpload').addEventListener('click',()=>uploadController?.abort());
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!uploadController&&!document.querySelector('dialog[open]'))loadVideos(true);});
  $('#openMytubeUpload')?.addEventListener('click',openUpload);$('#emptyAddMytube')?.addEventListener('click',openUpload);$('#closeMytubeUpload')?.addEventListener('click',()=>$('#mytubeUploadSheet').close());$('#startMytubeUpload')?.addEventListener('click',upload);$('#retryMytube')?.addEventListener('click',()=>loadVideos(true));$('#mytubeMore')?.addEventListener('click',()=>loadVideos());$('#mytubeInput')?.addEventListener('change',event=>{const file=event.target.files[0];if(file&&!$('#mytubeTitle').value)$('#mytubeTitle').value=file.name.replace(/\.[^.]+$/,'');});
  $('#newMytubeCollection')?.addEventListener('click',()=>$('#mytubeCollectionSheet').showModal());$('#closeMytubeCollection')?.addEventListener('click',()=>$('#mytubeCollectionSheet').close());$('#saveMytubeCollection')?.addEventListener('click',async()=>{const message=$('#mytubeCollectionMessage');try{await api('/api/mytube/collections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('#mytubeCollectionName').value,visibility:$('#mytubeCollectionVisibility').value})});$('#mytubeCollectionSheet').close();notice('Collection created.');loadVideos(true);}catch(error){message.textContent=error.message;}});
  $('#closeMytubeAddCollection')?.addEventListener('click',()=>$('#mytubeAddCollectionSheet').close());
  loadVideos(true);
})();

const audiobookCsrf = document.querySelector('meta[name="csrf-token"]').content;
const audiobookGrid = document.querySelector('#audiobookGrid');
const audiobookEmpty = document.querySelector('#audiobooksEmpty');
const audiobookLoadError = document.querySelector('#audiobooksLoadError');
const audiobookResults = document.querySelector('#audiobookResults');
const audiobookLoadMore = document.querySelector('#loadMoreAudiobooks');
const audiobookToast = document.querySelector('#platformToast');
let audiobookOwner = '', audiobookDeleted = false, audiobookSearchTimer, activeBook = null, progressTimer = null, sleepTimeout = null;
let audiobookPlayerClosing = false;
let audiobookRestoringPosition = true;
let audiobookHls = null, audiobookHlsFailures = 0;
let audiobookHlsLibrary = null, audiobookOffset = 0, audiobookLoading = false, audiobookRequest = 0;
const audiobookPlayerGuard = window.DavidPiAudiobookSafety.createGenerationGuard();
const audiobookStreamGuard = window.DavidPiAudiobookSafety.createGenerationGuard();
let audiobookPlayerGeneration = 0, audiobookStreamGeneration = 0;
const AUDIOBOOK_PAGE_SIZE = 24;
const AUDIOBOOK_NETWORK_TIMEOUT_MS = 4500;
const AUDIOBOOK_STALL_TIMEOUT_MS = 8000;
const AUDIOBOOK_SCOPE = /^[0-9a-f]{32}$/i;
const audiobookPageTitle = document.title;
let nativeArtworkKey = '';
let audiobookProgressScope = document.querySelector('meta[name="audiobook-progress-scope"]')?.content || '';
let audiobookProgressQueue = window.DavidPiAudiobookProgress.create(audiobookProgressScope);
let audiobookProgressSync = createAudiobookProgressSync();
let nativeAudiobookProgressFlush = null;
let nativeAudiobookUnboundCount = 0, audiobookIdentityName = '';
const nativeAudiobookProgressConflicts = new Map();
let audiobookContinuity = window.DavidPiAudiobookContinuity.create(audiobookProgressScope);
let audiobookProgressFlush = null, lastLocalProgressSecond = -1;
let audiobookPersistentSession = null, lastPersistentSecond = -1;
let audiobookBrowserOffline = {complete: new Map(), partial: new Map()};
let audiobookVisibleBooks = new Map();
let audiobookSourceMode = 'none', audiobookOfflineRecoveryPending = false, audiobookOfflinePlaybackSource = null;
const audiobookStallWatchdog = window.DavidPiAudiobookContinuity.createStallWatchdog(
  AUDIOBOOK_STALL_TIMEOUT_MS,
  async snapshot => {
    if (!snapshot || snapshot.source_mode === 'offline' || !audiobookPlayerIsActive(snapshot.book, snapshot.generation)) return;
    await recoverAudiobookOffline(
      snapshot.book, snapshot.position, snapshot.was_playing,
      'The online stream stalled.', snapshot.generation,
    );
  },
);

async function audiobookApi(url, options = {}) {
  const requestOptions = {...options};
  const deadlineMs = Number(requestOptions.deadlineMs || 0);
  delete requestOptions.deadlineMs;
  requestOptions.headers = {...requestOptions.headers, 'X-CSRF-Token': audiobookCsrf};
  const readResponse = async (input, init) => {
    const response = await fetch(input, init);
    const data = await response.json().catch(error => {if(init.signal?.aborted)throw error;return {};});
    if (!response.ok) { const error=new Error(data.error || 'Something went wrong.');error.status=response.status;error.data=data;throw error; }
    return data;
  };
  return deadlineMs > 0
    ? window.DavidPiAudiobookContinuity.fetchWithDeadline(readResponse, url, requestOptions, deadlineMs)
    : readResponse(url, requestOptions);
}
function audiobookNotice(text) { audiobookToast.textContent=text; audiobookToast.hidden=false; clearTimeout(audiobookNotice.timer); audiobookNotice.timer=setTimeout(()=>audiobookToast.hidden=true,3200); }
function audiobookTime(seconds) { const total=Math.max(0,Math.round(seconds||0)),hours=Math.floor(total/3600),minutes=Math.floor((total%3600)/60); return hours?`${hours} hr ${minutes} min`:`${minutes} min`; }
function audiobookSize(bytes) { const units=['B','KB','MB','GB'];let value=bytes||0,index=0;while(value>=1024&&index<units.length-1){value/=1024;index++;}return `${value<10&&index?value.toFixed(1):Math.round(value)} ${units[index]}`; }

function audiobookBrowserSaveLabel(book) {
  const complete = audiobookBrowserOffline.complete.get(book.id);
  if (complete?.progress_scope === audiobookProgressScope) return 'Saved offline';
  if (complete) return 'Link offline progress';
  if (audiobookBrowserOffline.partial.has(book.id)) return 'Resume offline save';
  return 'Save offline';
}

function renderAudiobookOfflineEntry() {
  const status = document.querySelector('#offlineEntryStatus');
  if (!status) return;
  if (window.DavidPiOffline?.saveAudiobook) {
    if(!window.DavidPiOffline.progress){
      status.textContent='Your Android downloads still play, but native offline listening positions stay on this device until you update the app. Web-player progress continues to sync.';
      const update=document.createElement('a');update.href='/device-backup';update.textContent='Check Android app update';status.append(' ',update);
    }else status.textContent = 'The Android app keeps full books offline. Listening positions sync when you return here online; older downloads may need the one-time profile link below.';
    return;
  }
  if (!window.DavidPiBrowserAudiobooks?.supported()) {
    status.textContent = 'This browser cannot keep protected offline copies. Use the Android app for dependable downloads.';
    return;
  }
  const ready = audiobookBrowserOffline.complete.size;
  const partial = audiobookBrowserOffline.partial.size;
  if (!ready && !partial) {
    status.textContent = 'Save a shared book once, then listen from this device without a connection. Interrupted saves can resume.';
    return;
  }
  status.textContent = `${ready} ${ready === 1 ? 'book' : 'books'} ready offline${partial ? ` · ${partial} interrupted ${partial === 1 ? 'save' : 'saves'} can resume` : ''}.`;
}

async function refreshAudiobookBrowserOffline() {
  if (window.DavidPiOffline?.saveAudiobook || !window.DavidPiBrowserAudiobooks?.supported()) {
    audiobookBrowserOffline = {complete: new Map(), partial: new Map()};
    renderAudiobookOfflineEntry();
    return;
  }
  try {
    const inventory = await window.DavidPiBrowserAudiobooks.inventory();
    audiobookBrowserOffline = {
      complete: new Map(inventory.books.map(book => [book.id, book])),
      partial: new Map(inventory.partials.map(book => [book.id, book])),
    };
  } catch (_error) {
    audiobookBrowserOffline = {complete: new Map(), partial: new Map()};
  }
  renderAudiobookOfflineEntry();
  syncAudiobookBrowserSaveButtons();
}

function syncAudiobookBrowserSaveButtons() {
  document.querySelectorAll('[data-offline-book-id]').forEach(button=>{
    const book=audiobookVisibleBooks.get(button.dataset.offlineBookId);
    if(book){
      button.textContent=audiobookBrowserSaveLabel(book);
      button.setAttribute('aria-label',`${button.textContent} for ${book.title} in this browser`);
    }
  });
}

function audiobookOfflineCandidate(book) {
  const saved = audiobookBrowserOffline.complete.get(book?.id);
  const savedScope=String(saved?.progress_scope||'').toLowerCase(),currentScope=String(audiobookProgressScope||'').toLowerCase();
  return AUDIOBOOK_SCOPE.test(currentScope)&&(!savedScope||savedScope===currentScope)?saved:null;
}

function audiobookOfflineShelf() {
  return [...audiobookBrowserOffline.complete.values()]
    .filter(book => audiobookOfflineCandidate(book))
    .map(book => queuedAudiobookProgress({
      ...book,
      offline_only: true,
      playback_mode: 'range',
      stream_url: window.DavidPiBrowserAudiobooks.localUrl(book.id),
      download_url: '',
      cover_url: '',
      chapters: [],
      is_mine: false,
    }));
}

function queuedAudiobookProgress(book) {
  audiobookProgressQueue.observe(book);
  const pending=audiobookProgressQueue.pending(book.id);
  if(pending?.dirty){book.position_seconds=pending.position;book.completed=pending.completed;}
  return book;
}

function queueAudiobookProgress(book, position, completed) {
  const numericPosition=Number(position);
  try { return audiobookProgressQueue.mark(book.id,Number.isFinite(numericPosition)?Math.max(0,numericPosition):0,Boolean(completed),Date.now(),book); }
  catch(error){document.querySelector('#progressState').textContent=error.message;return null;}
}

async function sendQueuedAudiobookProgress(entry) {
  return audiobookApi(`/api/audiobooks/${entry.book_id}/progress`,{
    method:'PUT',headers:{'Content-Type':'application/json'},
    deadlineMs:AUDIOBOOK_NETWORK_TIMEOUT_MS,
    body:JSON.stringify({position_seconds:entry.position,completed:entry.completed,progress_scope:entry.scope,
      base_revision:entry.base_revision,session_id:entry.session_id,sequence:entry.sequence}),
  });
}

function createAudiobookProgressSync() {
  const queue=audiobookProgressQueue;
  return window.DavidPiAudiobookProgress.createSync(queue,sendQueuedAudiobookProgress,{
    onAcknowledged:async(entry,data)=>{
      if(queue!==audiobookProgressQueue)return;
      const newer=queue.pending(entry.book_id),book=activeBook?.id===entry.book_id?activeBook:audiobookVisibleBooks.get(entry.book_id);
      if(book){Object.assign(book,data);book.position_seconds=newer?.dirty?newer.position:data.position_seconds;book.completed=newer?.dirty?newer.completed:data.completed;}
      await window.DavidPiBrowserAudiobooks?.updateProgressRevision?.(entry.book_id,data,entry.scope);
    },
    onConflict:()=>{renderAudiobookProgressConflicts();},
  });
}

function flushAudiobookProgress() {
  if(navigator.onLine===false)return Promise.resolve(false);
  void flushNativeAudiobookProgress();
  return audiobookProgressSync.flush().then(()=>true).catch(()=>false);
}

async function nativeProgressRequest(payload) {
  const response=await window.DavidPiOffline.progress(JSON.stringify(payload));
  if(String(response).startsWith('error:'))throw new Error(String(response).slice(6));
  return response==='ok'?{}:JSON.parse(response);
}

function flushNativeAudiobookProgress() {
  if(nativeAudiobookProgressFlush)return nativeAudiobookProgressFlush;
  if(!window.DavidPiOffline?.progress||navigator.onLine===false||!AUDIOBOOK_SCOPE.test(audiobookProgressScope))return Promise.resolve();
  const scope=audiobookProgressScope;
  nativeAudiobookProgressFlush=(async()=>{
    const {entries=[],unbound_count=0}=await nativeProgressRequest({action:'pending',scope});
    if(scope!==audiobookProgressScope)return;
    nativeAudiobookUnboundCount=Number(unbound_count)||0;
    for(const entry of entries){
      if(scope!==audiobookProgressScope)return;
      try{
        const data=await sendQueuedAudiobookProgress(entry);
        await nativeProgressRequest({...entry,action:'ack',progress_revision:data.progress_revision});
        nativeAudiobookProgressConflicts.delete(entry.book_id);
      }catch(error){
        if(error.status===409&&error.data?.conflict)nativeAudiobookProgressConflicts.set(entry.book_id,{...entry,native:true,conflict:error.data});
        else if([403,404,410].includes(error.status))continue;
        else break; // The Room outbox survives the network/bridge interruption.
      }
    }
    renderAudiobookProgressConflicts();
  })().catch(()=>{}).finally(()=>{nativeAudiobookProgressFlush=null;});
  return nativeAudiobookProgressFlush;
}

function renderAudiobookProgressConflicts() {
  let panel=document.querySelector('#audiobookProgressConflicts');
  if(!panel){panel=document.createElement('section');panel.id='audiobookProgressConflicts';panel.setAttribute('aria-label','Listening position choices');audiobookResults.parentElement.append(panel);}
  panel.replaceChildren();
  if(nativeAudiobookUnboundCount>0){
    const message=document.createElement('p'),button=document.createElement('button');
    message.textContent=`${nativeAudiobookUnboundCount} older Android ${nativeAudiobookUnboundCount===1?'download has':'downloads have'} a listening position not yet linked to a profile. Your audio stays saved.`;
    button.type='button';button.textContent='Link previous Android progress';
    button.addEventListener('click',async()=>{
      if(!confirm(`Pause native playback, then link the older Android listening positions to ${audiobookIdentityName||'the currently signed-in profile'}? You will choose which position to keep if another device differs. No audio is downloaded or removed.`))return;
      button.disabled=true;
      try{await nativeProgressRequest({action:'bind_legacy',scope:audiobookProgressScope});nativeAudiobookUnboundCount=0;await flushNativeAudiobookProgress();}
      catch(error){audiobookNotice(error.message);button.disabled=false;}
    });
    panel.append(message,button);
  }
  const stamp=seconds=>{const total=Math.floor(Number(seconds)||0);return `${Math.floor(total/3600)}:${String(Math.floor(total%3600/60)).padStart(2,'0')}:${String(total%60).padStart(2,'0')}`;};
  const candidates=[...audiobookProgressQueue.dirtyEntries().filter(item=>item.conflict),...nativeAudiobookProgressConflicts.values()].filter(entry=>entry.scope===audiobookProgressScope);
  for(const entry of candidates){
    const row=document.createElement('div'),message=document.createElement('p');
    message.textContent=`${audiobookVisibleBooks.get(entry.book_id)?.title||'Audiobook'} has two listening positions. Neither is discarded until you choose.`;
    row.append(message);
    for(const [keep,label] of [[true,`Use ${entry.native?'Android offline':'this device'} (${stamp(entry.position)})`],[false,`Use other saved position (${stamp(entry.conflict.current.position_seconds)})`]]){
      const button=document.createElement('button');button.type='button';button.textContent=label;
      button.addEventListener('click',async()=>{
        const scope=audiobookProgressScope,queue=audiobookProgressQueue;
        row.querySelectorAll('button').forEach(item=>item.disabled=true);
        try{
          const {book}=await audiobookApi(`/api/audiobooks/${entry.book_id}`,{deadlineMs:AUDIOBOOK_NETWORK_TIMEOUT_MS});
          if(scope!==audiobookProgressScope)return;
          if(entry.native){
            await nativeProgressRequest({...entry,action:'resolve',keep_candidate:keep,...book,scope,book_id:entry.book_id});
            nativeAudiobookProgressConflicts.delete(entry.book_id);renderAudiobookProgressConflicts();
            if(keep)await flushNativeAudiobookProgress();
            return;
          }
          const result=queue.resolve(entry.book_id,keep,book);
          if(!result)return;
          const position=keep?result.position:result.position_seconds;
          if(activeBook?.id===entry.book_id){Object.assign(activeBook,book,{position_seconds:position});document.querySelector('#bookAudio').currentTime=position;audiobookContinuity.remember(entry.book_id,position);}
          await window.DavidPiBrowserAudiobooks?.updateProgressRevision?.(entry.book_id,book,scope);
          await window.DavidPiBrowserAudiobooks?.updatePosition?.(entry.book_id,position,keep?result.completed:book.completed,scope);
          renderAudiobookProgressConflicts();
          if(keep)await flushAudiobookProgress();
        }catch(error){audiobookNotice(error.message);row.querySelectorAll('button').forEach(item=>item.disabled=false);}
      });
      row.append(button);
    }
    panel.append(row);
  }
  panel.hidden=!panel.childElementCount;
}

async function saveAudiobookInApp(book) {
  if(!window.DavidPiOffline?.saveAudiobook)return;
  const downloadUrl=new URL(book.download_url,window.location.origin).href;
  const coverUrl=book.cover_url?new URL(book.cover_url,window.location.origin).href:'';
  const duration=Number(book.duration_seconds),byteSize=Number(book.byte_size),position=Number(book.position_seconds);
  const payload={
    book_id:book.id,title:book.title,author:book.author||'',series:book.series||'',
    duration_seconds:Number.isFinite(duration)&&duration>=0?duration:0,byte_size:Number.isSafeInteger(byteSize)&&byteSize>=0?byteSize:0,
    content_type:book.content_type||'application/octet-stream',content_sha256:book.sha256||'',download_url:downloadUrl,
    cover_url:coverUrl,position_seconds:Number.isFinite(position)&&position>=0?position:0,
    completed:Boolean(book.completed),progress_scope:audiobookProgressScope,progress_revision:book.progress_revision,
  };
  try{const result=await window.DavidPiOffline.saveAudiobook(JSON.stringify(payload));if(String(result).startsWith('error:'))throw new Error(String(result).slice(6));if(result!=='queued')throw new Error('The Android app did not confirm this download.');audiobookNotice(window.DavidPiOffline.progress?`Saving “${book.title}” in the Android app.`:'Download queued. Update Android to sync native offline listening positions.');}
  catch(_error){audiobookNotice('The Android app could not start this download.');}
}

async function saveAudiobookInBrowser(book, button) {
  if(button.dataset.downloading==='true'){window.DavidPiBrowserAudiobooks.cancel(book.id);return;}
  if (audiobookBrowserOffline.complete.get(book.id)?.progress_scope === audiobookProgressScope) {
    window.location.assign('/static/audiobooks-offline.html');
    return;
  }
  if(!confirm('Save this shared book in this browser profile? Anyone who later uses this browser profile can play it until you remove it, even if server access is revoked. Do not use this on a shared or public browser.'))return;
  const original=button.textContent;button.dataset.downloading='true';button.textContent='Cancel download';
  try{
    const result=await window.DavidPiBrowserAudiobooks.save({...book,progress_scope:audiobookProgressScope},percent=>{button.textContent=`Cancel · saving ${percent}%`;});
    if(result.rebound)audiobookNotice('Saved copy linked to your listening progress.');
    else audiobookNotice(result.persistent?'Saved in this browser.':'Saved here, but this browser may remove the copy if storage is needed.');
  }catch(error){audiobookNotice(error.message||'This browser could not save the audiobook.');}
  finally{delete button.dataset.downloading;await refreshAudiobookBrowserOffline();button.disabled=false;button.textContent=audiobookBrowserSaveLabel(book)||original;}
}

function audiobookCover(book, className='') {
  const cover=document.createElement('div');cover.className=`audiobook-cover ${className}`;
  if(book.cover_url){const image=new Image();image.src=book.cover_url;image.alt='';image.loading=className==='player'?'eager':'lazy';image.decoding='async';cover.append(image);}else{const mark=document.createElement('span');mark.textContent='♫';cover.append(mark);}
  return cover;
}

function seekAudiobook(delta) {
  const audio=document.querySelector('#bookAudio');
  if(!Number.isFinite(audio.duration))return;
  audio.currentTime=Math.max(0,Math.min(audio.duration,audio.currentTime+delta));
}

function audiobookChapterIndex(position) {
  const chapters=activeBook?.chapters||[];
  let index=-1;
  chapters.forEach((chapter,chapterIndex)=>{if(Number(chapter.start)<=position)index=chapterIndex;});
  return index;
}

function syncAudiobookChapter() {
  const audio=document.querySelector('#bookAudio'),select=document.querySelector('#chapterSelect');
  const index=audiobookChapterIndex(Number(audio.currentTime||0));
  const chapter=index>=0?activeBook?.chapters?.[index]:null;
  select.value=chapter?String(chapter.start):'';
}

function seekAudiobookChapter(direction) {
  const audio=document.querySelector('#bookAudio'),chapters=activeBook?.chapters||[];
  if(!chapters.length){seekAudiobook(direction>0?30:-15);return;}
  const current=Math.max(0,audiobookChapterIndex(Number(audio.currentTime||0)));
  const chapterStart=Number(chapters[current]?.start||0);
  const target=direction>0
    ?Math.min(chapters.length-1,current+1)
    :Math.max(0,current-(audio.currentTime-chapterStart<5?1:0));
  audio.currentTime=Number(chapters[target].start||0);
  syncAudiobookChapter();
}

function updateMediaSessionPosition() {
  const audio=document.querySelector('#bookAudio');
  if(!('mediaSession' in navigator)||!Number.isFinite(audio.duration)||audio.duration<=0)return;
  try{navigator.mediaSession.setPositionState({duration:audio.duration,playbackRate:audio.playbackRate||1,position:Math.min(audio.currentTime,audio.duration)});}catch(_error){}
}

function updateNativeMediaSession() {
  const audio=document.querySelector('#bookAudio');
  if(!activeBook||!window.DavidPiMedia)return;
  try{window.DavidPiMedia.updatePlayback(!audio.paused,Number(audio.currentTime||0),Number(audio.duration||0),Number(audio.playbackRate||1));}catch(_error){}
}

function audiobookPlayerIsActive(book, generation) {
  return audiobookPlayerGuard.current(generation)
    && activeBook === book
    && !audiobookPlayerClosing;
}

function audiobookPersistentState() {
  const audio=document.querySelector('#bookAudio');
  return {
    title:activeBook?.title||'Audiobook',author:activeBook?.author||'Unknown author',
    playing:Boolean(activeBook&&!audio.paused&&!audio.ended),
    position:Number.isFinite(audio.currentTime)?audio.currentTime:0,
    duration:Number.isFinite(audio.duration)?audio.duration:0,
  };
}

function publishAudiobookSession(force=false) {
  const audio=document.querySelector('#bookAudio'),second=Math.floor(Number(audio.currentTime||0));
  if(!force&&second===lastPersistentSecond)return;
  lastPersistentSecond=second;audiobookPersistentSession?.publish();
}

function restoreAudiobookPlayer() {
  if(!activeBook||audiobookPlayerClosing)return false;
  const dialog=document.querySelector('#audiobookPlayer');
  try{window.focus();}catch(_error){}
  if(!dialog.open){try{dialog.showModal();}catch(_error){return false;}}
  audiobookPersistentSession?.setMinimized(false);
  return true;
}

function minimizeAudiobookPlayer() {
  if(!activeBook||audiobookPlayerClosing)return false;
  audiobookPersistentSession?.setMinimized(true);
  const dialog=document.querySelector('#audiobookPlayer');
  try{if(dialog.open)dialog.close();}catch(_error){}
  return true;
}

function audiobookStreamIsActive(book, playerGeneration, streamGeneration) {
  return audiobookPlayerIsActive(book, playerGeneration)
    && audiobookStreamGuard.current(streamGeneration);
}

async function configureNativeArtwork(book, generation=audiobookPlayerGeneration) {
  if(!window.DavidPiMedia)return;
  const key=String(book.cover_url||'');
  if(!key){nativeArtworkKey='';try{window.DavidPiMedia.clearArtwork();}catch(_error){}return;}
  if(nativeArtworkKey===key)return;
  nativeArtworkKey=key;
  try{
    const response=await fetch(key,{credentials:'same-origin',cache:'force-cache'});
    if(!response.ok)throw new Error('cover unavailable');
    const blob=await response.blob();
    if(!blob.type.startsWith('image/')||blob.size>4*1024*1024)throw new Error('cover rejected');
    const image=await createImageBitmap(blob);
    const canvas=document.createElement('canvas');canvas.width=512;canvas.height=512;
    const context=canvas.getContext('2d');context.fillStyle='#251f1a';context.fillRect(0,0,512,512);
    const scale=Math.min(512/image.width,512/image.height),width=image.width*scale,height=image.height*scale;
    context.drawImage(image,(512-width)/2,(512-height)/2,width,height);image.close?.();
    if(nativeArtworkKey!==key||!audiobookPlayerIsActive(book,generation))return;
    window.DavidPiMedia.setArtworkDataUrl(canvas.toDataURL('image/jpeg',0.86));
  }catch(_error){if(nativeArtworkKey===key&&audiobookPlayerIsActive(book,generation)){nativeArtworkKey='';try{window.DavidPiMedia.clearArtwork();}catch(_ignored){}}}
}

function configureMediaSession(book, generation=audiobookPlayerGeneration) {
  if(!audiobookPlayerIsActive(book,generation))return;
  if(window.DavidPiMedia){try{window.DavidPiMedia.setMetadata(book.title||'Audiobook',book.author||'Unknown author');}catch(_error){}}
  configureNativeArtwork(book,generation);
  if(!('mediaSession' in navigator))return;
  const artwork=book.cover_url?[{src:new URL(book.cover_url,location.origin).href,sizes:'600x600',type:'image/jpeg'}]:[];
  if ('MediaMetadata' in window) {
    try { navigator.mediaSession.metadata=new MediaMetadata({title:book.title,artist:book.author||'Unknown author',album:book.series||`${window.davidPiServerName || 'Home server'} Audiobooks`,artwork}); } catch (_error) {}
  }
  const audio=document.querySelector('#bookAudio');
  const whenActive=handler=>(...args)=>{if(audiobookPlayerIsActive(book,generation))handler(...args);};
  const actions={
    play:()=>{if(audiobookPlayerIsActive(book,generation))audio.play().catch(()=>{if(audiobookPlayerIsActive(book,generation))document.querySelector('#progressState').textContent='Playback could not resume yet. Open the server and tap play.';});}, pause:()=>{if(audiobookPlayerIsActive(book,generation))audio.pause();},
    seekbackward:whenActive(details=>seekAudiobook(-(details.seekOffset||15))),
    seekforward:whenActive(details=>seekAudiobook(details.seekOffset||30)),
    seekto:whenActive(details=>{const target=Number(details.seekTime);if(Number.isFinite(target))audio.currentTime=Math.max(0,Math.min(audio.duration||target,target));}),
    stop:whenActive(()=>{audio.pause();saveAudiobookProgress(true);}),
  };
  Object.entries(actions).forEach(([name,handler])=>{try{navigator.mediaSession.setActionHandler(name,handler);}catch(_error){}});
}

window.davidPiNativeAudioCommand = (command, value) => {
  const audio=document.querySelector('#bookAudio');
  const book=activeBook,generation=audiobookPlayerGeneration;
  if(!audio||!book||!audiobookPlayerIsActive(book,generation))return;
  if(command==='play'){
    const resume=()=>{if(audiobookPlayerIsActive(book,generation))audio.play().catch(()=>{if(audiobookPlayerIsActive(book,generation))document.querySelector('#progressState').textContent='Playback could not resume yet. Open the server and tap play.';});};
    if(audio.readyState>=2)resume();
    else{document.querySelector('#progressState').textContent='Reconnecting to the server…';audio.load();audiobookPlayerGuard.listen(generation,audio,'canplay',resume,{once:true});}
  }
  else if(command==='pause')audio.pause();
  else if(command==='forward')audio.currentTime=Math.min(audio.duration||audio.currentTime+30,audio.currentTime+30);
  else if(command==='back')audio.currentTime=Math.max(0,audio.currentTime-15);
  else if(command==='nextchapter')seekAudiobookChapter(1);
  else if(command==='previouschapter')seekAudiobookChapter(-1);
  else if(command==='seek')audio.currentTime=Math.max(0,Math.min(audio.duration||Number(value),Number(value)));
};

function destroyAudiobookStream() {
  audiobookRestoringPosition=true;
  audiobookStallWatchdog.cancel();
  audiobookStreamGeneration=audiobookStreamGuard.next();
  if(audiobookHls){audiobookHls.destroy();audiobookHls=null;}
  if(audiobookOfflinePlaybackSource){
    window.DavidPiBrowserAudiobooks?.releasePlaybackSource(audiobookOfflinePlaybackSource);
    audiobookOfflinePlaybackSource=null;
  }
  audiobookHlsFailures=0;
  audiobookSourceMode='none';
}

function loadAudiobookHlsLibrary() {
  if(window.Hls)return Promise.resolve(window.Hls);
  if(audiobookHlsLibrary)return audiobookHlsLibrary;
  audiobookHlsLibrary=new Promise((resolve,reject)=>{
    const script=document.createElement('script');
    script.src='/static/vendor/hls/hls.min.js?v=1.6.16';script.async=true;
    script.addEventListener('load',()=>window.Hls?resolve(window.Hls):reject(new Error('HLS library unavailable.')),{once:true});
    script.addEventListener('error',()=>reject(new Error('HLS library unavailable.')),{once:true});
    document.head.append(script);
  }).catch(error=>{audiobookHlsLibrary=null;throw error;});
  return audiobookHlsLibrary;
}

function restoreAudiobookPosition(audio, book, autoplay, playerGeneration, streamGeneration) {
  const resume=()=>{
    if(!audiobookStreamIsActive(book,playerGeneration,streamGeneration))return;
    if(book.position_seconds>0&&Number.isFinite(audio.duration)&&book.position_seconds<audio.duration-10)audio.currentTime=book.position_seconds;
    audiobookRestoringPosition=false;
    audio.playbackRate=Number(document.querySelector('#playbackSpeed').value);
    if(autoplay)audio.play().catch(()=>{if(audiobookStreamIsActive(book,playerGeneration,streamGeneration))document.querySelector('#progressState').textContent='Ready · tap play to begin.';});
  };
  if(audio.readyState>=1)resume();
  else audiobookStreamGuard.listen(streamGeneration,audio,'loadedmetadata',resume,{once:true});
}

function useRangeAudiobookStream(book, position=0, autoplay=false, reason='', playerGeneration=audiobookPlayerGeneration, expectedStreamGeneration=null) {
  if(!audiobookPlayerIsActive(book,playerGeneration))return;
  if(expectedStreamGeneration!==null&&!audiobookStreamGuard.current(expectedStreamGeneration))return;
  const audio=document.querySelector('#bookAudio'),state=document.querySelector('#progressState');
  destroyAudiobookStream();
  const streamGeneration=audiobookStreamGeneration;
  audiobookSourceMode='online-range';
  audio.pause();audio.src=book.stream_url;audio.preload='metadata';
  book.position_seconds=position??book.position_seconds??0;
  state.textContent=reason?'Segmented playback recovered with the compatible stream.':'Compatible stream ready while chapters are prepared.';
  restoreAudiobookPosition(audio,book,autoplay,playerGeneration,streamGeneration);
  audio.load();
}

async function useSegmentedAudiobookStream(book, autoplay=false, playerGeneration=audiobookPlayerGeneration) {
  if(!audiobookPlayerIsActive(book,playerGeneration))return;
  const audio=document.querySelector('#bookAudio'),state=document.querySelector('#progressState');
  destroyAudiobookStream();
  const streamGeneration=audiobookStreamGeneration;
  audiobookSourceMode='online-hls';
  state.textContent='Loading the next sections…';
  try {
    if(audio.canPlayType('application/vnd.apple.mpegurl')){
      audio.src=book.hls_url;audio.preload='metadata';restoreAudiobookPosition(audio,book,autoplay,playerGeneration,streamGeneration);audio.load();return;
    }
    const Hls=await loadAudiobookHlsLibrary();
    if(!audiobookStreamIsActive(book,playerGeneration,streamGeneration))return;
    if(Hls.isSupported()){
      const instance=new Hls({
        enableWorker:true,
        maxBufferLength:60,
        maxMaxBufferLength:180,
        maxBufferSize:33554432,
        backBufferLength:30,
        startPosition:Number(book.position_seconds||0),
      });
      if(!audiobookStreamIsActive(book,playerGeneration,streamGeneration)){instance.destroy();return;}
      audiobookHls=instance;
      instance.on(Hls.Events.MEDIA_ATTACHED,()=>{if(audiobookStreamIsActive(book,playerGeneration,streamGeneration)&&audiobookHls===instance)instance.loadSource(book.hls_url);});
      instance.on(Hls.Events.MANIFEST_PARSED,()=>{if(audiobookStreamIsActive(book,playerGeneration,streamGeneration)&&audiobookHls===instance)restoreAudiobookPosition(audio,book,autoplay,playerGeneration,streamGeneration);});
      instance.on(Hls.Events.ERROR,(_event,data)=>{
        if(!audiobookStreamIsActive(book,playerGeneration,streamGeneration)||audiobookHls!==instance)return;
        if(!data.fatal)return;
        audiobookHlsFailures+=1;
        if(data.type===Hls.ErrorTypes.NETWORK_ERROR&&audiobookHlsFailures<=2){instance.startLoad(audio.currentTime??book.position_seconds??0);return;}
        if(data.type===Hls.ErrorTypes.MEDIA_ERROR&&audiobookHlsFailures<=2){instance.recoverMediaError();return;}
        const position=Number(audio.currentTime??book.position_seconds??0),wasPlaying=!audio.paused;
        window.DavidPiAudiobookSafety.guard(async()=>{
          if(await recoverAudiobookOffline(book,position,wasPlaying,'The online stream dropped.',playerGeneration))return;
          useRangeAudiobookStream(book,position,wasPlaying,true,playerGeneration,streamGeneration);
        },()=>useRangeAudiobookStream(book,position,wasPlaying,true,playerGeneration,streamGeneration));
      });
      instance.attachMedia(audio);return;
    }
    useRangeAudiobookStream(book,book.position_seconds||0,autoplay,true,playerGeneration,streamGeneration);
  } catch (_error) {
    if(audiobookStreamIsActive(book,playerGeneration,streamGeneration))useRangeAudiobookStream(book,book.position_seconds||0,autoplay,true,playerGeneration,streamGeneration);
  }
}

async function useOfflineAudiobookStream(book, position=0, autoplay=false, reason='', playerGeneration=audiobookPlayerGeneration) {
  if(!audiobookPlayerIsActive(book,playerGeneration))return false;
  const offlineApi=window.DavidPiBrowserAudiobooks;
  if(!offlineApi?.playbackSource)return false;
  let source=null;
  try{
    source=await offlineApi.playbackSource(book.id,audiobookProgressScope);
    if(!source)return false;
    if(!audiobookPlayerIsActive(book,playerGeneration)){offlineApi.releasePlaybackSource(source);return false;}
    const audio=document.querySelector('#bookAudio'),state=document.querySelector('#progressState');
    destroyAudiobookStream();
    const streamGeneration=audiobookStreamGeneration;
    audiobookOfflinePlaybackSource=source;
    // The verified OPFS lookup is authoritative. Populate the presentation
    // cache after success so an early click never waits for inventory().
    audiobookBrowserOffline.complete.set(book.id,source);
    if(!Number.isSafeInteger(source.progress_revision)&&Number.isSafeInteger(book.progress_revision)&&Number(source.position_seconds||0)!==Number(book.position_seconds||0)){
      audiobookProgressQueue.importLegacy(book,Number(source.position_seconds||0),Boolean(source.completed));
      renderAudiobookProgressConflicts();
    }
    audiobookSourceMode='offline';
    audio.pause();audio.src=source.playback_url;audio.preload='metadata';
    book.position_seconds=Math.max(0,Number(position??book.position_seconds??source.position_seconds??0));
    state.textContent=reason?`${reason} Continuing from the verified offline copy.`:'Verified offline copy ready.';
    restoreAudiobookPosition(audio,book,autoplay,playerGeneration,streamGeneration);
    audio.load();
    return true;
  }catch(_error){
    if(source){
      if(audiobookOfflinePlaybackSource===source)audiobookOfflinePlaybackSource=null;
      offlineApi.releasePlaybackSource(source);
    }
    return false;
  }
}

async function recoverAudiobookOffline(book, position, autoplay, reason, playerGeneration=audiobookPlayerGeneration) {
  if(audiobookOfflineRecoveryPending||audiobookSourceMode==='offline')return false;
  audiobookOfflineRecoveryPending=true;
  try{return await useOfflineAudiobookStream(book,position,autoplay,reason,playerGeneration);}
  finally{audiobookOfflineRecoveryPending=false;}
}

async function startPreferredAudiobookStream(book, autoplay, generation) {
  const state=document.querySelector('#progressState');
  // `navigator.onLine` only describes the phone's network interface. It can
  // remain true on cellular while David-Pi/Tailscale is unreachable, so an
  // identity-bound verified local copy is always the first playback source.
  return window.DavidPiAudiobookSafety.startLocalFirst({
    openLocal:()=>useOfflineAudiobookStream(book,book.position_seconds||0,autoplay,'',generation),
    offlineOnly:Boolean(book.offline_only),
    online:navigator.onLine!==false,
    unavailable:()=>{
      if(audiobookPlayerIsActive(book,generation))state.textContent='No verified saved copy is available for this listening identity. Use Manage offline books to test or replace it.';
    },
    openOnline:async()=>{
      if(book.hls_url&&book.playback_mode==='segmented')await useSegmentedAudiobookStream(book,autoplay,generation);
      else useRangeAudiobookStream(book,book.position_seconds||0,autoplay,false,generation);
    },
  });
}

function populateAudiobookChapters(book) {
  const chapters=document.querySelector('#chapterSelect');chapters.innerHTML='<option value="">Full book</option>';
  (book.chapters||[]).forEach(chapter=>{const option=document.createElement('option');option.value=chapter.start;option.textContent=chapter.title;chapters.append(option);});
}

async function hydrateAudiobookDetails(book, generation=audiobookPlayerGeneration) {
  if(Array.isArray(book.chapters))return book;
  try{
    const suffix=audiobookDeleted?'?view=deleted':'';
    const data=await audiobookApi(`/api/audiobooks/${book.id}${suffix}`,{deadlineMs:AUDIOBOOK_NETWORK_TIMEOUT_MS});
    if(!audiobookPlayerIsActive(book,generation))return book;
    Object.assign(book,queuedAudiobookProgress(data.book));populateAudiobookChapters(book);
  }catch(_error){/* Playback remains available from the compact shelf record. */}
  return book;
}

function openAudiobook(book, autoplay=false) {
  if(activeBook===book&&!audiobookPlayerClosing){restoreAudiobookPlayer();if(autoplay)document.querySelector('#bookAudio').play().catch(()=>{});return;}
  if(activeBook&&!audiobookPlayerClosing){void audiobookPlayerTeardown.run().then(()=>openAudiobook(book,autoplay));return;}
  audiobookPlayerGeneration=audiobookPlayerGuard.next();destroyAudiobookStream();activeBook=book;
  const generation=audiobookPlayerGeneration,dialog=document.querySelector('#audiobookPlayer'),audio=document.querySelector('#bookAudio');
  audiobookPlayerClosing=false;audiobookPlayerTeardown.reset();lastLocalProgressSecond=-1;lastPersistentSecond=-1;
  document.title=`${book.title} · ${window.davidPiServerName || "Home server"}`;
  audio.title=book.title||'Saved audiobook';
  document.querySelector('#playerTitle').textContent=book.title;document.querySelector('#playerAuthor').textContent=book.author||'Unknown author';
  document.querySelector('#playerSeries').textContent=book.series||'Now playing';
  const download=document.querySelector('#downloadAudiobook');download.hidden=Boolean(book.offline_only||!book.download_url);if(book.download_url)download.href=book.download_url;else download.removeAttribute('href');
  const cover=document.querySelector('#playerCover');cover.replaceChildren(...audiobookCover(book,'player').childNodes);
  populateAudiobookChapters(book);hydrateAudiobookDetails(book,generation);
  const state=document.querySelector('#progressState');
  audio.pause();audio.removeAttribute('src');audio.load();audio.playbackRate=Number(document.querySelector('#playbackSpeed').value);
  const armStallRecovery=()=>{
    if(audiobookSourceMode==='offline')return;
    state.textContent='Connecting to the server…';
    audiobookStallWatchdog.arm(()=>({
      book,
      generation,
      position:Number.isFinite(audio.currentTime)?audio.currentTime:Number(book.position_seconds||0),
      was_playing:!audio.paused,
      source_mode:audiobookSourceMode,
    }));
  };
  audiobookPlayerGuard.listen(generation,audio,'waiting',armStallRecovery);
  audiobookPlayerGuard.listen(generation,audio,'stalled',armStallRecovery);
  audiobookPlayerGuard.listen(generation,audio,'playing',()=>{audiobookStallWatchdog.cancel();state.textContent=audiobookSourceMode==='offline'?'Playing verified offline copy · progress saves on this device.':'Playing · progress saves automatically.';});
  audiobookPlayerGuard.listen(generation,audio,'pause',()=>audiobookStallWatchdog.cancel());
  audiobookPlayerGuard.listen(generation,audio,'error',()=>{
    if(!audiobookPlayerIsActive(book,generation))return;
    if(audiobookSourceMode==='offline'){state.textContent='The saved copy could not continue. Manage offline books can test or replace it.';return;}
    const position=Number(audio.currentTime??book.position_seconds??0),wasPlaying=!audio.paused;
    window.DavidPiAudiobookSafety.guard(async()=>{
      if(!await recoverAudiobookOffline(book,position,wasPlaying,'The online stream became unavailable.',generation)&&audiobookPlayerIsActive(book,generation))state.textContent='Playback could not continue. Check the connection and try again.';
    },()=>{if(audiobookPlayerIsActive(book,generation))state.textContent='Playback could not continue. Check the connection and try again.';});
  });
  dialog.showModal();
  audiobookPersistentSession=window.DavidPiPersistentAudio?.claim({
    getState:audiobookPersistentState,
    onCommand:command=>{
      if(!audiobookPlayerIsActive(book,generation))return;
      if(command==='play')audio.play().catch(()=>{state.textContent='Playback could not resume yet. Return to Audiobooks and tap play.';});
      else if(command==='pause')audio.pause();
      else if(command==='back')seekAudiobook(-15);
      else if(command==='forward')seekAudiobook(30);
      else if(command==='expand')restoreAudiobookPlayer();
      else if(command==='stop')void audiobookPlayerTeardown.run();
    },
    onTakeover:()=>{
      // Stop sound immediately, then use the normal teardown path so the last
      // position is flushed and every stream/object URL is released.
      try{audio.pause();}catch(_error){}
      void audiobookPlayerTeardown.run();
    },
  })||null;
  if(window.DavidPiPersistentAudio?.supported&&!audiobookPersistentSession){
    state.textContent='This audiobook moved to another server tab.';
    void audiobookPlayerTeardown.run();
    return;
  }
  audiobookContinuity.remember(book.id,book.position_seconds||0);
  configureMediaSession(book,generation);
  window.DavidPiAudiobookSafety.guard(()=>startPreferredAudiobookStream(book,autoplay,generation),()=>{if(audiobookPlayerIsActive(book,generation))state.textContent='Playback could not be prepared. Close and try again.';});
}
async function saveAudiobookProgress(force=false) {
  const audio=document.querySelector('#bookAudio');if(!activeBook||audiobookRestoringPosition||!Number.isFinite(audio.currentTime))return;
  if(!force&&audio.paused)return;
  const bookId=activeBook.id,entry=queueAudiobookProgress(activeBook,audio.currentTime,audio.duration>0&&audio.currentTime>=audio.duration-15);
  if(!entry)return;
  if(navigator.onLine===false){if(activeBook?.id===bookId)document.querySelector('#progressState').textContent='Progress saved on this device · it will sync when connected.';return;}
  document.querySelector('#progressState').textContent='Saving progress…';
  try{const saved=await flushAudiobookProgress();if(activeBook?.id===bookId)document.querySelector('#progressState').textContent=audiobookProgressQueue.pending(bookId)?.conflict?'Choose a listening position on your shelf.':saved&&!audiobookProgressQueue.pending(bookId)?'Progress saved.':'Progress saved on this device · it will retry when connected.';}
  catch(_error){if(activeBook?.id===bookId)document.querySelector('#progressState').textContent=audiobookSourceMode==='offline'?'Progress saved on this device · it will sync when connected.':'Progress could not be saved yet.';}
}
const audiobookPlayerTeardown=window.DavidPiAudiobookSafety.createTeardown({
  persist:()=>saveAudiobookProgress(true),
  cleanup:()=>{
    audiobookPlayerClosing=true;
    const audio=document.querySelector('#bookAudio'),dialog=document.querySelector('#audiobookPlayer');
    audiobookPlayerGuard.cancel();audiobookPersistentSession?.release();audiobookPersistentSession=null;
    try{audio.pause();}catch(_error){}clearInterval(progressTimer);progressTimer=null;try{destroyAudiobookStream();}catch(_error){}
    audio.removeAttribute('src');audio.removeAttribute('title');try{audio.load();}catch(_error){}
    if(activeBook)audiobookContinuity.clear(activeBook.id);
    activeBook=null;lastPersistentSecond=-1;nativeArtworkKey='';try{window.DavidPiMedia?.clear();}catch(_error){}document.title=audiobookPageTitle;clearTimeout(sleepTimeout);sleepTimeout=null;document.querySelector('#sleepTimer').value='0';
    if('mediaSession' in navigator){try{navigator.mediaSession.metadata=null;navigator.mediaSession.playbackState='none';['play','pause','seekbackward','seekforward','seekto','stop'].forEach(action=>navigator.mediaSession.setActionHandler(action,null));}catch(_error){}}
    try{if(dialog.open)dialog.close();}catch(_error){}
  },
  report:()=>{document.querySelector('#progressState').textContent='Progress could not be saved yet.';},
});
function editAudiobook(book){activeBook=book;document.querySelector('#editAudiobookTitle').value=book.title;document.querySelector('#editAudiobookAuthor').value=book.author||'';document.querySelector('#editAudiobookVisibility').value=book.visibility;document.querySelector('#editAudiobookSheet').showModal();}
function audiobookCard(book) {
  const card=document.createElement('article');card.className='audiobook-card';
  const coverButton=document.createElement('button');coverButton.className='audiobook-cover-button';coverButton.type='button';coverButton.setAttribute('aria-label',`Play ${book.title}`);coverButton.append(audiobookCover(book));coverButton.addEventListener('click',()=>openAudiobook(book));
  const body=document.createElement('div');body.className='audiobook-card-body';
  const open=document.createElement('button');open.className='audiobook-open';open.type='button';
  const copy=document.createElement('span'),title=document.createElement('strong'),author=document.createElement('span'),meta=document.createElement('small');title.textContent=book.title;author.textContent=book.author||'Unknown author';meta.textContent=`${book.visibility==='private'?'Only me · ':''}${audiobookTime(book.duration_seconds)} · ${Math.round((book.duration_seconds?book.position_seconds/book.duration_seconds:0)*100)}% listened`;copy.append(title,author,meta);open.append(copy);open.addEventListener('click',()=>openAudiobook(book));
  const actions=document.createElement('div');actions.className='audiobook-actions';
  if(audiobookDeleted&&book.is_mine){const restore=document.createElement('button');restore.type='button';restore.textContent='Restore';restore.addEventListener('click',async()=>{restore.disabled=true;try{await audiobookApi(`/api/audiobooks/${book.id}/restore`,{method:'POST'});audiobookNotice('Audiobook restored.');loadAudiobooks();}catch(error){audiobookNotice(error.message);}finally{restore.disabled=false;}});actions.append(restore);}
  else {
    const play=document.createElement('button');play.type='button';play.className='audiobook-play';play.textContent=book.position_seconds>10?'Continue':'Play';play.addEventListener('click',()=>openAudiobook(book,true));actions.append(play);
    if(window.DavidPiOffline?.saveAudiobook){const save=document.createElement('button');save.type='button';save.textContent='Save offline';save.setAttribute('aria-label',`Save ${book.title} in the Android app`);save.addEventListener('click',()=>saveAudiobookInApp(book));actions.append(save);}
    else if(book.visibility==='shared'&&window.DavidPiBrowserAudiobooks?.supported()){const save=document.createElement('button');save.type='button';save.dataset.offlineBookId=book.id;save.textContent=audiobookBrowserSaveLabel(book);save.setAttribute('aria-label',`${save.textContent} for ${book.title} in this browser`);save.addEventListener('click',()=>saveAudiobookInBrowser(book,save));actions.append(save);}
    if(book.offline_only){body.append(open,actions);card.append(coverButton,body);return card;}
    const more=document.createElement('details');more.className='audiobook-more';const summary=document.createElement('summary');summary.textContent='More';const secondary=document.createElement('div');
    const download=document.createElement('a');download.className='audiobook-card-download';download.href=book.download_url;download.download='';download.textContent='Download original';download.setAttribute('aria-label',`Download the original file for ${book.title}`);secondary.append(download);
    if(book.is_mine){const edit=document.createElement('button');edit.type='button';edit.textContent='Edit details';edit.addEventListener('click',()=>editAudiobook(book));const remove=document.createElement('button');remove.type='button';remove.className='danger-text';remove.textContent='Move to Recently Deleted';remove.addEventListener('click',()=>{if(!confirm(`Move “${book.title}” to Recently Deleted?`))return;remove.disabled=true;window.DavidPiAudiobookSafety.guard(async()=>{await audiobookApi(`/api/audiobooks/${book.id}`,{method:'DELETE'});audiobookNotice('Audiobook moved to Recently Deleted.');await loadAudiobooks();},error=>audiobookNotice(error.message||'The audiobook could not be moved.')).finally(()=>{remove.disabled=false;});});secondary.append(edit,remove);}
    more.append(summary,secondary);actions.append(more);
  }
  body.append(open,actions);card.append(coverButton,body);return card;
}

async function restoreAudiobookContinuity() {
  if(activeBook)return;
  const remembered=audiobookContinuity.read();
  if(!remembered)return;
  let book=audiobookVisibleBooks.get(remembered.book_id)||audiobookOfflineShelf().find(item=>item.id===remembered.book_id);
  if(!book&&navigator.onLine!==false){
    try{book=queuedAudiobookProgress((await audiobookApi(`/api/audiobooks/${remembered.book_id}`,{deadlineMs:AUDIOBOOK_NETWORK_TIMEOUT_MS})).book);}
    catch(_error){return;}
  }
  if(!book)return;
  const pending=audiobookProgressQueue.pending(book.id);
  book.position_seconds=pending?.dirty?pending.position:Number(book.position_seconds||0);
  openAudiobook(book,false);
}

async function loadAudiobooks({append=false}={}){
  if(audiobookLoading)return;
  const requestId=++audiobookRequest;audiobookLoading=true;
  if(!append){audiobookOffset=0;audiobookResults.textContent='Loading your shelf…';audiobookLoadError.hidden=true;}
  audiobookGrid.setAttribute('aria-busy','true');audiobookLoadMore.disabled=true;
  const params=new URLSearchParams({q:document.querySelector('#audiobookSearch').value,owner:audiobookOwner,view:audiobookDeleted?'deleted':'',compact:'1',limit:String(AUDIOBOOK_PAGE_SIZE),offset:String(append?audiobookOffset:0)});
  const offlineRefresh=append?Promise.resolve():refreshAudiobookBrowserOffline();
  try{
    const data=await audiobookApi(`/api/audiobooks?${params}`,{deadlineMs:AUDIOBOOK_NETWORK_TIMEOUT_MS});if(requestId!==audiobookRequest)return;
    audiobookIdentityName=String(data.current_user||'');
    if(data.progress_scope&&data.progress_scope!==audiobookProgressScope){audiobookProgressScope=data.progress_scope;audiobookProgressQueue=window.DavidPiAudiobookProgress.create(audiobookProgressScope);audiobookProgressSync=createAudiobookProgressSync();audiobookContinuity=window.DavidPiAudiobookContinuity.create(audiobookProgressScope);}
    const books=data.books.map(queuedAudiobookProgress),cards=books.map(audiobookCard);
    if(append)books.forEach(book=>audiobookVisibleBooks.set(book.id,book));else audiobookVisibleBooks=new Map(books.map(book=>[book.id,book]));
    if(append)audiobookGrid.append(...cards);else audiobookGrid.replaceChildren(...cards);
    audiobookOffset=Number(data.next_offset??data.total??books.length);
    audiobookResults.textContent=`${data.total} ${data.total===1?'book':'books'}${data.has_more?` · showing ${audiobookGrid.childElementCount}`:''}`;
    audiobookEmpty.hidden=data.total!==0;audiobookLoadError.hidden=true;audiobookLoadMore.hidden=!data.has_more;
    flushAudiobookProgress();
    renderAudiobookProgressConflicts();
    restoreAudiobookContinuity();
  }catch(error){
    if(requestId!==audiobookRequest)return;
    await offlineRefresh;
    const offlineBooks=append?[]:audiobookOfflineShelf();
    if(offlineBooks.length){
      audiobookVisibleBooks=new Map(offlineBooks.map(book=>[book.id,book]));
      audiobookGrid.replaceChildren(...offlineBooks.map(audiobookCard));
      audiobookLoadError.hidden=true;audiobookEmpty.hidden=true;audiobookLoadMore.hidden=true;
      audiobookResults.textContent=`Offline shelf · ${offlineBooks.length} verified ${offlineBooks.length===1?'book':'books'} ready`;
      restoreAudiobookContinuity();
    }else{
      audiobookLoadError.hidden=false;audiobookEmpty.hidden=true;audiobookResults.textContent='Shelf unavailable';
      if(!append)audiobookGrid.replaceChildren();
      audiobookNotice(error.message);
    }
  }finally{
    if(requestId===audiobookRequest){audiobookLoading=false;audiobookGrid.setAttribute('aria-busy','false');audiobookLoadMore.disabled=false;}
  }
}

function showAudiobookUpload(){document.querySelector('#audiobookUploadMessage').textContent='';document.querySelector('#audiobookUploadSheet').showModal();}
document.querySelector('#openAudiobookUpload').addEventListener('click',showAudiobookUpload);document.querySelector('#emptyAddAudiobook').addEventListener('click',showAudiobookUpload);document.querySelector('#closeAudiobookUpload').addEventListener('click',()=>document.querySelector('#audiobookUploadSheet').close());
document.querySelector('#audiobookInput').addEventListener('change',event=>{const files=[...event.target.files];document.querySelector('#selectedAudiobooks').textContent=files.length?`${files.length} selected · ${audiobookSize(files.reduce((sum,file)=>sum+file.size,0))}`:'';});
document.querySelector('#startAudiobookUpload').addEventListener('click',async()=>{const input=document.querySelector('#audiobookInput'),button=document.querySelector('#startAudiobookUpload'),message=document.querySelector('#audiobookUploadMessage');if(!input.files.length){message.textContent='Choose at least one audiobook.';return;}const body=new FormData();[...input.files].slice(0,20).forEach(file=>body.append('books',file));body.append('visibility',document.querySelector('#audiobookVisibility').value);button.disabled=true;button.textContent='Importing…';message.textContent='Keep this page open while the original files are copied.';try{const data=await audiobookApi('/api/audiobooks/upload',{method:'POST',body});document.querySelector('#audiobookUploadSheet').close();input.value='';document.querySelector('#selectedAudiobooks').textContent='';audiobookNotice(`${data.added.length} audiobook${data.added.length===1?'':'s'} added.`);loadAudiobooks();}catch(error){message.textContent=error.message;}finally{button.disabled=false;button.textContent='Import audiobooks';}});
document.querySelector('#audiobookViews').addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;audiobookOwner=button.dataset.owner||'';audiobookDeleted=button.dataset.view==='deleted';document.querySelectorAll('#audiobookViews button').forEach(item=>{const selected=item===button;item.classList.toggle('selected',selected);item.setAttribute('aria-pressed',String(selected));});audiobookRequest+=1;audiobookLoading=false;loadAudiobooks();});
document.querySelector('#audiobookSearch').addEventListener('input',()=>{clearTimeout(audiobookSearchTimer);audiobookSearchTimer=setTimeout(()=>{audiobookRequest+=1;audiobookLoading=false;loadAudiobooks();},250);});
document.querySelector('#retryAudiobooks').addEventListener('click',()=>loadAudiobooks());
audiobookLoadMore.addEventListener('click',()=>loadAudiobooks({append:true}));
const audiobookPlayerDialog=document.querySelector('#audiobookPlayer');
document.querySelector('#closeAudiobookPlayer').addEventListener('click',minimizeAudiobookPlayer);
document.querySelector('#stopAudiobookPlayer').addEventListener('click',()=>{void audiobookPlayerTeardown.run();});
audiobookPlayerDialog.addEventListener('cancel',event=>{event.preventDefault();minimizeAudiobookPlayer();});
audiobookPlayerDialog.addEventListener('close',()=>{if(activeBook&&!audiobookPlayerClosing)audiobookPersistentSession?.setMinimized(true);});
document.querySelector('#playbackSpeed').addEventListener('change',event=>document.querySelector('#bookAudio').playbackRate=Number(event.target.value));
document.querySelector('#rewindAudiobook').addEventListener('click',()=>seekAudiobook(-15));
document.querySelector('#forwardAudiobook').addEventListener('click',()=>seekAudiobook(30));
document.querySelector('#chapterSelect').addEventListener('change',event=>{if(event.target.value!=='')document.querySelector('#bookAudio').currentTime=Number(event.target.value);});
document.querySelector('#sleepTimer').addEventListener('change',event=>{clearTimeout(sleepTimeout);const minutes=Number(event.target.value);if(minutes)sleepTimeout=setTimeout(()=>{document.querySelector('#bookAudio').pause();audiobookNotice('Sleep timer finished.');},minutes*60000);});
document.querySelector('#bookAudio').addEventListener('play',()=>{if('mediaSession' in navigator)navigator.mediaSession.playbackState='playing';updateNativeMediaSession();publishAudiobookSession(true);clearInterval(progressTimer);progressTimer=setInterval(saveAudiobookProgress,15000);});document.querySelector('#bookAudio').addEventListener('pause',()=>{if('mediaSession' in navigator)navigator.mediaSession.playbackState='paused';updateNativeMediaSession();publishAudiobookSession(true);clearInterval(progressTimer);if(!audiobookPlayerClosing)saveAudiobookProgress(true);});
document.querySelector('#bookAudio').addEventListener('loadedmetadata',()=>{updateMediaSessionPosition();publishAudiobookSession(true);});
document.querySelector('#bookAudio').addEventListener('ratechange',updateNativeMediaSession);
document.querySelector('#bookAudio').addEventListener('seeked',()=>{if(!audiobookPlayerClosing)saveAudiobookProgress(true);});
document.querySelector('#bookAudio').addEventListener('ended',()=>{saveAudiobookProgress(true);updateNativeMediaSession();publishAudiobookSession(true);try{window.DavidPiMedia?.clear();}catch(_error){}});
document.querySelector('#bookAudio').addEventListener('timeupdate',()=>{const audio=document.querySelector('#bookAudio');if(audiobookRestoringPosition)return;syncAudiobookChapter();const second=Math.floor(audio.currentTime);publishAudiobookSession();if(activeBook&&second>=0&&(lastLocalProgressSecond<0||Math.abs(second-lastLocalProgressSecond)>=5)){lastLocalProgressSecond=second;const completed=audio.duration>0&&audio.currentTime>=audio.duration-15;queueAudiobookProgress(activeBook,audio.currentTime,completed);audiobookContinuity.remember(activeBook.id,audio.currentTime);if(audiobookOfflineCandidate(activeBook))window.DavidPiBrowserAudiobooks.updatePosition(activeBook.id,audio.currentTime,completed,audiobookProgressScope).catch(()=>{});}if(second%10===0){updateMediaSessionPosition();updateNativeMediaSession();}});
document.addEventListener('click',event=>{
  const audio=document.querySelector('#bookAudio');
  if(!activeBook||audio.paused||audio.ended||audiobookPlayerClosing||event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
  const link=event.target.closest?.('a[href]');if(!link||link.hasAttribute('download')||(link.target&&link.target!=='_self'))return;
  let url;try{url=new URL(link.href,location.href);}catch(_error){return;}
  if(url.origin!==location.origin||url.pathname==='/logout'||url.pathname.startsWith('/api/'))return;
  if(window.DavidPiPersistentAudio?.openRoute(link.href)){event.preventDefault();minimizeAudiobookPlayer();}
},true);
document.addEventListener('visibilitychange',()=>{if(activeBook){configureMediaSession(activeBook);updateMediaSessionPosition();}if(!document.hidden)flushAudiobookProgress();});
window.addEventListener('online',flushAudiobookProgress);
window.addEventListener('offline',()=>{if(!activeBook||audiobookSourceMode==='offline')return;const audio=document.querySelector('#bookAudio'),book=activeBook,generation=audiobookPlayerGeneration,position=Number(audio.currentTime??book.position_seconds??0),wasPlaying=!audio.paused;window.DavidPiAudiobookSafety.guard(()=>recoverAudiobookOffline(book,position,wasPlaying,'Connection lost.',generation),()=>{});});
window.addEventListener('pageshow',()=>{flushAudiobookProgress();if(activeBook){configureMediaSession(activeBook);updateMediaSessionPosition();}});
document.querySelector('#closeAudiobookEdit').addEventListener('click',()=>document.querySelector('#editAudiobookSheet').close());document.querySelector('#cancelAudiobookEdit').addEventListener('click',()=>document.querySelector('#editAudiobookSheet').close());
document.querySelector('#saveAudiobookEdit').addEventListener('click',async()=>{const button=document.querySelector('#saveAudiobookEdit');button.disabled=true;try{await audiobookApi(`/api/audiobooks/${activeBook.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:document.querySelector('#editAudiobookTitle').value,author:document.querySelector('#editAudiobookAuthor').value,visibility:document.querySelector('#editAudiobookVisibility').value})});document.querySelector('#editAudiobookSheet').close();audiobookNotice('Audiobook updated.');loadAudiobooks();}catch(error){audiobookNotice(error.message);}finally{button.disabled=false;}});
renderAudiobookOfflineEntry();
loadAudiobooks();
if('serviceWorker' in navigator)window.DavidPiAudiobookSafety.guard(()=>navigator.serviceWorker.register('/sw.js'),()=>{});

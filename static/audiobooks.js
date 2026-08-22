const audiobookCsrf = document.querySelector('meta[name="csrf-token"]').content;
const audiobookGrid = document.querySelector('#audiobookGrid');
const audiobookEmpty = document.querySelector('#audiobooksEmpty');
const audiobookToast = document.querySelector('#platformToast');
let audiobookOwner = '', audiobookDeleted = false, audiobookSearchTimer, activeBook = null, progressTimer = null, sleepTimeout = null;
let audiobookHls = null, audiobookHlsFailures = 0;
const audiobookPageTitle = document.title;
let nativeArtworkKey = '';

async function audiobookApi(url, options = {}) {
  options.headers = {...options.headers, 'X-CSRF-Token': audiobookCsrf};
  const response = await fetch(url, options), data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Something went wrong.');
  return data;
}
function audiobookNotice(text) { audiobookToast.textContent=text; audiobookToast.hidden=false; clearTimeout(audiobookNotice.timer); audiobookNotice.timer=setTimeout(()=>audiobookToast.hidden=true,3200); }
function audiobookTime(seconds) { const total=Math.max(0,Math.round(seconds||0)),hours=Math.floor(total/3600),minutes=Math.floor((total%3600)/60); return hours?`${hours} hr ${minutes} min`:`${minutes} min`; }
function audiobookSize(bytes) { const units=['B','KB','MB','GB'];let value=bytes||0,index=0;while(value>=1024&&index<units.length-1){value/=1024;index++;}return `${value<10&&index?value.toFixed(1):Math.round(value)} ${units[index]}`; }

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

async function configureNativeArtwork(book) {
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
    if(nativeArtworkKey!==key)return;
    window.DavidPiMedia.setArtworkDataUrl(canvas.toDataURL('image/jpeg',0.86));
  }catch(_error){if(nativeArtworkKey===key){nativeArtworkKey='';try{window.DavidPiMedia.clearArtwork();}catch(_ignored){}}}
}

function configureMediaSession(book) {
  if(window.DavidPiMedia){try{window.DavidPiMedia.setMetadata(book.title||'Audiobook',book.author||'Unknown author');}catch(_error){}}
  configureNativeArtwork(book);
  if(!('mediaSession' in navigator))return;
  const artwork=book.cover_url?[{src:new URL(book.cover_url,location.origin).href,sizes:'600x600',type:'image/jpeg'}]:[];
  if ('MediaMetadata' in window) {
    try { navigator.mediaSession.metadata=new MediaMetadata({title:book.title,artist:book.author||'Unknown author',album:book.series||'David-Pi Audiobooks',artwork}); } catch (_error) {}
  }
  const audio=document.querySelector('#bookAudio');
  const actions={
    play:()=>audio.play(), pause:()=>audio.pause(),
    seekbackward:details=>seekAudiobook(-(details.seekOffset||15)),
    seekforward:details=>seekAudiobook(details.seekOffset||30),
    seekto:details=>{if(details.seekTime!=null)audio.currentTime=Math.max(0,Math.min(audio.duration||details.seekTime,details.seekTime));},
    stop:()=>{audio.pause();saveAudiobookProgress(true);},
  };
  Object.entries(actions).forEach(([name,handler])=>{try{navigator.mediaSession.setActionHandler(name,handler);}catch(_error){}});
}

window.davidPiNativeAudioCommand = (command, value) => {
  const audio=document.querySelector('#bookAudio');
  if(!audio)return;
  if(command==='play'){
    const resume=()=>audio.play().catch(()=>{document.querySelector('#progressState').textContent='Playback could not resume yet. Open David-Pi and tap play.';});
    if(audio.readyState>=2)resume();
    else{document.querySelector('#progressState').textContent='Reconnecting to David-Pi…';audio.load();audio.addEventListener('canplay',resume,{once:true});}
  }
  else if(command==='pause')audio.pause();
  else if(command==='forward')audio.currentTime=Math.min(audio.duration||audio.currentTime+30,audio.currentTime+30);
  else if(command==='back')audio.currentTime=Math.max(0,audio.currentTime-15);
  else if(command==='nextchapter')seekAudiobookChapter(1);
  else if(command==='previouschapter')seekAudiobookChapter(-1);
  else if(command==='seek')audio.currentTime=Math.max(0,Math.min(audio.duration||Number(value),Number(value)));
};

function destroyAudiobookStream() {
  if(audiobookHls){audiobookHls.destroy();audiobookHls=null;}
  audiobookHlsFailures=0;
}

function restoreAudiobookPosition(audio, book, autoplay) {
  const resume=()=>{
    if(book.position_seconds>0&&Number.isFinite(audio.duration)&&book.position_seconds<audio.duration-10)audio.currentTime=book.position_seconds;
    audio.playbackRate=Number(document.querySelector('#playbackSpeed').value);
    if(autoplay)audio.play().catch(()=>{document.querySelector('#progressState').textContent='Ready · tap play to begin.';});
  };
  if(audio.readyState>=1)resume();else audio.addEventListener('loadedmetadata',resume,{once:true});
}

function useRangeAudiobookStream(book, position=0, autoplay=false, reason='') {
  const audio=document.querySelector('#bookAudio'),state=document.querySelector('#progressState');
  destroyAudiobookStream();
  audio.pause();audio.src=book.stream_url;audio.preload='metadata';
  const rangeBook={...book,position_seconds:position||book.position_seconds||0};
  state.textContent=reason?'Segmented playback recovered with the compatible stream.':'Compatible stream ready while chapters are prepared.';
  restoreAudiobookPosition(audio,rangeBook,autoplay);
  audio.load();
}

function useSegmentedAudiobookStream(book, autoplay=false) {
  const audio=document.querySelector('#bookAudio'),state=document.querySelector('#progressState');
  destroyAudiobookStream();
  state.textContent='Loading the next sections…';
  if(audio.canPlayType('application/vnd.apple.mpegurl')){
    audio.src=book.hls_url;audio.preload='metadata';restoreAudiobookPosition(audio,book,autoplay);audio.load();return;
  }
  if(window.Hls&&window.Hls.isSupported()){
    audiobookHls=new window.Hls({
      enableWorker:true,
      maxBufferLength:900,
      maxMaxBufferLength:900,
      maxBufferSize:134217728,
      backBufferLength:120,
      startPosition:Number(book.position_seconds||0),
    });
    audiobookHls.on(window.Hls.Events.MEDIA_ATTACHED,()=>audiobookHls.loadSource(book.hls_url));
    audiobookHls.on(window.Hls.Events.MANIFEST_PARSED,()=>restoreAudiobookPosition(audio,book,autoplay));
    audiobookHls.on(window.Hls.Events.ERROR,(_event,data)=>{
      if(!data.fatal)return;
      audiobookHlsFailures+=1;
      if(data.type===window.Hls.ErrorTypes.NETWORK_ERROR&&audiobookHlsFailures<=2){audiobookHls.startLoad(audio.currentTime||book.position_seconds||0);return;}
      if(data.type===window.Hls.ErrorTypes.MEDIA_ERROR&&audiobookHlsFailures<=2){audiobookHls.recoverMediaError();return;}
      const position=Number(audio.currentTime||book.position_seconds||0),wasPlaying=!audio.paused;
      useRangeAudiobookStream(book,position,wasPlaying,true);
    });
    audiobookHls.attachMedia(audio);return;
  }
  useRangeAudiobookStream(book,book.position_seconds||0,autoplay,true);
}

function openAudiobook(book, autoplay=false) {
  activeBook=book;const dialog=document.querySelector('#audiobookPlayer'),audio=document.querySelector('#bookAudio');
  document.title=`${book.title} · David-Pi`;
  audio.title=book.title||'David-Pi audiobook';
  document.querySelector('#playerTitle').textContent=book.title;document.querySelector('#playerAuthor').textContent=book.author||'Unknown author';
  document.querySelector('#playerSeries').textContent=book.series||'Now playing';document.querySelector('#downloadAudiobook').href=book.download_url;
  const cover=document.querySelector('#playerCover');cover.replaceChildren(...audiobookCover(book,'player').childNodes);
  const chapters=document.querySelector('#chapterSelect');chapters.innerHTML='<option value="">Full book</option>';
  (book.chapters||[]).forEach(chapter=>{const option=document.createElement('option');option.value=chapter.start;option.textContent=chapter.title;chapters.append(option);});
  const state=document.querySelector('#progressState');
  audio.pause();audio.removeAttribute('src');audio.load();audio.playbackRate=Number(document.querySelector('#playbackSpeed').value);
  audio.addEventListener('waiting',()=>{state.textContent='Connecting to David-Pi…';},{once:true});
  audio.addEventListener('playing',()=>{state.textContent='Playing · progress saves automatically.';},{once:true});
  dialog.showModal();
  configureMediaSession(book);
  if(book.hls_url&&book.playback_mode==='segmented')useSegmentedAudiobookStream(book,autoplay);
  else useRangeAudiobookStream(book,book.position_seconds||0,autoplay,false);
}
async function saveAudiobookProgress(force=false) {
  const audio=document.querySelector('#bookAudio');if(!activeBook||!Number.isFinite(audio.currentTime))return;
  if(!force&&audio.paused)return;
  document.querySelector('#progressState').textContent='Saving progress…';
  try{await audiobookApi(`/api/audiobooks/${activeBook.id}/progress`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({position_seconds:audio.currentTime,completed:audio.duration>0&&audio.currentTime>=audio.duration-15})});activeBook.position_seconds=audio.currentTime;document.querySelector('#progressState').textContent='Progress saved.';}
  catch(error){document.querySelector('#progressState').textContent='Progress could not be saved yet.';}
}
function editAudiobook(book){activeBook=book;document.querySelector('#editAudiobookTitle').value=book.title;document.querySelector('#editAudiobookAuthor').value=book.author||'';document.querySelector('#editAudiobookVisibility').value=book.visibility;document.querySelector('#editAudiobookSheet').showModal();}
function audiobookCard(book) {
  const card=document.createElement('article');card.className='audiobook-card';
  const coverButton=document.createElement('button');coverButton.className='audiobook-cover-button';coverButton.type='button';coverButton.setAttribute('aria-label',`Play ${book.title}`);coverButton.append(audiobookCover(book));coverButton.addEventListener('click',()=>openAudiobook(book));
  const body=document.createElement('div');body.className='audiobook-card-body';
  const open=document.createElement('button');open.className='audiobook-open';open.type='button';
  const copy=document.createElement('span'),title=document.createElement('strong'),author=document.createElement('span'),meta=document.createElement('small');title.textContent=book.title;author.textContent=book.author||'Unknown author';meta.textContent=`${book.visibility==='private'?'Only me · ':''}${audiobookTime(book.duration_seconds)} · ${Math.round((book.duration_seconds?book.position_seconds/book.duration_seconds:0)*100)}% listened`;copy.append(title,author,meta);open.append(copy);open.addEventListener('click',()=>openAudiobook(book));
  const actions=document.createElement('div');actions.className='audiobook-actions';
  if(audiobookDeleted){const restore=document.createElement('button');restore.textContent='Restore';restore.addEventListener('click',async()=>{await audiobookApi(`/api/audiobooks/${book.id}/restore`,{method:'POST'});audiobookNotice('Audiobook restored.');loadAudiobooks();});actions.append(restore);}
  else {const play=document.createElement('button');play.className='audiobook-play';play.textContent=book.position_seconds>10?'Continue':'Play';play.addEventListener('click',()=>openAudiobook(book,true));const download=document.createElement('a');download.className='audiobook-card-download';download.href=book.download_url;download.download='';download.textContent='Download';download.setAttribute('aria-label',`Download ${book.title} for offline listening`);actions.append(play,download);if(book.is_mine){const edit=document.createElement('button');edit.textContent='Edit';edit.addEventListener('click',()=>editAudiobook(book));const remove=document.createElement('button');remove.className='danger-text';remove.textContent='Delete';remove.addEventListener('click',async()=>{if(!confirm(`Move “${book.title}” to Recently Deleted?`))return;await audiobookApi(`/api/audiobooks/${book.id}`,{method:'DELETE'});audiobookNotice('Audiobook moved to Recently Deleted.');loadAudiobooks();});actions.append(edit,remove);}}
  body.append(open,actions);card.append(coverButton,body);return card;
}
async function loadAudiobooks(){const params=new URLSearchParams({q:document.querySelector('#audiobookSearch').value,owner:audiobookOwner,view:audiobookDeleted?'deleted':''});try{const data=await audiobookApi(`/api/audiobooks?${params}`);audiobookGrid.replaceChildren(...data.books.map(audiobookCard));audiobookEmpty.hidden=data.books.length!==0;}catch(error){audiobookNotice(error.message);}}

function showAudiobookUpload(){document.querySelector('#audiobookUploadMessage').textContent='';document.querySelector('#audiobookUploadSheet').showModal();}
document.querySelector('#openAudiobookUpload').addEventListener('click',showAudiobookUpload);document.querySelector('#emptyAddAudiobook').addEventListener('click',showAudiobookUpload);document.querySelector('#closeAudiobookUpload').addEventListener('click',()=>document.querySelector('#audiobookUploadSheet').close());
document.querySelector('#audiobookInput').addEventListener('change',event=>{const files=[...event.target.files];document.querySelector('#selectedAudiobooks').textContent=files.length?`${files.length} selected · ${audiobookSize(files.reduce((sum,file)=>sum+file.size,0))}`:'';});
document.querySelector('#startAudiobookUpload').addEventListener('click',async()=>{const input=document.querySelector('#audiobookInput'),button=document.querySelector('#startAudiobookUpload'),message=document.querySelector('#audiobookUploadMessage');if(!input.files.length){message.textContent='Choose at least one audiobook.';return;}const body=new FormData();[...input.files].slice(0,20).forEach(file=>body.append('books',file));body.append('visibility',document.querySelector('#audiobookVisibility').value);button.disabled=true;button.textContent='Importing…';message.textContent='Keep this page open while the original files are copied.';try{const data=await audiobookApi('/api/audiobooks/upload',{method:'POST',body});document.querySelector('#audiobookUploadSheet').close();input.value='';document.querySelector('#selectedAudiobooks').textContent='';audiobookNotice(`${data.added.length} audiobook${data.added.length===1?'':'s'} added.`);loadAudiobooks();}catch(error){message.textContent=error.message;}finally{button.disabled=false;button.textContent='Import audiobooks';}});
document.querySelector('#audiobookViews').addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;audiobookOwner=button.dataset.owner||'';audiobookDeleted=button.dataset.view==='deleted';document.querySelectorAll('#audiobookViews button').forEach(item=>item.classList.toggle('selected',item===button));loadAudiobooks();});
document.querySelector('#audiobookSearch').addEventListener('input',()=>{clearTimeout(audiobookSearchTimer);audiobookSearchTimer=setTimeout(loadAudiobooks,250);});
document.querySelector('#closeAudiobookPlayer').addEventListener('click',async()=>{await saveAudiobookProgress(true);const audio=document.querySelector('#bookAudio');audio.pause();destroyAudiobookStream();audio.removeAttribute('src');audio.removeAttribute('title');audio.load();document.querySelector('#audiobookPlayer').close();activeBook=null;nativeArtworkKey='';try{window.DavidPiMedia?.clear();}catch(_error){}document.title=audiobookPageTitle;clearTimeout(sleepTimeout);});
document.querySelector('#playbackSpeed').addEventListener('change',event=>document.querySelector('#bookAudio').playbackRate=Number(event.target.value));
document.querySelector('#rewindAudiobook').addEventListener('click',()=>seekAudiobook(-15));
document.querySelector('#forwardAudiobook').addEventListener('click',()=>seekAudiobook(30));
document.querySelector('#chapterSelect').addEventListener('change',event=>{if(event.target.value!=='')document.querySelector('#bookAudio').currentTime=Number(event.target.value);});
document.querySelector('#sleepTimer').addEventListener('change',event=>{clearTimeout(sleepTimeout);const minutes=Number(event.target.value);if(minutes)sleepTimeout=setTimeout(()=>{document.querySelector('#bookAudio').pause();audiobookNotice('Sleep timer finished.');},minutes*60000);});
document.querySelector('#bookAudio').addEventListener('play',()=>{if('mediaSession' in navigator)navigator.mediaSession.playbackState='playing';updateNativeMediaSession();clearInterval(progressTimer);progressTimer=setInterval(saveAudiobookProgress,15000);});document.querySelector('#bookAudio').addEventListener('pause',()=>{if('mediaSession' in navigator)navigator.mediaSession.playbackState='paused';updateNativeMediaSession();clearInterval(progressTimer);saveAudiobookProgress(true);});
document.querySelector('#bookAudio').addEventListener('loadedmetadata',updateMediaSessionPosition);
document.querySelector('#bookAudio').addEventListener('ratechange',updateNativeMediaSession);
document.querySelector('#bookAudio').addEventListener('ended',()=>{saveAudiobookProgress(true);updateNativeMediaSession();try{window.DavidPiMedia?.clear();}catch(_error){}});
document.querySelector('#bookAudio').addEventListener('timeupdate',()=>{syncAudiobookChapter();if(Math.floor(document.querySelector('#bookAudio').currentTime)%10===0){updateMediaSessionPosition();updateNativeMediaSession();}});
document.addEventListener('visibilitychange',()=>{if(activeBook){configureMediaSession(activeBook);updateMediaSessionPosition();}});
window.addEventListener('pageshow',()=>{if(activeBook){configureMediaSession(activeBook);updateMediaSessionPosition();}});
document.querySelector('#closeAudiobookEdit').addEventListener('click',()=>document.querySelector('#editAudiobookSheet').close());document.querySelector('#cancelAudiobookEdit').addEventListener('click',()=>document.querySelector('#editAudiobookSheet').close());
document.querySelector('#saveAudiobookEdit').addEventListener('click',async()=>{const button=document.querySelector('#saveAudiobookEdit');button.disabled=true;try{await audiobookApi(`/api/audiobooks/${activeBook.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:document.querySelector('#editAudiobookTitle').value,author:document.querySelector('#editAudiobookAuthor').value,visibility:document.querySelector('#editAudiobookVisibility').value})});document.querySelector('#editAudiobookSheet').close();audiobookNotice('Audiobook updated.');loadAudiobooks();}catch(error){audiobookNotice(error.message);}finally{button.disabled=false;}});
loadAudiobooks();

const csrf = document.querySelector('meta[name="csrf-token"]').content;
const grid = document.querySelector('#fileGrid'), empty = document.querySelector('#filesEmpty'), toast = document.querySelector('#platformToast');
let folderId = null, kind = '', ownerView = '', deletedView = false, selectedAction = null, searchTimer;
let activePdf = null;
let visibleFiles = [], activeFileIndex = -1, fileTouchStartX = null, fileTouchStartY = null, previewGeneration = 0;
let loadGeneration = 0, loadController = null;
let fileOffset = 0, fileTotal = 0, fileHasMore = false, fileAppending = false;
const listedFileIds = new Set();
const FILE_PAGE_SIZE = 40, loadMoreFiles = document.querySelector('#loadMoreFiles'), fileResultCount = document.querySelector('#fileResultCount');
async function api(url, options = {}) {
  options.headers = {...options.headers, 'X-CSRF-Token': csrf};
  const controller = new AbortController(), timeout = options.body instanceof FormData ? 30 * 60 * 1000 : 15000;
  const timer = setTimeout(() => controller.abort(), timeout), abort = () => controller.abort();
  options.signal?.addEventListener('abort', abort, {once:true});
  if(options.signal?.aborted)controller.abort();
  try {
    const response = await fetch(url, {...options, signal:controller.signal}), data = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(data.error || 'Something went wrong.'); error.data = data; error.status = response.status; throw error; }
    return data;
  } finally { clearTimeout(timer); options.signal?.removeEventListener('abort', abort); }
}
function showToast(text) { toast.textContent = text; toast.hidden = false; clearTimeout(showToast.timer); showToast.timer = setTimeout(() => toast.hidden = true, 3200); }
function formatSize(bytes) { if (bytes < 1024) return `${bytes} B`; const units = ['KB','MB','GB','TB']; let value=bytes/1024, unit=0; while(value>=1024 && unit<units.length-1){value/=1024;unit++;} return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`; }
const icons = {pdf:'PDF', image:'▧', video:'▶', audio:'♪', text:'TXT', document:'DOC', spreadsheet:'XLS', presentation:'PPT', other:'FILE'};
function stopFilePreview() {
  previewGeneration++; activePdf?.destroy(); activePdf = null;
  document.querySelectorAll('#filePreview audio,#filePreview video').forEach(media => {
    media.pause(); media.removeAttribute('src'); media.querySelectorAll('source').forEach(source => source.remove()); media.load();
  });
}
function previewFile(file) {
  const dialog=document.querySelector('#fileViewer'), target=document.querySelector('#filePreview');
  stopFilePreview();
  const generation=++previewGeneration;
  activeFileIndex=visibleFiles.findIndex(item=>item.id===file.id);
  target.innerHTML=''; document.querySelector('#viewerFileName').textContent=file.name;
  document.querySelector('#viewerDownload').href=file.download_url;
  if (file.kind==='image') target.append(Object.assign(document.createElement('img'),{src:file.content_url,alt:file.name}));
  else if(file.kind==='pdf') renderPdf(file, target, generation);
  else if(file.kind==='video') { const media=Object.assign(document.createElement('video'),{src:file.content_url,controls:true,playsInline:true}); target.append(media); }
  else if(file.kind==='audio') { const media=Object.assign(document.createElement('audio'),{src:file.content_url,controls:true}); target.append(media); }
  else if(file.text_preview) { const pre=document.createElement('pre'); pre.textContent='Opening…'; target.append(pre); api(`/api/files/${file.id}/text`).then(data=>pre.textContent=data.text).catch(error=>pre.textContent=error.message); }
  else { const message=document.createElement('div'); message.className='unsupported-preview'; message.innerHTML='<strong>Preview isn’t available for this file yet.</strong><p>You can download it and open it in the appropriate app.</p>'; target.append(message); }
  dialog.showModal();
}
function pdfDelay(signal, duration = 1000) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('Preview closed', 'AbortError')); return; }
    const abort = () => { clearTimeout(timer); reject(new DOMException('Preview closed', 'AbortError')); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, duration);
    signal.addEventListener('abort', abort, {once:true});
  });
}
async function waitForPdf(url, signal, retry = false) {
  const deadline = Date.now() + 150000;
  while (!signal.aborted && Date.now() < deadline) {
    let data;
    try { data = await api(url + (retry ? (url.includes('?') ? '&' : '?') + 'retry=1' : ''), {signal}); }
    catch (error) { if (error.status !== 503) throw error; }
    retry = false;
    if (data?.state === 'ready' || data?.pages) return data;
    await pdfDelay(signal);
  }
  if (signal.aborted) throw new DOMException('Preview closed', 'AbortError');
  throw new Error('Preview is still being prepared. Try again shortly or download the original.');
}
async function renderPdf(file, target, generation) {
  const controller = new AbortController(); let requestNumber = 0, pageController = null;
  activePdf = {destroy:async()=>{requestNumber++; controller.abort(); pageController?.abort();}};
  const loading=document.createElement('p');loading.className='pdf-loading';loading.textContent='Opening PDF…';target.append(loading);
  try {
    const details=await waitForPdf(`/api/files/${file.id}/pdf`, controller.signal, true);
    if(generation!==previewGeneration||!document.querySelector('#fileViewer').open)return;
    loading.remove();
    const reader=document.createElement('section');reader.className='pdf-reader';
    const controls=document.createElement('nav');controls.className='pdf-controls';
    const previous=document.createElement('button');previous.textContent='←';previous.setAttribute('aria-label','Previous page');
    const pageInput=document.createElement('input');pageInput.type='number';pageInput.min='1';pageInput.max=String(details.pages);pageInput.value='1';pageInput.inputMode='numeric';pageInput.setAttribute('aria-label','Page number');
    const total=document.createElement('span');total.textContent=`of ${details.pages}`;
    const next=document.createElement('button');next.textContent='→';next.setAttribute('aria-label','Next page');
    const zoomOut=document.createElement('button');zoomOut.textContent='−';zoomOut.setAttribute('aria-label','Zoom out');
    const zoomIn=document.createElement('button');zoomIn.textContent='+';zoomIn.setAttribute('aria-label','Zoom in');
    controls.append(previous,pageInput,total,next,zoomOut,zoomIn);
    const stage=document.createElement('div');stage.className='pdf-stage';
    reader.append(controls,stage);target.append(reader);
    let pageNumber=1,zoom=1,pinchStart=0,pinchStartZoom=1;
    // Fit-width pages use the smaller cache tier. Only the page the reader
    // actually enlarges is upgraded, so opening a document does not schedule
    // three expensive 2000px Poppler renders.
    const fitRenderWidth=1200,zoomRenderWidth=2000;
    const prefetchedPages=new Set(),highResolutionRequests=new Set();
    function prefetch(page) {
      if(page<1||page>details.pages||prefetchedPages.has(page))return;
      prefetchedPages.add(page);
      api(`/api/files/${file.id}/pdf/pages/${page}?width=${fitRenderWidth}&status=1&prefetch=1`, {signal:controller.signal}).catch(()=>prefetchedPages.delete(page));
    }
    function upgradeCurrentPage(request,page) {
      const key=`${request}:${page}`;if(highResolutionRequests.has(key))return;
      const surface=stage.querySelector('.pdf-page-surface'),current=surface?.querySelector('img');if(!surface||!current)return;
      highResolutionRequests.add(key);const image=new Image();image.alt=current.alt;image.decoding='async';
      image.onload=()=>{if(request!==requestNumber||page!==pageNumber||!surface.isConnected)return;surface.replaceChildren(image);};
      image.onerror=()=>highResolutionRequests.delete(key);
      waitForPdf(`/api/files/${file.id}/pdf/pages/${page}?width=${zoomRenderWidth}&status=1`, pageController.signal)
        .then(data=>{if(request===requestNumber&&!controller.signal.aborted)image.src=data.url;})
        .catch(()=>highResolutionRequests.delete(key));
    }
    function draw() {
      const request=++requestNumber;
      pageController?.abort(); pageController=new AbortController();
      previous.disabled=pageNumber===1;next.disabled=pageNumber===details.pages;pageInput.value=String(pageNumber);
      zoomOut.disabled=zoom<=1;zoomIn.disabled=zoom>=4;pageInput.disabled=false;
      const waiting=document.createElement('p');waiting.className='pdf-loading';waiting.textContent=`Rendering page ${pageNumber}…`;stage.replaceChildren(waiting);
      const image=new Image();image.alt=`Page ${pageNumber} of ${details.pages}`;image.decoding='async';
      image.onload=()=>{if(request!==requestNumber)return;const surface=document.createElement('div');surface.className='pdf-page-surface';surface.append(image);stage.replaceChildren(surface);applyZoom(null,false);stage.scrollTo({top:0,left:0});prefetch(pageNumber+1);if(pageNumber>1)prefetch(pageNumber-1);};
      image.onerror=()=>{if(request!==requestNumber)return;const failure=document.createElement('div');failure.className='pdf-page-error';const copy=document.createElement('p');copy.textContent='This page could not be displayed. The original PDF is still safe.';const retry=document.createElement('button');retry.type='button';retry.textContent='Try this page again';retry.addEventListener('click',draw);failure.append(copy,retry);stage.replaceChildren(failure);};
      waitForPdf(`/api/files/${file.id}/pdf/pages/${pageNumber}?width=${fitRenderWidth}&status=1`, pageController.signal, true)
        .then(data=>{if(request===requestNumber&&!controller.signal.aborted)image.src=data.url;})
        .catch(error=>{if(error.name!=='AbortError'&&request===requestNumber)image.onerror();});
    }
    previous.addEventListener('click',()=>{if(pageNumber>1){pageNumber--;draw();}});
    next.addEventListener('click',()=>{if(pageNumber<details.pages){pageNumber++;draw();}});
    pageInput.addEventListener('change',()=>{pageNumber=Math.min(details.pages,Math.max(1,Number(pageInput.value)||1));draw();});
    function applyZoom(nextZoom,keepCenter=true,anchorX=stage.clientWidth/2,anchorY=stage.clientHeight/2){
      const surface=stage.querySelector('.pdf-page-surface');if(!surface)return;
      const oldZoom=zoom;if(nextZoom!==null)zoom=Math.max(1,Math.min(4,nextZoom));
      const fitWidth=Math.max(1,stage.clientWidth-20),contentX=stage.scrollLeft+anchorX,contentY=stage.scrollTop+anchorY;
      surface.style.width=`${Math.round(fitWidth*zoom)}px`;
      if(keepCenter&&oldZoom>0){const ratio=zoom/oldZoom;stage.scrollLeft=Math.max(0,contentX*ratio-anchorX);stage.scrollTop=Math.max(0,contentY*ratio-anchorY);}
      zoomOut.disabled=zoom<=1;zoomIn.disabled=zoom>=4;
      if(zoom>1.5)upgradeCurrentPage(requestNumber,pageNumber);
    }
    zoomOut.addEventListener('click',()=>applyZoom(zoom-.5));
    zoomIn.addEventListener('click',()=>applyZoom(zoom+.5));
    const touchDistance=touches=>Math.hypot(touches[0].clientX-touches[1].clientX,touches[0].clientY-touches[1].clientY);
    let pdfTouchX=null,pdfTouchY=null,lastPanX=null,lastPanY=null;
    stage.addEventListener('touchstart',event=>{if(event.touches.length===2){pinchStart=touchDistance(event.touches);pinchStartZoom=zoom;pdfTouchX=null;pdfTouchY=null;lastPanX=null;lastPanY=null;return;}const touch=event.touches[0];pdfTouchX=touch?.clientX??null;pdfTouchY=touch?.clientY??null;lastPanX=pdfTouchX;lastPanY=pdfTouchY;},{passive:true});
    stage.addEventListener('touchmove',event=>{if(event.touches.length===2&&pinchStart){event.preventDefault();const midpointX=(event.touches[0].clientX+event.touches[1].clientX)/2-stage.getBoundingClientRect().left;const midpointY=(event.touches[0].clientY+event.touches[1].clientY)/2-stage.getBoundingClientRect().top;applyZoom(pinchStartZoom*(touchDistance(event.touches)/pinchStart),true,midpointX,midpointY);return;}if(event.touches.length===1&&zoom>1&&lastPanX!==null){event.preventDefault();const touch=event.touches[0];stage.scrollLeft-=touch.clientX-lastPanX;stage.scrollTop-=touch.clientY-lastPanY;lastPanX=touch.clientX;lastPanY=touch.clientY;}},{passive:false});
    stage.addEventListener('touchcancel',()=>{pinchStart=0;pdfTouchX=null;pdfTouchY=null;lastPanX=null;lastPanY=null;},{passive:true});
    stage.addEventListener('touchend',event=>{pinchStart=0;lastPanX=null;lastPanY=null;if(pdfTouchX===null||pdfTouchY===null)return;const touch=event.changedTouches[0],dx=(touch?.clientX??pdfTouchX)-pdfTouchX,dy=(touch?.clientY??pdfTouchY)-pdfTouchY;pdfTouchX=null;pdfTouchY=null;if(zoom>1.05||Math.abs(dx)<55||Math.abs(dx)<Math.abs(dy)*1.35)return;event.stopPropagation();if(dx>0&&pageNumber>1){pageNumber--;draw();}else if(dx<0&&pageNumber<details.pages){pageNumber++;draw();}},{passive:true});
    draw();
  } catch(error) {
    if(error.name==='AbortError'||generation!==previewGeneration)return;
    loading.textContent=error.message||'This PDF could not be displayed. You can still download the original.';
    const retry=document.createElement('button');retry.type='button';retry.textContent='Try opening again';retry.addEventListener('click',()=>previewFile(file));target.append(retry);
  }
}
function askAction(file, permanent=false) {
  selectedAction={file,permanent}; document.querySelector('#fileConfirmTitle').textContent=permanent?'Delete permanently?':'Move to Recently Deleted?';
  document.querySelector('#fileConfirmCopy').textContent=permanent?'Deletion stays paused until a protected backup newer than this trash action is verified.':'You can restore it later.';
  document.querySelector('#acceptFileAction').textContent=permanent?'Request deletion':'Move to Recently Deleted';
  document.querySelector('#fileConfirm').showModal();
}
function fileCard(file) {
  const card=document.createElement('article'); card.className='file-card';
  const open=document.createElement('button'); open.className='file-open'; open.type='button';
  const icon=document.createElement('span'); icon.className=`file-icon ${file.kind}`; icon.textContent=icons[file.kind]||'FILE';
  const copy=document.createElement('span'), title=document.createElement('strong'), meta=document.createElement('small');
  title.textContent=file.name; title.title=file.name; meta.textContent=`${file.visibility==='private'?'Only me · ':''}Added by ${file.owner_display} · ${formatSize(file.byte_size)} · ${new Date(file.updated_at).toLocaleDateString()}`; copy.append(title,meta); open.append(icon,copy);
  open.setAttribute('aria-label',`${file.viewable?'Open':'Download'} ${file.name}`);
  open.addEventListener('click',()=>file.viewable?previewFile(file):window.location.assign(file.download_url));
  const actions=document.createElement('div'); actions.className='file-actions';
  if(deletedView) {
    if(file.can_edit){const restore=document.createElement('button'); restore.textContent='Restore'; restore.addEventListener('click',()=>runFileAction(restore,async()=>{await api(`/api/files/${file.id}/restore`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:file.version})});showToast(`${file.name} restored.`);await loadFiles();}));
    const purge=document.createElement('button'); purge.className='danger-text'; purge.textContent='Delete'; purge.addEventListener('click',()=>askAction(file,true)); actions.append(restore,purge);}
  } else {
    const download=document.createElement('a'); download.href=file.download_url; download.textContent='Download'; download.setAttribute('download','');
    actions.append(download);
    if(file.can_edit){const privacy=document.createElement('button'); privacy.textContent=file.visibility==='private'?'Share':'Only me'; privacy.addEventListener('click',()=>runFileAction(privacy,async()=>{await api(`/api/files/${file.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({visibility:file.visibility==='private'?'shared':'private',version:file.version})});showToast(file.visibility==='private'?'File shared.':'File is now private.');await loadFiles();}));
    const remove=document.createElement('button'); remove.className='danger-text'; remove.textContent='Delete'; remove.addEventListener('click',()=>askAction(file)); actions.append(privacy,remove);}
  }
  card.append(open,actions); return card;
}
function folderCard(folder) {
  const button=document.createElement('button'); button.className='folder-card'; button.type='button';
  button.innerHTML='<span class="folder-icon">⌑</span><span></span><b>→</b>'; button.children[1].textContent=folder.name; button.children[1].title=folder.name;
  button.setAttribute('aria-label',`${folder.name}, added by ${folder.owner_display}`);button.addEventListener('click',()=>{folderId=folder.id; deletedView=false; document.querySelector('#fileSearch').value=''; loadFiles();});
  return button;
}
function renderBreadcrumbs(items) {
  const nav=document.querySelector('#fileBreadcrumbs'); nav.innerHTML='';
  const home=document.createElement('button'); home.textContent='Files'; home.addEventListener('click',()=>{folderId=null;loadFiles();}); nav.append(home);
  items.forEach(item=>{const sep=document.createElement('span');sep.textContent='›';sep.setAttribute('aria-hidden','true');const button=document.createElement('button');button.textContent=item.name;button.title=item.name;button.addEventListener('click',()=>{folderId=item.id;loadFiles();});nav.append(sep,button);});
  const current=nav.querySelector('button:last-of-type');if(current)current.setAttribute('aria-current','page');
  requestAnimationFrame(()=>{nav.scrollLeft=nav.scrollWidth;});
}
function updateEmptyState(hasResults,query) {
  empty.hidden=hasResults;
  const title=document.querySelector('#filesEmptyTitle'),copy=document.querySelector('#filesEmptyCopy'),add=document.querySelector('#emptyAddFile');
  if(hasResults)return;
  if(query){title.textContent='No matching files.';copy.textContent='Try a different search or clear one of the filters.';add.hidden=true;}
  else if(deletedView){title.textContent='Recently Deleted is empty.';copy.textContent='Files moved here will be available to restore before they are permanently removed.';add.hidden=true;}
  else if(kind||ownerView){title.textContent='No files in this view.';copy.textContent='Try another file type or ownership filter.';add.hidden=true;}
  else{title.textContent='Nothing here yet.';copy.textContent='Upload a document, photo, PDF, or anything else you want to keep.';add.hidden=false;}
}
async function loadFiles({append=false}={}) {
  if(append&&(!fileHasMore||fileAppending))return;
  if(!append){fileOffset=0;fileTotal=0;fileHasMore=false;if(loadController)loadController.abort();loadController=new AbortController();loadGeneration+=1;}
  const generation=loadGeneration;
  if(append)fileAppending=true;
  const loading=document.querySelector('#filesLoading'),failure=document.querySelector('#filesError');
  if(!append){loading.hidden=false;failure.hidden=true;empty.hidden=true;grid.hidden=true;fileResultCount.hidden=true;loadMoreFiles.hidden=true;}
  grid.setAttribute('aria-busy','true');loadMoreFiles.disabled=true;if(append)loadMoreFiles.textContent='Loading…';
  const params=new URLSearchParams({folder:folderId||'',q:document.querySelector('#fileSearch').value,kind,view:deletedView?'deleted':'',owner:ownerView,limit:String(FILE_PAGE_SIZE),offset:String(fileOffset),summary:append?'0':'1'});
  try {
    const data=await api(`/api/files?${params}`,{signal:loadController.signal});if(generation!==loadGeneration)return;
    if(!append){grid.replaceChildren();listedFileIds.clear();renderBreadcrumbs(data.breadcrumbs);visibleFiles=[];fileTotal=Number(data.total||0);document.querySelector('#openUpload').disabled=!data.can_add_here;document.querySelector('#newFolder').disabled=!data.can_add_here;}
    const fragment=document.createDocumentFragment();
    if(!append)data.folders.forEach(folder=>fragment.append(folderCard(folder)));
    data.files.forEach(file=>{if(listedFileIds.has(file.id))return;listedFileIds.add(file.id);fragment.append(fileCard(file));if(file.viewable)visibleFiles.push(file);});grid.append(fragment);
    fileOffset=data.next_offset??(fileOffset+data.files.length);fileHasMore=Boolean(data.has_more);
    fileResultCount.textContent=fileHasMore?`${listedFileIds.size.toLocaleString()} of ${fileTotal.toLocaleString()} files`:`${fileTotal.toLocaleString()} ${fileTotal===1?'file':'files'}`;
    fileResultCount.hidden=false;loadMoreFiles.hidden=!fileHasMore;
    updateEmptyState(grid.childElementCount!==0,document.querySelector('#fileSearch').value.trim());
    grid.hidden=false;
  } catch(error){
    if(error.name==='AbortError'||generation!==loadGeneration)return;
    if(append&&fileOffset){showToast(`${error.message} Existing files remain available.`);}
    else{visibleFiles=[];grid.replaceChildren();document.querySelector('#filesErrorCopy').textContent=`${error.message} Your saved files were not changed.`;failure.hidden=false;showToast(error.message);}
  } finally {
    if(generation===loadGeneration){loading.hidden=true;grid.setAttribute('aria-busy','false');fileAppending=false;loadMoreFiles.disabled=false;loadMoreFiles.textContent='Load more files';}
  }
}
async function runFileAction(button,action){button.disabled=true;try{await action();}catch(error){showToast(error.message);}finally{button.disabled=false;}}
function openUpload(){if(document.querySelector('#openUpload').disabled)return;document.querySelector('#uploadMessage').textContent='';document.querySelector('#uploadSheet').showModal();}
const uploadStorageKey = `david-pi:files-upload:${document.querySelector('meta[name="files-upload-scope"]').content}`;
let uploadKey = '', pendingUploadId = null, pendingUploadState = '', uploadPoll = null, uploadRefreshing = false, uploadSelectionGeneration = 0;
try { uploadKey = sessionStorage.getItem(uploadStorageKey) || ''; } catch (_) {}
function rememberUploadKey(key) { uploadKey = key; try { if(key)sessionStorage.setItem(uploadStorageKey,key);else sessionStorage.removeItem(uploadStorageKey); } catch (_) {} }
function acceptUploadState(data) {
  const message=document.querySelector('#uploadMessage');
  pendingUploadState=data.state;
  document.querySelector('#startFileUpload').textContent=data.state==='completed'?'Upload files':data.state==='failed'?'Retry finishing':'Finishing…';
  if(data.state==='completed') {
    const count=Array.isArray(data.added)?data.added.length:0;
    message.textContent=`${count} file${count===1?'':'s'} uploaded.`;showToast(message.textContent);
    pendingUploadId=null;rememberUploadKey('');document.querySelector('#fileInput').value='';document.querySelector('#selectedFiles').textContent='';
    document.querySelector('#startFileUpload').disabled=false;loadFiles();
  } else {
    pendingUploadId=data.upload_id;message.textContent=data.error||(data.state==='processing'?'Finishing your upload…':'Upload received. Files are queued and will finish automatically.');
    document.querySelector('#startFileUpload').disabled=data.state!=='failed';
  }
}
async function refreshUploads() {
  if(uploadRefreshing)return;uploadRefreshing=true;clearTimeout(uploadPoll);
  try {
    if(uploadKey&&!pendingUploadId){
      const key=uploadKey,generation=uploadSelectionGeneration;
      try{const received=await api('/api/files/uploads?key='+encodeURIComponent(key));if(key===uploadKey&&generation===uploadSelectionGeneration)acceptUploadState(received);}catch(error){if(error.status!==404)throw error;}
    }
    const data=await api('/api/files/uploads'), rows=data.uploads||[], area=document.querySelector('#fileUploadStatuses');area.replaceChildren();
    if(pendingUploadId&&!rows.some(item=>item.upload_id===pendingUploadId)){
      const receipt=pendingUploadId;
      try{const received=await api('/api/files/uploads/'+encodeURIComponent(receipt));if(receipt===pendingUploadId)acceptUploadState(received);}
      catch(error){if(error.status!==404)throw error;if(receipt===pendingUploadId){pendingUploadId=null;pendingUploadState='not_received';document.querySelector('#startFileUpload').disabled=false;document.querySelector('#startFileUpload').textContent='Upload files';document.querySelector('#uploadMessage').textContent='Upload not received yet. Retry with the same selected files.';}}
    }
    const pending=rows.filter(item=>['queued','processing','checking'].includes(item.state));
    const failed=rows.filter(item=>item.state==='failed');
    const panel=document.querySelector('#fileUploadActivity');panel.hidden=!rows.length;if(pending.length||failed.length)panel.open=true;
    document.querySelector('#fileUploadSummary').textContent=pending.length?`${pending.length} upload${pending.length===1?'':'s'} finishing`:failed.length?`${failed.length} upload${failed.length===1?' needs':'s need'} attention`:'Recent uploads';
    for(const item of rows){
      const row=document.createElement('p'),label=document.createElement('span');
      label.textContent=item.state==='completed'?`${item.added.length} file${item.added.length===1?'':'s'} uploaded${item.added.length?' · '+item.added.join(', '):''}`:item.error||(item.state==='processing'?'Finishing upload…':'Upload queued');row.append(label);
      if(item.state==='failed'){const retry=document.createElement('button');retry.type='button';retry.textContent='Retry finishing';retry.addEventListener('click',async()=>{retry.disabled=true;try{await api('/api/files/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({upload_id:item.upload_id})});refreshUploads();}catch(error){document.querySelector('#fileUploadCheckMessage').textContent=error.message;retry.disabled=false;}});row.append(retry);}
      area.append(row);if(item.upload_id===pendingUploadId)acceptUploadState(item);
    }
    document.querySelector('#fileUploadCheckMessage').textContent='';
    if((pending.length||['queued','processing','checking'].includes(pendingUploadState))&&document.visibilityState!=='hidden')uploadPoll=setTimeout(refreshUploads,3000);
  } catch(error){document.querySelector('#fileUploadCheckMessage').textContent=`Upload status could not be checked. ${error.message}`;if(document.visibilityState!=='hidden')uploadPoll=setTimeout(refreshUploads,10000);}
  finally {uploadRefreshing=false;}
}
document.querySelector('#openUpload').addEventListener('click',openUpload); document.querySelector('#emptyAddFile').addEventListener('click',openUpload);
document.querySelector('#closeUpload').addEventListener('click',()=>document.querySelector('#uploadSheet').close());
document.querySelector('#fileInput').addEventListener('change',event=>{uploadSelectionGeneration++;pendingUploadId=null;pendingUploadState='';rememberUploadKey(crypto.randomUUID());document.querySelector('#startFileUpload').disabled=false;document.querySelector('#startFileUpload').textContent='Upload files';const files=[...event.target.files];document.querySelector('#selectedFiles').textContent=files.length?`${files.length} selected · ${formatSize(files.reduce((sum,file)=>sum+file.size,0))}`:'';});
document.querySelector('#startFileUpload').addEventListener('click',async()=>{
  const input=document.querySelector('#fileInput'),button=document.querySelector('#startFileUpload'),message=document.querySelector('#uploadMessage');
  if(pendingUploadState==='failed'&&pendingUploadId){button.disabled=true;try{acceptUploadState(await api('/api/files/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({upload_id:pendingUploadId})}));refreshUploads();}catch(error){message.textContent=error.message;button.disabled=false;}return;}
  if(!input.files.length){message.textContent='Choose at least one file.';return;} const body=new FormData();[...input.files].forEach(file=>body.append('files',file));if(folderId)body.append('folder_id',folderId);body.append('visibility',document.querySelector('#fileVisibility').value);
  button.disabled=true;button.textContent='Uploading…';message.textContent='Keep this page open while the upload finishes.';
  if(!uploadKey)rememberUploadKey(crypto.randomUUID());
  const key=uploadKey,generation=uploadSelectionGeneration;input.disabled=true;document.querySelector('#fileVisibility').disabled=true;
  try{const data=await api('/api/files/upload',{method:'POST',headers:{'Idempotency-Key':key},body});if(generation===uploadSelectionGeneration)acceptUploadState(data);refreshUploads();}
  catch(error){
    if(generation!==uploadSelectionGeneration)return;
    message.textContent=`${error.message} Checking whether the upload arrived…`;
    try{const received=await api('/api/files/uploads?key='+encodeURIComponent(key));if(generation===uploadSelectionGeneration)acceptUploadState(received);refreshUploads();}
    catch(_){if(generation===uploadSelectionGeneration)message.textContent='The upload could not be confirmed. Keep these files selected and try again; the same upload receipt prevents duplicates.';}
  }finally{input.disabled=false;document.querySelector('#fileVisibility').disabled=false;button.disabled=Boolean(pendingUploadId)&&pendingUploadState!=='failed';button.textContent=pendingUploadState==='failed'?'Retry finishing':pendingUploadId?'Finishing…':'Upload files';}
});
function openFolder(){document.querySelector('#folderName').value='';document.querySelector('#folderMessage').textContent='';document.querySelector('#folderSheet').showModal();setTimeout(()=>document.querySelector('#folderName').focus(),50);}
document.querySelector('#newFolder').addEventListener('click',openFolder);document.querySelector('#closeFolder').addEventListener('click',()=>document.querySelector('#folderSheet').close());document.querySelector('#cancelFolder').addEventListener('click',()=>document.querySelector('#folderSheet').close());
document.querySelector('#createFolder').addEventListener('click',async()=>{const button=document.querySelector('#createFolder');button.disabled=true;try{await api('/api/files/folders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.querySelector('#folderName').value,parent_id:folderId})});document.querySelector('#folderSheet').close();showToast('Folder created.');loadFiles();}catch(error){document.querySelector('#folderMessage').textContent=error.message;}finally{button.disabled=false;}});
function selectChip(nav,button){nav.querySelectorAll('button').forEach(item=>{const selected=item===button;item.classList.toggle('selected',selected);item.setAttribute('aria-pressed',String(selected));});}
document.querySelector('#fileKinds').addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;deletedView=button.dataset.view==='deleted';kind=button.dataset.kind||'';selectChip(document.querySelector('#fileKinds'),button);loadFiles();});
document.querySelector('#fileOwners').addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;ownerView=button.dataset.owner||'';selectChip(document.querySelector('#fileOwners'),button);loadFiles();});
document.querySelector('#retryFiles').addEventListener('click',loadFiles);
loadMoreFiles.addEventListener('click',()=>loadFiles({append:true}));
document.querySelector('#fileSearch').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadFiles,250);});
async function movePreviewFile(direction){const wanted=activeFileIndex+direction;if(wanted<0||wanted>=visibleFiles.length)return;previewFile(visibleFiles[wanted]);}
document.querySelector('#fileViewer').addEventListener('keydown',event=>{if(event.target.closest('.pdf-reader,input,video,audio'))return;if(event.key==='ArrowLeft'){event.preventDefault();movePreviewFile(-1);}else if(event.key==='ArrowRight'){event.preventDefault();movePreviewFile(1);}});
document.querySelector('#filePreview').addEventListener('touchstart',event=>{if(event.target.closest('button,a,input,video,audio')){fileTouchStartX=null;fileTouchStartY=null;return;}const touch=event.changedTouches[0];fileTouchStartX=touch?.clientX??null;fileTouchStartY=touch?.clientY??null;},{passive:true});
document.querySelector('#filePreview').addEventListener('touchend',event=>{if(event.target.closest('.pdf-stage')||fileTouchStartX===null||fileTouchStartY===null)return;const touch=event.changedTouches[0],dx=(touch?.clientX??fileTouchStartX)-fileTouchStartX,dy=(touch?.clientY??fileTouchStartY)-fileTouchStartY;fileTouchStartX=null;fileTouchStartY=null;if(Math.abs(dx)<55||Math.abs(dx)<Math.abs(dy)*1.35)return;movePreviewFile(dx>0?-1:1);},{passive:true});
document.querySelector('#closeFileViewer').addEventListener('click',()=>document.querySelector('#fileViewer').close());
document.querySelector('#fileViewer').addEventListener('close',stopFilePreview);
document.querySelector('#fileViewer').addEventListener('cancel',stopFilePreview);
window.addEventListener('pagehide',()=>{stopFilePreview();clearTimeout(uploadPoll);});
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'){loadFiles();refreshUploads();}else clearTimeout(uploadPoll);});
window.addEventListener('pageshow',event=>{if(event.persisted){loadFiles();refreshUploads();}});
document.querySelector('#cancelFileAction').addEventListener('click',()=>{document.querySelector('#fileConfirm').close();selectedAction=null;});
document.querySelector('#acceptFileAction').addEventListener('click',async()=>{const button=document.querySelector('#acceptFileAction');button.disabled=true;try{if(selectedAction.permanent)await api(`/api/files/${selectedAction.file.id}/purge`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:'permanently delete',version:selectedAction.file.version})});else await api(`/api/files/${selectedAction.file.id}`,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:selectedAction.file.version})});document.querySelector('#fileConfirm').close();showToast(selectedAction.permanent?'File permanently deleted.':'File moved to Recently Deleted.');selectedAction=null;loadFiles();}catch(error){showToast(error.message);}finally{button.disabled=false;}});
document.querySelectorAll('#fileKinds,#fileOwners').forEach(nav=>{const selected=nav.querySelector('.selected');if(selected)selectChip(nav,selected);});
loadFiles();
refreshUploads();

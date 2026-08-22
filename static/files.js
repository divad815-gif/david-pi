const csrf = document.querySelector('meta[name="csrf-token"]').content;
const grid = document.querySelector('#fileGrid'), empty = document.querySelector('#filesEmpty'), toast = document.querySelector('#platformToast');
let folderId = null, kind = '', ownerView = '', deletedView = false, selectedAction = null, searchTimer;
let activePdf = null;
let visibleFiles = [], activeFileIndex = -1, fileTouchStartX = null, fileTouchStartY = null, previewGeneration = 0;
async function api(url, options = {}) {
  options.headers = {...options.headers, 'X-CSRF-Token': csrf};
  const response = await fetch(url, options), data = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(data.error || 'Something went wrong.'); error.data = data; throw error; }
  return data;
}
function showToast(text) { toast.textContent = text; toast.hidden = false; clearTimeout(showToast.timer); showToast.timer = setTimeout(() => toast.hidden = true, 3200); }
function formatSize(bytes) { if (bytes < 1024) return `${bytes} B`; const units = ['KB','MB','GB','TB']; let value=bytes/1024, unit=0; while(value>=1024 && unit<units.length-1){value/=1024;unit++;} return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`; }
const icons = {pdf:'PDF', image:'▧', video:'▶', audio:'♪', text:'TXT', document:'DOC', spreadsheet:'XLS', presentation:'PPT', other:'FILE'};
function previewFile(file) {
  const dialog=document.querySelector('#fileViewer'), target=document.querySelector('#filePreview');
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
async function renderPdf(file, target, generation) {
  const loading=document.createElement('p');loading.className='pdf-loading';loading.textContent='Opening PDF…';target.append(loading);
  try {
    const details=await api(`/api/files/${file.id}/pdf`);
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
    let pageNumber=1,zoom=1,requestNumber=0,pinchStart=0,pinchStartZoom=1;
    // The 2000px cache tier remains readable at roughly 4x phone zoom while
    // preserving the page aspect ratio. Zoom changes the page surface in real
    // pixels; it never stretches only one image dimension.
    const renderWidth=2000;
    const prefetchedPages=new Set();
    function prefetch(page) {
      if(page<1||page>details.pages||prefetchedPages.has(page))return;
      prefetchedPages.add(page);
      const warm=new Image();warm.decoding='async';warm.src=`/api/files/${file.id}/pdf/pages/${page}?width=${renderWidth}`;
    }
    function draw() {
      const request=++requestNumber;
      previous.disabled=pageNumber===1;next.disabled=pageNumber===details.pages;pageInput.value=String(pageNumber);
      zoomOut.disabled=zoom<=1;zoomIn.disabled=zoom>=4;pageInput.disabled=false;
      const waiting=document.createElement('p');waiting.className='pdf-loading';waiting.textContent=`Rendering page ${pageNumber}…`;stage.replaceChildren(waiting);
      const image=new Image();image.alt=`Page ${pageNumber} of ${details.pages}`;image.decoding='async';
      image.onload=()=>{if(request!==requestNumber)return;const surface=document.createElement('div');surface.className='pdf-page-surface';surface.append(image);stage.replaceChildren(surface);applyZoom(null,false);stage.scrollTo({top:0,left:0});prefetch(pageNumber+1);if(pageNumber>1)prefetch(pageNumber-1);};
      image.onerror=()=>{if(request!==requestNumber)return;const failure=document.createElement('div');failure.className='pdf-page-error';const copy=document.createElement('p');copy.textContent='This page could not be displayed. The original PDF is still safe.';const retry=document.createElement('button');retry.type='button';retry.textContent='Try this page again';retry.addEventListener('click',draw);failure.append(copy,retry);stage.replaceChildren(failure);};
      image.src=`/api/files/${file.id}/pdf/pages/${pageNumber}?width=${renderWidth}`;
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
    }
    zoomOut.addEventListener('click',()=>applyZoom(zoom-.5));
    zoomIn.addEventListener('click',()=>applyZoom(zoom+.5));
    const touchDistance=touches=>Math.hypot(touches[0].clientX-touches[1].clientX,touches[0].clientY-touches[1].clientY);
    let pdfTouchX=null,pdfTouchY=null,lastPanX=null,lastPanY=null;
    stage.addEventListener('touchstart',event=>{if(event.touches.length===2){pinchStart=touchDistance(event.touches);pinchStartZoom=zoom;pdfTouchX=null;pdfTouchY=null;lastPanX=null;lastPanY=null;return;}const touch=event.touches[0];pdfTouchX=touch?.clientX??null;pdfTouchY=touch?.clientY??null;lastPanX=pdfTouchX;lastPanY=pdfTouchY;},{passive:true});
    stage.addEventListener('touchmove',event=>{if(event.touches.length===2&&pinchStart){event.preventDefault();const midpointX=(event.touches[0].clientX+event.touches[1].clientX)/2-stage.getBoundingClientRect().left;const midpointY=(event.touches[0].clientY+event.touches[1].clientY)/2-stage.getBoundingClientRect().top;applyZoom(pinchStartZoom*(touchDistance(event.touches)/pinchStart),true,midpointX,midpointY);return;}if(event.touches.length===1&&zoom>1&&lastPanX!==null){event.preventDefault();const touch=event.touches[0];stage.scrollLeft-=touch.clientX-lastPanX;stage.scrollTop-=touch.clientY-lastPanY;lastPanX=touch.clientX;lastPanY=touch.clientY;}},{passive:false});
    stage.addEventListener('touchcancel',()=>{pinchStart=0;pdfTouchX=null;pdfTouchY=null;lastPanX=null;lastPanY=null;},{passive:true});
    stage.addEventListener('touchend',event=>{pinchStart=0;lastPanX=null;lastPanY=null;if(pdfTouchX===null||pdfTouchY===null)return;const touch=event.changedTouches[0],dx=(touch?.clientX??pdfTouchX)-pdfTouchX,dy=(touch?.clientY??pdfTouchY)-pdfTouchY;pdfTouchX=null;pdfTouchY=null;if(zoom>1.05||Math.abs(dx)<55||Math.abs(dx)<Math.abs(dy)*1.35)return;event.stopPropagation();if(dx>0&&pageNumber>1){pageNumber--;draw();}else if(dx<0&&pageNumber<details.pages){pageNumber++;draw();}},{passive:true});
    activePdf={destroy:async()=>{requestNumber++;}};
    draw();
  } catch(error) {
    loading.textContent='This PDF could not be displayed. You can still download the original.';
    console.error(error);
  }
}
function askAction(file, permanent=false) {
  selectedAction={file,permanent}; document.querySelector('#fileConfirmTitle').textContent=permanent?'Delete permanently?':'Move to Recently Deleted?';
  document.querySelector('#fileConfirmCopy').textContent=permanent?'This cannot be undone.':'You can restore it later.';
  document.querySelector('#acceptFileAction').textContent=permanent?'Delete permanently':'Move to Recently Deleted';
  document.querySelector('#fileConfirm').showModal();
}
function fileCard(file) {
  const card=document.createElement('article'); card.className='file-card';
  const open=document.createElement('button'); open.className='file-open'; open.type='button';
  const icon=document.createElement('span'); icon.className=`file-icon ${file.kind}`; icon.textContent=icons[file.kind]||'FILE';
  const copy=document.createElement('span'), title=document.createElement('strong'), meta=document.createElement('small');
  title.textContent=file.name; meta.textContent=`${file.visibility==='private'?'Only me · ':''}Added by ${file.owner_display} · ${formatSize(file.byte_size)} · ${new Date(file.updated_at).toLocaleDateString()}`; copy.append(title,meta); open.append(icon,copy);
  open.addEventListener('click',()=>file.viewable?previewFile(file):window.location.assign(file.download_url));
  const actions=document.createElement('div'); actions.className='file-actions';
  if(deletedView) {
    const restore=document.createElement('button'); restore.textContent='Restore'; restore.addEventListener('click',async()=>{await api(`/api/files/${file.id}/restore`,{method:'POST'});showToast(`${file.name} restored.`);loadFiles();});
    const purge=document.createElement('button'); purge.className='danger-text'; purge.textContent='Delete'; purge.addEventListener('click',()=>askAction(file,true)); actions.append(restore,purge);
  } else {
    const download=document.createElement('a'); download.href=file.download_url; download.textContent='Download'; download.setAttribute('download','');
    const privacy=document.createElement('button'); privacy.textContent=file.visibility==='private'?'Share':'Only me'; privacy.addEventListener('click',async()=>{await api(`/api/files/${file.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({visibility:file.visibility==='private'?'shared':'private'})});showToast(file.visibility==='private'?'File shared.':'File is now private.');loadFiles();});
    const remove=document.createElement('button'); remove.className='danger-text'; remove.textContent='Delete'; remove.addEventListener('click',()=>askAction(file)); actions.append(download,privacy,remove);
  }
  card.append(open,actions); return card;
}
function folderCard(folder) {
  const button=document.createElement('button'); button.className='folder-card'; button.type='button';
  button.innerHTML='<span class="folder-icon">⌑</span><span></span><b>→</b>'; button.children[1].textContent=folder.name;
  button.addEventListener('click',()=>{folderId=folder.id; deletedView=false; document.querySelector('#fileSearch').value=''; loadFiles();});
  return button;
}
function renderBreadcrumbs(items) {
  const nav=document.querySelector('#fileBreadcrumbs'); nav.innerHTML='';
  const home=document.createElement('button'); home.textContent='Files'; home.addEventListener('click',()=>{folderId=null;loadFiles();}); nav.append(home);
  items.forEach(item=>{const sep=document.createElement('span');sep.textContent='›';const button=document.createElement('button');button.textContent=item.name;button.addEventListener('click',()=>{folderId=item.id;loadFiles();});nav.append(sep,button);});
}
async function loadFiles() {
  const params=new URLSearchParams({folder:folderId||'',q:document.querySelector('#fileSearch').value,kind,view:deletedView?'deleted':'',owner:ownerView});
  try {
    const data=await api(`/api/files?${params}`); grid.innerHTML=''; renderBreadcrumbs(data.breadcrumbs); visibleFiles=data.files.filter(file=>file.viewable);
    data.folders.forEach(folder=>grid.append(folderCard(folder))); data.files.forEach(file=>grid.append(fileCard(file)));
    empty.hidden=data.folders.length+data.files.length!==0;
  } catch(error){showToast(error.message);}
}
function openUpload(){document.querySelector('#uploadMessage').textContent='';document.querySelector('#uploadSheet').showModal();}
document.querySelector('#openUpload').addEventListener('click',openUpload); document.querySelector('#emptyAddFile').addEventListener('click',openUpload);
document.querySelector('#closeUpload').addEventListener('click',()=>document.querySelector('#uploadSheet').close());
document.querySelector('#fileInput').addEventListener('change',event=>{const files=[...event.target.files];document.querySelector('#selectedFiles').textContent=files.length?`${files.length} selected · ${formatSize(files.reduce((sum,file)=>sum+file.size,0))}`:'';});
document.querySelector('#startFileUpload').addEventListener('click',async()=>{
  const input=document.querySelector('#fileInput'),button=document.querySelector('#startFileUpload'),message=document.querySelector('#uploadMessage');
  if(!input.files.length){message.textContent='Choose at least one file.';return;} const body=new FormData();[...input.files].forEach(file=>body.append('files',file));if(folderId)body.append('folder_id',folderId);body.append('visibility',document.querySelector('#fileVisibility').value);
  button.disabled=true;button.textContent='Uploading…';message.textContent='Keep this page open while the upload finishes.';
  try{const data=await api('/api/files/upload',{method:'POST',body});document.querySelector('#uploadSheet').close();input.value='';document.querySelector('#selectedFiles').textContent='';showToast(`${data.added.length} file${data.added.length===1?'':'s'} uploaded.`);await loadFiles();}
  catch(error){message.textContent=`${error.message} Nothing was removed.`;}finally{button.disabled=false;button.textContent='Upload files';}
});
function openFolder(){document.querySelector('#folderName').value='';document.querySelector('#folderMessage').textContent='';document.querySelector('#folderSheet').showModal();setTimeout(()=>document.querySelector('#folderName').focus(),50);}
document.querySelector('#newFolder').addEventListener('click',openFolder);document.querySelector('#closeFolder').addEventListener('click',()=>document.querySelector('#folderSheet').close());document.querySelector('#cancelFolder').addEventListener('click',()=>document.querySelector('#folderSheet').close());
document.querySelector('#createFolder').addEventListener('click',async()=>{const button=document.querySelector('#createFolder');button.disabled=true;try{await api('/api/files/folders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.querySelector('#folderName').value,parent_id:folderId})});document.querySelector('#folderSheet').close();showToast('Folder created.');loadFiles();}catch(error){document.querySelector('#folderMessage').textContent=error.message;}finally{button.disabled=false;}});
document.querySelector('#fileKinds').addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;deletedView=button.dataset.view==='deleted';kind=button.dataset.kind||'';document.querySelectorAll('#fileKinds button').forEach(item=>item.classList.toggle('selected',item===button));loadFiles();});
document.querySelector('#fileOwners').addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;ownerView=button.dataset.owner||'';document.querySelectorAll('#fileOwners button').forEach(item=>item.classList.toggle('selected',item===button));loadFiles();});
document.querySelector('#fileSearch').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadFiles,250);});
async function movePreviewFile(direction){const wanted=activeFileIndex+direction;if(wanted<0||wanted>=visibleFiles.length)return;if(activePdf){await activePdf.destroy();activePdf=null;}previewFile(visibleFiles[wanted]);}
document.querySelector('#filePreview').addEventListener('touchstart',event=>{if(event.target.closest('button,a,input,video,audio')){fileTouchStartX=null;fileTouchStartY=null;return;}const touch=event.changedTouches[0];fileTouchStartX=touch?.clientX??null;fileTouchStartY=touch?.clientY??null;},{passive:true});
document.querySelector('#filePreview').addEventListener('touchend',event=>{if(event.target.closest('.pdf-stage')||fileTouchStartX===null||fileTouchStartY===null)return;const touch=event.changedTouches[0],dx=(touch?.clientX??fileTouchStartX)-fileTouchStartX,dy=(touch?.clientY??fileTouchStartY)-fileTouchStartY;fileTouchStartX=null;fileTouchStartY=null;if(Math.abs(dx)<55||Math.abs(dx)<Math.abs(dy)*1.35)return;movePreviewFile(dx>0?-1:1);},{passive:true});
document.querySelector('#closeFileViewer').addEventListener('click',async()=>{previewGeneration++;document.querySelector('#fileViewer').close();if(activePdf){await activePdf.destroy();activePdf=null;}});
document.querySelector('#cancelFileAction').addEventListener('click',()=>{document.querySelector('#fileConfirm').close();selectedAction=null;});
document.querySelector('#acceptFileAction').addEventListener('click',async()=>{const button=document.querySelector('#acceptFileAction');button.disabled=true;try{if(selectedAction.permanent)await api(`/api/files/${selectedAction.file.id}/purge`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:'permanently delete'})});else await api(`/api/files/${selectedAction.file.id}`,{method:'DELETE'});document.querySelector('#fileConfirm').close();showToast(selectedAction.permanent?'File permanently deleted.':'File moved to Recently Deleted.');selectedAction=null;loadFiles();}catch(error){showToast(error.message);}finally{button.disabled=false;}});
loadFiles();

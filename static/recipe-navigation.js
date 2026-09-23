(() => {
  const key = 'davidPiRecipeWeekV1';
  function monday(value) {
    const d = value && /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(value+'T12:00:00') : new Date();
    if (!Number.isFinite(d.getTime())) return monday();
    d.setDate(d.getDate()-(d.getDay()+6)%7);
    return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
  }
  let saved; try { saved=sessionStorage.getItem(key); } catch (_) {}
  let week=monday(new URLSearchParams(location.search).get('week') || saved);
  function select(value) {
    week=monday(value);
    try { sessionStorage.setItem(key,week); } catch (_) {}
    document.querySelectorAll('[data-recipe-tab]').forEach(a=>{const url=new URL(a.href);url.searchParams.set('week',week);a.href=url.pathname+url.search;});
    const url=new URL(location.href);url.searchParams.set('week',week);history.replaceState(history.state,'',url);
    return week;
  }
  window.RecipeWeek={get value(){return week;},select};
  select(week);
  const input=document.querySelector('#recipeWeek');
  if (!input) return;
  const status=document.querySelector('#recipeWeekStatus'), retry=document.querySelector('#recipeWeekRetry');
  let plan=null, busy=false, generation=0;
  async function api(options) {
    const response=await fetch('/api/recipes/weekly-plan'+(options?'':'?week='+week),options);
    const data=await response.json();
    if(!response.ok){const e=new Error(data.error||'Could not load the week.');e.data=data;throw e;}
    return data.plan;
  }
  function update(){
    document.querySelectorAll('[data-add-week]').forEach(b=>{
      const added=plan?.recipes.some(r=>r.id===b.dataset.addWeek);
      b.disabled=busy||!plan||added;
      b.textContent=added?'✓ Added to week':'Add to week';
    });
  }
  async function load(){
    const g=++generation;plan=null;input.value=select(input.value||week);update();
    const start=new Date(week+'T12:00:00'),end=new Date(start);end.setDate(end.getDate()+6);
    document.querySelector('#recipeWeekRange').textContent=start.toLocaleDateString(undefined,{month:'short',day:'numeric'})+' – '+end.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});
    status.textContent='Loading planned recipes…';retry.hidden=true;
    try { const result=await api();if(g!==generation)return;plan=result;status.textContent='Add recipes below to this shared week.'; }
    catch(e){if(g!==generation)return;status.textContent=e.message;retry.hidden=false;}
    update();
  }
  async function add(id){
    if(busy||!plan)return;busy=true;input.disabled=true;update();status.textContent='Adding recipe…';
    try {
      plan=await api({method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify({week,version:plan.version,action:'add_recipe',recipe_id:id})});
      status.textContent='Added to your shared week.';retry.hidden=true;
    } catch(e) {
      if(e.data?.plan)plan=e.data.plan;
      status.textContent=e.message+' Review the week and try again.';
      retry.hidden=false;
    } finally {busy=false;input.disabled=false;update();}
  }
  window.RecipeWeek.decorate=(recipe,open)=>{
    const card=document.createElement('article');card.className='recipe-card-container';card.append(open);
    if(!recipe.deleted_at){
      const button=document.createElement('button');button.type='button';button.className='recipe-week-add';
      if(recipe.visibility==='private'){
        button.textContent='Share recipe to add';button.title='This recipe is private. Change its sharing setting before adding it to the household week.';
        button.addEventListener('click',()=>{status.textContent=button.title;open.click();});
      }else{
        button.dataset.addWeek=recipe.id;button.textContent=plan?.recipes.some(r=>r.id===recipe.id)?'✓ Added to week':'Add to week';
        button.disabled=busy||!plan||plan.recipes.some(r=>r.id===recipe.id);
        button.addEventListener('click',()=>add(recipe.id));
      }
      card.append(button);
    }
    return card;
  };
  input.value=week;input.addEventListener('change',load);retry.addEventListener('click',load);
  window.addEventListener('pageshow',e=>{if(e.persisted&&!busy)load();});
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&!busy)load();});
  load();
})();

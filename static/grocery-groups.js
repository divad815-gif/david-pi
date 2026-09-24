/* General name matching: measurement/alias dictionaries never gate food names.
 * Every original line and item ID remains the source of truth. */
(() => {
  const fractions = {'¼':'1/4','½':'1/2','¾':'3/4','⅓':'1/3','⅔':'2/3','⅛':'1/8','⅜':'3/8','⅝':'5/8','⅞':'7/8'};
  const amountPattern = '(?:\\d+\\s+\\d+\\/\\d+|\\d+\\/\\d+|(?:\\d+(?:\\.\\d+)?|\\.\\d+))';
  const leadingAmount = new RegExp('^('+amountPattern+')(?:\\s*(?:[-–—]|to)\\s*('+amountPattern+'))?', 'u');
  const unitAliases = {
    g:['g','gram','grams'], kg:['kg','kilogram','kilograms'],
    ml:['ml','milliliter','milliliters','millilitre','millilitres'], l:['l','liter','liters','litre','litres'],
    oz:['oz','ounce','ounces'], lb:['lb','lbs','pound','pounds'],
    cup:['cup','cups'], tbsp:['tbsp','tbs','tbls','tblsp','tablespoon','tablespoons'], tsp:['tsp','teaspoon','teaspoons'],
    'fl oz':['fl oz','fl. oz','fluid ounce','fluid ounces'],
    pint:['pint','pints','pt'], quart:['quart','quarts','qt'], gallon:['gallon','gallons','gal'],
    clove:['clove','cloves'], sprig:['sprig','sprigs'], slice:['slice','slices'], piece:['piece','pieces'],
    can:['can','cans','tin','tins'], packet:['packet','packets','pack','packs'], package:['package','packages'],
    jar:['jar','jars'], bottle:['bottle','bottles'], bag:['bag','bags'], box:['box','boxes'], tub:['tub','tubs'],
    bunch:['bunch','bunches'], handful:['handful','handfuls'], pinch:['pinch','pinches'], dash:['dash','dashes']
  };
  const opaqueUnits = new Set(['can','packet','package','jar','bottle','bag','box','tub','bunch','handful','pinch','dash']);
  const volumeUnits = new Set(['ml','l','cup','tbsp','tsp','fl oz','pint','quart','gallon']);
  const unitLookup = new Map(Object.entries(unitAliases).flatMap(([unit,names])=>names.map(name=>[name,unit])));
  const unitPattern = new RegExp('^('+[...unitLookup.keys()].sort((a,b)=>b.length-a.length).map(s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|')+')(?:\\.)?(?=\\s|[(/]|$)\\s*','u');
  const phraseAliases = new Map([['parmesan cheese','parmesan'],['scallion','spring onion'],['green onion','spring onion']]);
  const relatedForms = new Map();
  for(const meat of ['lamb','beef','pork','chicken','turkey']) {
    relatedForms.set(meat,{family:meat,form:meat});
    for(const form of [`ground ${meat}`,`${meat} chop`,`${meat} stew meat`,`${meat} breast`,`${meat} thigh`,`${meat} leg`]) relatedForms.set(form,{family:meat,form});
    for(const alias of [`${meat} mince`,`minced ${meat}`]) relatedForms.set(alias,{family:meat,form:`ground ${meat}`});
  }
  const inflections = new Map([['leaves','leaf'],['loaves','loaf'],['halves','half'],['tomatoes','tomato'],['potatoes','potato']]);
  const invariant = new Set(['couscous','asparagus','hummus','molasses','oats','grits','watercress','swiss','citrus']);
  const prepPattern = /^(?:(?:finely|roughly|coarsely|thinly|freshly)\s+)?(?:chopped|sliced|diced|peeled|crushed|minced|grated|shaved|beaten|melted)(?=\s|,|$)/u;
  function normalize(text) {
    return String(text).replace(/[¼½¾⅓⅔⅛⅜⅝⅞]/g,c=>' '+fractions[c]).normalize('NFKC').replace(/⁄/g,'/').toLocaleLowerCase('en').replace(/\s+/g,' ').trim();
  }
  function singular(phrase) {
    return phrase.replace(/[\p{L}]+$/u,word=>{
      if(invariant.has(word))return word;
      if(inflections.has(word))return inflections.get(word);
      if(word.length>4&&/ies$/.test(word))return word.slice(0,-3)+'y';
      if(/(?:ches|shes|xes|zzes)$/.test(word))return word.slice(0,-2);
      if(word.length>3&&/s$/.test(word)&&!/(?:ss|us|is|ous|ics|ses)$/.test(word))return word.slice(0,-1);
      return word;
    });
  }
  function rational(n,d=1n) {
    if(d<=0n)return null;
    let a=n<0n?-n:n,b=d;while(b){const t=b;b=a%b;a=t;}
    return {numerator:String(n/a),denominator:String(d/a)};
  }
  function quantity(text) {
    if(text.length>48)return null;
    let n=0n,d=1n;
    for(const term of text.split(/\s+/)) {
      let tn,td;
      if(term.includes('/')){const parts=term.split('/');tn=BigInt(parts[0]);td=BigInt(parts[1]);}
      else {const parts=term.split('.');td=10n**BigInt(parts[1]?.length||0);tn=BigInt((parts[0]||'0')+(parts[1]||''));}
      if(td===0n||td>1000000n||(term.includes('/')&&td>1000n))return null;
      n=n*td+tn*d;d*=td;
    }
    if(n<=0n||n>1000000n*d)return null;
    return rational(n,d);
  }
  function consumeUnit(text) {
    const m=text.match(unitPattern);return m?{unit:unitLookup.get(m[1]),rest:text.slice(m[0].length).trim()}:null;
  }
  function cleanName(text) {
    let name=text.trim().replace(/^of\s+/,'').replace(/[.,;]+$/,'').trim(), preparation=[];
    // An explicit related meat form must not lose its minced distinction.
    if(relatedForms.has(singular(name)))return {name:singular(name),preparation};
    let m;
    while((m=name.match(prepPattern))){preparation.push(m[0]);name=name.slice(m[0].length).replace(/^[,\s]+/,'');}
    const tail=name.match(/^(.*?)(?:,\s*|\s+)((?:(?:finely|roughly|coarsely|thinly|freshly)\s+)?(?:chopped|sliced|diced|peeled|crushed|minced|grated|shaved|beaten|melted))$/u);
    if(tail&&tail[1]){name=tail[1].trim();preparation.push(tail[2]);}
    return {name:singular(name),preparation};
  }
  function parse(text) {
    const normalized=normalize(text);
    const fallback=reason=>({text,identity:normalized,key:'literal:'+normalized,form:normalized,reason,arithmeticSafe:false});
    if(!normalized)return fallback('empty line');
    let rest=normalized, q=null, unit='count', uncertain=false, reason='normalized ingredient name';
    const approx=rest.match(/^(?:about|approximately|approx\.?|around)\s+/);
    if(approx){rest=rest.slice(approx[0].length);uncertain=true;}
    const vague=rest.match(/^(?:to taste|as required|as needed|for frying|for serving|for garnish|for garnishing)\s+/);
    if(vague){rest=rest.slice(vague[0].length);uncertain=true;}
    const amount=rest.match(leadingAmount);
    if(amount){q=quantity(amount[1]);rest=rest.slice(amount[0].length).trim();if(amount[2]||!q)uncertain=true;}
    // Parenthesized package sizes stay verbatim, never multiplied.
    const packageSize=rest.match(/^\([^)]*\d[^)]*\)\s*/);
    if(packageSize){rest=rest.slice(packageSize[0].length);uncertain=true;}
    const measured=consumeUnit(rest);
    if(measured){unit=measured.unit;rest=measured.rest;if(opaqueUnits.has(unit))uncertain=true;}
    // Recover a name from dual measurements or pack multipliers; do not add them.
    if(/^(?:\/|x\s|×)/.test(rest)){
      const secondary=rest.replace(/^(?:\/|x|×)\s*/,'').match(leadingAmount);
      if(!secondary)return fallback('ambiguous measurement');
      const next=consumeUnit(rest.replace(/^(?:\/|x|×)\s*/,'').slice(secondary[0].length).trim());
      if(!next)return fallback('ambiguous measurement');
      rest=next.rest;uncertain=true;
    }
    const container=consumeUnit(rest);
    if(container&&opaqueUnits.has(container.unit)){rest=container.rest;uncertain=true;}
    const suffix=rest.match(/(?:,?\s+)(?:to taste|as required|as needed|optional)$/);
    if(suffix){rest=rest.slice(0,-suffix[0].length);uncertain=true;}
    if(!rest||/^[\d/–—-]/.test(rest))return fallback('ingredient name not safely recovered');
    const cleaned=cleanName(rest);
    if(!cleaned.name)return fallback('ingredient name not safely recovered');
    let identity=phraseAliases.get(cleaned.name)||cleaned.name;
    if(phraseAliases.has(cleaned.name))reason='whole-name alias';
    const related=relatedForms.get(identity);
    let form=related?.form||identity;
    if(related){identity=related.family;reason='explicit related form';}
    // Mincing meat is a purchasing/form distinction, not just removable prep.
    // This also covers adverbs and trailing "beef, minced" descriptions.
    if(related&&cleaned.preparation.some(p=>/\bminced\b/.test(p))&&!form.startsWith('ground '))form='ground '+form;
    if(volumeUnits.has(unit)&&cleaned.preparation.length)form+=' ['+cleaned.preparation.join(', ')+']';
    if(/[()\/\d]|\b(?:or|and|plus)\b/.test(cleaned.name))uncertain=true;
    const result={text,identity,key:'ingredient:'+identity,form,unit,preparation:cleaned.preparation,reason,arithmeticSafe:Boolean(q&&!uncertain)};
    if(result.arithmeticSafe){
      if(unit==='kg'||unit==='l'){q=rational(BigInt(q.numerator)*1000n,BigInt(q.denominator));result.unit=unit==='kg'?'g':'ml';}
      result.quantity=q;
    } else result.reason += '; original amount retained';
    return result;
  }
  function display(q){
    const n=BigInt(q.numerator),d=BigInt(q.denominator),whole=n/d,remainder=n%d;
    if(!remainder)return String(whole);
    const reduced=rational(remainder,d),fraction=reduced.numerator+'/'+reduced.denominator;
    return (whole?whole+' ':'')+fraction;
  }
  function describe(p){
    let form=p.form,unit=p.unit;
    const plural=BigInt(p.quantity.numerator)>BigInt(p.quantity.denominator);
    if(unit==='count'&&plural)form=form.replace(/\b(chop|onion|egg|carrot|breast|thigh|leg|yolk|cube)$/, '$1s').replace(/\bleaf$/, 'leaves');
    if(['cup','clove','sprig','slice','piece'].includes(unit)&&plural)unit+='s';
    return display(p.quantity)+(unit==='count'?'':' '+unit)+' '+form;
  }
  function group(items){
    const map=new Map();
    for(const item of items){
      const p=parse(item.text),key=p.key;
      if(!map.has(key))map.set(key,{key,label:p.identity?p.identity[0].toUpperCase()+p.identity.slice(1):item.text,items:[],parts:[]});
      const g=map.get(key);g.items.push(item);g.parts.push(p);
    }
    return [...map.values()].map(g=>{
      const sums=new Map(),parts=[];
      for(const p of g.parts){
        if(!p.arithmeticSafe){parts.push(p.text);continue;}
        const key=p.unit+'|'+p.form;
        if(!sums.has(key)){const sum={...p};sums.set(key,sum);parts.push(sum);}
        else {const sum=sums.get(key),a=sum.quantity,b=p.quantity;sum.quantity=rational(BigInt(a.numerator)*BigInt(b.denominator)+BigInt(b.numerator)*BigInt(a.denominator),BigInt(a.denominator)*BigInt(b.denominator));}
      }
      g.summary=parts.map(p=>typeof p==='string'?p:describe(p)).join(' + ');
      g.recipeCount=new Set(g.items.map(i=>i.recipe_id).filter(Boolean)).size;g.extras=g.items.filter(i=>!i.recipe_id).length;return g;
    });
  }
  globalThis.GroceryGroups={group,parse};
})();

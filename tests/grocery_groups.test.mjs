import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const context=vm.createContext({});vm.runInContext(fs.readFileSync(new URL('../static/grocery-groups.js',import.meta.url),'utf8'),context);
const groups=(...lines)=>context.GroceryGroups.group(lines.map((text,i)=>({id:String(i),text,recipe_id:String(i)})));
test('matching units and fractions combine',()=>{assert.equal(groups('1 cup rice','2 cups rice')[0].summary,'3 cups rice');assert.equal(groups('½ cup rice','1 1/2 cups rice')[0].summary,'2 cups rice');});
test('metric conversions are exact and weight never becomes volume',()=>{assert.equal(groups('1 kg lamb','500 g lamb')[0].summary,'1500 g lamb');assert.match(groups('1 cup rice','100 g rice')[0].summary,/1 cup rice \+ 100 g rice/);});
test('related lamb forms retain distinct quantities',()=>{const g=groups('4 lamb chops','2 lb ground lamb','1 lb lamb stew meat')[0];assert.equal(g.recipeCount,3);assert.equal(g.summary,'4 lamb chops + 2 lb ground lamb + 1 lb lamb stew meat');});
test('ambiguous amounts stay verbatim and different ingredients stay separate',()=>{assert.equal(groups('1-2 cups rice','rice to taste')[0].summary,'1-2 cups rice + rice to taste');assert.equal(groups('2 cans tomato (400 g each)')[0].summary,'2 cans tomato (400 g each)');assert.equal(groups('1 cup almond flour','1 cup wheat flour').length,2);});
test('unknown names and identical alternatives group without guessed quantities',()=>{assert.equal(groups('1 packet mystery','2 packets mystery').length,1);assert.equal(groups('1 packet mystery','2 packets mystery')[0].summary,'1 packet mystery + 2 packets mystery');assert.equal(groups('salt or pepper','salt or pepper').length,1);assert.equal(groups('salt or pepper','1 tsp salt').length,2);});
test('source recipe count is distinct and extras are separate',()=>{const input=[{id:'a',text:'1 cup rice',recipe_id:'r'},{id:'b',text:'1 cup rice',recipe_id:'r'},{id:'c',text:'1 cup rice',recipe_id:null}];const before=JSON.stringify(input),g=context.GroceryGroups.group(input)[0];assert.equal(g.recipeCount,1);assert.equal(g.extras,1);assert.equal(JSON.stringify(input),before);});
test('meat mincing remains distinct even with adverbs or trailing preparation',()=>{
  assert.equal(groups('100 g finely minced beef','200 g beef')[0].summary,'100 g ground beef + 200 g beef');
  assert.equal(groups('100 g beef, minced','200 g ground beef')[0].summary,'300 g ground beef');
  assert.equal(groups('100 g finely minced chicken breast','200 g chicken breast')[0].summary,'100 g ground chicken breast + 200 g chicken breast');
});

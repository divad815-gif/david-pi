import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const ctx = vm.createContext({});
vm.runInContext(fs.readFileSync(new URL('../static/grocery-groups.js', import.meta.url), 'utf8'), ctx);
const items = lines => lines.map((text, i) => ({id:`item-${i}`, text, recipe_id:`recipe-${i}`}));
const groups = (...lines) => ctx.GroceryGroups.group(items(lines));

// Independently reviewed pairs drawn from the shared recipe corpus, not an alias whitelist.
const matching = [
 ['1 tsp Salt','1/2 tsp Salt'], ['Pinch Salt','To taste Salt'],
 ['1 chopped Onion','1 Onion'], ['1 sliced Onion','1 Diced Onion'],
 ['1 finely chopped Onion','2 chopped Onions'], ['1 Egg','2 Eggs'],
 ['1 beaten Egg','2 Beaten Eggs'], ['3 Egg','4 Eggs'],
 ['2 cloves minced Garlic','2 cloves Garlic'], ['1 clove finely chopped Garlic','3 Cloves Crushed Garlic'],
 ['1 clove peeled crushed Garlic','4 Cloves Chopped Garlic'],
 ['Pinch Pepper','To taste Pepper'], ['1 tsp Pepper','pinch Pepper'],
 ['1 tsp Baking Powder','2 tsp Baking Powder'], ['1 teaspoon Baking Powder','1 tsp Baking Powder'],
 ['2 tablespoons Olive Oil','1 tablespoon Olive Oil'], ['2 tbs Olive Oil','2 tblsp Olive Oil'],
 ['1 tblsp Olive Oil','3 tbs Olive Oil'], ['1 cup Water','4 cups Water'],
 ['2 cups Water','1/4 cup Water'], ['1 tsp Sugar','1 cup Sugar'], ['1/2 cup Sugar','2 tsp Sugar'],
 ['25g Butter','50g Butter'], ['100g Butter','150g Butter'], ['1 tablespoon Butter','2 tbs Butter'],
 ['1 tsp Paprika','1/2 tsp Paprika'], ['1 Bay Leaf','2 Bay Leaves'],
 ['1 chopped Red Chilli','1 sliced Red Chilli'], ['Handful Parsley','Chopped Parsley'],
 ['Handful Coriander','Bunch Coriander'], ['1/4 tsp Black Pepper','1 tsp Black Pepper'],
 ['To taste Black Pepper','Pinch Black Pepper'], ['1 cup Milk','200ml Milk'],
 ['1/2 cup Milk','1 cup Milk'], ['1 tablespoon Soy Sauce','1 tbs Soy Sauce'],
 ['1 tablespoon Vegetable Oil','2 tbs Vegetable Oil'], ['2 tablespoons Vegetable Oil','2 tbs Vegetable Oil'],
 ['2 Carrots','1 Carrots'], ['1 chopped Red Pepper','1 sliced Red Pepper'],
 ['1 teaspoon Salt','1 tsp Salt'],
];
const separate = [
 ['1 cup Milk','400ml Coconut Milk'], ['1 tsp Cumin','1 tsp Ground Cumin'],
 ['1 Egg','3 Egg Yolks'], ['1 tsp Sugar','1 tablespoon Brown Sugar'],
 ['1 tsp Sugar','100g Caster Sugar'], ['1 Onion','1 sliced Red Onions'],
 ['1 Onion','1 bunch Spring Onions'], ['1 tsp Pepper','1 Red Pepper'],
 ['1 tsp Pepper','1 tsp Black Pepper'], ['1 tsp Salt','1 tsp Sugar'],
 ['2 tablespoons Olive Oil','2 tablespoons Vegetable Oil'], ['1 tablespoon Soy Sauce','2 tsp Sesame Seed Oil'],
 ['1 tsp Ginger','1 tsp Turmeric'], ['1 tsp Cinnamon','1 Cinnamon Stick'],
 ['1 tsp Allspice','1 tsp Paprika'], ['1 Bay Leaf','2 sprigs Thyme'],
 ['Handful Mint','Handful Coriander'], ['Chopped Parsley','Handful Coriander'],
 ['1 Lime','Juice of 1 Lime'], ['Juice of 1 Lemon','Zest of 1 Lemon'],
 ['Zest of 1 Orange','Zest of 1 Lemon'], ['1 clove Garlic','1 chopped Onion'],
 ['1 tsp Baking Powder','10g Yeast'], ['50g Almonds','250g Self-raising Flour'],
 ['1 Chicken Stock Cube','1 lb Ground Beef'], ['1 chopped Red Chilli','1 chopped Red Pepper'],
 ['1 tablespoon Tomato Puree','400g Tinned Tomatos'], ['1 cup Water','1 cup Milk'],
 ['2 Carrots','1 Onion'], ['1 tsp Vanilla Extract','1 tsp Cinnamon'],
 ['1 tablespoon Butter','1 tablespoon Olive Oil'], ['1 tsp Ground Cumin','1 tsp Black Pepper'],
 ['1/2 tsp Paprika','1 tsp Turmeric'], ['1 cup Sugar','1 cup Water'],
 ['50g Butter','50g Almonds'], ['1 Egg','1 Carrots'],
 ['1 chopped Red Chilli','1 sliced Red Onions'], ['1 Lime','1 Bay Leaf'],
 ['1 tablespoon Brown Sugar','1 tablespoon Soy Sauce'], ['2 sprigs Thyme','Handful Mint'],
];
for (const [a,b] of matching) test(`corpus groups: ${a} / ${b}`, () => assert.equal(groups(a,b).length, 1));
for (const [a,b] of separate) test(`corpus distinguishes: ${a} / ${b}`, () => assert.equal(groups(a,b).length, 2));

// Reserved until the first parser implementation was complete. Ten positive and
// ten negative comparisons of real corpus lines check behavior independently.
const reserved = [
 [true,'1 tsp Salt','Dash Salt'], [true,'1 cup Sugar','1 tablespoon Sugar'],
 [true,'175g Butter','1 tablespoon Butter'], [true,'3 Egg','2 Beaten Eggs'],
 [true,'1/4 tsp Black Pepper','To taste Black Pepper'],
 [true,'1 tablespoon Soy Sauce','2 tbs Soy Sauce'],
 [true,'2 cloves chopped Garlic','1 clove peeled crushed Garlic'],
 [true,'1 Diced Onion','2 chopped Onions'], [true,'1 tblsp Olive Oil','2 tablespoons Olive Oil'],
 [true,'Pinch Pepper','1 tsp Pepper'],
 [false,'1 tsp Ground Cumin','1 tsp Cinnamon'],
 [false,'Juice of 1 Lemon','Juice of 1 Lime'],
 [false,'1 tablespoon Tomato Puree','1 tablespoon Soy Sauce'],
 [false,'2 Eggs','3 Egg Yolks'], [false,'400ml Coconut Milk','1/2 cup Milk'],
 [false,'1 tsp Sugar','100g Caster Sugar'], [false,'1 Onion','1 sliced Red Onions'],
 [false,'1 tsp Vanilla Extract','1 tsp Baking Powder'],
 [false,'1 tablespoon Vegetable Oil','1 tablespoon Butter'],
 [false,'1 chopped Red Pepper','1 tsp Pepper'],
];
for(const [same,a,b] of reserved) test(`reserved ${same?'match':'distinct'}: ${a} / ${b}`,()=>assert.equal(groups(a,b).length,same?1:2));

test('actual spaghetti quantities group without converting ounces', () => {
 const g=groups('320g Spaghetti','16 ounces Spaghetti','300g Spaghetti');
 assert.equal(g.length,1); assert.equal(g[0].recipeCount,3);
 assert.match(g[0].summary,/620 g/); assert.match(g[0].summary,/16 oz/);
});
test('unknown names support structural recognition, fractions, metric scaling', () => {
 for (const name of ['freekeh','velnora grain','quexuli','山椒']) {
  const g=groups(`200g ${name}`,`0.3 kg ${name.toUpperCase()}`);
  assert.equal(g.length,1,name); assert.match(g[0].summary,/500 g/);
  assert.equal(groups(`½ cup ${name}`,`1 1/2 cups ${name}`)[0].summary.startsWith('2 cups'),true,name);
 }
});
test('meaningful modifiers and compound names prevent false merges', () => {
 for(const [a,b] of [['milk','coconut milk'],['garlic','garlic powder'],['spaghetti','gluten-free spaghetti'],['rice','cooked rice'],['basil','dried basil'],['almond flour','wheat flour'],['freekeh','roasted freekeh']]) assert.equal(groups(`1 cup ${a}`,`2 cups ${b}`).length,2,`${a}/${b}`);
});
test('uncertain quantities remain present and no originals are lost', () => {
 const input=items(['1-2 cups freekeh','freekeh to taste','2 cans freekeh (400 g each)','1 cup freekeh']);
 const before=JSON.stringify(input), result=ctx.GroceryGroups.group(input);
 assert.equal(JSON.stringify(input),before);
 assert.deepEqual(Array.from(result.flatMap(g=>g.items.map(i=>i.id))).sort(),input.map(i=>i.id).sort());
 for(const line of input.slice(0,3)) assert.ok(result.some(g=>g.summary.includes(line.text)),line.text);
});
test('input reorder cannot change quantity totals or group membership', () => {
 const input=items(['320g spaghetti','16 oz spaghetti','300 g spaghetti','1 cup velnora','2 cups velnora']);
 const canonical = arr => JSON.stringify(Array.from(ctx.GroceryGroups.group(arr),g=>({ids:Array.from(g.items,i=>i.id).sort(), parts:g.summary.split(' + ').sort()})).sort((a,b)=>a.ids[0].localeCompare(b.ids[0])));
 assert.equal(canonical(input),canonical([...input].reverse()));
});
test('recipe count counts recipes, extras counted separately', () => {
 const input=[{id:'a',text:'1 cup freekeh',recipe_id:'same'},{id:'b',text:'2 cups freekeh',recipe_id:'same'},{id:'c',text:'3 cups freekeh',recipe_id:null}];
 const g=ctx.GroceryGroups.group(input); assert.equal(g.length,1);assert.equal(g[0].recipeCount,1);assert.equal(g[0].extras,1);assert.match(g[0].summary,/6 cups/);
});

import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const data=JSON.parse(readFileSync(new URL('./domain-expansion-spatial-data.json',import.meta.url),'utf8'));
const rendered=page=>page.waitForFunction(()=>{const p=document.querySelector('.js-plotly-plot');return Boolean(p?.data?.length&&p._fullLayout)});
export const domainExpansionSpatialCases=['dense','csr','csc'].map(storage=>{
 const fixture=data.cases[storage];let requests=[];
 return {name:`domain-expansion-spatial-${storage}`,component:'SpatialWindowPreview.vue',filename:`synthetic-${storage}.h5ad`,reader:'spatial-window',
 descriptor:{adapter:'spatial-window',capabilities:{operations:['preview'],input_mode:'window',shared:false}},file:{size:fixture.tree.metadata.source_bytes},
 init:async()=>{requests=[]},
 preview:request=>{requests.push(request);if(request.kind==='tree'){assert.deepEqual(request.options,{});return fixture.tree}assert.equal(request.kind,'geometry');assert.equal(request.version,'1'.repeat(64));const key=request.options.feature===0?'first':'window';assert.deepEqual(request.options,fixture[key].selected);return fixture[key]},
 ready:page=>page.getByRole('button',{name:'读取空间窗口',exact:true}).waitFor(),
 verify:async page=>{
  assert.equal(requests.length,1);assert.equal(await page.locator('.js-plotly-plot').count(),0);
  await page.getByRole('button',{name:'读取空间窗口',exact:true}).click();await rendered(page);
  const before=await page.locator('.js-plotly-plot').evaluate(p=>({type:p.data[0].type,mode:p.data[0].mode,x:p.data[0].x,y:p.data[0].y,values:p.data[0].marker.color,ids:p.data[0].customdata,yAutorange:p.layout.yaxis.autorange??null,xLabel:p.layout.xaxis.title.text,connect:p.data[0].connectgaps}));
  assert.equal(before.type,'scatter');assert.equal(before.mode,'markers');assert.deepEqual(before.x,[1,2,3,4]);assert.deepEqual(before.y,[10,20,30,40]);assert.deepEqual(before.values,[0,3,0,7]);assert.deepEqual(before.ids,[0,1,2,3]);assert.notEqual(before.yAutorange,'reversed');assert.equal(before.connect,false);assert.match(before.xLabel,/单位未知/);
  await page.getByLabel('空间特征序号').fill('1');await page.getByLabel('空间起始观测').fill('1');await page.getByLabel('空间观测数').fill('2');
  assert.equal(requests.length,2);assert.equal(await page.locator('.js-plotly-plot').count(),0);
  await page.getByRole('button',{name:'读取空间窗口',exact:true}).click();await rendered(page);
  const after=await page.locator('.js-plotly-plot').evaluate(p=>({x:p.data[0].x,y:p.data[0].y,values:p.data[0].marker.color,ids:p.data[0].customdata}));
  assert.deepEqual(after,{x:[2,3],y:[20,30],values:[0,5],ids:[1,2]});assert.equal(requests.length,3);assert.deepEqual(await page.locator('[role=alert]').allTextContents(),[]);
  const note=await page.getByRole('note').textContent();assert.match(note,/不反转 Y/);assert.match(note,/不代表完整 SpatialData/);
  const text=await page.locator('body').textContent();assert.doesNotMatch(text,/PRIVATE-A|病例-|基因甲/);
  return {metadataOnlyInitially:true,versionPinned:true,rawValues:true,ordinalLabelsOnly:true,noAxisInversion:true,noAutomaticWindowReads:true,storage,readBytes:fixture.window.metadata.read_bytes,numericBytes:fixture.window.metadata.numeric_bytes_read,visiblePlot:await page.locator('.js-plotly-plot').boundingBox()};
 },
 beforeUnmount:page=>page.evaluate(()=>{window.__heldSpatial=document.querySelector('.js-plotly-plot')}),
 verifyCleanup:async page=>{const result=await page.evaluate(()=>({detached:!window.__heldSpatial.isConnected,purged:!window.__heldSpatial.data}));assert.deepEqual(result,{detached:true,purged:true});return result}
 };
});

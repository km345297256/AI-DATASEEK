/** Original HDF5 reader outputs, real Plotly, no listening socket or business API. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const fixtures=JSON.parse(readFileSync(new URL('./domain-expansion-radar-data.json',import.meta.url),'utf8'));
const base={component:'RadarWindowPreview.vue',filename:'synthetic.h5',reader:'radar-window',descriptor:{adapter:'radar-window',view_kind:'image',limits:{max_input_bytes:8388608,max_output_bytes:2097152},capabilities:{operations:['preview'],input_mode:'window',shared:false}}};
async function select(page){for(const [name,value]of [['射线起点',1],['射线数',2],['距离门起点',1],['距离门数',4]])await page.getByLabel(`雷达${name}`,{exact:true}).fill(String(value));}
const rendered=page=>page.waitForFunction(()=>{const p=document.querySelector('[data-testid=radar-plot]');return p?._fullLayout&&p.data?.length===2;});
function scenario(group){const data=fixtures[group];let requests=[];return {...base,name:`domain-expansion-radar-${group}`,file:{size:data.tree.metadata.source_bytes},init:async()=>{requests=[];},
  preview:request=>{requests.push(request);if(request.kind==='tree'){assert.deepEqual(request.options,{});return data.tree;}assert.equal(request.kind,'image');assert.equal(request.version,'1'.repeat(64));assert.deepEqual(request.options,data.image.selected);return data.image;},
  ready:page=>page.getByRole('button',{name:'读取雷达窗口',exact:true}).waitFor(),
  verify:async page=>{
    assert.equal(requests.length,1);assert.equal(await page.locator('.js-plotly-plot').count(),0);await select(page);assert.equal(requests.length,1);
    await page.getByRole('button',{name:'读取雷达窗口',exact:true}).click();await rendered(page);
    const read=()=>page.getByTestId('radar-plot').evaluate(p=>({x:p.data[0].x,y:p.data[0].y,z:p.data[0].z,flags:p.data[1].z,text:p.data[1].text,raw:p.data[1].customdata,colorTitle:p.data[0].colorbar.title.text,yTitle:p._fullLayout.yaxis.title.text,xTitle:p._fullLayout.xaxis.title.text,rowPixels:p.data[0].y.map(y=>p._fullLayout.yaxis.l2p(y)),rasters:p.querySelectorAll('.heatmaplayer image').length,rasterBytes:[...p.querySelectorAll('.heatmaplayer image')].map(el=>(el.getAttribute('href')||el.getAttribute('xlink:href')||'').length)}));
    const raw=await read();assert.deepEqual(raw.x,[1375,1625,1875,2125]);assert.deepEqual(raw.y,[1,2]);assert.deepEqual(raw.z,[[null,null,18,19],[26,27,28,29]]);assert.deepEqual(raw.flags,[[1,2,null,null],[null,null,null,null]]);assert.deepEqual(raw.raw,[[255,0,18,19],[26,27,28,29]]);
    assert.match(raw.text[0][0],/nodata/);assert.match(raw.text[0][1],/undetect/);assert.match(raw.colorTitle,/无物理单位/);assert.match(raw.xTitle,/非地面距离/);assert.match(raw.yTitle,/非方位角/);assert.ok(raw.rowPixels[0]<raw.rowPixels[1]);assert.equal(raw.rasters,2);assert.ok(raw.rasterBytes.every(n=>n>50));
    await page.evaluate(()=>{window.__radarOldPlot=document.querySelector('[data-testid=radar-plot]');});
    await page.getByLabel('应用雷达声明标定',{exact:true}).check();await rendered(page);
    const physical=await read();assert.deepEqual(physical.z,group==='positive'?[[null,null,-23,-22.5],[-19,-18.5,-18,-17.5]]:[[null,null,-36.5,-36.75],[-38.5,-38.75,-39,-39.25]]);assert.deepEqual(physical.flags,raw.flags);assert.match(physical.colorTitle,/DBZH \[dBZ\]/);assert.equal(requests.length,2);
    assert.equal(await page.evaluate(()=>!window.__radarOldPlot.isConnected&&!window.__radarOldPlot._fullLayout),true);
    await page.getByLabel('应用雷达声明标定',{exact:true}).uncheck();await rendered(page);assert.deepEqual((await read()).z,raw.z);assert.equal(requests.length,2);
    assert.match(await page.locator('section').innerText(),/a1gate=3/);assert.deepEqual(await page.locator('[role=alert]').allTextContents(),[]);
    await page.getByTestId('radar-plot').scrollIntoViewIfNeeded();const bounds=await page.getByTestId('radar-plot').boundingBox();assert.ok(bounds.width>450&&bounds.height>=400);
    return {realPlotly:true,metadataOnlyInitially:true,storedCodes:raw,declaredCalibration:physical,noRequestsOnLocalToggle:true,versionPinned:true,visible:bounds};
  },
  beforeUnmount:page=>page.evaluate(()=>{window.__radarPlot=document.querySelector('[data-testid=radar-plot]');}),
  verifyCleanup:async page=>{const clean=await page.evaluate(()=>({detached:!window.__radarPlot.isConnected,purged:!window.__radarPlot.data&&!window.__radarPlot._fullLayout}));assert.deepEqual(clean,{detached:true,purged:true});return clean;},
};}
export const domainExpansionRadarCases=[scenario('positive'),scenario('negative'),
  {...base,name:'domain-expansion-radar-wrong-selection',file:{size:fixtures.positive.tree.metadata.source_bytes},preview:request=>{if(request.kind==='tree')return fixtures.positive.tree;const value=structuredClone(fixtures.positive.image);value.selected.sweep=2;return value;},
    setup:async page=>{await page.getByRole('button',{name:'读取雷达窗口',exact:true}).waitFor();await select(page);await page.getByRole('button',{name:'读取雷达窗口',exact:true}).click();},ready:page=>page.locator('[role=alert]').waitFor(),expectedError:/雷达响应、存储码或窗口选择无效/,
    verify:async page=>{assert.equal(await page.locator('.js-plotly-plot').count(),0);return {wrongSweepRejected:true};}},
  {...base,name:'domain-expansion-radar-unsafe-unit',file:{size:fixtures.positive.tree.metadata.source_bytes},preview:()=>{const value=structuredClone(fixtures.positive.tree);value.choices.sweeps[0].quantities[0].unit='<img src="https://example.invalid/track">';return value;},ready:page=>page.locator('[role=alert]').waitFor(),expectedError:/雷达响应、存储码或窗口选择无效/,
    verify:async page=>{assert.equal(await page.locator('.js-plotly-plot').count(),0);assert.equal(await page.locator('img').count(),0);return {unsafeUnitRejectedBeforePlot:true};}},
];

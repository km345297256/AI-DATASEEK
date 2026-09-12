/** Actual bounded-reader responses, with independent offline Pyteomics oracle tests. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./mass-spectrum-data.json', import.meta.url),'utf8'));
let requests=[], releasePending;
const base={component:'MassSpectrumPreview.vue',reader:'mass-spectrum',descriptor:{adapter:'mass-spectrum'}};
export const massSpectrumCases = ['mgf','mzml'].map(format=>({
  ...base,name:'domain-expansion-mass-'+format,filename:'synthetic.'+format,file:{size:data[format+'_tree'].metadata.source_bytes},init:async()=>{requests=[];},
  preview:request=>{requests.push(request);if(request.kind==='tree'){assert.deepEqual(request.options,{offset:0});return data[format+'_tree'];}assert.equal(request.kind,'series');assert.equal(request.version,'1'.repeat(64));assert.deepEqual(request.options,{spectrum:'s-000000'});return data[format+'_series'];},
  ready:page=>page.getByRole('button',{name:'读取所选质谱',exact:true}).waitFor(),
  verify:async page=>{
    assert.equal(requests.length,1);assert.equal(await page.locator('.js-plotly-plot').count(),0);
    await page.getByLabel('质谱选择',{exact:true}).selectOption('s-000000');await page.getByRole('button',{name:'读取所选质谱',exact:true}).click();
    await page.waitForFunction(()=>{const p=document.querySelector('.js-plotly-plot');return !!p?.data?.length&&!!p._fullLayout;});
    const result=await page.locator('.js-plotly-plot').evaluate(p=>({x:p.data[0].x,y:p.data[0].y,connectgaps:p.data[0].connectgaps,simplify:p.data[0].line.simplify,type:p.data[0].type,unit:p.layout.yaxis.title.text}));
    if(format==='mgf'){assert.deepEqual(result.x,[150.5,150.5,null,100.25,100.25,null,200.75,200.75,null]);assert.deepEqual(result.y,[0,10,null,0,25,null,0,5,null]);assert.match(result.unit,/单位未声明/);}
    else{assert.deepEqual(result.x,[100,100.25,100.5,100.75]);assert.deepEqual(result.y,[-1,3,7,2]);assert.match(result.unit,/detector counts/);}
    assert.equal(result.connectgaps,false);assert.equal(result.simplify,false);assert.equal(result.type,'scatter');assert.equal(requests.length,2);assert.deepEqual(await page.locator('[role=alert]').allTextContents(),[]);
    const stats=await page.locator('[data-testid=mass-spectrum-stats]').textContent();assert.match(stats,format==='mgf'?/centroid 棒谱/:/profile 连续谱/);if(format==='mzml')assert.match(stats,/2.5 min/);
    assert.equal((await page.locator('body').textContent()).includes('/private/hidden'),false);
    return {treeFirst:true,explicitVersion:true,originalNumericValues:result,rawTitlesHidden:true};
  },
  beforeUnmount:async page=>{
    await page.evaluate(()=>{window.__massPlot=document.querySelector('.js-plotly-plot');});
    const gate=new Promise(resolve=>{releasePending=resolve;});
    await page.route('**/api/v1/files/*/visualization',async route=>{assert.equal(route.request().postDataJSON().version,'1'.repeat(64));await gate;await route.abort('aborted').catch(()=>{});},{times:1});
    await page.evaluate(()=>{const original=window.fetch;window.fetch=function(input,init){if(String(input).endsWith('/visualization'))window.__massSignal=init.signal;return original.call(this,input,init);};});
    const pending=page.waitForRequest(r=>r.url().endsWith('/visualization'));await page.getByRole('button',{name:'读取所选质谱',exact:true}).click();await pending;
  },
  verifyCleanup:async page=>{const result=await page.evaluate(()=>({detached:!window.__massPlot.isConnected,purged:!window.__massPlot.data,pendingAborted:window.__massSignal.aborted}));assert.deepEqual(result,{detached:true,purged:true,pendingAborted:true});releasePending();return result;},
}));
massSpectrumCases.push({...base,name:'domain-expansion-mass-pagination',filename:'synthetic.mgf',file:{size:data.page_first.metadata.source_bytes},init:async()=>{requests=[];},
  preview:request=>{requests.push(request);assert.equal(request.kind,'tree');if(request.options.offset===0)return data.page_first;assert.equal(request.options.offset,64);assert.equal(request.version,'1'.repeat(64));return data.page_second;},
  ready:page=>page.getByRole('button',{name:'下一页质谱',exact:true}).waitFor(),
  verify:async page=>{await page.waitForFunction(()=>document.querySelector('select[aria-label="质谱选择"]')?.options.length===65);await page.getByRole('button',{name:'下一页质谱',exact:true}).click();await page.waitForFunction(()=>document.querySelector('select[aria-label="质谱选择"]')?.options.length===2);assert.equal(await page.getByRole('button',{name:'下一页质谱',exact:true}).isDisabled(),true);assert.equal(await page.locator('.js-plotly-plot').count(),0);assert.equal(requests.length,2);return {pages:[64,1],versionPinned:true,noArrayDecode:true};},
});

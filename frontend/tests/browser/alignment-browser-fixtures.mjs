import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const fixture=JSON.parse(readFileSync(new URL('./alignment-browser-data.json',import.meta.url),'utf8'));
export const alignmentBrowserCases=[];
for(const mode of ['normal','wrong-selection','private-label']){
 let requests=[];
 const name='main-migration-alignment-'+mode;
 alignmentBrowserCases.push({name,component:'AlignmentBrowserPreview.vue',filename:'fixture.sam',reader:'alignment-browser',descriptor:{adapter:'alignment-browser',view_kind:'map'},file:{size:fixture.tree.metadata.source_bytes},init:async()=>{requests=[];},
  preview:r=>{requests.push(r);if(r.kind==='tree')return fixture.tree;assert.equal(r.kind,'table');assert.equal(r.version,'1'.repeat(64));assert.deepEqual(r.options,fixture.table_ui.selected);const raw=structuredClone(fixture.table_ui);if(mode==='wrong-selection')raw.selected.end++;if(mode==='private-label')raw.alignment.reads[0].name='/Users/private/hidden';return raw;},
  ready:async p=>{await p.getByRole('button',{name:'读取比对区域',exact:true}).waitFor();if(mode!=='normal'){await p.getByLabel('比对起点',{exact:true}).fill('100');await p.getByLabel('比对终点',{exact:true}).fill('160');await p.getByLabel('比对展示条数',{exact:true}).fill('20');await p.getByRole('button',{name:'读取比对区域',exact:true}).click();await p.getByRole('alert').waitFor();}},
  expectedError:mode==='normal'?undefined:/比对预览协议或数据选择无效/,
  verify:async p=>{
   if(mode!=='normal'){assert.equal(requests.length,2);assert.equal(await p.getByRole('img',{name:'比对 reads 轨道',exact:true}).count(),0);return {rejected:mode}}
   assert.equal(requests.length,1);assert.equal(await p.getByRole('img',{name:'比对覆盖度',exact:true}).count(),0);
   await p.getByLabel('比对起点',{exact:true}).fill('100');await p.getByLabel('比对终点',{exact:true}).fill('160');await p.getByLabel('比对展示条数',{exact:true}).fill('20');
   await p.getByRole('button',{name:'读取比对区域',exact:true}).click();
   await p.getByRole('img',{name:'比对覆盖度',exact:true}).waitFor();assert.equal(requests.length,2);
   const reads=p.getByRole('img',{name:'比对 reads 轨道',exact:true});assert.equal(await reads.getByRole('button').count(),5);
   assert.ok(await reads.locator('circle').count()>=2);assert.equal(await reads.locator('line[stroke-dasharray]').count(),1);assert.ok(await reads.locator('rect[fill="none"]').count()>=1);
   await reads.getByRole('button',{name:'read pair',exact:true}).first().click();await p.getByLabel('Read 详情',{exact:true}).waitFor();assert.match(await p.getByLabel('Read 详情',{exact:true}).innerText(),/5M2I3M2D4M5N3M/);
   await p.getByRole('checkbox',{name:'log(1 + 覆盖度)',exact:true}).check();assert.equal(requests.length,2);
   assert.match(await p.locator('section').innerText(),/已扫描至文件末尾/);assert.deepEqual(await p.getByRole('alert').allTextContents(),[]);
   return {reads:5,coverage:true,pairLink:true,cigarEvents:true,details:true,logWithoutRead:true,explicitVersion:true};
  }});
}

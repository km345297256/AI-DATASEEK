import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const fixtures=JSON.parse(readFileSync(new URL('./database-records-data.json',import.meta.url),'utf8'));
export const databaseRecordsCases=['bson','redis-rdb'].flatMap(reader=>['pages','empty'].map(mode=>{
  let requests=[];const f=fixtures[reader],tree=mode==='empty'?f.empty:f.tree;
  return {name:`database-records-${reader}-${mode}`,component:'DatabaseRecordsPreview.vue',filename:'synthetic.'+(reader==='bson'?'bson':'rdb'),reader,
    descriptor:{adapter:'database-records',capabilities:{operations:['preview'],input_mode:'whole',shared:false}},file:{size:tree.metadata.source_bytes},
    init:async()=>{requests=[]},preview:request=>{
      requests.push(request);if(request.kind==='tree'){assert.deepEqual(request.options,{});return tree}
      assert.equal(request.kind,'table');assert.equal(request.version,'1'.repeat(64));
      const p=mode==='empty'?f.emptyPage:request.options.offset===0?f.first:f.second;assert.deepEqual(request.options,p.selected);return p;
    },
    ready:page=>page.getByRole('button',{name:'读取记录分页',exact:true}).waitFor(),
    verify:async page=>{
      assert.equal(requests.length,1);assert.equal(await page.getByRole('table',{name:'记录 字段与类型',exact:true}).count(),0);
      await page.getByLabel('记录 本页条数',{exact:true}).fill('2');assert.equal(requests.length,1);
      await page.getByRole('button',{name:'读取记录分页',exact:true}).click();
      if(mode==='empty'&&reader==='redis-rdb'){
        await page.getByText('当前分页为空，未自动调整偏移。',{exact:true}).waitFor();assert.equal(await page.getByRole('button',{name:'下一页',exact:true}).isDisabled(),true);
        return {emptyExplicit:true,metadataFirst:true,requests:requests.length};
      }
      const count=mode==='empty'?f.emptyPage.table.records.length:f.first.table.records.length;
      await page.locator('details > summary').first().waitFor();assert.equal(await page.locator('details > summary').count(),count);
      for(const s of await page.locator('details > summary').all())await s.click();
      const text=await page.locator('section').textContent();
      for(const r of (mode==='empty'?f.emptyPage:f.first).table.records)for(const n of r.nodes){
        if(['integer','text','decimal128','objectid'].includes(n.cell.type)&&n.cell.value!=='')assert.ok(text.includes(n.cell.value));
      }
      assert.equal(await page.locator('td img,td script,td iframe').count(),0);
      if(mode==='pages'){
        await page.getByRole('button',{name:'下一页',exact:true}).click();
        await page.waitForFunction(()=>document.querySelector('details > summary')?.textContent?.includes('记录 2'));
        assert.equal(requests.length,3);assert.equal(requests[2].options.offset,2);
        await page.getByLabel('记录 本页条数',{exact:true}).fill('1');assert.equal(await page.locator('details').count(),0);assert.equal(requests.length,3);
      }
      assert.deepEqual(await page.getByRole('alert').allTextContents(),[]);
      return {metadataFirst:true,versionPinned:true,exactTypes:true,htmlInert:true,manualPaging:true,requests:requests.length};
    },
    beforeUnmount:page=>page.evaluate(()=>{window.__heldRecords=document.querySelector('section')}),
    verifyCleanup:async page=>{assert.equal(await page.evaluate(()=>!window.__heldRecords?.isConnected),true);return {detached:true}},
  };
}));

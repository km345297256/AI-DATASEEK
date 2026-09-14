import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const fixtures=JSON.parse(readFileSync(new URL('./physical-database-data.json',import.meta.url),'utf8'));
export const physicalDatabaseCases=Object.entries(fixtures).map(([reader,fixture])=>{
  let requests=[];
  return {name:`physical-${reader}`,component:'PhysicalDatabasePreview.vue',filename:'synthetic.'+fixture.metadata.format,reader,
    descriptor:{adapter:'physical-database',capabilities:{operations:['preview'],input_mode:'whole',shared:false}},file:{size:fixture.metadata.source_bytes},
    init:async()=>{requests=[]},preview:request=>{requests.push(request);assert.equal(request.kind,'tree');assert.deepEqual(request.options,{});return fixture},
    ready:page=>page.getByRole('table',{name:'物理数据库文件预览',exact:true}).waitFor(),
    verify:async page=>{
      assert.equal(requests.length,1);assert.match(await page.getByRole('note').textContent(),reader==='mysql-sdi'?/不保证提交一致性/:/不代表最新逻辑状态/);
      assert.equal(await page.locator('tbody tr').count(),fixture.table.rows.length);
      const text=await page.getByRole('table',{name:'物理数据库文件预览',exact:true}).textContent();
      for(const row of fixture.table.rows)for(const cell of row)if(cell)assert.ok(text.includes(cell));
      await page.getByLabel('物理文件 搜索',{exact:true}).fill(reader==='mysql-sdi'?'decimal':'deletion');
      assert.equal(await page.locator('tbody tr').count(),1);assert.equal(requests.length,1);
      assert.equal(await page.getByRole('button',{name:'下一页',exact:true}).isDisabled(),true);
      return {nativePhysicalProjection:true,noLogicalStateClaim:true,localSearchOnly:true,requests:requests.length};
    },
    beforeUnmount:page=>page.evaluate(()=>{window.__heldPhysical=document.querySelector('table')}),
    verifyCleanup:async page=>{assert.equal(await page.evaluate(()=>!window.__heldPhysical?.isConnected),true);return {detached:true}},
  };
});

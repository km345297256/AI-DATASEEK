import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const fixtures=JSON.parse(readFileSync(new URL('./pg-dump-data.json',import.meta.url),'utf8'));
export const pgDumpCases=Object.entries(fixtures).map(([key,fixture])=>{
  let requests=[];
  return {name:`pg-dump-${key}`,component:'DatabaseDumpPreview.vue',filename:'synthetic.'+fixture.metadata.format,reader:'pg-dump',
    descriptor:{adapter:'database-dump',capabilities:{operations:['preview'],input_mode:'whole',shared:false}},file:{size:fixture.metadata.source_bytes},
    init:async()=>{requests=[]},preview:request=>{requests.push(request);assert.equal(request.kind,'tree');assert.deepEqual(request.options,{});return fixture},
    ready:page=>page.getByRole('table',{name:'备份 对象目录',exact:true}).waitFor(),
    verify:async page=>{
      assert.equal(requests.length,1);assert.match(await page.getByRole('note').textContent(),/未解压、校验或恢复数据段/);
      assert.equal(await page.locator('tbody tr').count(),fixture.tree.length);
      const text=await page.getByRole('table',{name:'备份 对象目录',exact:true}).textContent();
      for(const n of fixture.tree)for(const s of Object.values(n.attributes))assert.ok(text.includes(s));
      assert.ok(!text.includes('fixture_database')&&!text.includes('/Users/')&&!text.includes('CREATE TABLE'));
      await page.getByLabel('备份 对象类型',{exact:true}).selectOption('TABLE');
      assert.equal(await page.locator('tbody tr').count(),fixture.tree.filter(n=>n.attributes.object_type==='TABLE').length);
      await page.getByLabel('备份 搜索名称',{exact:true}).fill('measurements');
      assert.equal(await page.locator('tbody tr').count(),1);assert.equal(requests.length,1);
      assert.equal(await page.getByRole('button',{name:'下一页',exact:true}).isDisabled(),true);
      return {nativeArchiveDirectory:true,unverifiedDataExplicit:true,privateLabelsHidden:true,localFiltersOnly:true,requests:requests.length};
    },
    beforeUnmount:page=>page.evaluate(()=>{window.__heldPG=document.querySelector('table')}),
    verifyCleanup:async page=>{assert.equal(await page.evaluate(()=>!window.__heldPG?.isConnected),true);return {detached:true}},
  };
});

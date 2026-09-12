import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const fixtures=JSON.parse(readFileSync(new URL('./database-native-data.json',import.meta.url),'utf8'));
export const databaseTableCases=['pages-chart','empty'].map(mode=>{
  let requests=[];
  return {name:`database-table-${mode}`,component:'DatabaseTablePreview.vue',filename:'synthetic.duckdb',reader:'database-table',
    descriptor:{adapter:'database-table',capabilities:{operations:['preview'],input_mode:'whole',shared:false}},file:{size:fixtures.tree.metadata.source_bytes},
    init:async()=>{requests=[]},
    preview:request=>{
      requests.push(request);if(request.kind==='tree'){assert.deepEqual(request.options,{});return fixtures.tree}
      assert.equal(request.kind,'table');assert.equal(request.version,'1'.repeat(64));
      const o=request.options,key=o.table===fixtures.empty.selected.table?'empty':o.columns.length===2?'columns':o.row_offset===0?'first':'second';
      assert.deepEqual(o,fixtures[key].selected);return fixtures[key];
    },
    ready:page=>page.getByRole('button',{name:'读取数据库分页',exact:true}).waitFor(),
    verify:async page=>{
      assert.equal(requests.length,1);assert.equal(await page.getByRole('table',{name:'数据库 数据分页',exact:true}).count(),0);
      assert.match(await page.getByRole('note').textContent(),/不是大库分块/);
      await page.getByLabel('数据库 本页行数',{exact:true}).fill('2');
      if(mode==='empty'){
        await page.getByLabel('数据库 数据表',{exact:true}).selectOption(fixtures.empty.selected.table);
        await page.getByRole('button',{name:'读取数据库分页',exact:true}).click();
        await page.getByText('当前分页为空，未自动调整起始行。',{exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'下一页',exact:true}).isDisabled(),true);
        return {metadataFirst:true,emptyExplicit:true,requests:requests.length};
      }
      await page.getByLabel('数据库 数据表',{exact:true}).selectOption(fixtures.first.selected.table);
      assert.equal(await page.getByLabel('数据库 列 6 nested',{exact:true}).isDisabled(),true);
      assert.equal(requests.length,1);
      await page.getByRole('button',{name:'读取数据库分页',exact:true}).click();
      await page.getByRole('table',{name:'数据库 数据分页',exact:true}).waitFor();
      let text=await page.getByRole('table',{name:'数据库 数据分页',exact:true}).textContent();
      for(const row of fixtures.first.table.rows)for(const c of row)if(['integer','decimal'].includes(c.type))assert.ok(text.includes(c.value));
      await page.getByText('<img src=x onerror=alert(1)>',{exact:true}).waitFor();
      assert.equal(await page.locator('td img').count(),0);
      await page.getByText('当前页数值图表（不扫描全表）',{exact:true}).click();
      await page.getByLabel('数据库 图表列',{exact:true}).selectOption('2');
      await page.getByRole('img',{name:'数据库 当前页数值图',exact:true}).waitFor();
      assert.equal(await page.locator('svg[aria-label="数据库 当前页数值图"] circle').count(),2);
      assert.equal(requests.length,2,'Chart must reuse page, never query database');
      await page.getByRole('button',{name:'下一页',exact:true}).click();
      await page.getByRole('button',{name:'下一页',exact:true}).waitFor();
      await page.getByText('观测三',{exact:true}).waitFor();
      assert.equal(await page.getByRole('button',{name:'下一页',exact:true}).isDisabled(),true);
      await page.getByLabel('数据库 起始行',{exact:true}).fill('0');
      for(const c of fixtures.first.choices.tables.find(t=>t.id===fixtures.first.selected.table).columns){
        if(c.previewable&&!fixtures.columns.selected.columns.includes(c.id))await page.getByLabel(`数据库 列 ${c.id} ${c.label}`,{exact:true}).uncheck();
      }
      assert.equal(requests.length,3);assert.equal(await page.getByRole('table',{name:'数据库 数据分页',exact:true}).count(),0);
      await page.getByRole('button',{name:'读取数据库分页',exact:true}).click();
      await page.getByRole('table',{name:'数据库 数据分页',exact:true}).waitFor();
      assert.equal(await page.locator('table[aria-label="数据库 数据分页"] thead th').count(),3);
      assert.deepEqual(await page.getByRole('alert').allTextContents(),[]);
      return {metadataFirst:true,versionPinned:true,integerDecimalExact:true,unsafeHtmlInert:true,unsupportedColumnsDisabled:true,chartReusesPage:true,selectedColumnsOnly:true,requests:requests.length};
    },
    beforeUnmount:page=>page.evaluate(()=>{window.__heldDatabase=document.querySelector('table[aria-label="数据库 数据分页"]')}),
    verifyCleanup:async page=>{assert.equal(await page.evaluate(()=>!window.__heldDatabase?.isConnected),true);return {detached:true}},
  };
});

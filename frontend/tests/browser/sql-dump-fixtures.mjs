import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const fixtures=JSON.parse(readFileSync(new URL('./sql-dump-data.json',import.meta.url),'utf8'));

export const sqlDumpCases=['pages','empty','dialect-reset'].map(mode=>{
  let requests=[];
  return {name:`sql-dump-${mode}`,component:'DatabaseDumpPreview.vue',filename:'synthetic.sql',reader:'sql-dump',
    descriptor:{adapter:'database-dump',capabilities:{operations:['preview'],input_mode:'whole',shared:false}},file:{size:fixtures.tree.metadata.source_bytes},
    init:async()=>{requests=[]},
    preview:request=>{
      requests.push(request);
      if(request.kind==='tree'){assert.deepEqual(request.options,{dialect:'sqlite'});return fixtures.tree}
      assert.equal(request.kind,'table');assert.equal(request.version,'1'.repeat(64));
      const o=request.options,key=o.table===fixtures.empty.selected.table?'empty':o.columns.length===2?'columns':o.row_offset===0?'first':'second';
      assert.deepEqual(o,fixtures[key].selected);return fixtures[key];
    },
    ready:page=>page.getByRole('button',{name:'读取 SQL 转储目录',exact:true}).waitFor(),
    verify:async page=>{
      assert.equal(requests.length,0,'No dialect inference or automatic file request');
      assert.equal(await page.getByRole('button',{name:'读取 SQL 转储目录',exact:true}).isDisabled(),true);
      assert.match(await page.getByRole('note').textContent(),/不执行 SQL、不恢复数据库/);
      await page.getByLabel('SQL 转储方言',{exact:true}).selectOption('sqlite');
      assert.equal(requests.length,0,'Selecting a dialect must not trigger parsing');
      await page.getByRole('button',{name:'读取 SQL 转储目录',exact:true}).click();
      await page.getByRole('button',{name:'读取 SQL 字面量分页',exact:true}).waitFor();
      assert.equal(requests.length,1);assert.equal(await page.getByRole('table',{name:'SQL 转储字面量分页',exact:true}).count(),0);
      await page.getByLabel('SQL 转储本页行数',{exact:true}).fill('2');
      if(mode==='empty'){
        await page.getByLabel('SQL 转储数据表',{exact:true}).selectOption(fixtures.empty.selected.table);
        await page.getByRole('button',{name:'读取 SQL 字面量分页',exact:true}).click();
        await page.getByText('当前分页为空，未自动调整起始行。',{exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'下一页',exact:true}).isDisabled(),true);
        return {explicitDialect:true,manualDirectory:true,emptyExplicit:true,requests:requests.length};
      }
      await page.getByLabel('SQL 转储数据表',{exact:true}).selectOption(fixtures.first.selected.table);
      assert.equal(requests.length,1);
      await page.getByRole('button',{name:'读取 SQL 字面量分页',exact:true}).click();
      await page.getByRole('table',{name:'SQL 转储字面量分页',exact:true}).waitFor();
      const text=await page.getByRole('table',{name:'SQL 转储字面量分页',exact:true}).textContent();
      for(const row of fixtures.first.table.rows)for(const cell of row)if(['number-literal','text'].includes(cell.type)&&cell.value)assert.ok(text.includes(cell.value));
      await page.getByText('<img src=x onerror=alert(1)>',{exact:true}).waitFor();
      assert.equal(await page.locator('td img').count(),0);
      assert.equal(await page.locator('svg').count(),0,'SQL literal text must not be converted into a numeric chart');
      if(mode==='dialect-reset'){
        await page.getByLabel('SQL 转储方言',{exact:true}).selectOption('postgres');
        assert.equal(requests.length,2,'Dialect change cancels/clears but does not auto-request');
        assert.equal(await page.getByRole('table',{name:'SQL 转储字面量分页',exact:true}).count(),0);
        assert.equal(await page.getByLabel('SQL 转储数据表',{exact:true}).count(),0);
        await page.getByLabel('SQL 转储方言',{exact:true}).selectOption('sqlite');
        assert.equal(requests.length,2);
        await page.getByRole('button',{name:'读取 SQL 转储目录',exact:true}).click();
        await page.getByRole('button',{name:'读取 SQL 字面量分页',exact:true}).waitFor();
        assert.equal(requests.length,3);
        return {explicitDialect:true,manualDirectory:true,changedDialectClearsOldState:true,requests:requests.length};
      }
      await page.getByRole('button',{name:'下一页',exact:true}).click();
      await page.getByText('观测三',{exact:true}).waitFor();
      assert.equal(await page.getByRole('button',{name:'下一页',exact:true}).isDisabled(),true);
      await page.getByLabel('SQL 转储起始行',{exact:true}).fill('0');
      for(const c of fixtures.first.choices.tables.find(t=>t.id===fixtures.first.selected.table).columns){
        if(!fixtures.columns.selected.columns.includes(c.id))await page.getByLabel(`SQL 转储列 ${c.id} ${c.label}`,{exact:true}).uncheck();
      }
      assert.equal(requests.length,3);assert.equal(await page.getByRole('table',{name:'SQL 转储字面量分页',exact:true}).count(),0);
      await page.getByRole('button',{name:'读取 SQL 字面量分页',exact:true}).click();
      await page.getByRole('table',{name:'SQL 转储字面量分页',exact:true}).waitFor();
      assert.equal(await page.locator('table[aria-label="SQL 转储字面量分页"] thead th').count(),3);
      assert.deepEqual(await page.getByRole('alert').allTextContents(),[]);
      return {explicitDialect:true,manualDirectory:true,versionPinned:true,exactLiterals:true,unsafeHtmlInert:true,noNumericCoercion:true,selectedColumnsOnly:true,requests:requests.length};
    },
    beforeUnmount:page=>page.evaluate(()=>{window.__heldSqlDump=document.querySelector('table[aria-label="SQL 转储字面量分页"]')}),
    verifyCleanup:async page=>{assert.equal(await page.evaluate(()=>!window.__heldSqlDump?.isConnected),true);return {detached:true}},
  };
});

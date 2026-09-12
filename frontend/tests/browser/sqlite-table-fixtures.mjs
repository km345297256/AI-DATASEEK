import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const fixtures=JSON.parse(readFileSync(new URL('./sqlite-table-data.json',import.meta.url),'utf8'));
const table=fixtures.first.selected.table;
export const sqliteTableCases=['typed-pages','empty'].map(mode=>{
 let requests=[];
 return {name:`sqlite-table-${mode}`,component:'SqliteTablePreview.vue',filename:'synthetic.sqlite',reader:'sqlite-table',
  descriptor:{adapter:'sqlite-table',capabilities:{operations:['preview'],input_mode:'whole',shared:false}},file:{size:fixtures.tree.metadata.source_bytes},
  init:async()=>{requests=[]},
  preview:request=>{
   requests.push(request);
   if(request.kind==='tree'){assert.deepEqual(request.options,{});return fixtures.tree}
   assert.equal(request.kind,'table');assert.equal(request.version,'1'.repeat(64));
   const o=request.options,key=o.table===fixtures.empty.selected.table?'empty':o.columns.length===2?'columns':o.row_offset===0?'first':o.row_offset===2?'second':'last';
   assert.deepEqual(o,fixtures[key].selected);return fixtures[key];
  },
  ready:page=>page.getByRole('button',{name:'读取 SQLite 分页',exact:true}).waitFor(),
  verify:async page=>{
   assert.equal(requests.length,1);assert.equal(await page.getByRole('table',{name:'SQLite 数据分页'}).count(),0);
   const note=await page.getByRole('note').textContent();assert.match(note,/整文件最多 16 MiB/);assert.match(note,/不是大库分块/);
   await page.getByLabel('SQLite 本页行数',{exact:true}).fill('2');
   if(mode==='empty'){
    await page.getByRole('button',{name:'读取 SQLite 分页',exact:true}).click();
    await page.getByText('当前分页为空，未自动调整起始行。',{exact:true}).waitFor();
    assert.equal(requests.length,2);assert.equal(await page.getByRole('button',{name:'下一页',exact:true}).isDisabled(),true);
    return {metadataOnlyInitially:true,emptyWithoutFabricatedRows:true,versionPinned:true};
   }
   await page.getByLabel('SQLite 数据表',{exact:true}).selectOption(table);
   assert.equal(requests.length,1);
   await page.getByRole('button',{name:'读取 SQLite 分页',exact:true}).click();
   await page.getByText('-9223372036854775808',{exact:true}).waitFor();
   assert.equal(await page.getByText('9223372036854775807',{exact:true}).count(),1);
   assert.match(await page.getByRole('table',{name:'SQLite 数据分页'}).textContent(),/BLOB · 3 字节（未解码）/);
   await page.getByRole('button',{name:'下一页',exact:true}).click();
   await page.getByText('9007199254740993',{exact:true}).waitFor();
   let text=await page.getByRole('table',{name:'SQLite 数据分页'}).textContent();assert.match(text,/文本已省略 · 513 字节/);assert.match(text,/非有限实数/);assert.doesNotMatch(text,/\/private\/hidden/);
   await page.getByRole('button',{name:'下一页',exact:true}).click();
   await page.getByText('<img src=x onerror=alert(1)>',{exact:true}).waitFor();
   assert.equal(await page.locator('td img').count(),0);assert.equal(await page.getByRole('button',{name:'下一页',exact:true}).isDisabled(),true);
   await page.getByLabel('SQLite 起始行',{exact:true}).fill('0');
   for(const [id,name]of [[0,'id'],[2,'signal'],[4,'payload'],[5,'optional']])await page.getByLabel(`SQLite 列 ${id} ${name}`,{exact:true}).uncheck();
   assert.equal(requests.length,4);assert.equal(await page.getByRole('table',{name:'SQLite 数据分页'}).count(),0);
   await page.getByRole('button',{name:'读取 SQLite 分页',exact:true}).click();await page.getByText('-9223372036854775808',{exact:true}).waitFor();
   assert.equal(requests.length,5);assert.equal(await page.locator('thead th').count(),3);assert.deepEqual(await page.locator('[role=alert]').allTextContents(),[]);
   return {metadataOnlyInitially:true,versionPinned:true,int64Exact:true,blobMetadataOnly:true,omissionExplicit:true,nonfiniteSeparate:true,inertHtml:true,selectedColumnsOnly:true,wholeFileLimit:16777216,requests:requests.length};
  },
  beforeUnmount:page=>page.evaluate(()=>{window.__heldSqlite=document.querySelector('table[aria-label="SQLite 数据分页"]')}),
  verifyCleanup:async page=>{const detached=await page.evaluate(()=>!window.__heldSqlite?.isConnected);assert.equal(detached,true);return {detached}}
 };
});

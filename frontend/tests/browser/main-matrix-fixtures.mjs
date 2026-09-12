import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const data=JSON.parse(readFileSync(new URL('./main-matrix-data.json',import.meta.url),'utf8'));
let requests=[];
export const mainMatrixCases=[{name:'main-migration-matrix-workbench',component:'MatrixWorkbenchPreview.vue',filename:'synthetic.npz',reader:'matrix-workbench',descriptor:{adapter:'matrix-workbench',view_kind:'image'},file:{size:data.tree.metadata.source_bytes},init:async()=>{requests=[];},
 preview:r=>{requests.push(r);if(r.kind==='tree'){assert.deepEqual(r.options,{});return data.tree;}assert.equal(r.version,'1'.repeat(64));if(r.kind==='series'){assert.deepEqual(r.options,{});return data.series;}assert.equal(r.kind,'image');const found=r.options.max_points===32?data.orthogonal.find(v=>JSON.stringify(v.selected.axes)===JSON.stringify(r.options.axes)):data.image;assert.deepEqual(r.options,found.selected);return found;},
 ready:p=>p.getByRole('button',{name:'显示矩阵切片',exact:true}).waitFor(),verify:async p=>{
  assert.equal(requests.length,1);assert.equal(await p.locator('canvas').count(),0);await p.getByRole('button',{name:'显示矩阵切片',exact:true}).click();await p.waitForFunction(()=>document.querySelector('canvas')?.dataset.matrixValues==='12');
  assert.equal(requests.length,5);assert.equal(await p.getByRole('img',{name:'张量正交切面'}).count(),3);
  const painted=await p.locator('canvas').evaluate(c=>{const pixels=c.getContext('2d').getImageData(0,0,c.width,c.height).data;return {width:c.width,height:c.height,colored:Array.from(pixels).filter(v=>v!==0).length};});assert.equal(painted.width,4);assert.equal(painted.height,3);assert.ok(painted.colored>20);
  await p.getByLabel('色带',{exact:true}).selectOption('gray');await p.getByRole('checkbox',{name:'对称对数'}).check();await p.getByRole('button',{name:'放大',exact:true}).click();assert.equal(requests.length,5);
  await p.getByLabel('显示方式',{exact:true}).selectOption('table');assert.equal(await p.locator('tbody tr').count(),3);assert.match(await p.locator('table').innerText(),/11/);
  await p.getByRole('button',{name:'读取已保存结果曲线',exact:true}).click();await p.getByRole('img',{name:'特征值分布',exact:true}).waitFor();assert.equal(requests.length,6);assert.equal(await p.locator('svg[aria-label="特征值分布"] circle').count(),2);assert.deepEqual(await p.locator('[role=alert]').allTextContents(),[]);
  await p.getByLabel('显示方式',{exact:true}).selectOption('heatmap');return {realCanvas:true,orthogonalPlanes:3,savedCurves:4,requests:requests.length,localPainting:true};
 },beforeUnmount:p=>p.evaluate(()=>{window.__matrixCanvas=document.querySelector('canvas');}),verifyCleanup:async p=>{const value=await p.evaluate(()=>({detached:!window.__matrixCanvas.isConnected,width:window.__matrixCanvas.width,height:window.__matrixCanvas.height}));assert.deepEqual(value,{detached:true,width:0,height:0});return value;}}];

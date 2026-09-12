import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const data=JSON.parse(readFileSync(new URL('./geometry-data.json',import.meta.url),'utf8'));
export const geometryCases=['gro','mesh','cell','null','only'].map(which=>{
 const gro=which==='gro',prefix=gro?'gro':'mesh',reader=gro?'gro-trajectory':'simulation-mesh',result=data[gro?'gro_geometry':which==='mesh'?'mesh_geometry':`mesh_${which}`];let requests=[];
 return {name:`domain-expansion-geometry-${which}`,component:gro?'GroTrajectoryPreview.vue':'SimulationMeshPreview.vue',filename:`synthetic.${gro?'gro':'vtu'}`,reader,descriptor:{adapter:reader,view_kind:'structure'},file:{size:result.metadata.source_bytes},init:async()=>{requests=[];},
 preview:r=>{requests.push(r);if(r.kind==='tree'){assert.deepEqual(r.options,{});return data[`${prefix}_tree`];}assert.equal(r.kind,'geometry');assert.equal(r.version,'1'.repeat(64));assert.deepEqual(r.options,result.selected);return result;},
 ready:p=>p.getByRole('button',{name:gro?'显示所选帧':'绘制所选网格',exact:true}).waitFor(),
 verify:async p=>{
  assert.equal(requests.length,1);assert.equal(await p.locator('canvas').count(),0);
  if(gro)await p.getByLabel('轨迹帧',{exact:true}).selectOption('1');else if(which!=='only'){await p.getByLabel('网格场',{exact:true}).selectOption(result.selected.field);await p.getByLabel('场分量',{exact:true}).selectOption(String(result.selected.component));}
  assert.equal(requests.length,1);await p.getByRole('button',{name:gro?'显示所选帧':'绘制所选网格',exact:true}).click();
  await p.waitForFunction(()=>document.querySelector('canvas')?.dataset.geometryPoints);
  const actual=await p.locator('canvas').evaluate(c=>{const gl=c.getContext('webgl2'),pixels=new Uint8Array(c.width*c.height*4);gl.readPixels(0,0,c.width,c.height,gl.RGBA,gl.UNSIGNED_BYTE,pixels);let colored=0;for(let i=0;i<pixels.length;i+=4)if(Math.max(pixels[i],pixels[i+1],pixels[i+2])-Math.min(pixels[i],pixels[i+1],pixels[i+2])>50)colored++;return {points:+c.dataset.geometryPoints,edges:+c.dataset.geometryEdges,association:c.dataset.geometryAssociation,width:c.width,height:c.height,colored};});
  assert.equal(actual.points,gro?4:which==='cell'?2:5);assert.equal(actual.edges,gro?0:9);assert.equal(actual.association,gro?'atoms':which==='only'?'none':which==='cell'?'cell':'point');assert.ok(actual.width>400&&actual.height>=400);assert.ok(actual.colored>5);assert.equal(requests.length,2);assert.deepEqual(await p.locator('[role=alert]').allTextContents(),[]);
  const text=await p.getByTestId('geometry-values').textContent();if(gro){assert.match(text,/0\.01/);assert.match(text,/P0/);assert.match(await p.getByTestId('gro-box').textContent(),/0\.2/);}if(which==='cell')assert.match(text,/0\.25/);if(which==='null')assert.match(text,/—/);
  await p.locator('canvas').hover();await p.mouse.wheel(0,-120);assert.equal(requests.length,2);return {realWebGL:true,pinnedSelection:true,noInteractionRequest:true,actual};
 },beforeUnmount:p=>p.evaluate(()=>{window.__geometryCanvas=document.querySelector('canvas');window.__geometryLost=false;window.__geometryCanvas.addEventListener('webglcontextlost',()=>{window.__geometryLost=true;});}),
 verifyCleanup:async p=>{await p.waitForFunction(()=>window.__geometryLost);const value=await p.evaluate(()=>({detached:!window.__geometryCanvas.isConnected,contextLost:window.__geometryLost}));assert.deepEqual(value,{detached:true,contextLost:true});return value;}};
});

/** Actual canvas pixels compared against pydicom's independently generated oracle. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const data=JSON.parse(readFileSync(new URL('./domain-expansion-dicom-data.json',import.meta.url),'utf8'));
const oracle=JSON.parse(readFileSync(new URL('./domain-expansion-dicom-oracle.json',import.meta.url),'utf8'));
const base={component:'DicomWindowPreview.vue',filename:'synthetic.dcm',reader:'dicom-window',descriptor:{adapter:'dicom-window',capabilities:{operations:['preview'],input_mode:'window',shared:false}}};
async function select(page){await page.getByLabel('DICOM 帧',{exact:true}).fill('1');for(const key of ['列','行'])await page.getByLabel(`DICOM ${key}`,{exact:true}).fill('1');for(const key of ['宽','高'])await page.getByLabel(`DICOM ${key}`,{exact:true}).fill('2');await page.getByLabel('确认 DICOM 已脱敏',{exact:true}).check();}
const pixels=page=>page.getByTestId('dicom-canvas').evaluate(c=>({shape:[c.height,c.width],rgba:[...c.getContext('2d').getImageData(0,0,c.width,c.height).data],bounds:c.getBoundingClientRect().toJSON()}));
export const domainExpansionDicomCases=['mono2','mono1'].map(group=>{let requests=[];const fixture=data[group],photo=fixture.tree.choices.image.photometric;
  return {...base,name:`domain-expansion-dicom-${group}`,file:{size:fixture.tree.metadata.source_bytes},init:async()=>{requests=[];},
    preview:req=>{requests.push(req);if(req.kind==='tree'){assert.deepEqual(req.options,{});return fixture.tree;}assert.equal(req.version,'1'.repeat(64));assert.deepEqual(req.options,fixture.image.selected);return fixture.image;},
    ready:page=>page.getByRole('button',{name:'读取 DICOM 区域',exact:true}).waitFor(),
    verify:async page=>{
      assert.equal(requests.length,1);assert.equal(await page.locator('canvas').count(),0);assert.equal(await page.getByRole('button',{name:'读取 DICOM 区域',exact:true}).isEnabled(),false);
      await select(page);assert.equal(requests.length,1);await page.getByRole('button',{name:'读取 DICOM 区域',exact:true}).click();await page.getByTestId('dicom-canvas').waitFor();
      const original=await pixels(page);assert.deepEqual(original.shape,[2,2]);assert.deepEqual(original.rgba,oracle.display.find(r=>r.mono===photo&&r.center===1350).rgba);
      await page.evaluate(()=>{window.__oldDicomCanvas=document.querySelector('[data-testid=dicom-canvas]');});
      await page.getByLabel('DICOM 窗位',{exact:true}).fill('1250');await page.getByLabel('DICOM 窗宽',{exact:true}).fill('1');
      const contrast=await pixels(page);assert.deepEqual(contrast.rgba,oracle.display.find(r=>r.mono===photo&&r.center===1250).rgba);assert.equal(requests.length,2,'Window/level uses cached ROI without any file/API reads');
      assert.equal(await page.evaluate(()=>window.__oldDicomCanvas.width===0&&!window.__oldDicomCanvas.isConnected),true);
      await page.getByLabel('确认 DICOM 已脱敏',{exact:true}).uncheck();assert.equal(await page.locator('canvas').count(),0);assert.equal(requests.length,2);
      await page.getByLabel('确认 DICOM 已脱敏',{exact:true}).check();assert.equal(requests.length,2);assert.equal(await page.locator('canvas').count(),0);
      await page.getByRole('button',{name:'读取 DICOM 区域',exact:true}).click();await page.getByTestId('dicom-canvas').waitFor();assert.equal(requests.length,3);
      const text=await page.locator('section').innerText();assert.match(text,/不用于诊断/);assert.match(text,/不保证像素真正脱敏/);assert.doesNotMatch(text,/SYNTHETIC-NAME|SYNTHETIC-ID|1\.2\.826/);
      assert.deepEqual(await page.locator('[role=alert]').allTextContents(),[]);await page.getByTestId('dicom-canvas').scrollIntoViewIfNeeded();const bounds=await page.getByTestId('dicom-canvas').boundingBox();assert.ok(bounds.width>=400&&bounds.height>=300);
      return {realCanvas:true,pydicomOracle:true,rawStorage:[1110,1120,1210,1220],originalPixels:original.rgba,localContrastPixels:contrast.rgba,noReadForWindowLevel:true,explicitDeidentifiedConfirmation:true,unconfirmPurges:true,metadataNotDisplayed:true,bounds};
    },
    beforeUnmount:page=>page.evaluate(()=>{window.__dicomCanvas=document.querySelector('[data-testid=dicom-canvas]');}),
    verifyCleanup:async page=>{const clean=await page.evaluate(()=>({detached:!window.__dicomCanvas.isConnected,width:window.__dicomCanvas.width,height:window.__dicomCanvas.height}));assert.deepEqual(clean,{detached:true,width:0,height:0});return clean;},
  };
});
domainExpansionDicomCases.push({...base,name:'domain-expansion-dicom-wrong-frame',file:{size:data.mono2.tree.metadata.source_bytes},
  preview:req=>{if(req.kind==='tree')return data.mono2.tree;const r=structuredClone(data.mono2.image);r.selected.frame=0;return r;},
  setup:async page=>{await page.getByRole('button',{name:'读取 DICOM 区域',exact:true}).waitFor();await select(page);await page.getByRole('button',{name:'读取 DICOM 区域',exact:true}).click();},
  ready:page=>page.locator('[role=alert]').waitFor(),expectedError:/DICOM 响应或窗口选择无效/,
  verify:async page=>{assert.equal(await page.locator('canvas').count(),0);return {wrongFrameRejectedBeforeCanvas:true};},
});
domainExpansionDicomCases.push({...base,name:'domain-expansion-dicom-phi-envelope',file:{size:data.mono2.tree.metadata.source_bytes},
  preview:()=>{const r=structuredClone(data.mono2.tree);r.metadata.PatientName='SYNTHETIC-PHI-MUST-NOT-DISPLAY';return r;},
  ready:page=>page.locator('[role=alert]').waitFor(),expectedError:/DICOM 响应或窗口选择无效/,
  verify:async page=>{assert.equal(await page.locator('canvas').count(),0);assert.doesNotMatch(await page.locator('body').innerText(),/SYNTHETIC-PHI/);return {unexpectedPhiFieldsRejected:true};},
});

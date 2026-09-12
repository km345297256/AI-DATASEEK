import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const data=JSON.parse(readFileSync(new URL('./main-bio-data.json',import.meta.url),'utf8'));
export const mainBioCases=[];
for(const key of ['fasta','fastq']){
 let requests=[];
 mainBioCases.push({name:`main-migration-bio-${key}`,component:'SequenceBrowserPreview.vue',filename:`synthetic.${key==='fasta'?'fa':'fq'}`,reader:'sequence-browser',descriptor:{adapter:'sequence-browser',view_kind:'table'},file:{size:data[key].metadata.source_bytes},init:async()=>{requests=[];},
 preview:r=>{requests.push(r);if(r.kind==='tree'){assert.deepEqual(r.options,{});return data[key+'_tree'];}assert.equal(r.kind,'table');assert.equal(r.version,'1'.repeat(64));const option=r.options;let which=key;if(key==='fasta'&&option.motif==='ACG')which=option.start===5?'fasta_next':'fasta_search';assert.deepEqual(option,data[which].selected);return data[which];},
 ready:p=>p.getByRole('button',{name:'显示所选序列',exact:true}).waitFor(),
 verify:async p=>{assert.equal(requests.length,1);assert.equal(await p.getByTestId('sequence-bases').count(),0);await p.getByLabel('序列窗口',{exact:true}).selectOption('50');assert.equal(requests.length,1);await p.getByRole('button',{name:'显示所选序列',exact:true}).click();await p.getByTestId('sequence-bases').waitFor();assert.match(await p.getByTestId('sequence-range').textContent(),key==='fasta'?/1–50/:/1–6/);
   if(key==='fasta'){await p.getByLabel('序列模体',{exact:true}).fill('ACG');await p.getByRole('button',{name:'搜索',exact:true}).click();await p.getByTestId('sequence-search-count').waitFor();assert.match(await p.getByTestId('sequence-search-count').textContent(),/300 处命中/);assert.equal(await p.getByTestId('sequence-bases').locator('.bg-yellow-200').count(),38);await p.getByRole('button',{name:'下个命中',exact:true}).click();await p.getByTestId('sequence-bases').waitFor();await p.getByRole('button',{name:'下个命中',exact:true}).click();await p.getByTestId('sequence-bases').waitFor();assert.match(await p.getByTestId('sequence-range').textContent(),/5–54/);}
   else{assert.equal(await p.getByLabel('逐碱基质量轨道',{exact:true}).count(),1);assert.match(await p.getByTestId('sequence-statistics').textContent(),/Q28\.83/);assert.match(await p.getByTestId('sequence-statistics').textContent(),/Q20 以下 1 个/);assert.equal(await p.locator('[title="位置 1 Q0"]').count(),1);}
   assert.deepEqual(await p.locator('[role=alert]').allTextContents(),[]);return {explicitPinned:true,search:key==='fasta',quality:key==='fastq',requests:requests.length};}});
}
for(const key of ['bed','wig','bedgraph']){
 let requests=[];
 mainBioCases.push({name:`main-migration-bio-${key}`,component:'GenomeTracksPreview.vue',filename:`synthetic.${key}`,reader:'genome-tracks',descriptor:{adapter:'genome-tracks',view_kind:'map'},file:{size:data[key].metadata.source_bytes},init:async()=>{requests=[];},
 preview:r=>{requests.push(r);if(r.kind==='tree')return data[key+'_tree'];assert.equal(r.kind,'map');assert.equal(r.version,'1'.repeat(64));assert.deepEqual(Object.keys(r.options).sort(),['chromosome','end','start']);const result=structuredClone(data[key]);result.selected=r.options;result.tracks=result.tracks.filter(f=>f.chromosome===r.options.chromosome&&(f.start===f.end?r.options.start<=f.start&&f.start<r.options.end:f.start<r.options.end&&f.end>r.options.start));return result;},
 ready:p=>p.getByRole('button',{name:'绘制所选区域',exact:true}).waitFor(),
 verify:async p=>{assert.equal(requests.length,1);assert.equal(await p.getByLabel('基因组交互轨道',{exact:true}).count(),0);await p.getByLabel('轨道起始位置',{exact:true}).fill('1');await p.getByLabel('轨道结束位置',{exact:true}).fill(key==='bed'?'200':'100');await p.getByRole('button',{name:'绘制所选区域',exact:true}).click();await p.getByLabel('基因组交互轨道',{exact:true}).waitFor();assert.equal(await p.locator('[data-track-feature]').count(),key==='bed'?3:2);
   const label=key==='bed'?'区间轨道':'信号轨道';await p.getByLabel(label,{exact:true}).uncheck();assert.equal(await p.locator('[data-track-feature]').count(),0);await p.getByLabel(label,{exact:true}).check();assert.equal(await p.locator('[data-track-feature]').count(),key==='bed'?3:2);assert.equal(requests.length,2);
   await p.locator('[data-track-feature]').first().click();await p.getByTestId('track-detail').waitFor();assert.match(await p.getByTestId('track-detail').textContent(),key==='bed'?/方向 \+/:/值 4/);
   await p.getByLabel('轨道放大',{exact:true}).click();await p.getByLabel('基因组交互轨道',{exact:true}).waitFor();assert.equal(requests.length,3);assert.ok(requests[2].options.end-requests[2].options.start<(key==='bed'?200:100));assert.deepEqual(await p.locator('[role=alert]').allTextContents(),[]);return {pinnedRegion:true,toggleNoRead:true,zoomReload:true,noSignalInterpolation:true};}});
}
for(const key of ['blast12','blast13']){
 let requests=[];
 mainBioCases.push({name:`main-migration-bio-${key}`,component:'BlastHitsPreview.vue',filename:`synthetic.${key==='blast12'?'m8':'blast'}`,reader:'blast-hits',descriptor:{adapter:'blast-hits',view_kind:'table'},file:{size:data[key].metadata.source_bytes},init:async()=>{requests=[];},
 preview:r=>{requests.push(r);if(r.kind==='tree')return data[key+'_tree'];assert.equal(r.kind,'table');assert.equal(r.version,'1'.repeat(64));const which=r.options.min_identity===90?'blast_filter':key;assert.deepEqual(r.options,data[which].selected);return data[which];},
 ready:p=>p.getByRole('button',{name:'筛选并显示命中',exact:true}).waitFor(),
 verify:async p=>{assert.equal(requests.length,1);assert.equal(await p.getByTestId('blast-table').count(),0);await p.getByRole('button',{name:'筛选并显示命中',exact:true}).click();await p.getByTestId('blast-table').waitFor();assert.equal(await p.locator('[data-blast-hit]').count(),key==='blast12'?3:2);assert.match(await p.getByTestId('blast-table').textContent(),/1e-350/);assert.match(await p.getByTestId('blast-table').textContent(),key==='blast12'?/未知/:/10\.00%/);
   await p.getByRole('button',{name:'subject-reverse',exact:true}).click();assert.match(await p.getByTestId('blast-detail').textContent(),/Query 400–301（反向）/);assert.match(await p.getByTestId('blast-detail').textContent(),/Subject 900–801（反向）/);
   if(key==='blast13'){await p.getByLabel('最低一致性',{exact:true}).fill('90');await p.getByLabel('最低覆盖度',{exact:true}).fill('5');await p.getByRole('button',{name:'筛选并显示命中',exact:true}).click();await p.getByTestId('blast-table').waitFor();assert.equal(await p.locator('[data-blast-hit]').count(),1);assert.match(await p.getByTestId('blast-total').textContent(),/筛选后 1 条/);}
   assert.deepEqual(await p.locator('[role=alert]').allTextContents(),[]);return {realDirection:true,correctCoverage:true,sourceEvalue:true,filter:key==='blast13'};}});
}

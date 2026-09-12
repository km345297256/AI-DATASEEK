import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { parseRippleWindow } from '../src/visualizations/extended/rippleWindowData.ts';
const fixture=JSON.parse(readFileSync(new URL('./browser/domain-expansion-ripple-data.json',import.meta.url),'utf8'));
function publicResult(kind) {
  const {metadata,kind:privateKind,contract_version,type,reader,warnings,sampled,...payload}=structuredClone(fixture[kind]);
  return {metadata,payload:{...payload,view_kind:kind}};
}
const widths={int8:1,uint8:1,int16:2,uint16:2,int32:4,uint32:4,float32:4,float64:8};
for(const kind of ['image','series'])for(const [dtype,width] of Object.entries(widths)) {
  function typed() {
    const r=publicResult(kind),c=r.payload.choices.cube;
    c.dtype=dtype;c.byte_order=width===1?'dont-care':'big-endian';
    r.payload.array.dtype=dtype;r.payload.array.values=r.payload.array.values.map((_,i)=>i);
    r.metadata.data_bytes=c.data_offset+c.width*c.height*c.depth*width;
    r.metadata.source_bytes=r.metadata.header_bytes+r.metadata.data_bytes;
    r.metadata.read_bytes=r.metadata.header_bytes+r.payload.array.values.length*width;r.metadata.read_requests=2;
    return r;
  }
  test(`${kind} ${dtype} exact minimum bytes remains valid`,()=>{const r=typed();assert.equal(parseRippleWindow(kind,r.payload,r.metadata).reads,2);});
  for(const mutation of ['one-read','header-only','one-byte-short'])test(`${kind} ${dtype} rejects ${mutation} result even with valid values`,()=>{
    const r=typed();
    if(mutation==='one-read')r.metadata.read_requests=1;
    else if(mutation==='header-only')r.metadata.read_bytes=r.metadata.header_bytes;
    else r.metadata.read_bytes--;
    assert.throws(()=>parseRippleWindow(kind,r.payload,r.metadata));
  });
}
for(const kind of ['tree','image','series'])for(const axis of [0,1,2])test(`${kind} default-origin false provenance axis ${axis}`,()=>{
  const r=publicResult(kind);assert.notEqual(r.payload.choices.cube.axes[axis].origin,0);
  r.payload.choices.cube.axes[axis].origin_defaulted=true;
  assert.throws(()=>parseRippleWindow(kind,r.payload,r.metadata));
});
for(const axis of [0,1,2])test(`zero default remains valid for declared axis ${axis}`,()=>{
  const r=publicResult('tree');Object.assign(r.payload.choices.cube.axes[axis],{origin:0,origin_defaulted:true});
  assert.ok(parseRippleWindow('tree',r.payload,r.metadata));
});
test('ev-per-chan may default to zero but never to an undeclared nonzero origin',()=>{
  const r=publicResult('tree'),axis=r.payload.choices.cube.axes[2];
  Object.assign(axis,{origin:0,origin_defaulted:true,calibration:'ev-per-chan'});
  assert.ok(parseRippleWindow('tree',r.payload,r.metadata));
  axis.origin=100;assert.throws(()=>parseRippleWindow('tree',r.payload,r.metadata));
});

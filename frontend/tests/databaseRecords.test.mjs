import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import * as vue from 'vue';
import {compileScript,parse} from '@vue/compiler-sfc';
import ts from 'typescript';
import {usePreviewLoad} from '../src/composables/usePreviewLoad.ts';
import * as identity from '../src/visualizations/previewIdentity.ts';
import * as helpers from '../src/visualizations/extended/databaseRecordsData.ts';

const fixtures=JSON.parse(readFileSync(new URL('./browser/database-records-data.json',import.meta.url),'utf8'));
const version='a'.repeat(64),revision='b'.repeat(64);
function result(reader='bson',key='tree'){
  const {contract_version,type,kind,metadata,warnings,sampled,reader:ignored,...payload}=structuredClone(fixtures[reader][key]);
  return {contract_version,plugin_id:reader==='bson'?'viz-bson':'viz-redis-rdb',kind,version,revision,metadata,warnings,sampled,payload:{...payload,view_kind:kind}};
}
for(const reader of ['bson','redis-rdb'])for(const key of Object.keys(fixtures[reader]))test(`records ${reader} ${key} matches strict v2 payload`,()=>{
  const r=result(reader,key),parsed=helpers.parseDatabaseRecords(r.kind,r.payload,r.metadata,r.kind==='table'?r.payload.selected:undefined);
  assert.equal(parsed.engine,reader);assert.equal(parsed.sourceBytes,fixtures[reader][key].metadata.source_bytes);
});
const mutations=[
  ['unknown engine',r=>r.metadata.engine='wiredtiger'],['wrong extension',r=>r.metadata.format='archive'],
  ['host path',r=>r.metadata.path='/Users/private'],['unknown payload option',r=>r.payload.sql='DROP TABLE t'],
  ['source boolean',r=>r.metadata.source_bytes=true],['oversized source',r=>r.metadata.source_bytes=16777217],
  ['wrong version',r=>r.metadata.format_version=11],['wrong checksum',r=>r.metadata.checksum='verified'],
  ['wrong ordering',r=>r.metadata.ordering='sorted'],['too many nodes',r=>r.metadata.nodes_returned=4097],
  ['wrong budget',r=>r.metadata.limits.max_depth=20],['wrong total',r=>r.metadata.total_records=1],
  ['unsafe group label',r=>r.payload.choices.groups[0].label='/Users/private'],
  ['wrong group database',r=>r.payload.choices.groups[0].database=0],['wrong count',r=>r.payload.choices.groups[0].record_count=1],
  ['unknown record type',r=>r.payload.choices.groups[0].counts={javascript:3}],
  ['boolean offset',r=>r.payload.selected.offset=true],['wrong page offset',r=>r.payload.table.offset=1],
  ['missing next page',r=>r.payload.table.has_more=false],['wrong index',r=>r.payload.table.records[0].index='1'],
  ['numeric index',r=>r.payload.table.records[0].index=0],['BSON key',r=>r.payload.table.records[0].key={type:'text',value:'k'}],
  ['BSON expiry',r=>r.payload.table.records[0].expires_at_ms='1'],['truncation hidden',r=>r.payload.table.records[0].truncated=false],
  ['root parent cycle',r=>r.payload.table.records[0].nodes[0].parent=0],['child parent cycle',r=>r.payload.table.records[0].nodes[1].parent=1],
  ['wrong child count',r=>r.payload.table.records[0].nodes[0].cell.count=0],['unknown node field',r=>r.payload.table.records[0].nodes[1].html='<img>'],
  ['unsafe field',r=>r.payload.table.records[0].nodes[1].key='/Users/private'],['false omitted label',r=>r.payload.table.records[0].nodes[1].key_omitted=true],
  ['coerced int64',r=>r.payload.table.records[0].nodes[2].cell.value=9223372036854775807],
  ['overflow int64',r=>r.payload.table.records[0].nodes[2].cell.value='9223372036854775808'],
  ['bad decimal bits',r=>r.payload.table.records[0].nodes[4].cell.bid='a'],
  ['bad decimal text',r=>r.payload.table.records[0].nodes[4].cell.value='eval(1)'],
];
for(const [name,change] of mutations)test(`records rejects ${name}`,()=>{const r=result('bson','first');change(r);assert.throws(()=>helpers.parseDatabaseRecords('table',r.payload,r.metadata))});
for(const bad of [{type:'text',value:'x'.repeat(513)},{type:'text',value:'\ud800'},{type:'text',value:'file:/secret'},
  {type:'integer',value:'+1'},{type:'integer',value:'01'},{type:'integer',value:'-0'},
  {type:'integer',value:'1\n'},{type:'date-ms',value:'1\n'},
  {type:'timestamp',seconds:'1\n',increment:'0'},{type:'timestamp',seconds:'1',increment:'0\n'},
  {type:'binary',bytes:1,subtype:'00\n'},{type:'objectid',value:'a'.repeat(24)+'\n'},
  {type:'date-ms',value:'1.25'},{type:'timestamp',seconds:'4294967296',increment:'0'},
  {type:'binary',bytes:true,subtype:null},{type:'binary',bytes:1,subtype:'gg'},
  {type:'omitted',bytes:512,reason:'text-budget'},{type:'unsupported',name:'eval'},
  {type:'real',value:Infinity},{type:'null',value:0},{type:'boolean',value:1},
  {type:'decimal128',value:'1.2301',bid:'0c300000000000000000000000003830'},
  {type:'decimal128',value:'1.23',bid:'0c300000000000000000000000003830'},
  {type:'decimal128',value:'0.0000',bid:'000000000000000000000000000038b0'},
  {type:'decimal128',value:'1e3',bid:'a'.repeat(32)}])test(`records invalid scalar ${JSON.stringify(bad)}`,()=>assert.throws(()=>helpers.validateRecordCell(bad)));
test('record text keeps precision, date milliseconds, null, false and inert HTML',()=>{
  assert.equal(helpers.recordCellText({type:'integer',value:'9007199254740993'}),'9007199254740993');
  assert.equal(helpers.recordCellText({type:'null',value:null}),'NULL');assert.equal(helpers.recordCellText({type:'text',value:''}),'空字符串');
  assert.equal(helpers.recordCellText({type:'boolean',value:false}),'false');
  assert.match(helpers.recordCellText({type:'decimal128',value:'1.2300',bid:'0c300000000000000000000000003830'}),/^1\.2300 · BID /);
  assert.equal(helpers.recordCellText({type:'date-ms',value:'-9223372036854775808'}),'-9223372036854775808（UTC Unix 毫秒）');
  assert.match(helpers.recordCellText({type:'timestamp',seconds:'4294967295',increment:'4294967295'}),/seconds=4294967295，increment=4294967295/);
  assert.equal(helpers.recordCellText({type:'text',value:'<img src=x onerror=alert(1)>'}),'<img src=x onerror=alert(1)>');
});
test('record node depth reflects parent indexes and nested structure',()=>{
  const r=result('bson','first'),data=helpers.parseDatabaseRecords('table',r.payload,r.metadata),record=data.records[1];
  const depths=helpers.recordNodeDepths(record);assert.equal(depths[0],0);assert.ok(Math.max(...depths)>=4);
  record.nodes.forEach((node,i)=>assert.equal(depths[i],node.parent===null?0:depths[node.parent]+1));
});
for(const patch of [{offset:true},{offset:-1},{offset:100001},{limit:0},{limit:51},{group_id:'anything'},{group_id:fixtures.bson.first.selected.group_id+'\n'},{sql:'SELECT 1'}])test(`bad record selection ${JSON.stringify(patch)}`,()=>assert.throws(()=>helpers.validateRecordSelection({...fixtures.bson.first.selected,...patch})));

const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();await vue.nextTick()};
const pending=()=>{let resolve;return {promise:new Promise(r=>resolve=r),resolve:v=>resolve(v)}};
function mount(t,response,reader='bson'){
  response??=options=>result(reader,options.kind==='tree'?'tree':options.offset===0?'first':'second');
  let depthCalculations=0;
  const seen=[],modules={vue,'../../composables/usePreviewLoad':{usePreviewLoad},'../previewIdentity':identity,'./databaseRecordsData':{...helpers,recordNodeDepths(record){depthCalculations++;return helpers.recordNodeDepths(record)}},
    '../runtime':{requestVisualization:async(file,plugin,operation,options,signal)=>{seen.push({file,plugin,operation,options,signal});return response(options)}}};
  const source=readFileSync(new URL('../src/visualizations/extended/DatabaseRecordsPreview.vue',import.meta.url),'utf8');
  const compiled=ts.transpileModule(compileScript(parse(source).descriptor,{id:'database-records'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText;
  const mod={exports:{}};new Function('require','module','exports',compiled)(id=>{assert.ok(id in modules,id);return modules[id]},mod,mod.exports);
  const props=vue.reactive({file:{file_id:'record-file',filename:'synthetic.'+(reader==='bson'?'bson':'rdb'),size:fixtures[reader].tree.metadata.source_bytes},plugin:{id:reader==='bson'?'viz-bson':'viz-redis-rdb',reader,adapter:'database-records',enabled:true,version:'1',capabilities:{operations:['preview'],input_mode:'whole',shared:false},limits:{max_input_bytes:16777216,max_output_bytes:2097152}}});
  const scope=vue.effectScope(),state=scope.run(()=>mod.exports.default.setup(props,{expose(){}}));t.after(()=>scope.stop());return {props,state,seen,depthCalculations:()=>depthCalculations,stop:()=>scope.stop()};
}
async function select(v){await flush();v.state.limit.value=2;await flush()}
for(const reader of ['bson','redis-rdb'])test(`${reader} component metadata first, explicit version-pinned paging only`,async t=>{
  const v=mount(t,undefined,reader);await select(v);assert.equal(v.seen.length,1);assert.equal(v.state.data.value,undefined);assert.equal(v.state.error.value,'');
  await v.state.loadPage();assert.equal(v.state.error.value,'');assert.equal(v.seen[1].options.version,version);assert.deepEqual(v.state.data.value.records,fixtures[reader].first.table.records);
  await v.state.page(1);assert.equal(v.state.error.value,'');assert.deepEqual(v.state.data.value.records,fixtures[reader].second.table.records);
  v.state.limit.value=1;assert.equal(v.state.data.value,undefined);assert.equal(v.seen.length,3);
});
for(const action of ['disable','unmount','file','group','offset','limit','plugin-budget'])test(`records late response after ${action} cannot publish`,async t=>{
  const p=pending(),v=mount(t,o=>o.kind==='tree'?result():p.promise);await select(v);const task=v.state.loadPage();await flush();
  if(action==='disable')v.props.plugin.enabled=false;else if(action==='unmount')v.stop();else if(action==='file')v.props.file.file_id='changed';
  else if(action==='group')v.state.groupId.value='g-'+'0'.repeat(24);else if(action==='offset')v.state.offset.value=2;else if(action==='limit')v.state.limit.value=1;else v.props.plugin.limits.max_input_bytes--;
  assert.equal(v.seen[1].signal.aborted,true);p.resolve(result('bson','first'));await task;assert.equal(v.state.data.value,undefined);
});
test('unchanged identities do not reload and disabling clears directory and records',async t=>{
  const v=mount(t);await select(v);await v.state.loadPage();v.props.file=structuredClone(vue.toRaw(v.props.file));v.props.plugin=structuredClone(vue.toRaw(v.props.plugin));await flush();assert.equal(v.seen.length,2);
  v.props.plugin.enabled=false;assert.equal(v.state.catalog.value,undefined);assert.equal(v.state.data.value,undefined);assert.equal(v.seen.length,2);
});
test('record directory version hash with trailing newline is rejected',async t=>{
  const v=mount(t,()=>{const response=result();response.version+='\n';return response});await flush();
  assert.equal(v.state.catalog.value,undefined);assert.notEqual(v.state.error.value,'');assert.equal(v.seen.length,1);
});
for(const change of ['kind','version','source','format','catalog','selection'])test(`records mismatched ${change} refused by component`,async t=>{
  const v=mount(t,o=>{const r=result('bson',o.kind==='tree'?'tree':'first');if(o.kind==='table'){
    if(change==='kind')r.kind='tree';if(change==='version')r.version='c'.repeat(64);if(change==='source')r.metadata.source_bytes++;if(change==='format')r.metadata.format='rdb';
    if(change==='catalog'){r.payload.choices.groups[0].record_count=4;r.payload.choices.groups[0].counts.document=4;r.metadata.total_records=4}
    if(change==='selection'){r.payload.selected.limit=3;r.payload.table.limit=3}
  }return r});await select(v);await v.state.loadPage();assert.notEqual(v.state.error.value,'');assert.equal(v.state.data.value,undefined);
});
test('Vue template uses inert interpolation and no embed, HTML injection or model tools',()=>{
  const source=readFileSync(new URL('../src/visualizations/extended/DatabaseRecordsPreview.vue',import.meta.url),'utf8');
  assert.doesNotMatch(source,/v-html|<iframe|eval\(|new Function|model\.generate/);
  assert.match(source,/recordCellText\(node\.cell\)/);
});
test('node depth layout computed once per loaded record and never causes reads',async t=>{
  const v=mount(t);await select(v);await v.state.loadPage();const reads=v.seen.length;
  const map=v.state.nodeDepths.value;assert.equal(map.size,2);assert.equal(v.depthCalculations(),2);
  for(let i=0;i<20;i++)assert.equal(v.state.nodeDepths.value,map);
  assert.equal(v.depthCalculations(),2);assert.equal(v.seen.length,reads);
  await v.state.page(1);assert.equal(v.state.nodeDepths.value.size,1);assert.equal(v.depthCalculations(),3);assert.equal(v.seen.length,reads+1);
});

const shapeNode=(id,parent,key,cell,key_omitted=false)=>({id,parent,key,key_omitted,cell});
function withShape(reader,nodes,truncated=false){
  const r=result(reader,'first');r.payload.table.records[0].nodes=nodes;r.payload.table.records[0].truncated=truncated;
  r.metadata.nodes_returned=r.payload.table.records.reduce((n,record)=>n+record.nodes.length,0);
  r.metadata.omitted_values=r.payload.table.records.reduce((n,record)=>n+Number(record.key?.type==='omitted')+record.nodes.reduce((s,node)=>s+Number(node.key_omitted)+Number(node.cell.type==='omitted'),0),0);
  r.metadata.unsupported_values=r.payload.table.records.reduce((n,record)=>n+record.nodes.filter(node=>node.cell.type==='unsupported').length,0);return r;
}
const badShapes=[
  ['bson','return to ended parent',[shapeNode(0,null,null,{type:'document',count:2}),shapeNode(1,0,'a',{type:'document',count:1}),shapeNode(2,0,'b',{type:'null',value:null}),shapeNode(3,1,'late',{type:'null',value:null})]],
  ['bson','Redis container in BSON',[shapeNode(0,null,null,{type:'document',count:1}),shapeNode(1,0,'bad',{type:'hash',count:0})]],
  ['bson','duplicate visible document field',[shapeNode(0,null,null,{type:'document',count:2}),shapeNode(1,0,'same',{type:'null',value:null}),shapeNode(2,0,'same',{type:'null',value:null})]],
  ['bson','noncanonical array index',[shapeNode(0,null,null,{type:'document',count:1}),shapeNode(1,0,'array',{type:'array',count:1}),shapeNode(2,1,'01',{type:'null',value:null})]],
  ['bson','missing BSON subtype',[shapeNode(0,null,null,{type:'document',count:1}),shapeNode(1,0,'binary',{type:'binary',bytes:1,subtype:null})]],
  ['redis-rdb','integer instead of Redis string',[shapeNode(0,null,null,{type:'list',count:1}),shapeNode(1,0,'0',{type:'integer',value:'1'})]],
  ['redis-rdb','nested object in hash',[shapeNode(0,null,null,{type:'hash',count:1}),shapeNode(1,0,'nested',{type:'array',count:0})]],
  ['redis-rdb','wrong list index',[shapeNode(0,null,null,{type:'list',count:1}),shapeNode(1,0,'1',{type:'text',value:'one'})]],
  ['redis-rdb','zset member score swapped',[shapeNode(0,null,null,{type:'zset',count:1}),shapeNode(1,0,'0',{type:'entry',count:2}),shapeNode(2,1,'score',{type:'text',value:'one'}),shapeNode(3,1,'member',{type:'real',value:1.25})]],
  ['redis-rdb','NaN zset score',[shapeNode(0,null,null,{type:'zset',count:1}),shapeNode(1,0,'0',{type:'entry',count:2}),shapeNode(2,1,'member',{type:'text',value:'one'}),shapeNode(3,1,'score',{type:'nonfinite',value:'NaN'})]],
];
for(const [reader,label,nodes] of badShapes)test(`records strict shape rejects ${label}`,()=>{const r=withShape(reader,nodes);assert.throws(()=>helpers.parseDatabaseRecords('table',r.payload,r.metadata))});
for(const [reader,type] of [['bson','document'],['redis-rdb','hash']])test(`${reader} distinct omitted field names may share placeholder`,()=>{
  const r=withShape(reader,[shapeNode(0,null,null,{type,count:2}),shapeNode(1,0,'字段名已省略',{type:'text',value:'one'},true),shapeNode(2,0,'字段名已省略',{type:'text',value:'two'},true)],true);
  assert.equal(helpers.parseDatabaseRecords('table',r.payload,r.metadata).records[0].nodes[2].key_omitted,true);
});

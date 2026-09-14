import {need, keys, int, intText, same, bytes} from './databasePreviewGuards.ts';

export const PHYSICAL_LIMITS={max_input_bytes:16777216,max_output_bytes:2097152,max_rows:1024,max_key_bytes:128,max_value_bytes:512};
export const PHYSICAL_COLUMNS={
  'mysql-sdi':['category','table','name','definition'],
  'sst-records':['key_hex','key_bytes','sequence','record_type','value_hex','value_bytes','omitted'],
};
export type PhysicalReader=keyof typeof PHYSICAL_COLUMNS;
const versions={'mysql-sdi':'8.0.46','sst-records':'6.11.4'};
const formats={'mysql-sdi':['ibd'],'sst-records':['sst','ldb']};
const label=(v:unknown):v is string=>typeof v==='string'&&Array.from(v).length>=1&&Array.from(v).length<=128&&v===v.trim()&&/^[\p{L}\p{N}_ .()-]+$/u.test(v);
export interface PhysicalData {reader:PhysicalReader;format:string;sourceBytes:number;toolVersion:string;columns:string[];rows:string[][]}
export function parsePhysical(payload:unknown,metadata:unknown,reader:PhysicalReader):PhysicalData{
  need(Object.prototype.hasOwnProperty.call(PHYSICAL_COLUMNS,reader));
  keys(payload,'view_kind media_type table choices selected');
  need(payload.view_kind==='tree'&&payload.media_type==='application/json');keys(payload.choices,'');keys(payload.selected,'');
  keys(metadata,'engine format source_bytes input_mode tool_version rows_returned logical_state_verified limits');
  const m=metadata;need(m.engine===reader&&formats[reader].includes(m.format)&&int(m.source_bytes,48,16777216)&&m.input_mode==='whole'&&m.tool_version===versions[reader]&&m.logical_state_verified===false&&same(m.limits,PHYSICAL_LIMITS));
  keys(payload.table,'columns rows');const {columns,rows}=payload.table;
  need(same(columns,PHYSICAL_COLUMNS[reader])&&Array.isArray(rows)&&rows.length<=1024&&int(m.rows_returned,rows.length,rows.length));
  for(const row of rows){
    need(Array.isArray(row)&&row.length===columns.length&&row.every(v=>typeof v==='string'));
    if(reader==='mysql-sdi'){
      const [category,parent,name,definition]=row;
      need(['table','column','index'].includes(category)&&label(parent)&&label(name)&&definition.length<=512&&definition===definition.trim());
      if(category==='table')need(parent==='-'&&definition==='InnoDB');
      else if(category==='column')need(/^type=[a-z0-9(), _]+;nullable=(true|false);ordinal=[1-9][0-9]{0,3};hidden=[1-4]$/.test(definition));
      else need(/^type=[1-5];columns=[0-9]{1,3}(,[0-9]{1,3}){0,127}$/.test(definition));
    }else{
      const [key,kb,seq,type,value,vb,omission]=row;
      need(intText(kb,0n,16777216n)&&intText(vb,0n,16777216n)&&intText(seq,0n,2n**56n-1n)&&['deletion','value','merge','single-deletion'].includes(type));
      const km=Number(kb)>128,vm=Number(vb)>512;
      need(omission===(km&&vm?'both':km?'key':vm?'value':'none'));
      for(const [hex,length,missing] of [[key,Number(kb),km],[value,Number(vb),vm]] as const)need(hex.length===(missing?0:2*length)&&hex===hex.trim()&&/^[0-9a-f]*$/.test(hex));
      if(type==='deletion'||type==='single-deletion')need(value===''&&vb==='0');
    }
  }
  need(bytes(JSON.stringify({payload,metadata}))<=2097152);
  return {reader,format:m.format,sourceBytes:m.source_bytes,toolVersion:m.tool_version,columns,rows};
}

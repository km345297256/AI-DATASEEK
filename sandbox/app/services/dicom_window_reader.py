"""Restricted native Part 10 grayscale reader; byte capabilities only, no DICOM services.

PS3.10 7 / PS3.5 7,8 / PS3.3 C.7.6.3,C.11. No generic sequence decoder.
Unknown values are skipped by validated length, never decoded or returned. Every
header precedes Pixel Data: metadata inspection never requests pixel bytes.
"""
import re
import struct
from decimal import Decimal
from .dicom_window_payload import (ERROR,LIMITS,MAX_SOURCE,MAX_TOTAL,MAX_SINGLE,MAX_READS,PRIVACY,WARNINGS,
    dtype,finite,integer,require,stored_bounds,validate_image,validate_dicom_window_options,validate_dicom_window_payload)

LONG_VR={b"OB",b"OD",b"OF",b"OL",b"OV",b"OW",b"SQ",b"UC",b"UR",b"UT",b"UN",b"SV",b"UV"}
SHORT_VR={b"AE",b"AS",b"AT",b"CS",b"DA",b"DS",b"DT",b"FL",b"FD",b"IS",b"LO",b"LT",b"PN",b"SH",b"SL",b"SS",b"ST",b"TM",b"UI",b"UL",b"US"}
SOP_UIDS={"1.2.840.10008.5.1.4.1.1.2":"CT","1.2.840.10008.5.1.4.1.1.4":"MR","1.2.840.10008.5.1.4.1.1.7":"SC",
          "1.2.840.10008.5.1.4.1.1.7.2":"SC-multiframe-8","1.2.840.10008.5.1.4.1.1.7.3":"SC-multiframe-16"}
READ_TAGS={0x00080016:b"UI",0x00080018:b"UI",0x00120062:b"CS",0x00280002:b"US",0x00280004:b"CS",0x00280006:b"US",0x00280008:b"IS",
    0x00280010:b"US",0x00280011:b"US",0x00280100:b"US",0x00280101:b"US",0x00280102:b"US",0x00280103:b"US",0x00280120:None,0x00280121:None,
    0x00280301:b"CS",0x00280302:b"CS",0x00281050:b"DS",0x00281051:b"DS",0x00281052:b"DS",0x00281053:b"DS",0x00281054:b"LO",0x00281056:b"CS"}
FORBIDDEN={0x00283000,0x00283010,0x00289145,0x00409096,0x52009229,0x52009230,0x20500010,0x20500020,0x7FE00008,0x7FE00009}

def text(value):
    require(type(value) is bytes and 0<len(value)<=64)
    return value.decode("ascii",errors="strict").rstrip(" \x00")
def uid(value):
    s=text(value);require(len(s)<=64 and re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))+",s));return s
def decimal(value):
    s=text(value).strip(); require(len(s)<=16 and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?",s))
    n=float(s);require(finite(n,2e12) and (n!=0 or Decimal(s)==0));return n

class Source:
    def __init__(self,callback,size,limits):
        maximum={"max_read_bytes":MAX_SINGLE,"max_total_bytes":MAX_TOTAL,"max_reads":MAX_READS}
        require(callable(callback) and integer(size,152,MAX_SOURCE))
        require(limits is None or type(limits) is dict and set(limits)==set(maximum) and all(integer(v,1,maximum[k]) for k,v in limits.items()))
        self.callback,self.size,self.limits=callback,size,maximum if limits is None else limits
        self.total=self.reads=0
    def plan(self,spans):
        require(self.reads+len(spans)<=self.limits["max_reads"] and self.total+sum(n for _,n in spans)<=self.limits["max_total_bytes"]
                and all(integer(o,0,self.size-1) and integer(n,1,min(self.size-o,self.limits["max_read_bytes"])) for o,n in spans))
    def read(self,o,n):
        self.plan([(o,n)]);self.total+=n;self.reads+=1;data=self.callback(o,n);require(type(data) is bytes and len(data)==n);return data

def header(read,position,explicit,end):
    require(position+8<=end); raw=read(position,8);group,element=struct.unpack_from("<HH",raw);tag=group*65536+element
    if explicit:
        vr=raw[4:6];require(vr in LONG_VR|SHORT_VR)
        if vr in LONG_VR:
            require(raw[6:8]==b"\0\0" and position+12<=end);length=struct.unpack("<I",read(position+8,4))[0];start=position+12
        else:length=struct.unpack_from("<H",raw,6)[0];start=position+8
    else:vr=None;length=struct.unpack_from("<I",raw,4)[0];start=position+8
    require(length!=0xFFFFFFFF and length%2==0 and start+length<=end)
    return tag,vr,start,length

def inspect(source):
    require(source.read(0,132)[128:]==b"DICM")
    first=source.read(132,12);require(first[:8]==b"\x02\x00\x00\x00UL\x04\x00")
    length=struct.unpack_from("<I",first,8)[0];require(0<length<=65536 and 144+length<source.size)
    data=source.read(144,length);pos=0;meta={};previous=0x00020000
    while pos<len(data):
        tag,vr,start,n=header(lambda o,n:data[o:o+n],pos,True,len(data));require(tag>>16==2 and tag>previous);previous=tag
        require(tag not in meta);meta[tag]=(vr,data[start:start+n]);pos=start+n
    def mv(tag,vr):require(tag in meta and meta[tag][0]==vr);return meta[tag][1]
    require(mv(0x00020001,b"OB")==b"\0\1")
    sop=uid(mv(0x00020002,b"UI"));require(sop in SOP_UIDS)
    instance=uid(mv(0x00020003,b"UI"));uid(mv(0x00020012,b"UI"))
    syntax=uid(mv(0x00020010,b"UI"));require(syntax in ("1.2.840.10008.1.2","1.2.840.10008.1.2.1"));explicit=syntax.endswith(".1")
    values={};pos=144+length;previous=0;count=0
    while pos<source.size:
        require(pos+12<=1048576); tag,vr,start,n=header(source.read,pos,explicit,source.size);count+=1
        require(count<=4096 and tag>previous and tag>>16 not in (0,1,2,3,4,5,6,7,0xFFFF) and tag not in FORBIDDEN and not 0x6000<=tag>>16<=0x60FF)
        previous=tag
        if tag==0x7FE00010:
            require(not explicit or vr in (b"OB",b"OW"));pixel_start,pixel_length,pixel_vr=start,n,vr;break
        require(start+n<=1048576 and (not explicit or vr!=b"SQ"))
        if tag in READ_TAGS:
            expected=READ_TAGS[tag]
            require(n<=64 and n>0 and (not explicit or expected is None and vr in (b"SS",b"US") or vr==expected))
            values[tag]=(vr,source.read(start,n))
        pos=start+n
    else:require(False)
    def raw(tag):require(tag in values);return values[tag][1]
    def us(tag):v=raw(tag);require(len(v)==2);return struct.unpack("<H",v)[0]
    require(uid(raw(0x00080016))==sop and uid(raw(0x00080018))==instance)
    require(text(raw(0x00120062))=="YES" and text(raw(0x00280301))=="NO")
    if 0x00280302 in values:require(text(raw(0x00280302))=="NO")
    require(us(0x00280002)==1 and 0x00280006 not in values)
    frames=1
    if 0x00280008 in values:
        value=text(raw(0x00280008)).strip();require(re.fullmatch(r"[1-9][0-9]{0,3}",value));frames=int(value)
    image={"rows":us(0x00280010),"columns":us(0x00280011),"frames":frames,"bits_allocated":us(0x00280100),"bits_stored":us(0x00280101),"pixel_representation":us(0x00280103),
           "photometric":text(raw(0x00280004)),"sop_class":SOP_UIDS[sop],"rescale":{"slope":1,"intercept":0,"unit":"unspecified","declared":False},"window":None,"padding":None}
    require(us(0x00280102)==image["bits_stored"]-1)
    require((0x00281052 in values)==(0x00281053 in values))
    if 0x00281052 in values:
        image["rescale"]={"slope":decimal(raw(0x00281053)),"intercept":decimal(raw(0x00281052)),"unit":text(raw(0x00281054)) if 0x00281054 in values else "unspecified","declared":True}
    else:require(0x00281054 not in values)
    require((0x00281050 in values)==(0x00281051 in values))
    if 0x00281056 in values: require(text(raw(0x00281056))=="LINEAR")
    if 0x00281050 in values:image["window"]={"center":decimal(raw(0x00281050)),"width":decimal(raw(0x00281051)),"function":"LINEAR"}
    require(0x00280121 not in values or 0x00280120 in values)
    if 0x00280120 in values:
        endpoints=[]
        for tag in (0x00280120,0x00280121):
            if tag in values:
                vr,v=values[tag];require(len(v)==2 and (not explicit or vr==(b"SS" if image["pixel_representation"] else b"US")))
                endpoints.append(struct.unpack("<h" if image["pixel_representation"] else "<H",v)[0])
        image["padding"]={"low":min(endpoints),"high":max(endpoints)}
    validate_image(image)
    pixels=image["rows"]*image["columns"]*frames*(image["bits_allocated"]//8)
    require(pixel_length==pixels+(pixels%2) and pixel_start+pixel_length==source.size and pixels<=4294967294)
    # 16-bit native data requires OW; OB is accepted only for 8-bit explicit data.
    require(not explicit or image["bits_allocated"]==8 or pixel_vr==b"OW")
    return image,pixel_start,pixels,"explicit-little" if explicit else "implicit-little"

def _preview(read_range,size,kind,options,limits):
    selected=validate_dicom_window_options(kind,{} if options is None else options)
    source=Source(read_range,size,limits);image,start,pixels,syntax=inspect(source)
    result={"contract_version":2,"type":"dicom-window","reader":"dicom-window","kind":kind,"media_type":"application/json","choices":{"image":image},"selected":selected,"warnings":list(WARNINGS),"sampled":False}
    padding_count=0
    if kind=="tree":result["tree"]=[{"path":"/pixels","node_type":"array","shape":[image["frames"],image["rows"],image["columns"]],"dtype":dtype(image)}]
    else:
        x,y,w,h=selected["roi"];require(selected["frame"]<image["frames"] and x+w<=image["columns"] and y+h<=image["rows"])
        byte=image["bits_allocated"]//8;rowbytes=image["columns"]*byte;frame_start=start+selected["frame"]*image["rows"]*rowbytes
        ranges=[(frame_start+(y+i)*rowbytes+x*byte,w*byte) for i in range(h)]
        # Coalesce only bounded row gaps; each skipped byte still counts toward budget.
        groups=[]
        for o,n in ranges:
            if groups and o-(groups[-1][-1][0]+groups[-1][-1][1])<=4096 and o+n-groups[-1][0][0]<=source.limits["max_read_bytes"]:groups[-1].append((o,n))
            else:groups.append([(o,n)])
        spans=[(g[0][0],g[-1][0]+g[-1][1]-g[0][0]) for g in groups]
        if pixels%2:spans.append((start+pixels,1))
        source.plan(spans);values=[];mask=2**image["bits_stored"]-1;sign=2**(image["bits_stored"]-1)
        for group,(o,n) in zip(groups,spans):
            data=source.read(o,n)
            for offset,length in group:
                for (v,) in struct.iter_unpack("<B" if byte==1 else "<H",data[offset-o:offset-o+length]):
                    v&=mask
                    if image["pixel_representation"] and v&sign:v-=2**image["bits_stored"]
                    values.append(v)
        if pixels%2:require(source.read(start+pixels,1)==b"\0")
        padding=image["padding"]
        padding_count=sum(padding["low"]<=v<=padding["high"] for v in values) if padding else 0
        result["array"]={"shape":[h,w],"dimensions":["row","column"],"dtype":dtype(image),"values":values}
    result["metadata"]={"format":"dicom","transfer_syntax":syntax,"input_mode":"window","source_bytes":size,"read_bytes":source.total,"read_requests":source.reads,
        "header_bytes":start,"pixel_bytes":pixels,"padding_pixels":padding_count,"metadata_hidden":True,"privacy":PRIVACY,"value_semantics":"raw-stored-integers","limits":dict(LIMITS)}
    return validate_dicom_window_payload(result,kind=kind,options=selected,source_bytes=size,read_bytes=source.total,read_requests=source.reads)

def dicom_window_preview(read_range,size,kind="tree",options=None,limits=None):
    try:return _preview(read_range,size,kind,options,limits)
    except Exception:raise ValueError(ERROR) from None

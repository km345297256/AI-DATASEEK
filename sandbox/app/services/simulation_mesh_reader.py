"""Serial, single-piece, ASCII VTU only; never invokes a solver or XML resource."""
import math
import re
from xml.etree import ElementTree as ET
from .simulation_mesh_payload import ERROR,WARNING,LIMITS,CELL_SIZES,need,label,validate_simulation_mesh_options,validate_simulation_mesh_payload

TYPES={"Int8":(-128,127),"UInt8":(0,255),"Int16":(-32768,32767),"UInt16":(0,65535),"Int32":(-2147483648,2147483647),"UInt32":(0,4294967295),"Int64":(-9007199254740991,9007199254740991),"UInt64":(0,9007199254740991),"Float32":None,"Float64":None}
class _BoundedTree(ET.TreeBuilder):
    """Enforce structural budgets at start tags, before constructing each node."""
    def __init__(self):
        super().__init__();self.nodes=0;self.depth=0
    def start(self,tag,attrs):
        self.nodes+=1;self.depth+=1
        need(self.nodes<=256 and self.depth<=7 and len(attrs)<=8 and len(tag)<=64
             and all(len(k)<=64 and len(v)<=128 for k,v in attrs.items())
             and not any('{' in x or ':' in x for x in (tag,*attrs)))
        return super().start(tag,attrs)
    def end(self,tag):
        result=super().end(tag);self.depth-=1;return result
    def doctype(self,*args):need(False)
    def pi(self,*args):need(False)

def _xml(text):
    parser=ET.XMLParser(target=_BoundedTree())
    # Small feeds bound how far the parser can advance before a rejected tag.
    for offset in range(0,len(text),65536):parser.feed(text[offset:offset+65536])
    return parser.close()
def _int(t,maximum):
    need(re.fullmatch(r"[0-9]{1,10}",t or "") is not None);v=int(t);need(v<=maximum);return v
def _array(node,count,*,geometry=False):
    need(node.tag=="DataArray" and not list(node) and not set(node.attrib)-{"Name","type","format","NumberOfComponents","RangeMin","RangeMax"})
    dtype=node.get("type");need(dtype in TYPES and node.get("format")=="ascii" and count<=131072)
    for key in ("RangeMin","RangeMax"):
        if key in node.attrib:need(len(node.attrib[key])<=64)
    words=(node.text or "").split();need(len(words)==count);values=[]
    for token in words:
        need(len(token)<=64)
        if TYPES[dtype] is not None:
            need(re.fullmatch(r"[+-]?[0-9]+",token) is not None);v=int(token);lo,hi=TYPES[dtype];need(lo<=v<=hi and abs(v)<=1e12)
        else:
            if token.lower() in {"nan","+nan","-nan","inf","+inf","-inf","infinity","+infinity","-infinity"}:
                need(not geometry);values.append(None);continue
            need(re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?",token) is not None)
            v=float(token);need(math.isfinite(v) and abs(v)<=1e12)
            if v==0:need(not any(c in "123456789" for c in re.split('[eE]',token)[0]))
            if dtype=="Float32":
                # The textual value represents float32 storage, not arbitrary float64.
                import struct
                original=v;v=struct.unpack("<f",struct.pack("<f",v))[0]
                need(math.isfinite(v) and (v!=0 or original==0))
        values.append(v)
    return values
def simulation_mesh_preview(data,fmt,kind="tree",options=None):
    try:
        selected=validate_simulation_mesh_options(kind,{} if options is None else options)
        need(fmt=="vtu" and type(data) is bytes and 0<len(data)<=16*1024**2 and b"\x00" not in data)
        text=data.decode("utf8");text=re.sub(r'^\s*<\?xml\s+version=[^?]+\?>',"",text,count=1)
        need(not re.search(r"<!DOCTYPE|<!ENTITY|<\?|\b(?:href|src|Source|offset)\s*=",text,re.I))
        root=_xml(text);need(root.tag=="VTKFile" and root.get("type")=="UnstructuredGrid" and root.get("version") in {"0.1","1.0"} and root.get("byte_order") in {"LittleEndian","BigEndian"} and not set(root.attrib)-{"type","version","byte_order","header_type"})
        need(root.get("header_type","UInt32") in {"UInt32","UInt64"})
        need(len(root)==1 and root[0].tag=="UnstructuredGrid" and not root[0].attrib and len(root[0])==1)
        p=root[0][0];need(p.tag=="Piece" and set(p.attrib)=={"NumberOfPoints","NumberOfCells"});n=_int(p.get("NumberOfPoints"),4096);nc=_int(p.get("NumberOfCells"),2048);need(n>0 and nc>0)
        children={x.tag:x for x in p};need(len(children)==len(p) and {"Points","Cells"}<=set(children) and not set(children)-{"Points","Cells","PointData","CellData"})
        points=children["Points"];need(not points.attrib and len(points)==1 and _int(points[0].get("NumberOfComponents","1"),9)==3 and points[0].get("type") in {"Float32","Float64"})
        xyz=_array(points[0],n*3,geometry=True);coords=[xyz[i:i+3] for i in range(0,len(xyz),3)]
        cellnode=children["Cells"];need(not cellnode.attrib and len(cellnode)==3)
        arrays={x.get("Name"):x for x in cellnode};need(set(arrays)=={"connectivity","offsets","types"} and all(_int(x.get("NumberOfComponents","1"),9)==1 and x.get("type") in TYPES and TYPES[x.get("type")] is not None for x in arrays.values()))
        offsets=_array(arrays["offsets"],nc,geometry=True);types=_array(arrays["types"],nc,geometry=True)
        need(all(type(t) is int and t in CELL_SIZES for t in types));total=sum(CELL_SIZES[t] for t in types);conn=_array(arrays["connectivity"],total,geometry=True);cells=[];pos=0
        for t,end in zip(types,offsets):
            need(type(end) is int and end==pos+CELL_SIZES[t]);indices=conn[pos:end]
            need(all(type(i) is int and 0<=i<n for i in indices) and len(set(indices))==len(indices));cells.append({"type":t,"points":indices});pos=end
        fields=[];field_values=0;chosen=None
        for tag,association,count in (("PointData","point",n),("CellData","cell",nc)):
            node=children.get(tag)
            if node is None:continue
            need(not set(node.attrib)-{"Scalars","Vectors","Normals","Tensors","GlobalIds","PedigreeIds"} and all(label(x) for x in node.attrib.values()))
            names=set()
            for i,a in enumerate(node):
                need(len(fields)<32 and label(a.get("Name")) and a.get("Name") not in names);names.add(a.get("Name"));components=_int(a.get("NumberOfComponents","1"),9);need(components>=1)
                field_values+=components*count;need(field_values<=131072)
                values=_array(a,components*count);ident=association[0]+"-"+str(i)
                fields.append({"id":ident,"name":a.get("Name"),"association":association,"components":components,"tuples":count})
                if selected.get("field")==ident:
                    need(selected["component"]<components);chosen={"id":ident,"association":association,"component":selected["component"],"values":values[selected["component"]::components]}
        need(kind=="tree" or selected["field"] is None or chosen is not None)
        v={"contract_version":2,"type":"simulation-mesh","reader":"simulation-mesh","kind":kind,"media_type":"application/json","choices":{"fields":fields},"selected":selected,"warnings":[WARNING],"sampled":False,
           "metadata":{"format":"vtu","input_mode":"whole","source_bytes":len(data),"point_count":n,"cell_count":nc,"cell_types":sorted(set(types)),"field_values":field_values,"coordinate_unit":"unspecified","field_unit":"unspecified","limits":dict(LIMITS),"value_semantics":"stored component; no interpolation or deformation"}}
        if kind=="tree":v["tree"]=[{"path":"/mesh","node_type":"array","attributes":{"label":"VTU mesh"}}]
        else:v["mesh"]={"points":coords,"cells":cells,"field":chosen}
        return validate_simulation_mesh_payload(v,kind=kind,options=selected,fmt=fmt,size=len(data))
    except Exception:raise ValueError(ERROR) from None

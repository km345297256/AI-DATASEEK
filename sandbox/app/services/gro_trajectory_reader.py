"""Bounded concatenated GRO frames; only original storage values, no inferred chemistry."""
import math
import re
from .gro_trajectory_payload import ERROR,WARNING,LIMITS,need,atom,box,validate_gro_trajectory_options,validate_gro_trajectory_payload

def _number(text):
    need(isinstance(text,str) and len(text)<=64 and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?",text.strip()))
    value=float(text);need(math.isfinite(value) and abs(value)<=1e12)
    if value==0: need(not any(c in "123456789" for c in re.split('[eE]',text)[0]))
    return value

def _record(line):
    need(len(line)>=44)
    fields={"residue_number":line[:5],"residue_name":line[5:10].strip(),"atom_name":line[10:15].strip(),"atom_number":line[15:20]}
    for k in ("residue_number","atom_number"):
        need(re.fullmatch(r" *\d{1,5}",fields[k]) is not None);fields[k]=int(fields[k])
    need(atom(fields))
    dots=[m.start() for m in re.finditer(r"\.",line[20:])]
    need(len(dots) in {3,6});width=dots[1]-dots[0];precision=width-5
    need(3<=precision<=8 and dots[2]-dots[1]==width and len(line)==20+width*len(dots))
    values=[]
    for i in range(len(dots)):
        token=line[20+i*width:20+(i+1)*width]
        need(re.fullmatch(r" *-?\d+\.\d{"+str(precision+(i>=3))+"}",token) is not None)
        value=_number(token);need(abs(value)<=1e6);values.append(value)
    return fields,values[:3],values[3:] or None

def gro_trajectory_preview(data,fmt,kind="tree",options=None):
    try:
        options=validate_gro_trajectory_options(kind,{} if options is None else options)
        need(fmt=="gro" and type(data) is bytes and 0<len(data)<=16*1024**2 and b"\x00" not in data)
        text=data.decode("ascii");need(not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",text))
        lines=text.splitlines();need(lines and len(lines)<=131264 and all(len(x)<=512 for x in lines))
        position=0;frames=[];selected=None;identities=None;total=0
        while position<len(lines):
            need(len(frames)<64 and position+2<len(lines));title,count=lines[position:position+2]
            need(re.fullmatch(r" *\d{1,5} *",count) is not None);n=int(count);need(1<=n<=8192 and position+n+2<len(lines))
            total+=n;need(total<=131072)
            stamps=list(re.finditer(r"(?<![A-Za-z])t\s*=",title));need(len(stamps)<=1)
            time=None if not stamps else _number(title[stamps[0].end():].strip())
            frame_atoms=[];positions=[];velocities=[];has_vel=None
            for line in lines[position+2:position+2+n]:
                identity,point,velocity=_record(line)
                frame_atoms.append(identity)
                if has_vel is None:has_vel=velocity is not None
                need(has_vel==(velocity is not None))
                if options.get("frame")==len(frames):positions.append(point);velocities.append(velocity)
            if identities is None:identities=frame_atoms
            need(frame_atoms==identities)
            values=lines[position+n+2].split();need(len(values) in {3,9});vectors=[_number(v) for v in values]
            if len(vectors)==3:vectors+= [0.0]*6
            need(box(vectors));frames.append({"id":len(frames),"atoms":n,"time_ps":time,"velocities":has_vel,"box":vectors})
            if options.get("frame")==len(frames)-1:selected={"positions":positions,"atoms":frame_atoms,"velocities":velocities if has_vel else None}
            position+=n+3
        value={"contract_version":2,"type":"gro-trajectory","reader":"gro-trajectory","kind":kind,"media_type":"application/json","choices":{"frames":frames},"selected":options,"warnings":[WARNING],"sampled":False,
               "metadata":{"format":"gro","input_mode":"whole","source_bytes":len(data),"frame_count":len(frames),"total_atoms":total,"coordinate_unit":"nm","velocity_unit":"nm/ps","time_unit":"ps","topology_consistency":"ordered GRO identifiers; bonds and elements unspecified","limits":dict(LIMITS)}}
        if kind=="tree":value["tree"]=[{"path":f"/frames/{i}","node_type":"array","attributes":{"label":f"Frame {i+1}"}} for i in range(len(frames))]
        else:need(selected is not None);value["trajectory"]=selected
        return validate_gro_trajectory_payload(value,kind=kind,options=options,fmt=fmt,size=len(data))
    except Exception:raise ValueError(ERROR) from None

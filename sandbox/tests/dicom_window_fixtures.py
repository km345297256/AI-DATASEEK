"""Original generated Part10 profiles: no patient file or data dependencies."""
import json
import struct
from app.services.dicom_window_reader import LONG_VR, dicom_window_preview

def element(tag,vr,value,explicit=True):
    if isinstance(value,str):value=value.encode('ascii')
    if isinstance(value,int):value=struct.pack('<h' if vr==b'SS' else '<I' if vr==b'UL' else '<H',value)
    if len(value)%2:value+=b'\0' if vr in (b'UI',b'OB',b'OW') else b' '
    prefix=struct.pack('<HH',tag>>16,tag&65535)
    return prefix+(vr+b'\0\0'+struct.pack('<I',len(value)) if vr in LONG_VR else vr+struct.pack('<H',len(value)))+value if explicit else prefix+struct.pack('<I',len(value))+value

def fixture(*,explicit=True,mono='MONOCHROME2',bits=16,stored=None,signed=False,frames=2,rows=3,columns=4,overrides=None,drop=(),pixel_values=None):
    stored=stored or bits
    sop='1.2.840.10008.5.1.4.1.1.2' if signed else '1.2.840.10008.5.1.4.1.1.7.'+('2' if bits==8 else '3')
    if signed:frames=1
    fields={0x00080016:(b'UI',sop),0x00080018:(b'UI','1.2.826.0.1.3680043.10.999.1'),
        0x00100010:(b'PN','SYNTHETIC-NAME-MUST-NOT-LEAK'),0x00100020:(b'LO','SYNTHETIC-ID-MUST-NOT-LEAK'),
        0x00120062:(b'CS','YES'),0x00280002:(b'US',1),0x00280004:(b'CS',mono),0x00280008:(b'IS',str(frames)),
        0x00280010:(b'US',rows),0x00280011:(b'US',columns),0x00280100:(b'US',bits),0x00280101:(b'US',stored),0x00280102:(b'US',stored-1),0x00280103:(b'US',int(signed)),
        0x00280301:(b'CS','NO'),0x00281050:(b'DS','1350'),0x00281051:(b'DS','1000'),0x00281052:(b'DS','-1000'),0x00281053:(b'DS','2'),0x00281054:(b'LO','HU')}
    fields.update(overrides or {})
    for tag in drop:fields.pop(tag,None)
    values=pixel_values if pixel_values is not None else [(f*1000+y*100+x*10)%(2**stored) for f in range(frames) for y in range(rows) for x in range(columns)]
    pixel=b''.join(struct.pack('<B' if bits==8 else '<H',v & (2**bits-1)) for v in values)
    fields[0x7FE00010]=(b'OB' if bits==8 else b'OW',pixel)
    meta={0x00020001:(b'OB',b'\0\1'),0x00020002:(b'UI',sop),0x00020003:(b'UI','1.2.826.0.1.3680043.10.999.1'),
          0x00020010:(b'UI','1.2.840.10008.1.2.1' if explicit else '1.2.840.10008.1.2'),0x00020012:(b'UI','1.2.826.0.1.3680043.10.999')}
    encoded=b''.join(element(tag,*value) for tag,value in sorted(meta.items()))
    data=bytes(128)+b'DICM'+element(0x00020000,b'UL',len(encoded))+encoded+b''.join(element(tag,*value,explicit=explicit) for tag,value in sorted(fields.items()))
    return data,values

def preview(data,kind='tree',options=None,limits=None):
    calls=[]
    def read(offset,length):calls.append((offset,length));return data[offset:offset+length]
    return dicom_window_preview(read,len(data),kind,options,limits),calls

def browser_payloads():
    outputs={}
    for name,mono in [('mono2','MONOCHROME2'),('mono1','MONOCHROME1')]:
        data,_=fixture(mono=mono)
        outputs[name]={'tree':preview(data)[0],'image':preview(data,'image',{'frame':1,'roi':[1,1,2,2],'confirm_deidentified':True})[0]}
    return outputs

if __name__=='__main__':print(json.dumps(browser_payloads(),ensure_ascii=False,allow_nan=False))

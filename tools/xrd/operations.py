#!/usr/bin/env python3
from __future__ import annotations
import base64, json, re, sys, xml.etree.ElementTree as ET
from pathlib import Path
import numpy as np

def args():
    return json.loads(base64.urlsafe_b64decode(sys.argv[2] + '=' * (-len(sys.argv[2]) % 4)))
def write(path, value):
    if path:
        p=Path(path); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def local(tag): return tag.rsplit('}',1)[-1].lower()
def numbers(text):
    return [float(x) for x in re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', text or '')]

def position_values(node, count):
    """Decode the axis representations used by standard PANalytical XRDML."""
    values = numbers(node.text)
    if values:
        return values
    children = {local(child.tag): child for child in node}
    for name in ('listpositions', 'positions'):
        if name in children:
            return numbers(children[name].text)
    if 'startposition' in children and 'endposition' in children:
        start = numbers(children['startposition'].text)
        end = numbers(children['endposition'].text)
        if len(start) == len(end) == 1:
            return np.linspace(start[0], end[0], count).tolist()
    if 'commonposition' in children:
        common = numbers(children['commonposition'].text)
        if len(common) == 1:
            return common * count
    return []

def parse(path):
    root=ET.parse(path).getroot(); scans=[]
    for node in root.iter():
        if local(node.tag) != 'scan': continue
        ints=[]; position_nodes=[]; legacy_positions=[]
        for child in node.iter():
            name=local(child.tag); text=(child.text or '').strip()
            if name == 'positions': position_nodes.append(child)
            elif name in {'theta','twotheta','two-theta'}: legacy_positions += numbers(text)
            elif name in {'intensities','counts','intensity'}: ints += numbers(text)
        # A scan may contain Omega, Phi and other motor axes as well as 2Theta.
        # Combining them corrupts the diffraction angle/intensity pairing.
        preferred = [p for p in position_nodes if re.sub(r'[^a-z0-9]', '', p.get('axis', '').lower()) in {'2theta','twotheta'}]
        if not preferred:
            preferred = [p for p in position_nodes if not p.get('axis')]
        vals = position_values(preferred[0], len(ints)) if preferred and ints else legacy_positions
        if vals and ints:
            if len(vals) != len(ints):
                raise ValueError('XRDML axis/intensity length mismatch')
            scans.append({'positions':vals,'intensities':ints,'attributes':{local(k):v for k,v in node.attrib.items()}})
    meta={local(k):v for k,v in root.attrib.items()}
    for node in root.iter():
        if local(node.tag) in {'instrument','sample','measurement','wavelength','startposition','endposition','step'}:
            text=' '.join((node.text or '').split());
            if text and len(text)<500: meta.setdefault(local(node.tag),text)
    return {'metadata':meta,'scans':scans}
def emit(data, output=None):
    data={'success':True,**data}; write(output,data); print(json.dumps(data,ensure_ascii=False))
def load(a):
    d=parse(a['input_path']); i=int(a.get('scan_index',0));
    if not d['scans']: raise ValueError('XRDML contains no scan with positions and intensities')
    return d,d['scans'][min(max(i,0),len(d['scans'])-1)]
def main():
    op=sys.argv[1]; a=args();
    if op=='xrdml_inspect':
        d=parse(a['input_path']); emit({'scan_count':len(d['scans']),'metadata':d['metadata'],'points_per_scan':[len(s['positions']) for s in d['scans']]},a.get('output_path')); return
    d,s=load(a); x=np.asarray(s['positions']); y=np.asarray(s['intensities']);
    if op in {'xrdml_merge_scans','xrdml_resample'}:
        if op=='xrdml_merge_scans':
            grid=np.unique(np.concatenate([np.asarray(v['positions']) for v in d['scans']])); values=np.nanmean([np.interp(grid,v['positions'],v['intensities'],left=np.nan,right=np.nan) for v in d['scans']],axis=0)
        else:
            step=float(a.get('step',0.02)); grid=np.arange(float(x.min()),float(x.max())+step/2,step); values=np.interp(grid,x,y)
        emit({'rows':[{'two_theta':float(i),'intensity':float(j)} for i,j in zip(grid,values)],'operation':op},a.get('output_path')); return
    if op=='xrdml_validate':
        issues=[]
        for i,scan in enumerate(d['scans']):
            if len(scan['positions'])!=len(scan['intensities']): issues.append(f'scan {i}: axis/intensity length mismatch')
            if len(scan['positions'])<2: issues.append(f'scan {i}: too few points')
            if not np.isfinite(scan['positions']).all() or not np.isfinite(scan['intensities']).all(): issues.append(f'scan {i}: non-finite values')
        emit({'valid':not issues,'issues':issues},a.get('output_path')); return
    if op=='xrdml_list_scans': emit({'scans':[{'index':i,'point_count':len(v['positions']),'min_angle':min(v['positions']) if v['positions'] else None,'max_angle':max(v['positions']) if v['positions'] else None,'attributes':v['attributes']} for i,v in enumerate(d['scans'])]},a.get('output_path')); return
    if op in {'xrdml_extract_scan','xrdml_export_csv'}:
        out=a.get('output_path'); rows=[{'two_theta':float(i),'intensity':float(j)} for i,j in zip(x,y)]
        if out and str(out).lower().endswith('.csv'):
            import csv; p=Path(out); p.parent.mkdir(parents=True,exist_ok=True)
            with p.open('w',newline='',encoding='utf-8') as f: w=csv.DictWriter(f,fieldnames=['two_theta','intensity']); w.writeheader(); w.writerows(rows)
        emit({'scan_index':int(a.get('scan_index',0)),'rows':rows,'metadata':d['metadata']},None if out and str(out).lower().endswith('.csv') else out); return
    if op in {'xrdml_background_subtract','xrdml_smooth'}:
        from scipy.signal import savgol_filter
        yy=y.copy();
        if op=='xrdml_background_subtract': yy=yy-np.minimum.accumulate(yy)
        else:
            window=min(len(yy)//2*2-1, int(a.get('window',11))); window=max(5,window if window%2 else window-1); yy=savgol_filter(yy,window,min(3,window-2))
        emit({'rows':[{'two_theta':float(i),'intensity':float(j)} for i,j in zip(x,yy)],'method':op},a.get('output_path')); return
    if op=='xrdml_zero_shift_correct': emit({'rows':[{'two_theta':float(i+a.get('shift',0)),'intensity':float(j)} for i,j in zip(x,y)],'shift':a.get('shift',0)},a.get('output_path')); return
    if op=='xrdml_wavelength_convert':
        old=float(a.get('wavelength',1.5406)); new=float(a.get('target_wavelength',1.5406)); theta=np.radians(x/2); d=old/(2*np.sin(theta)); xx=np.degrees(2*np.arcsin(np.clip(new/(2*d),-1,1))); emit({'rows':[{'two_theta':float(i),'intensity':float(j)} for i,j in zip(xx,y)],'source_wavelength':old,'target_wavelength':new},a.get('output_path')); return
    if op=='xrdml_peak_detect':
        from scipy.signal import find_peaks, peak_widths
        idx,_=find_peaks(y,distance=int(a.get('min_distance',3)),prominence=float(a.get('prominence',0))); widths=peak_widths(y,idx,rel_height=.5)[0]
        emit({'peaks':[{'index':int(i),'two_theta':float(x[i]),'intensity':float(y[i]),'fwhm_points':float(w)} for i,w in zip(idx,widths)]},a.get('output_path')); return
    if op in {'xrdml_peak_fit','xrdml_microstrain'}: emit({'note':'需要用户提供峰窗口、仪器展宽和晶体模型；当前返回候选峰及结构化入口','peaks':main_peak(x,y)},a.get('output_path')); return
    if op in {'xrdml_visualize','xrdml_compare_patterns'}:
        import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
        fig,ax=plt.subplots(figsize=(10,5)); ax.plot(x,y,label=Path(a['input_path']).name); ax.set(xlabel='2θ (degree)',ylabel='Intensity'); ax.legend(); fig.tight_layout(); out=a.get('output_path') or 'xrdml.png'; Path(out).parent.mkdir(parents=True,exist_ok=True); fig.savefig(out,dpi=150); plt.close(fig); emit({'output_path':out,'point_count':len(x)},None); return
    if op=='xrdml_export_report': emit({'metadata':d['metadata'],'scan_count':len(d['scans']),'summary':{'points':len(x),'min_intensity':float(y.min()),'max_intensity':float(y.max())},'peaks':main_peak(x,y)},a.get('output_path')); return
    if op=='xrdml_crystallite_size': emit({'note':'需要仪器展宽和峰宽参数；仅返回可用峰候选','peaks':main_peak(x,y)},a.get('output_path')); return
    raise ValueError('unsupported XRDML operation')
def main_peak(x,y):
    from scipy.signal import find_peaks
    ids,_=find_peaks(y,prominence=float(np.std(y))); return [{'two_theta':float(x[i]),'intensity':float(y[i])} for i in ids]
if __name__=='__main__': main()

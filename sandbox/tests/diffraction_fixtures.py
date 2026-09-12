"""Original synthetic XML profile fixtures, never user or instrument files."""
import json
from app.services.diffraction_reader import diffraction_preview

def xrdml(*, version="2.1", coupled=False, explicit=False, scans=2):
    tag = "intensities" if version.startswith("1.") else "counts"
    positions = "<listPositions>10 10.25 11.5 14</listPositions>" if explicit else "<startPosition>10</startPosition><endPosition>13</endPosition>"
    fixed = "<startPosition>5</startPosition><endPosition>6.5</endPosition>" if coupled else "<commonPosition>5</commonPosition>"
    scan = f'''<scan status="Completed" scanAxis="{'2Theta-Omega' if coupled else '2Theta'}" mode="Pre-set time" appendNumber="0"><dataPoints>
<positions axis="2Theta" unit="deg">{positions}</positions><positions axis="Omega" unit="deg">{fixed}</positions>
<commonCountingTime unit="seconds">2</commonCountingTime><commonBeamAttenuationFactor>3</commonBeamAttenuationFactor>
<{tag} unit="counts">10 40 20 5</{tag}></dataPoints></scan>'''
    return (f'<xrdMeasurements xmlns="http://www.xrdml.com/XRDMeasurement/{version}" status="Completed"><xrdMeasurement>' + scan * scans + '</xrdMeasurement></xrdMeasurements>').encode()

def cansas(*, version="1.1", errors=True, scans=2):
    namespace = "urn:cansas1d:1.1" if version == "1.1" else "cansas1d/1.0"
    rows = []
    for q, intensity, dx, dy in ((.01, 10, .001, .5), (.021, -2, .0012, .2), (.045, 5, .0015, .3), (.1, 1, .002, .1)):
        errors_text = f'<Qdev unit="1/A">{dx}</Qdev><Idev unit="1/cm">{dy}</Idev>' if errors else ""
        rows.append(f'<Idata><Q unit="1/A">{q}</Q><I unit="1/cm">{intensity}</I>{errors_text}</Idata>')
    return (f'<SASroot xmlns="{namespace}" version="{version}"><SASentry><Title>synthetic only</Title>' + ('<SASdata>' + ''.join(rows) + '</SASdata>') * scans + '</SASentry></SASroot>').encode()

def payloads():
    output = {}
    for key, raw, fmt in (("xrdml", xrdml(), "xrdml"), ("sas", cansas(), "xml"), ("explicit", xrdml(explicit=True), "xrdml")):
        output[key] = {"tree": diffraction_preview(raw, fmt), "series": diffraction_preview(raw, fmt, "series", {"scan": 1})}
    return output

if __name__ == "__main__": print(json.dumps(payloads(), ensure_ascii=False))

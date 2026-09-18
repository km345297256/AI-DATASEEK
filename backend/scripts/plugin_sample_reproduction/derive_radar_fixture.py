"""Generate the reviewed local radar contract fixture, never an observed storm."""
import io,sys
from pathlib import Path
import h5py
sys.path.insert(0,'/repo/sandbox/tests')
from radar_window_fixtures import radar_bytes
folder=Path('/samples/viz-dataseek-odim24-fixture');folder.mkdir(exist_ok=True)
buffer=io.BytesIO(radar_bytes())
with h5py.File(buffer,'r+') as f:
    # Do not distribute the security test's fake private-path canary.
    del f['how']
data=buffer.getvalue()
for path,value in [(folder/'synthetic-odim24.h5',data),(folder/'LICENSE.txt',Path('/repo/LICENSE').read_bytes())]:
    if path.exists():
        assert path.read_bytes()==value
    else:
        with path.open('xb') as f:f.write(value)
print({'bytes':len(data),'synthetic':True,'sweeps':2,'quantities':['DBZH','VRADH']})

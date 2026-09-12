"""Opaque Ripple pair grants never expose names, siblings, or larger budgets."""
import json
import threading
import pytest
from app.infrastructure.external.sandbox import window_visualization_worker as module
from test_window_visualization_worker import invoke,frame,line,request,SUCCESS

def resources():return [{"key":"header","offset":0,"size":128},{"key":"data","offset":128,"size":512}]
def call(monkeypatch,content,**kwargs):
    return invoke(monkeypatch,content,reader="ripple-window",format="rpl",kind="tree",**{"resources":resources(),"size":640,**kwargs})

def test_profile_and_host_confinement(monkeypatch):
    limits={"max_read_bytes":1048576,"max_total_bytes":8388608,"max_reads":256}
    client,observed,run=call(monkeypatch,frame(line(request()))+frame(line(request(id=2,offset=128)))+frame(line(SUCCESS)),limits=limits)
    assert run()["ok"] and observed==[(0,4),(128,4)]
    init=json.loads(bytes(client.wire.sent).splitlines()[0])
    assert init["resources"]==resources() and init["limits"]==limits and init["reader"]=="ripple-window"
    settings=client.settings
    assert settings["user"]=="65534:65534" and settings["network_mode"]=="none" and settings["read_only"]
    assert settings["cap_drop"]==["ALL"] and settings["security_opt"]==["no-new-privileges:true"]
    assert not {"mounts","volumes","ports","privileged"}&settings.keys()
    assert client.cleaned and client.closed and client.wire.closed
    for key in limits:
        with pytest.raises(module.VisualizationWorkerError):module.validate_limits({**limits,key:limits[key]+1},"ripple-window")
    with pytest.raises(module.VisualizationWorkerError):module.validate_limits(limits,"edf")

@pytest.mark.parametrize("change",[lambda r:r.reverse(),lambda r:r.pop(),lambda r:r[1].__setitem__("offset",127),
    lambda r:r[0].__setitem__("key","cube.rpl"),lambda r:r[1].__setitem__("key","/private/data.raw"),
    lambda r:r[1].__setitem__("file_id","other"),lambda r:r[1].__setitem__("size",True),lambda r:r[0].__setitem__("size",65537)])
def test_bad_resources_before_container(monkeypatch,change):
    rows=resources();change(rows)
    client,observed,run=call(monkeypatch,frame(line(SUCCESS)),resources=rows)
    with pytest.raises(module.VisualizationWorkerError):run()
    assert not observed and client.settings is None

@pytest.mark.parametrize("change",[{"offset":126},{"offset":639},{"offset":640},{"path":"cube.raw"},{"resource":"data"}])
def test_cross_member_or_address_invention_rejected(monkeypatch,change):
    client,observed,run=call(monkeypatch,frame(line(request(**change)))+frame(line(SUCCESS)))
    with pytest.raises(module.VisualizationWorkerError):run()
    assert not observed and client.cleaned and client.closed and client.wire.closed

@pytest.mark.parametrize("extra",[frame(line(SUCCESS)),frame(b"private diagnostic",2),frame(line(request(id=2)))])
def test_terminal_extra_rejected(monkeypatch,extra):
    client,observed,run=call(monkeypatch,frame(line(SUCCESS))+extra)
    with pytest.raises(module.VisualizationWorkerError):run()
    assert not observed and client.cleaned and client.closed and client.wire.closed

def test_cancel_range_and_count_budget(monkeypatch):
    cancelled=threading.Event()
    def read(o,n):cancelled.set();return b"x"*n
    client,observed,run=call(monkeypatch,frame(line(request()))+frame(line(SUCCESS)),cancelled=cancelled,read=read)
    with pytest.raises(module.VisualizationWorkerError):run()
    assert observed==[(0,4)] and len(bytes(client.wire.sent).splitlines())==1 and client.cleaned and client.closed
    content=b"".join(frame(line(request(id=i))) for i in range(1,258))+frame(line(SUCCESS))
    client,observed,run=call(monkeypatch,content,limits={"max_read_bytes":1048576,"max_total_bytes":8388608,"max_reads":256})
    with pytest.raises(module.VisualizationWorkerError):run()
    assert len(observed)==256 and client.cleaned and client.closed

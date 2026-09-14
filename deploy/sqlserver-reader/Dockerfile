# Experimental ARM-host execution through Docker's existing amd64 emulator.
# Upstream digest checked against Microsoft's registry on 2026-09-14.
FROM mcr.microsoft.com/mssql/server:2022-CU26-ubuntu-22.04@sha256:ba4c8329f48fb8f02e1416be6a930ebfd71268caee78aa985f3af4315e457c89
USER root
COPY probe.py /opt/dataseek-sqlserver/probe.py
RUN test -x /usr/bin/python3 && test -x /usr/bin/timeout && \
    test -x /opt/mssql-tools18/bin/sqlcmd && \
    chmod 0555 /opt/dataseek-sqlserver/probe.py
# Upstream marks sqlservr CAP_NET_BIND_SERVICE+ep. Port 1433 is unprivileged;
# remove the unnecessary file capability instead of widening cap_drop: ALL.
RUN /usr/bin/python3 -c "import os; p='/opt/mssql/bin/sqlservr'; os.removexattr(p, 'security.capability') if 'security.capability' in os.listxattr(p) else None; assert 'security.capability' not in os.listxattr(p)"
USER 10001:0
ENTRYPOINT ["/usr/bin/timeout", "--signal=KILL", "180s", "/usr/bin/python3", "-I", "-B", "/opt/dataseek-sqlserver/probe.py"]

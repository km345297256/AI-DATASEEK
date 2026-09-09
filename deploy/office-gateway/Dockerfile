FROM docker.1ms.run/nginx:stable-alpine
COPY nginx.conf.template /etc/nginx/office.conf.template
COPY start.sh /office-gateway-start.sh
RUN chmod 0555 /office-gateway-start.sh
USER 101:101
RUN ONLYOFFICE_GATEWAY_SECRET=0000000000000000000000000000000000000000000000000000000000000000 /office-gateway-start.sh --check
ENTRYPOINT ["/office-gateway-start.sh"]

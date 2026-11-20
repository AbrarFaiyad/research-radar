FROM alpine:3.20

# FilterType (used in tinyproxy.conf) needs tinyproxy 1.11.2 or later.
RUN apk add --no-cache "tinyproxy>=1.11.2"
# tinyproxy.conf and filter are rendered from [builds] allow_hosts by radar.builds.sandbox.
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY filter /etc/tinyproxy/filter

EXPOSE 8888
USER tinyproxy
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]

#!/bin/bash
docker build -t gsm-sip-gw .

docker kill --signal=SIGINT gsm-sip-gw-container
docker wait gsm-sip-gw-container
docker rm -f gsm-sip-gw-container

docker run -d \
    --name gsm-sip-gw-container \
    --net host \
    --privileged \
    -v /dev:/dev \
    -v /proc:/porc \
    -v /sys:/sys \
    --restart unless-stopped \
    gsm-sip-gw:latest \
    python3 gw.py "$@"

docker logs -f gsm-sip-gw-container

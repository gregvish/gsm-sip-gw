FROM ubuntu:20.04


RUN apt-get update && DEBIAN_FRONTEND="noninteractive" apt-get install -y \
    pkg-config \
    python3 \
    python3-pip \
    libpython3-dev \
    libqmi-utils \
    curl

RUN curl -o /etc/apt/trusted.gpg.d/agp-debian-key.gpg \
    http://download.ag-projects.com/agp-debian-key.gpg

RUN echo "deb http://ag-projects.com/ubuntu focal main" >> \
    /etc/apt/sources.list.d/sipsimple.list && \
    echo "deb-src http://ag-projects.com/ubuntu focal main" >> \
    /etc/apt/sources.list.d/sipsimple.list

RUN apt-get update && DEBIAN_FRONTEND="noninteractive" apt-get install -y \
    python3-sipsimple


RUN pip install pyserial-asyncio==0.6


RUN useradd -ms /bin/bash user
RUN addgroup user dialout
RUN addgroup user audio

USER user
WORKDIR /home/user/

COPY *.py ./
COPY asoundrc ./.asoundrc

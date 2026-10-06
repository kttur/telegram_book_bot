FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends tor curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY torrc /etc/tor/torrc
USER debian-tor
CMD ["tor", "-f", "/etc/tor/torrc"]

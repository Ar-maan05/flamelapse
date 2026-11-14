# flamelapse with the usual build tools, for running on macOS, Windows or any Linux without installing anything.
#   docker build -t flamelapse .
#   docker run --rm -it -v "$PWD":/work flamelapse quick https://github.com/lz4/lz4 --run "{bin} -1 -c big.bin"
# The report lands in ./lz4/flamelapse-report on your machine; open index.html from there.
# Projects that need more libraries to build: extend this image (FROM flamelapse) and apt-get install them.
FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates valgrind python3 python3-pip python3-venv \
        build-essential autoconf automake libtool pkg-config bison flex \
        cmake meson ninja-build \
    && rm -rf /var/lib/apt/lists/*

COPY . /opt/flamelapse-src
RUN python3 -m venv /opt/flamelapse && /opt/flamelapse/bin/pip install --no-cache-dir /opt/flamelapse-src \
    && rm -rf /opt/flamelapse-src

ENV PATH=/opt/flamelapse/bin:$PATH BROWSER=true
RUN git config --system --add safe.directory '*'
WORKDIR /work
ENTRYPOINT ["flamelapse"]
CMD ["--help"]

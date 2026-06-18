# Pins the exact toolchain CI uses so "works in the container" == "passes in CI".
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake ninja-build pkg-config bison \
      libssl-dev libncurses-dev libldap2-dev libsasl2-dev \
      ccache git curl ca-certificates clang clang-format-18 gdb sudo \
 && rm -rf /var/lib/apt/lists/*

# Non-root dev user with passwordless sudo for apt during experimentation.
RUN useradd -ms /bin/bash dev && echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev
USER dev
WORKDIR /workspace

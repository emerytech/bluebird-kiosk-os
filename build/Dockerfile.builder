FROM --platform=linux/amd64 debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        live-build \
        debootstrap \
        squashfs-tools \
        xorriso \
        isolinux \
        syslinux-common \
        grub-efi-amd64-bin \
        grub-pc-bin \
        mtools \
        dosfstools \
        ca-certificates \
        sudo \
        rsync \
        wget \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
CMD ["bash"]

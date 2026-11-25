#!/bin/sh
# ARMHF CI only: prepare a disposable Debian guest, then run the unchanged
# package build under a real ARMv7 Linux kernel rather than linux-user QEMU.
set -eu

fail() { printf '%s\n' "ARMHF_VM_ERROR: $*" >&2; exit 1; }

container_guard() {
    test -f /.dockerenv && test "${G2C_ARMHF_VM_MODE:-}" = "$1" || fail 'not the isolated VM preparation/host container'
    test "$(dpkg --print-architecture)" = "$2" || fail 'unexpected preparation/host architecture'
    test "$(. /etc/os-release; printf '%s' "$VERSION_CODENAME")" = trixie || fail 'not Debian trixie'
}

snapshot_apt() {
    # Keep the signed snapshot identical to the existing release build inputs.
    snapshot=20261027T000000Z
    grep -Fq "\"snapshot\": \"$snapshot\"" /src/packaging/ci-lock.json || fail 'CI snapshot mismatch'
    export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
    chmod 755 /usr/sbin/policy-rc.d
    find /etc/apt/sources.list.d -type f -delete
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s trixie main\ndeb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s trixie-updates main\ndeb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/%s trixie-security main\n' "$snapshot" "$snapshot" "$snapshot" > /etc/apt/sources.list
    apt-get update
}

prepare() {
    container_guard prepare armhf
    snapshot_apt
    # User-mode emulation is used ONLY to install the kernel and boot tools.
    # No application build or application test is run in this container.
    apt-get install -y --no-install-recommends linux-image-armmp-lpae initramfs-tools busybox iproute2 e2fsprogs ca-certificates
    set -- /boot/vmlinuz-*-armmp-lpae
    test "$#" = 1 && test -f "$1" || fail 'missing or ambiguous ARMv7 kernel'
    kernel=${1#/boot/vmlinuz-}
    printf '\nvirtio_mmio\nvirtio_blk\nvirtio_net\next4\n' >> /etc/initramfs-tools/modules
    mkinitramfs -o "/boot/initrd.img-$kernel" "$kernel"
    test -s "/boot/initrd.img-$kernel" || fail 'missing guest initramfs'
    printf '%s\n' "$kernel" > /etc/g2c-armhf-kernel
    install -m 0755 /src/packaging/armhf-vm.sh /usr/local/sbin/g2c-armhf-init
    mkdir -p /src /out /work /cargo-cache
    apt-get clean
    printf 'ARMHF_VM_PREPARED kernel=%s\n' "$kernel"
}

mount_if_needed() {
    target=$1; shift
    if ! mountpoint -q "$target"; then mount "$@" "$target"; fi
}

# Field 22 of /proc/PID/stat, counting from the final ')' of comm.
start_ticks() {
    set -f
    # Intentional field splitting; comm has already been stripped.
    # shellcheck disable=SC2086
    set -- ${1##*)}
    test "$#" -ge 20 || return 1
    case ${20} in ''|*[!0-9]*) return 1;; esac
    printf '%s\n' "${20}"
}

finish_guest() {
    rc=$?
    trap - 0
    set +e
    printf '%s\n' "$rc" > /out/vm-exit
    sync
    printf 'ARMHF_VM_EXIT=%s\n' "$rc"
    # PID 1 is our isolated guest init, not the runner or a user system.
    busybox poweroff -f
    exec sleep infinity
}

boot() {
    test "$$" = 1 || fail 'guest init must be PID 1'
    mount_if_needed /proc -t proc proc
    grep -qw 'g2c_armhf_vm=1' /proc/cmdline || fail 'missing dedicated VM boot marker'
    test "$(uname -m)" = armv7l && test "$(getconf LONG_BIT)" = 32 || fail 'not an ARMv7 32-bit guest'
    test "$(uname -r)" = "$(cat /etc/g2c-armhf-kernel)" || fail 'wrong guest kernel'
    test "$(dpkg --print-architecture)" = armhf || fail 'wrong guest userspace'
    mkdir -p /out /work /cargo-cache
    trap finish_guest 0
    mount_if_needed /sys -t sysfs sysfs
    mount_if_needed /dev -t devtmpfs devtmpfs
    mkdir -p /dev/pts /dev/shm /run
    mount_if_needed /dev/pts -t devpts -o gid=5,mode=620,ptmxmode=0666 devpts
    mount_if_needed /dev/shm -t tmpfs -o mode=1777,nosuid,nodev tmpfs
    mount_if_needed /run -t tmpfs -o mode=0755,nosuid,nodev tmpfs
    modprobe virtio_net
    ip link set lo up
    ip link set eth0 up
    ip addr add 10.0.2.15/24 dev eth0
    ip route add default via 10.0.2.2
    rm -f /etc/resolv.conf
    printf 'nameserver 10.0.2.3\n' > /etc/resolv.conf
    # Check the precise identity invariant that failed under QEMU linux-user.
    # The builtin reads its own stat, while cat reads that same process externally.
    IFS= read -r self_stat < /proc/1/stat
    other_stat=$(cat /proc/1/stat)
    own_ticks=$(start_ticks "$self_stat")
    other_ticks=$(start_ticks "$other_stat")
    test "$own_ticks" = "$other_ticks" || fail 'guest process generation disagreement'
    printf 'execution=qemu-system-arm\narchitecture=armhf\nmachine=armv7l\nbits=32\nkernel=%s\nproc_generation=consistent\n' "$(uname -r)" > /out/vm-platform.txt
    cat /out/vm-platform.txt
    mount --bind /src /src
    mount -o remount,bind,ro /src
    : > /.gpt2codex-alpha-isolated
    # The existing script still performs all Rust/Qt tests, package inspection,
    # lintian and dependency reporting. No nocheck path or test waiver.
    G2C_ALPHA_CONTAINER=1 G2C_EXPECT_ARCH=armhf /bin/sh /src/gpt2codex-alpha-container.sh build
}

run_vm() {
    container_guard run amd64
    test -s /vm/rootfs.tar && test -s /vm/source.tar || fail 'VM inputs missing'
    snapshot_apt
    # QEMU system and filesystem tools also come from the signed frozen snapshot.
    apt-get install -y --no-install-recommends qemu-system-arm e2fsprogs
    qemu-system-arm --version | tee /out/vm-qemu-version.txt
    rootfs=$(mktemp -d /tmp/g2c-armhf-root.XXXXXX)
    tar --numeric-owner -xpf /vm/rootfs.tar -C "$rootfs"
    rm -f /vm/rootfs.tar
    rm -f "$rootfs/.dockerenv"
    mkdir -p "$rootfs/src" "$rootfs/out"
    tar --no-same-owner -xpf /vm/source.tar -C "$rootfs/src"
    kernel=$(cat "$rootfs/etc/g2c-armhf-kernel")
    case "$kernel" in *[!a-zA-Z0-9.+_-]*|'') fail 'invalid guest kernel name';; esac
    test -s "$rootfs/boot/vmlinuz-$kernel" && test -s "$rootfs/boot/initrd.img-$kernel" || fail 'guest boot files missing'
    cp "$rootfs/boot/vmlinuz-$kernel" /vm/vmlinuz
    cp "$rootfs/boot/initrd.img-$kernel" /vm/initrd.img
    # A sparse raw disk avoids loop mounts and privileged Docker execution.
    truncate -s 12G /vm/root.ext4
    mkfs.ext4 -q -F -m 0 -d "$rootfs" /vm/root.ext4
    rm -rf -- "$rootfs"
    printf 'ARMHF_VM_BOOT kernel=%s\n' "$kernel"
    # No host devices or credentials are mounted into the guest; only its own disk.
    # Guest networking is outbound via QEMU SLIRP, with no forwarded host ports.
    timeout --kill-after=20s 150m qemu-system-arm \
        -M virt,gic-version=2,highmem=off -cpu cortex-a15 -smp 4 -m 3072 \
        -accel tcg,thread=multi -nodefaults -display none -serial stdio -monitor none -no-reboot \
        -kernel /vm/vmlinuz -initrd /vm/initrd.img \
        -append 'root=/dev/vda rw rootwait console=ttyAMA0 net.ifnames=0 panic=5 init=/usr/local/sbin/g2c-armhf-init g2c_armhf_vm=1' \
        -drive file=/vm/root.ext4,format=raw,if=none,id=rootdisk \
        -device virtio-blk-device,drive=rootdisk \
        -netdev user,id=net0 -device virtio-net-device,netdev=net0 \
        -object rng-random,filename=/dev/urandom,id=rng0 -device virtio-rng-device,rng=rng0
    # A QEMU shutdown alone is not success. Recover the guest receipt without
    # mounting the disk, and refuse missing, incomplete or failed evidence.
    recovered=$(mktemp -d /tmp/g2c-armhf-out.XXXXXX)
    debugfs -R "rdump /out $recovered" /vm/root.ext4
    guestout=$recovered/out
    test -f "$guestout/vm-exit" || fail 'guest completion receipt missing'
    cp "$guestout/vm-exit" /out/vm-exit
    if test -f "$guestout/vm-platform.txt"; then cp "$guestout/vm-platform.txt" /out/vm-platform.txt; fi
    test "$(cat /out/vm-exit)" = 0 || fail 'guest package build or tests failed'
    test -s /out/vm-platform.txt && grep -Fxq 'execution=qemu-system-arm' /out/vm-platform.txt && grep -Fxq 'proc_generation=consistent' /out/vm-platform.txt || fail 'guest platform receipt missing or invalid'
    test -s "$guestout/result-armhf.json" && test -s "$guestout/build-dependencies.tsv" && test -d "$guestout/validated" || fail 'guest build evidence missing'
    cp -a "$guestout/validated" /out/
    cp "$guestout/build-dependencies.tsv" /out/
    cp "$guestout/result-armhf.json" /out/
}

case ${1:-boot} in
    prepare) prepare;;
    run) run_vm;;
    boot) boot;;
    *) fail 'expected prepare, run or guest boot';;
esac

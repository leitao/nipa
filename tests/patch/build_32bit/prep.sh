#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
#
# Copyright (C) 2019 Netronome Systems, Inc.

cc="ccache gcc"
output_dir=build_32bit/
ncpu=$(grep -c processor /proc/cpuinfo)
build_flags="-j $ncpu"

make CC="$cc" O=$output_dir ARCH=i386 allmodconfig
make CC="$cc" O=$output_dir ARCH=i386 $build_flags
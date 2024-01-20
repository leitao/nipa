#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

import datetime
import shutil
import fcntl
import os
import re
import queue
import sys
import tempfile
import threading
import time

from lib import CbArg
from lib import Fetcher
from lib import VM, new_vm, guess_indicators


"""
Config:

[executor]
name=executor
group=test-group
test=test-name
init=force / continue / next
[remote]
branches=https://url-to-branches-manifest
[local]
base_path=/common/path
json_path=base-relative/path/to/json
results_path=base-relative/path/to/raw/outputs
tree_path=/root-path/to/kernel/git
[www]
url=https://url-to-reach-base-path
# Specific stuff
[env]
paths=/extra/exec/PATH:/another/bin
[vm]
paths=/extra/exec/PATH:/another/bin
init_prompt=expected_on-boot#
virtme_opt=--opt,--another one
default_timeout=15
boot_timeout=45
[ksft]
targets=net


Expected:
group1 test1 skip
group1 test3 fail
group3 testV skip
"""


def namify(what):
    name = re.sub(r'[^0-9a-zA-Z]+', '-', what)
    if name[-1] == '-':
        name = name[:-1]
    return name


def get_prog_list(vm, target):
    tmpdir = tempfile.mkdtemp()
    vm.tree_cmd(f"make -C tools/testing/selftests/ TARGETS={target} INSTALL_PATH={tmpdir} install")

    with open(os.path.join(tmpdir, 'kselftest-list.txt'), "r") as fp:
        targets = fp.readlines()
    vm.tree_cmd("rm -rf " + tmpdir)
    return [e.split(":")[1].strip() for e in targets]


def vm_thread(config, results_path, thr_id, in_queue, out_queue):
    target = config.get('ksft', 'target')
    vm = None
    vm_id = 1

    while True:
        try:
            work_item = in_queue.get(block=False)
        except queue.Empty:
            print(f"INFO: thr-{thr_id} has no more work, exiting")
            break

        if vm is None:
            vm_id, vm = new_vm(results_path, vm_id, config=config, thr=thr_id)

        test_id = work_item[0]
        prog = work_item[1]
        test_name = namify(prog)
        file_name = f"{test_id}-{test_name}"

        print(f"INFO: thr-{thr_id} testing == " + prog)
        vm.cmd(f'make -C tools/testing/selftests TARGETS={target} TEST_PROGS={prog} TEST_GEN_PROGS="" run_tests')

        try:
            vm.drain_to_prompt()
            if vm.fail_state:
                retcode = 1
            else:
                retcode = vm.bash_prev_retcode()
        except TimeoutError:
            vm.ctrl_c()
            vm.drain_to_prompt()
            retcode = 1

        indicators = guess_indicators(vm.log_out)

        result = 'pass'
        if indicators["skip"] or not indicators["pass"]:
            result = 'skip'

        if retcode == 4:
            result = 'skip'
        elif retcode:
            result = 'fail'
        if indicators["fail"]:
            result = 'fail'

        if vm.fail_state == 'oops':
            vm.extract_crash(results_path + f'/vm-crash-thr{thr_id}-{vm_id}')
        vm.dump_log(results_path + '/' + file_name, result=retcode,
                    info={"vm-id": vm_id, "found": indicators, "vm_state": vm.fail_state})

        print(f"INFO: thr-{thr_id} {prog} >> retcode:", retcode, "result:", result, "found", indicators)

        out_queue.put({'test': test_name, 'result': result, 'file_name': file_name})

        if vm.fail_state:
            print(f"INFO: thr-{thr_id} VM kernel crashed, destroying it")
            vm.stop()
            vm.dump_log(results_path + f'/vm-stop-thr{thr_id}-{vm_id}')
            vm = None

    if vm is not None:
        vm.stop()
        vm.dump_log(results_path + f'/vm-stop-thr{thr_id}-{vm_id}')
    return


def test(binfo, rinfo, cbarg):
    print("Run at", datetime.datetime.now())
    cbarg.refresh_config()
    config = cbarg.config

    results_path = os.path.join(config.get('local', 'base_path'),
                                config.get('local', 'results_path'),
                                rinfo['run-cookie'])
    os.makedirs(results_path)

    link = config.get('www', 'url') + '/' + \
           config.get('local', 'results_path') + '/' + \
           rinfo['run-cookie']
    rinfo['link'] = link
    target = config.get('ksft', 'target')

    vm = VM(config)
    vm.build([f"tools/testing/selftests/{target}/config"])
    shutil.copy(os.path.join(config.get('local', 'tree_path'), '.config'),
                results_path + '/config')
    vm.tree_cmd("make headers")
    vm.tree_cmd(f"make -C tools/testing/selftests/{target}/")
    vm.dump_log(results_path + '/build')

    progs = get_prog_list(vm, target)

    in_queue = queue.Queue()
    out_queue = queue.Queue()
    threads = []

    i = 0
    for prog in progs:
        i += 1
        in_queue.put((i, prog, ))

    thr_cnt = int(config.get("cfg", "thread_cnt"))
    delay = float(config.get("cfg", "thread_spawn_delay", fallback=0))
    for i in range(thr_cnt):
        print("INFO: starting VM", i)
        threads.append(threading.Thread(target=vm_thread,
                                        args=[config, results_path, i, in_queue, out_queue]))
        threads[i].start()
        time.sleep(delay)

    for i in range(thr_cnt):
        threads[i].join()

    grp_name = "selftests-" + namify(target)
    cases = []
    while not out_queue.empty():
        r = out_queue.get()
        cases.append({'test': r['test'], 'group': grp_name, 'result': r["result"],
                      'link': link + '/' + r['file_name']})
    if not in_queue.empty():
        print("ERROR: in queue is not empty")

    print("Done at", datetime.datetime.now())

    return cases


def main() -> None:
    cfg_paths = ['remote.config', 'vmksft.config', 'vmksft-p.config']
    if len(sys.argv) > 1:
        cfg_paths += sys.argv[1:]

    cbarg = CbArg(cfg_paths)
    config = cbarg.config

    base_dir = config.get('local', 'base_path')

    f = Fetcher(test, cbarg,
                name=config.get('executor', 'name'),
                branches_url=config.get('remote', 'branches'),
                results_path=os.path.join(base_dir, config.get('local', 'json_path')),
                url_path=config.get('www', 'url') + '/' + config.get('local', 'json_path'),
                tree_path=config.get('local', 'tree_path'),
                first_run=config.get('executor', 'init', fallback="continue"))
    f.run()


if __name__ == "__main__":
    main()
import itertools
import multiprocessing
import os
import signal
import sys
import shutil
import subprocess
import textwrap
from threading import Thread
import time
import typing as t
import re
from pathlib import Path
from typing import List
from builder import BuildArtifact
from builder.aflpp import AflConfig
from runner.base import EvaluationRunner
from logger import get_logger

log = get_logger()


def vagrant_cmd(
    cmd: str, cwd: t.Optional[Path] = None, sigint_after: t.Optional[int] = None
):
    if cwd is None:
        cwd = Path(os.getcwd())
    # We are mounting the parent dir, since our dir structure has folders
    # that contain commas, which confuse Docker's --mount flag.
    escpaed_cwd = cwd.parent.as_posix()
    prefix = ""
    if sigint_after:
        prefix = f"timeout --preserve-status {sigint_after}s "
    cmd = f"""
        docker run -it --rm \
        -e LIBVIRT_DEFAULT_URI \
        -v /var/run/libvirt/:/var/run/libvirt/ \
        -v ~/.vagrant.d:/.vagrant.d \
        --mount type=bind,source={escpaed_cwd}/,destination={escpaed_cwd} \
        -w "{cwd.as_posix()}" \
        --network host \
        vagrantlibvirt/vagrant-libvirt:latest \
        {prefix} vagrant {cmd}
    """
    return textwrap.dedent(cmd)


class VmRunner(EvaluationRunner):
    ALREADY_BUILD_CONFIGURATIONS = set()

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        custom_attrs: t.Optional[t.Dict[str, str]],
        jobs_per_vm: int = 1,
        memory_per_vm_mib: int = 1024,
    ) -> None:
        super().__init__(target, afl_config, job_cnt, timeout_s, custom_attrs)
        self._vm_workdir = Path("/work")
        self._vm_build_artifact = self._target.with_new_root(self._vm_workdir)
        self._vm_afl_config = AflConfig(self._vm_workdir / "aflpp")
        self._ssh_config = self.work_dir() / "vm_ssh_config"
        # Make sure we can always map thread + ht
        assert jobs_per_vm == 1 or (jobs_per_vm % 2) == 0
        self._jobs_per_vm = jobs_per_vm
        self._memory_min = memory_per_vm_mib
        # self._jobs_per_vm_map[i] is the number of jobs VM i gets assigned
        self._jobs_per_vm_map = []
        jobs_left = job_cnt
        while True:
            jobs_assigned = min(jobs_per_vm, jobs_left)
            self._jobs_per_vm_map.append(jobs_assigned)
            jobs_left -= jobs_assigned
            if not jobs_left:
                break

    def _vagrant_config(
        self,
        jobs_per_vm: t.List[int],
        memory_mib: int = 1024,
    ):
        # cpu_set_conf = ""
        # if cpu_set:
        #     cpu_set_str = [str(e) for e in cpu_set]
        #     cpu_set_str = ",".join(cpu_set_str)
        #     cpu_set_conf = f"libvirt.cpuset = {cpu_set_str}"

        next_job_id = 0
        num_jobs = sum(jobs_per_vm)
        assert num_jobs == self.job_cnt()

        # If we schedule more than one job per VM
        if any(e for e in self._jobs_per_vm_map if e > 1):
            # Create a list that contains (interleaved) a cpu id of a thread and a hyper thread
            all_free_cpus = list(range(multiprocessing.cpu_count()))
            assert (len(all_free_cpus) % 2) == 0
            threads = all_free_cpus[: len(all_free_cpus) // 2]
            hyper_threads = all_free_cpus[len(all_free_cpus) // 2 :]
            assert len(threads) == len(hyper_threads)

            free_cpus = itertools.zip_longest(threads, hyper_threads)
            free_cpus = list(itertools.chain(*free_cpus))
        else:
            # If we do not have multiple tasks per container, we just schedule
            # on threads first and on hyper threads last.
            free_cpus = list(range(multiprocessing.cpu_count()))

        per_job_config = []
        # current_vm_job_cnt is the number of jobs the vm should get assigned
        for vm_id, current_vm_job_cnt in enumerate(jobs_per_vm):
            allocated_cpus = []
            try:
                for _ in range(current_vm_job_cnt):
                    allocated_cpus.append(free_cpus.pop(0))
            except IndexError:
                pass

            if len(allocated_cpus) < current_vm_job_cnt:
                # Instead of allocating fewer cores than jobs, we do ont pin at all.
                allocated_cpus = None

            cpu_set_str = ""
            cpuaffinity_str = ""
            if allocated_cpus:
                cpu_set_str = ",".join([str(e) for e in allocated_cpus])
                cpu_set_str = f'libvirt.cpuset = "{cpu_set_str}"'

                # libvirt.cpuaffinitiy 0 => '0-4,^3', 1 => '5', 2 => '6,7'
                cpuaffinity_str = "libvirt.cpuaffinitiy "
                for vm_cpu_id, host_cpu_id in enumerate(allocated_cpus):
                    cpuaffinity_str += f"{vm_cpu_id} => '{host_cpu_id}', "
                cpuaffinity_str = cpuaffinity_str.rstrip(", ")

            afl_cmds = ""
            for _ in range(current_vm_job_cnt):
                afl_work_dir = self._vm_workdir / f"{next_job_id}"
                target_args = " ".join(self._vm_build_artifact.args)
                afl_cmd = f"timeout {self._timeout_s}s {self._vm_afl_config.afl_fuzz()} -i {self._vm_build_artifact.seed_dir} -o {afl_work_dir} -- {self._vm_build_artifact.bin_path} {target_args}"
                afl_cmd = f"tmux new-session -s 'fuzz-{next_job_id}' -d {afl_cmd}"
                afl_cmds += f"{afl_cmd}\n"
                next_job_id += 1

            run_script = f"""
            #!/bin/bash
            # Spawn all afl instances
            set -eu

            function handler()
            {{
                killall "afl-fuzz" || true
            }}
            trap handler SIGINT

            echo "[+] This is vm {vm_id} running {current_vm_job_cnt} jobs"
            export AFL_NO_UI=1
            #export AFL_NO_AFFINITY=1
            export AFL_TRY_AFFINITY=1

            {afl_cmds}\n

            # give afl-fuzz some time to spawn such that the condition below does not fail
            sleep 10s

            # Wait for all instances being killed by the timeout
            while pgrep "afl-fuzz" >/dev/null; do
                echo "Checking if afl-fuzz terminated (timeout is set to {self._timeout_s} seconds)"
                # Timing is not curial here since we limit the execution time of each instance via timeout
                sleep 10s
            done

            # Copy results
            whoami
            id
            ls -la /work-dir-synced-with-host
            echo "Access Test" | sudo tee /work-dir-synced-with-host/test
            ctr=0
            for f in {self._vm_workdir}/*/*/fuzzer_stats; do
                cp -vb $f /work-dir-synced-with-host/{vm_id}_${{ctr}}_fuzzer_stats
                ctr=$((ctr+1))
            done

            echo "[+] Shutting down..."
            poweroff
            """
            run_script = textwrap.dedent(run_script)
            run_script_name = f"{vm_id}_run_script.sh"
            run_script_path = self.work_dir() / run_script_name
            run_script_vm_path = self._vm_workdir / run_script_name
            run_script_path.write_text(run_script)

            template = f"""
            config.vm.define :vm_{vm_id} do |runner|
                runner.vm.provider :libvirt do |libvirt|
                    libvirt.cpus = {current_vm_job_cnt}
                    # {cpu_set_str}
                    {cpuaffinity_str}
                    # libvirt.cpu_mode = "custom"
                    # libvirt.nested = true
                    # libvirt.cpu_mode = "custom"
                    # libvirt.cpu_model = 'custom'
                    # libvirt.cpu_feature :name => 'invtsc', :policy => 'require'
                    libvirt.cpu_mode = "host-model"
                    libvirt.clock_timer :name => 'rtc', :present => 'no', :tickpolicy => 'catchup'
                    libvirt.cputopology :sockets => '1', :cores => '{current_vm_job_cnt}', :threads => '1'
                    libvirt.memory = "{memory_mib}m"
                    libvirt.graphics_type = "none"
                    libvirt.video_type = "none"
                end

                runner.vm.provision "shell", run: 'always', inline: <<-SHELL
                    set -eu
                    echo "[+] This is vm {vm_id} running {current_vm_job_cnt} jobs"
                    echo core | sudo tee /proc/sys/kernel/core_pattern

                    export AFL_NO_UI=1
                    #export AFL_NO_AFFINITY=1
                    export AFL_TRY_AFFINITY=1
                    ls -al {self._vm_workdir}
                    chmod +x {run_script_vm_path}
                SHELL
            end
            """
            template = template.strip()
            template = textwrap.dedent(template)
            template = textwrap.indent(template, "    ")
            per_job_config.append(template)

        per_job_config = "\n".join(per_job_config)

        vagrant_config = f"""
        Vagrant.configure("2") do |config|
            config.vm.box = "generic/ubuntu2204"
            config.vm.box_version = "4.3.12"
            config.vm.guest = "ubuntu"
            config.vm.synced_folder ".", "/work-dir-synced-with-host", type: "9p", disabled: false, accessmode: "mapped", mount: true, access: "any"

            config.vm.provider :libvirt do |libvirt|
                libvirt.graphics_type = "none"
                libvirt.video_type = "none"
            end

            config.vm.provision "shell", inline: <<-SHELL
                set -eu
                export DEBIAN_FRONTEND="noninteractive"
                echo "[+] Copying stuff to {self._vm_workdir}"
                rsync -r --exclude=".vagrant" /work-dir-synced-with-host/ {self._vm_workdir}
                echo "[+] Installing dependencies, this may take a while..."
                apt-get update -y > /dev/null
                apt-get install -y llvm clang lld > /dev/null
            SHELL

            # Create a VM for each job
            {per_job_config}
        end
        """
        return textwrap.dedent(vagrant_config)

    def prepare(self, purge: bool = False) -> bool:
        if not super().prepare(purge):
            # Parent decided that we are prepared.
            return True

        # Check if libvirt + virsh is installed
        try:
            output = subprocess.check_output(
                "sudo virsh list --all --name", shell=True, encoding="utf8"
            ).strip()
            if output:
                useful_virsh_cmds = (
                    "sudo virsh list --all --name | xargs -I{} sudo virsh destroy {}\n"
                )
                useful_virsh_cmds += (
                    "sudo virsh list --all --name| xargs -I{} sudo virsh undefine {}"
                )
                raise RuntimeError(
                    f"They seem to be other virtual machines running:\n{output}.\nUseful commands to delete the machines:\n{useful_virsh_cmds}"
                )
        except Exception as e:
            raise RuntimeError(
                "Looks like libvirt + virsh is not installed: sudo apt install qemu qemu-kvm libvirt-clients libvirt-daemon-system virtinst bridge-utils && sudo systemctl restart libvirtd"
            ) from e

        subprocess.check_call(
            "docker pull vagrantlibvirt/vagrant-libvirt:latest", shell=True
        )

        # Check if it works
        subprocess.check_call(
            vagrant_cmd("global-status"), shell=True, stdout=subprocess.DEVNULL
        )

        work_dir = self.work_dir()

        # Copy afl++
        shutil.copytree(self.afl_config().root(), work_dir / "aflpp")

        # Copy seeds + binary
        shutil.copy(self.target().bin_path, work_dir)
        shutil.copytree(self.target().seed_dir, work_dir / self.target().seed_dir.name)

        # Create the Vagrant config
        config = self._vagrant_config(self._jobs_per_vm_map, self._memory_min)
        vagrant_config_path = work_dir / "Vagrantfile"
        vagrant_config_path.write_text(config)

        # v9 uses the local user qemu is running at to check perms.
        # This is required for the vm being able to write into our work dir.
        # Certainly, this could be a little more fine grained :)
        subprocess.check_call(f"sudo chmod -R 777 {self.work_dir()}", shell=True)

        # Check the config
        subprocess.check_call(
            vagrant_cmd("validate", work_dir),
            shell=True,
            cwd=work_dir,
            stderr=subprocess.DEVNULL,
        )

        # Clean up leftovers
        subprocess.run(
            vagrant_cmd("destroy --force", work_dir),
            shell=True,
            cwd=work_dir,
            check=False,
        )

        # Test each unique target + afl configuration once, such that it will not fail later during
        # running the actual evaluation.
        target_tuple = (self.target().name, self.afl_config().root().as_posix())
        if False and target_tuple not in VmRunner.ALREADY_BUILD_CONFIGURATIONS:
            log.info(
                f"Testing {target_tuple}, since is was prepared for the first time"
            )
            VmRunner.ALREADY_BUILD_CONFIGURATIONS.add(target_tuple)

            log.info("Starting VMs. Test should terminate in 120s. If not, hit ctrl+c")
            # Start it once for testing.
            # For some reason (dockered) vagrant does not like to be not running on a tty,
            # so we make this blocking and send SIGINT after some time
            subprocess.run(
                vagrant_cmd("up --parallel", work_dir, sigint_after=120),
                shell=True,
                cwd=work_dir,
                check=True,
            )

            # # Trigger clean shutdown if still running.
            # try:
            #     # Hack to deal with vagrant sometimes ignoring SIGINT :/
            #     for _ in range(3):
            #         log.info(f"Sendin SIGINT to vagrant machines (pid={process.pid})")
            #         os.kill(process.pid, signal.SIGTERM)
            #         log.info("Waiting for machines to terminate")
            #         try:
            #             process.wait(120)
            #         except subprocess.TimeoutExpired:
            #             log.info("Timeout while waiting")
            #         else:
            #             break
            #     else:
            #         # final try
            #         process.wait(120)
            # except subprocess.TimeoutExpired:
            #     raise
            # except:
            #     # killing may fail if the machines are already terminated
            #     pass

            # Dump ssh config we are later going to use to control the machines
            # ssh_config = subprocess.check_output(
            #     vagrant_cmd("ssh-config", work_dir),
            #     shell=True,
            #     encoding="utf8",
            #     cwd=work_dir,
            # )
            # self._ssh_config.write_text(ssh_config)

            # Shut it down
            subprocess.check_call(
                vagrant_cmd("halt", work_dir), shell=True, cwd=work_dir
            )

            fuzzers_stats = list(self.work_dir().glob("fuzzer_stats*"))
            if len(fuzzers_stats) != self.job_cnt():
                log.warning(
                    f"There are only {len(fuzzers_stats)} stats files, but we spawned {self.job_cnt()}. However, this might be due to high load causing some jobs to be not spawned, yet"
                )

            for f in fuzzers_stats:
                f.unlink()

        return True

    def start(self) -> None:
        work_dir = self.work_dir()

        # Start all VMs
        subprocess.check_call(
            vagrant_cmd("up --parallel", work_dir), shell=True, cwd=work_dir
        )

        ssh_session: t.List[subprocess.Popen] = []  # type: ignore
        for vm_id, _ in enumerate(self._jobs_per_vm_map):
            log.info(f"Starting fuzzer on {vm_id}")
            run_script_name = f"{vm_id}_run_script.sh"
            run_script_vm_path = self._vm_workdir / run_script_name

            vms_ssh_session = subprocess.Popen(
                vagrant_cmd(
                    f"ssh -t vm_{vm_id} -- sudo bash -c '{run_script_vm_path}'",
                    work_dir,
                ),
                shell=True,
                cwd=work_dir,
            )
            ssh_session.append(vms_ssh_session)

        log.info(f"Waiting for {len(ssh_session)} fuzzers to terminate")
        # Some extra time for shutdown + boot
        deadline = time.monotonic() + self._timeout_s + 600

        while time.monotonic() < deadline:
            time.sleep(10)
            ssh_session = [s for s in ssh_session if s.poll() is None]
            log.info(f"There are still {len(ssh_session)} fuzzers running")
            if not ssh_session:
                break
        else:
            raise RuntimeError(f"VMs did not terminate. This should not happen!")

        # fix perms of files created in the vm
        subprocess.check_call(f"sudo chmod -R 777 {self.work_dir()}", shell=True)

        subprocess.check_call(
            vagrant_cmd("destroy --force", work_dir), shell=True, cwd=work_dir
        )

    def stats_files_paths(self) -> List[Path]:
        return list(self.work_dir().glob("*fuzzer_stats*"))

    def purge(self) -> None:
        work_dir = self.work_dir()
        subprocess.run("killall vagrant", shell=True)
        subprocess.run(
            vagrant_cmd("destroy --force", work_dir),
            shell=True,
            cwd=work_dir,
            check=False,
        )
        return super().purge()


class DefaultVmRunner(VmRunner):
    """
    The first version of the VmRunner that was booting all VMs at once.
    While each VM started fuzzing as soon as it was booted.
    This was replaced by DefaultVmRunnerV2, which first boots all VMs
    and then starts fuzzing. This way the fuzzers are not slowed down by the
    other VMs booting. This type was actually only used for naming and the
    "old" implementation does not exists anymore.
    """

    def __init__(self) -> None:
        raise RuntimeError(
            "Use DefaultVmRunnerV2, this runner is deprecated and should not be used to generate new data!"
        )


class DefaultVmRunnerV2(VmRunner):
    pass


# Leaf some room for other stuff (in total we have 251Gi)
TOTAL_MEM_MIB = 230 * 1024


class VmRunner8cores(VmRunner):

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        custom_attrs: t.Optional[t.Dict[str, str]],
    ) -> None:
        super().__init__(
            target,
            afl_config,
            job_cnt,
            timeout_s,
            custom_attrs,
            jobs_per_vm=8,
            memory_per_vm_mib=(8 * 1024),
        )


class VmRunner8coresV2(VmRunner):
    """
    Compared to `VmRunner8cores` this one uses:
    libvirt.cpu_mode = "host-model"
    libvirt.clock_timer :name => 'rtc', :present => 'no', :tickpolicy => 'catchup'
    libvirt.cputopology :sockets => '1', :cores => '{current_vm_job_cnt}', :threads => '1'
    """

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        custom_attrs: t.Optional[t.Dict[str, str]],
    ) -> None:
        super().__init__(
            target,
            afl_config,
            job_cnt,
            timeout_s,
            custom_attrs,
            jobs_per_vm=8,
            memory_per_vm_mib=(8 * 1024),
        )


class VmRunner2cores(VmRunner):
    """
    Same as `VmRunner8coresV2` but with 2 cores per VM.
    """

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        custom_attrs: t.Optional[t.Dict[str, str]],
    ) -> None:
        super().__init__(
            target,
            afl_config,
            job_cnt,
            timeout_s,
            custom_attrs,
            jobs_per_vm=2,
            memory_per_vm_mib=(2 * 1024),
        )


class DefaultVmRunnerV3(VmRunner):
    """
    Same as `VmRunner8coresV2` but with one core per machine.
    """

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        custom_attrs: t.Optional[t.Dict[str, str]],
    ) -> None:
        super().__init__(
            target,
            afl_config,
            job_cnt,
            timeout_s,
            custom_attrs,
            jobs_per_vm=1,
            memory_per_vm_mib=1024,
        )

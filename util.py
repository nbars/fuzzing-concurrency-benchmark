from pathlib import Path
import subprocess


def set_scaling_governor(governor: str = "performance"):
    subprocess.check_call(
        f"echo {governor} | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
        shell=True,
    )


def set_turbo(enabled: bool):
    intel_no_turbo_file = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
    if intel_no_turbo_file.exists():
        subprocess.check_call(
            f"echo {("1", "0")[enabled]} | sudo tee {intel_no_turbo_file.as_posix()}",
            shell=True,
        )
    else:
        subprocess.check_call(
            f"echo {("0", "1")[enabled]} | sudo tee /sys/devices/system/cpu/cpufreq/boost",
            shell=True,
        )

def set_shm_rmid_forced(enabled: bool):
    subprocess.check_call(
        f"echo {("0", "1")[enabled]} | sudo tee /proc/sys/kernel/shm_rmid_forced",
        shell=True,
    )
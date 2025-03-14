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
        enabled_str = "0" if enabled else "1"
        subprocess.check_call(
            f"echo {enabled_str} | sudo tee {intel_no_turbo_file.as_posix()}",
            shell=True,
        )
    else:
        enabled_str = "1" if enabled else "0"
        subprocess.check_call(
            f"echo {enabled_str} | sudo tee /sys/devices/system/cpu/cpufreq/boost",
            shell=True,
        )


def set_shm_rmid_forced(enabled: bool):
    enabled_str = "1" if enabled else "0"
    subprocess.check_call(
        f"echo {enabled_str} | sudo tee /proc/sys/kernel/shm_rmid_forced",
        shell=True,
    )

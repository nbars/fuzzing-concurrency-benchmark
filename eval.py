#!/usr/bin/env python3

import typing as t
import argparse
import shutil
import re
import random
from abc import ABC, abstractmethod
from logger import get_logger, setup_root_logger
from pathlib import Path

def range_limited_int(lower_limit: int = 1) -> t.Callable[[str], int]:
     def check(val: str):
        r = int(val)
        assert r >= lower_limit
        return r
     return check

def parse_time_as_s(val: str) -> int:
    m = re.match(r"^(?P<fac>[1-9][0-9]*)(?P<suffix>s|m|d)$", val)
    assert m
    suffix_to_scalar = {
        "s": 1,
        "m": 60,
        "h": 3600,
    }
    return suffix_to_scalar[m.group("suffix")] * int(m.group("fac")) # type: ignore


def main():
    setup_root_logger()
    log = get_logger()

    main_parser = argparse.ArgumentParser(
        prog="eval", description="Benchmark suit to evaluate the influence of different abstraction mechanisms on fuzzing throughput."
    )

    main_parser.add_argument("--min-concurrent-jobs", type=range_limited_int(), default=1, help="The minimum number of jobs the test series is started with.")
    main_parser.add_argument("--concurrent-jobs-step", type=range_limited_int(), default=1, help="Steps in that the value is increment start at min-concurrent-jobs to max-concurrent-jobs.")
    main_parser.add_argument("--additional-jobs-step", type=set, default=set([52, 104]), help="")
    main_parser.add_argument("--max-concurrent-jobs", type=range_limited_int(), default=208, help="The maximum number of jobs the test series is executed for with.")
    main_parser.add_argument("--storage", type=Path, default=Path("storage"), help="The Folder to store results in.")
    main_parser.add_argument("--step-timeout", type=parse_time_as_s, default="10m", help="The minimum number of jobs the test series is started with.")

    args = main_parser.parse_args()
    timeout_s = args.step_timeout
    storage_path = Path(args.storage)
    assert args.min_concurrent_jobs <= args.max_concurrent_jobs
    storage_path.mkdir(parents=True, exist_ok=True)

    # Right bound inclusive
    additional_jobs_step: t.Set[int] = args.additional_jobs_step
    job_cnt_configurations_set = set(range(args.min_concurrent_jobs, args.max_concurrent_jobs + 1, args.concurrent_jobs_step))
    job_cnt_configurations_set = additional_jobs_step | job_cnt_configurations_set
    job_cnt_configurations = list(job_cnt_configurations_set)
    random.shuffle(job_cnt_configurations)

    if job_cnt_configurations[-1] != args.max_concurrent_jobs:
        log.warning(f"Current steps value will cause the final concurrent value not being the specified max-concurrent-jobs value. Last value evaluated is {job_cnt_configurations[-1]}")


    log.info(f"We are going to perform {len(job_cnt_configurations)} different configurations with a timeout of {timeout_s} seconds")

    from runner import PlainRunner
    runner_type = [
        PlainRunner
    ]
    total_runtime = timeout_s * len(job_cnt_configurations) * len(runner_type)
    if total_runtime > 3600:
        total_runtime = f"{total_runtime/3600:.1f} hours"
    else:
        total_runtime = f"{total_runtime/60} minutes"
    log.info(f"This campaign will take {total_runtime} in total")


    log.info("Building base using the PlainRunner")
    base_runner = PlainRunner(job_cnt_configurations[-1], timeout_s)
    base_runner.build(purge=False)

    # Information about the target that has been build by the PlainRunner and is going to be copied in the
    # other runners.
    # TODO: Actually, the PlainRunner should not be the builder and there should be another class for that :/
    build_artifact_info = base_runner.get_build_artifact_info()

    # We need some runner instances, so just build them for the first configuration.
    runners = [ty(job_cnt_configurations[-1], timeout_s) for ty in runner_type]
    for r in runners:
        if isinstance(r, PlainRunner):
            # Already build above, since is servers as base for all others.
            continue
        log.info(f"Preparing runners: {r}")
        r.build()

    for job_cnt in job_cnt_configurations:
        log.info(f"Next concurrency level check is {job_cnt}")
        runners = [ty(job_cnt, timeout_s) for ty in runner_type]
        log.info(f"Runners that are going to be execute for this level: {[str(r) for r in runners]}")
        for r in runners:
            log.info(f"Next runner is {r}")
            job_storage = storage_path / f"{job_cnt}-jobs_{r.name()}"
            if job_storage.exists():
                log.info(f"Results for current configuration are already at {job_storage}, skipping...")
                continue

            log.info(f"Final results will be located at {job_storage}")
            r.start(purge=True)

            stats_files = r.stats_files_paths()
            job_storage.mkdir(parents=True)
            for i, stats_path in enumerate(stats_files):
                shutil.copy(stats_path, job_storage / f"{i}_stats_file.txt")


if __name__ == "__main__":
     main()
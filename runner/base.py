from abc import ABC, abstractmethod
from pathlib import Path
import typing as t

AFLPP_VERSION = "v4.21c"

class EvaluationRunner(ABC):

    def __init__(self, job_cnt: int, timeout_s: int) -> None:
        self._job_cnt = job_cnt
        self._timeout_s = timeout_s

    @abstractmethod
    def build(self, purge: bool = False) -> None:
        raise NotImplementedError()

    def __str__(self) -> str:
        return f"{self.__class__.__name__}({self._job_cnt=}, {self._timeout_s=})"

    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def start(self, purge: bool) -> None:
        raise NotImplementedError()

    @abstractmethod
    def purge_previous_run(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def stats_files_paths(self) -> t.List[Path]:
        raise NotImplementedError()

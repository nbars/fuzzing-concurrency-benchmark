import os
from pathlib import Path
import subprocess
from builder import Builder


class AflConfig:

    def __init__(self, build_root: Path):
        self._root = build_root
        self._cc = build_root / "afl-clang-fast"
        self._cxx = build_root / "afl-clang-fast++"
        self._afl_fuzz = build_root / "afl-fuzz"
        for attr, attr_val in self.__dict__.items():
            if isinstance(attr_val, Path):
                assert (
                    attr_val.exists()
                ), f"Attribute {attr} does points to a non existing file {attr_val}"

    def root(self) -> Path:
        return self._root

    def afl_cc(self) -> Path:
        return self._cc

    def afl_cxx(self) -> Path:
        return self._cxx

    def afl_fuzz(self) -> Path:
        return self._afl_fuzz


class AflppBuilder(Builder):

    def __init__(self, git_commit_or_tag: str = "v4.21c") -> None:
        super().__init__()
        self._git_commit_or_tag = git_commit_or_tag
        self._src_dir = self.work_dir() / "src"

    def build(self, purge: bool = False) -> bool:
        if not super().build(purge):
            return False

        subprocess.check_call(
            f"git clone https://github.com/AFLplusplus/AFLplusplus {self._src_dir}",
            shell=True,
        )

        env = Builder.get_build_env()
        subprocess.check_call(
            f"sudo apt install -y clang llvm lld && git checkout {self._git_commit_or_tag} && make -j ",
            shell=True,
            cwd=self._src_dir,
            env=env,
        )
        return True

    def afl_config(self) -> AflConfig:
        return AflConfig(self._src_dir)

    def target_name(self) -> str:
        return "aflpp-fuzzer"

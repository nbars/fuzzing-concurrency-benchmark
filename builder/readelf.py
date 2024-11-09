import os
from pathlib import Path
import shutil
import subprocess
import textwrap
from . import AflTargetBuildere, BuildArtifact, Builder
from logger import get_logger
from . import seeds

log = get_logger()


class ReadelfBuilder(AflTargetBuildere):

    def __init__(
        self, afl_cc: Path, afl_cxx: Path, git_commit_or_tag: str = "binutils-2_43"
    ) -> None:
        super().__init__(afl_cc, afl_cxx)
        self._git_commit_or_tag = git_commit_or_tag
        self._build_dir = self.work_dir() / "build"
        self._build_dir.mkdir(exist_ok=True)
        self._src_dir = self.work_dir() / "src-binutils-gdb"

    def build(self, purge: bool = False) -> bool:
        if not super().build(purge):
            return False

        binutils_src_dir = self._src_dir
        if not binutils_src_dir.exists():
            subprocess.check_call(
                f"git clone git://sourceware.org/git/binutils-gdb.git {binutils_src_dir}",
                shell=True,
            )

        subprocess.check_call(
            f"git checkout {self._git_commit_or_tag}", shell=True, cwd=binutils_src_dir
        )

        try:
            env = Builder.get_build_env()
            subprocess.check_call(
                "sudo apt update && sudo apt-get build-dep -y binutils",
                shell=True,
                env=env,
            )
        except subprocess.SubprocessError:
            log.error(
                f"Check the output above, likely deb-src entries are missing (sed -i 's/^# deb-src/deb-src/g' /etc/apt/sources.list).",
                exc_info=True,
            )
            log.error(
                "Your config might also be located in /etc/apt/sources.list.d/<...>"
            )
            raise

        # Configure with as few deps as possible, such that we do not get sad if running on a different system.
        # This should produce redelf with only these deps:
        # linux-vdso.so.1 (0x00007ffcb6b9e000)
        # libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f5819cf8000)
        # /lib64/ld-linux-x86-64.so.2 (0x00007f581a325000)
        rela_src_dir = os.path.relpath(self._src_dir, self._build_dir)
        configure_cmd = f"""
        {rela_src_dir}/configure --prefix=/usr       \
                    --enable-ld=default \
                    --disable-plugins    \
                    --disable-shared     \
                    --disable-werror    \
                    --disable-gdbserver   \
                    --disable-gdb   \
                    --without-zstd \
                    --disable-lto \
                    --enable-compressed-debug-sections=none \
        """
        configure_cmd = textwrap.dedent(configure_cmd)
        env = Builder.get_build_env()
        env.update(
            {
                "CC": self.afl_cc().as_posix(),
                "CXX": self.afl_cxx().as_posix(),
                "CFLAGS": "-fuse-ld=lld",
                "CXXFLAGS": "-fuse-ld=lld",
                "LD": "/usr/bin/lld",
            }
        )

        subprocess.check_call(
            f"{configure_cmd} && make -j", shell=True, cwd=self._build_dir, env=env
        )
        assert self.build_artifact().validate()
        return True

    def build_artifact(self) -> BuildArtifact:
        return BuildArtifact(
            self.target_name(),
            self._build_dir / "binutils/readelf",
            seeds.elf_files(),
            ["-a", "@@"],
        )

    def target_name(self) -> str:
        return "readelf"

from pathlib import Path


def elf_files() -> Path:
    return Path(__file__).parent / "elf_files"
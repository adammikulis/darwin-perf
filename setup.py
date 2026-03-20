"""Build script for the native C extension."""

from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            "darwin_perf._native",
            sources=[
                "src/darwin_perf/_native.c",
                "src/darwin_perf/_gpu.c",
                "src/darwin_perf/_ioreport.c",
                "src/darwin_perf/_cpu.c",
                "src/darwin_perf/_memory.c",
                "src/darwin_perf/_proc.c",
                "src/darwin_perf/_net.c",
                "src/darwin_perf/_sensors.c",
            ],
            extra_link_args=["-framework", "IOKit", "-framework", "CoreFoundation"],
            language="c",
        ),
    ],
)

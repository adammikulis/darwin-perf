"""Build script for the native C extension."""

from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            "darwin_perf._native",
            sources=["src/darwin_perf/_native.c"],
            extra_link_args=["-framework", "IOKit", "-framework", "CoreFoundation"],
            language="c",
        ),
    ],
)

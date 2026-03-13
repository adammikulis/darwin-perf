"""Build script for the native C extension."""

from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            "macos_gpu_proc._native",
            sources=["src/macos_gpu_proc/_native.c"],
            extra_link_args=["-framework", "IOKit", "-framework", "CoreFoundation"],
            language="c",
        ),
    ],
)

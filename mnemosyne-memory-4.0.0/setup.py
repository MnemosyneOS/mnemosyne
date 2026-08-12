#!/usr/bin/env python3
"""Mnemosyne Memory — zero-dependency AI Agent Memory Engine.
PyPI: mnemosyne-l1 (mnemosyne-memory is taken by mnemosyne-oss)
"""
from setuptools import setup, find_packages
import os

HERE = os.path.dirname(os.path.abspath(__file__))

setup(
    name="mnemosyne-l1",
    version="5.1.0",
    description="Zero-dependency AI Agent Memory Engine — L1 Lexical Cache",
    long_description=open(os.path.join(HERE, "comparison.md"), encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="胡景产品负责人 (Jingkun Hu)",
    author_email="<your-user>ngkun@qq.com",
    url="https://github.com/FrankHu-HK/mnemosyne",
    license="MIT",
    py_modules=["mnemosyne", "mcp_server"],
    package_dir={"": "scripts"},
    python_requires=">=3.8",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
    keywords="ai, memory, agent, retrieval, token-optimization, zero-dependency, local-first",
)

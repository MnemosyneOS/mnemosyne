#!/usr/bin/env python3
"""Mnemosyne OS — zero-dependency AI Agent Memory Engine.
PyPI: ai-memo-os (mnemosyne-memory is taken by mnemosyne-oss)
"""
from setuptools import setup, find_packages
import os

HERE = os.path.dirname(os.path.abspath(__file__))

def _read_long_desc():
    for name in ("comparison.md", "README.md"):
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            try:
                return open(p, encoding="utf-8").read()
            except Exception:
                pass
    return "Zero-dependency AI Agent Memory Engine."

setup(
    name="ai-memo-os",
    version="8.1.0",
    description="Zero-dependency, local-first AI memory engine: graph memory, multimodal ingestion, reranking, temporal reasoning, hash-chained audit ledger, capsule compression, memory formation + runtime layers.",
    long_description=_read_long_desc(),
    long_description_content_type="text/markdown",
    author="Mnemosyne OS contributors",
    url="https://github.com/MnemosyneOS/mnemosyne",
    project_urls={
        "Homepage": "https://github.com/MnemosyneOS/mnemosyne",
        "Documentation": "https://github.com/MnemosyneOS/mnemosyne/blob/main/README.md",
        "Source": "https://github.com/MnemosyneOS/mnemosyne",
        "Changelog": "https://github.com/MnemosyneOS/mnemosyne/blob/main/CHANGELOG.md",
    },
    license="MIT",
    packages=find_packages(exclude=("tests", "tests.*", "benchmarks", "quality_eval")),
    python_requires=">=3.8",
    install_requires=[],
    extras_require={
        "numpy": ["numpy"],
        "transformers": ["transformers"],
        "tiktoken": ["tiktoken"],
    },
    include_package_data=True,
    package_data={
        "mnemosyne.webui": [
            "static/index.html", "static/logo.jpg",
            "static/css/app.css", "static/js/app.js",
            "web_config/agents.json", "web_config/api.md",
            "web_config/external_sources.json", "web_config/namespace.json",
            "web_config/profiles.json",
        ],
    },
    entry_points={
        "console_scripts": [
            "mnemosyne=mnemosyne.cli:main",
            "mnemosyne-web=mnemosyne.webui.web_server:run_server",
        ],
    },
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
    keywords="ai, memory, agent, retrieval, mcp, token-optimization, zero-dependency, local-first, long-term-memory",
)

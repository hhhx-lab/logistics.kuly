"""
测试公共配置。

`external` 标记的测试会调用真实付费服务，默认跳过；只有显式 `-m external` 选中时才运行，
避免日常全量测试产生费用或依赖外网。
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "external: 调用真实外部付费服务，默认跳过，需 -m external 显式运行")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    markexpr = config.getoption("-m", default="") or ""
    if "external" in markexpr and "not external" not in markexpr:
        return
    skip = pytest.mark.skip(reason="调用真实外部服务，需 -m external 显式运行")
    for item in items:
        if "external" in item.keywords:
            item.add_marker(skip)

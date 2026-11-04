"""畅课（TronClass）课程文档归档与通知汇总工具。"""

from __future__ import annotations

import sys
from pathlib import Path

# 登录用的 Playwright 装在项目根的 .vendor 里（系统 site-packages 写不进去），
# 这里补一条搜索路径，让 import playwright 能正常工作。
_VENDOR = Path(__file__).resolve().parent.parent / ".vendor"
if _VENDOR.is_dir():
    sys.path.insert(0, str(_VENDOR))

__version__ = "2.0.0"
__all__ = ["__version__"]

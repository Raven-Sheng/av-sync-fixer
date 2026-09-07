"""桌面入口；CLI 仍由 main.py 提供。"""

import sys


def main() -> int:
    try:
        from gui.main_window import launch
    except ImportError as exc:
        print(f"无法加载桌面界面：{exc}\n请在项目环境中运行 python -m pip install -r requirements.txt。", file=sys.stderr)
        return 1
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())

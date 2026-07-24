"""
dglab-ai-master 环境依赖验证与安装请求。

用法：
    python3 check_env.py                    # 只验证并报告，不做任何修改
    python3 check_env.py --install          # 缺依赖时执行安装（须先征得用户同意）
    python3 check_env.py --venv <路径>      # 在指定路径创建 venv 并装入依赖

流程约定（SKILL.md「环境准备」一节）：
1. Agent 先以无参数运行本脚本验证运行环境。
2. 缺依赖时，Agent 必须向用户发起安装请求，说明要装什么、装到哪。
3. 用户同意后，才以 --install 重新运行本脚本完成安装。
4. 若 --install 因权限失败（如用户 site-packages 目录被 root 占用），
   改用 --venv 回退方案（无需 sudo）。之后所有脚本须用报告打印出的
   venv 解释器路径运行。

退出码：0 = 环境就绪；2 = 缺依赖（未安装）；1 = 安装失败或 Python 版本过低。
"""
import importlib.util
import json
import os
import subprocess
import sys

MIN_PYTHON = (3, 9)

# (pip 包名, import 模块名, 是否必需)
DEPENDENCIES = [
    ("websocket-client", "websocket", True),   # V4 协议客户端（dglab_v4_client.py）
    ("websockets", "websockets", True),        # 自建 V4 Relay 服务端（dglab_v4_relay.py）
    ("qrcode[pil]", "qrcode", True),           # 配对二维码图片生成（APP 只能扫码接入）
]
# 可选替代依赖：存在时也在报告中标注，但不阻止就绪判定
OPTIONAL = []


def check() -> dict:
    report = {
        "python": ".".join(map(str, sys.version_info[:3])),
        "python_ok": sys.version_info[:2] >= MIN_PYTHON,
        "python_executable": sys.executable,
        "dependencies": [],
        "optional": [],
        "ready": True,
    }
    if not report["python_ok"]:
        report["ready"] = False

    for pkg, module, required in DEPENDENCIES:
        found = importlib.util.find_spec(module) is not None
        report["dependencies"].append(
            {"package": pkg, "module": module, "installed": found,
             "required": required})
        if required and not found:
            report["ready"] = False

    for pkg, module in OPTIONAL:
        report["optional"].append(
            {"package": pkg, "module": module,
             "installed": importlib.util.find_spec(module) is not None})

    return report


def install_missing(report: dict) -> bool:
    for dep in report["dependencies"]:
        if dep["required"] and not dep["installed"]:
            print(f"安装依赖: {dep['package']} ...", flush=True)
            rc = subprocess.call(
                [sys.executable, "-m", "pip", "install", dep["package"]])
            if rc != 0:
                return False
    return True


def create_venv(path: str) -> int:
    """venv 回退方案：创建虚拟环境并装入依赖，返回退出码。
    用 venv 解释器自身调 pip，规避路径含空格时 shebang 失效的问题。"""
    if subprocess.call([sys.executable, "-m", "venv", path]) != 0:
        print(json.dumps({"ok": False, "error": "venv 创建失败"},
                         ensure_ascii=False))
        return 1
    venv_py = os.path.join(path, "bin", "python3")
    if not os.path.exists(venv_py):  # Windows 布局
        venv_py = os.path.join(path, "Scripts", "python.exe")
    pkgs = [pkg for pkg, _, required in DEPENDENCIES if required]
    if subprocess.call([venv_py, "-m", "pip", "install", "-q", *pkgs]) != 0:
        print(json.dumps({"ok": False, "error": "venv 内 pip 安装失败"},
                         ensure_ascii=False))
        return 1
    # 用 venv 解释器自验
    rc = subprocess.call([venv_py, os.path.abspath(__file__)])
    if rc == 0:
        print(f"\nvenv 就绪。后续请使用解释器: {venv_py}")
    return rc


def main():
    do_install = "--install" in sys.argv
    if "--venv" in sys.argv:
        idx = sys.argv.index("--venv")
        if idx + 1 >= len(sys.argv):
            print("用法: python3 check_env.py --venv <路径>")
            sys.exit(1)
        sys.exit(create_venv(sys.argv[idx + 1]))

    report = check()

    if not report["ready"] and do_install and report["python_ok"]:
        if not install_missing(report):
            print(json.dumps({"ok": False, "error": "pip 安装失败",
                              "fallback": "python3 check_env.py --venv <路径>"},
                             ensure_ascii=False))
            sys.exit(1)
        report = check()  # 安装后重新验证

    report["ok"] = report["ready"]
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not report["ready"]:
        missing = [d["package"] for d in report["dependencies"]
                   if d["required"] and not d["installed"]]
        if missing:
            print(f"\n缺少依赖: {', '.join(missing)}")
            print("请征得用户同意后运行: python3 check_env.py --install")
        sys.exit(2)


if __name__ == "__main__":
    main()

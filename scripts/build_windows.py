"""在 Windows 上生成带图标的独立 EXE，仅打包程序和图标。"""
import hashlib
from pathlib import Path
import subprocess
import sys


def main():
    if sys.platform != 'win32':
        raise SystemExit('请在 Windows 或 GitHub Windows runner 上运行此脚本。')
    root = Path(__file__).resolve().parent.parent
    name = '汽配批发管理系统'
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--onefile', '--windowed', '--name', name,
        '--icon', str(root / 'assets' / 'app-icon.ico'),
        '--add-data', f'{root / "assets"}:assets',
        str(root / 'app.py'),
    ], cwd=root, check=True)
    dist = root / 'dist'
    exe = dist / f'{name}.exe'
    if not exe.is_file() or exe.read_bytes()[:2] != b'MZ':
        raise SystemExit('未生成有效的 Windows EXE。')
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    (dist / 'SHA256SUMS.txt').write_text(f'{digest}  {exe.name}\n', encoding='utf-8')
    (dist / 'README.txt').write_text(
        f'{name}\n\n'
        '适用于 Windows 10/11 64 位。解压后双击 EXE 启动，无需安装 Python。\n'
        '首次启动请在系统设置中填写公司资料。\n'
        '数据保存在 %LOCALAPPDATA%\\AutoPartsMVP\\autoparts.db。\n'
        '更新程序不影响该位置的已有数据。请使用软件中的备份数据库功能定期备份。\n'
        '打印请选择 A4 横向，并关闭浏览器页眉页脚。\n', encoding='utf-8-sig')
    print(f'Generated: {exe}')


if __name__ == '__main__':
    main()

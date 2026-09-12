import sys


def _is_cli_invocation(argv):
    """判断这次启动应该走命令行还是图形界面。

    双击（无参数）→ GUI；带链接或命令行选项 → CLI。
    """
    if not argv:
        return False
    first = argv[0]
    if first in ('--cli', '-h', '--help', '--version'):
        return True
    if first.startswith(('http://', 'https://', 'ftp://', 'ftps://')):
        return True
    if first in ('-i', '--input', '-o', '--output', '--json', '--sha256'):
        return True
    return False


def main():
    argv = sys.argv[1:]
    if _is_cli_invocation(argv):
        # 命令行模式：不导入 PyQt5，启动快也便于脚本调用
        import cli
        cli.attach_console()          # 打包版没控制台窗口，挂到调用者的控制台上
        return cli.main([a for a in argv if a != '--cli'])

    from PyQt5.QtWidgets import QApplication
    from ui import MainWindow

    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())

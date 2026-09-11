import sys
from PyQt5.QtWidgets import QApplication
from ui import MainWindow

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    # MainWindow 还会再设置一次（含样式表）；这里先保证启动瞬间不是浅色闪屏
    app.setPalette(QApplication.instance().palette())
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

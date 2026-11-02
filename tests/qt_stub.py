"""A tiny fake PyQt6 — just enough behaviour to drive inputdeck/gui.py in a headless test.

It is NOT a Qt emulator: it exists so my own GUI logic (state handling, wiring, dialogs'
results) can be executed without Qt installed. Real-Qt API misuse can't be caught here.
"""
import sys
import types

SIGNALS = {"clicked", "toggled", "pressed", "released", "textChanged", "currentTextChanged",
           "currentIndexChanged", "itemDoubleClicked", "currentItemChanged", "valueChanged",
           "triggered", "activated", "accepted", "rejected", "finished", "timeout",
           "customContextMenuRequested"}


class _BoundSignal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *args):
        for fn in list(self.slots):
            fn(*args)


class pyqtSignal:
    def __init__(self, *types_):
        self.name = None

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, owner):
        if obj is None:
            return self
        return obj._signals.setdefault(self.name, _BoundSignal())


class _Enum:
    def __init__(self, name="E"):
        self._n = name

    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        return _Enum(f"{self._n}.{n}")

    def __call__(self, *a, **k):
        return _Enum(f"{self._n}()")

    def __or__(self, other):
        return self

    __ror__ = __or__

    def __eq__(self, other):
        return isinstance(other, _Enum) and self._n == other._n

    def __lt__(self, other):
        return False

    __gt__ = __le__ = __ge__ = __lt__

    def __hash__(self):
        return hash(self._n)

    def __repr__(self):
        return f"<{self._n}>"


class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Enum(f"{cls.__name__}.{name}")


class _Obj(metaclass=_Meta):
    def __new__(cls, *a, **k):
        obj = super().__new__(cls)
        obj._signals = {}
        return obj

    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        if name.startswith("__") or name == "_signals":
            raise AttributeError(name)
        if name in SIGNALS:
            return self._signals.setdefault(name, _BoundSignal())
        return lambda *a, **k: _Enum("ret")


# ------------------------------------------------------------------ behaving widgets
class QObject(_Obj):
    pass


class QWidget(_Obj):
    def setVisible(self, v):
        self.__dict__["_visible"] = v

    def setWindowTitle(self, t):
        self.__dict__["_title"] = t

    def windowTitle(self):
        return self.__dict__.get("_title", "")


class QLabel(QWidget):
    def __init__(self, text="", *a):
        self._t = text

    def setText(self, t):
        self._t = t

    def text(self):
        return self._t


class QLineEdit(QWidget):
    def __init__(self, *a):
        self._t = ""

    def setText(self, t):
        self._t = t

    def text(self):
        return self._t


class QPlainTextEdit(QWidget):
    def __init__(self, *a):
        self._t = ""

    def setPlainText(self, t):
        self._t = t
        self.textChanged.emit()

    def toPlainText(self):
        return self._t

    def appendPlainText(self, t):
        self._t += t + "\n"


class QComboBox(QWidget):
    def __init__(self, *a):
        self.items, self.idx, self.data = [], -1, []

    def addItem(self, t, data=None):
        self.items.append(t)
        self.data.append(data)
        if self.idx < 0:
            self.idx = 0

    def findData(self, d):
        return self.data.index(d) if d in self.data else -1

    def currentData(self):
        return self.data[self.idx] if 0 <= self.idx < len(self.data) else None

    def addItems(self, ts):
        for t in ts:
            self.addItem(t)

    def itemText(self, i):
        return self.items[i]

    def clear(self):
        self.items, self.idx, self.data = [], -1, []
        self.currentTextChanged.emit("")

    def count(self):
        return len(self.items)

    def currentIndex(self):
        return self.idx

    def setCurrentIndex(self, i):
        changed = i != self.idx
        self.idx = i
        if changed:
            self.currentIndexChanged.emit(i)
            self.currentTextChanged.emit(self.currentText())

    def currentText(self):
        return self.items[self.idx] if 0 <= self.idx < len(self.items) else ""

    def findText(self, t):
        return self.items.index(t) if t in self.items else -1

    def setCurrentText(self, t):
        if t in self.items:
            self.setCurrentIndex(self.items.index(t))


class QSpinBox(QWidget):
    def __init__(self, *a):
        self.v = 0

    def setValue(self, v):
        self.v = v

    def value(self):
        return self.v


QDoubleSpinBox = QSpinBox


class QCheckBox(QWidget):
    def __init__(self, *a):
        self.c = False

    def setChecked(self, c):
        if c != self.c:
            self.c = c
            self.toggled.emit(c)

    def isChecked(self):
        return self.c


class QListWidgetItem(_Obj):
    def __init__(self, text="", *a):
        self._t, self._d, self._cs, self._hidden = text, {}, None, False

    def text(self):
        return self._t

    def setText(self, t):
        self._t = t

    def setData(self, role, v):
        self._d[role] = v

    def data(self, role):
        return self._d.get(role)

    def setCheckState(self, s):
        self._cs = s

    def checkState(self):
        return self._cs

    def setHidden(self, h):
        self._hidden = h

    def isHidden(self):
        return self._hidden


class QListWidget(QWidget):
    def __init__(self, *a):
        self.rows, self.cur, self.sel = [], None, []

    def addItem(self, it):
        self.rows.append(QListWidgetItem(it) if isinstance(it, str) else it)

    def addItems(self, ts):
        for t in ts:
            self.addItem(t)

    def clear(self):
        self.rows, self.cur, self.sel = [], None, []

    def item(self, i):
        return self.rows[i]

    def count(self):
        return len(self.rows)

    def currentItem(self):
        return self.cur

    def setCurrentItem(self, it):
        prev, self.cur = self.cur, it
        self.currentItemChanged.emit(it, prev)

    def setCurrentRow(self, r):
        self.setCurrentItem(self.rows[r])

    def selectedItems(self):
        return list(self.sel)


class QTableWidgetItem(_Obj):
    def __init__(self, text=""):
        self._t = text

    def text(self):
        return self._t


class QTableWidget(QWidget):
    def __init__(self, rows=0, cols=0):
        self.cells, self.n, self.cur = {}, rows, -1

    def setRowCount(self, n):
        self.n, self.cells = n, {}

    def rowCount(self):
        return self.n

    def setItem(self, r, c, it):
        self.cells[(r, c)] = it

    def item(self, r, c):
        return self.cells.get((r, c))

    def currentRow(self):
        return self.cur

    def selectRow(self, r):
        self.cur = r


class QDialog(QWidget):
    on_exec = None            # test hook: fn(dialog) -> bool

    def exec(self):
        return bool(QDialog.on_exec(self)) if QDialog.on_exec else False

    def accept(self):
        self._accepted = True

    def reject(self):
        self._accepted = False


class QMainWindow(QWidget):
    def statusBar(self):
        return self

    def showMessage(self, text, ms=0):
        self.last_status = text


class QMessageBox(_Obj):
    answer = _Enum("QMessageBox.StandardButton.Yes")
    log = []

    @staticmethod
    def question(*a, **k):
        QMessageBox.log.append(("question", a[2]))
        return QMessageBox.answer

    @staticmethod
    def warning(parent, title, text):
        QMessageBox.log.append(("warning", text))

    information = critical = warning


class QInputDialog(_Obj):
    text_answer = ("Name", True)

    @staticmethod
    def getText(*a, **k):
        return QInputDialog.text_answer

    @staticmethod
    def getInt(parent, title, label, value, *a):
        return value, True


class QFileDialog(_Obj):
    @staticmethod
    def getOpenFileName(*a, **k):
        return "", ""


class QApplication(_Obj):
    @staticmethod
    def instance():
        return None

    @staticmethod
    def quit():
        pass


class QSystemTrayIcon(_Obj):
    @staticmethod
    def isSystemTrayAvailable():
        return False


class QTimer(_Obj):
    @staticmethod
    def singleShot(ms, fn):
        pass


def install():
    core = types.ModuleType("PyQt6.QtCore")
    core.QObject, core.pyqtSignal, core.QTimer = QObject, pyqtSignal, QTimer
    core.Qt = _Enum("Qt")
    core.QPointF = core.QRectF = core.QUrl = _Enum("geom")
    gui = types.ModuleType("PyQt6.QtGui")
    for n in ("QAction", "QBrush", "QColor", "QFontDatabase", "QIcon", "QKeySequence", "QShortcut",
              "QPixmap", "QPainter", "QPen", "QPainterPath", "QLinearGradient", "QPalette", "QPolygonF",
              "QDesktopServices"):
        setattr(gui, n, _Enum(n) if n != "QAction" and n != "QShortcut" else type(n, (_Obj,), {}))
    widgets = types.ModuleType("PyQt6.QtWidgets")
    behaving = dict(QCheckBox=QCheckBox, QComboBox=QComboBox, QDialog=QDialog, QDoubleSpinBox=QDoubleSpinBox,
                    QFileDialog=QFileDialog, QInputDialog=QInputDialog, QLabel=QLabel, QLineEdit=QLineEdit,
                    QListWidget=QListWidget, QListWidgetItem=QListWidgetItem, QMainWindow=QMainWindow,
                    QMessageBox=QMessageBox, QPlainTextEdit=QPlainTextEdit, QSpinBox=QSpinBox,
                    QSystemTrayIcon=QSystemTrayIcon, QTableWidget=QTableWidget,
                    QTableWidgetItem=QTableWidgetItem, QApplication=QApplication, QWidget=QWidget)
    for n in ("QAbstractItemView", "QDialogButtonBox", "QHBoxLayout", "QHeaderView", "QMenu", "QPushButton",
              "QSplitter", "QTabWidget", "QToolButton", "QVBoxLayout"):
        setattr(widgets, n, type(n, (QWidget,), {}))
    for n, v in behaving.items():
        setattr(widgets, n, v)
    pkg = types.ModuleType("PyQt6")
    pkg.QtCore, pkg.QtGui, pkg.QtWidgets = core, gui, widgets
    sys.modules.update({"PyQt6": pkg, "PyQt6.QtCore": core, "PyQt6.QtGui": gui, "PyQt6.QtWidgets": widgets})

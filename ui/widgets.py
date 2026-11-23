# -*- coding: utf-8 -*-
"""自定义控件：站点状态圆点、下载按钮、气泡提示。"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from . import theme as _theme


class StatusDot(QWidget):
    """站点状态圆点控件"""
    
    def __init__(self, site_name="", parent=None):
        super().__init__(parent)
        self.site_name = site_name
        self.status = "idle"
        if self.site_name:
            # 短名圆点：按文字宽度自适应，避免底部挤成一团
            fm = QFontMetrics(QFont("Microsoft YaHei", 9))
            text_w = fm.width(self.site_name)
            self.setFixedSize(max(72, text_w + 28), 28)
        else:
            self.setFixedSize(12, 12)
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        color_map = {
            "idle": QColor(_theme.IDLE),
            "searching": _theme.warning_qcolor(),
            "success": _theme.success_qcolor(),
            "failed": _theme.danger_qcolor(),
            "no_result": QColor(_theme.IDLE),
        }
        
        color = color_map.get(self.status, QColor(_theme.IDLE))
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        
        if self.site_name:
            dot_x = 5
            dot_y = 10
            dot_size = 10
            painter.drawEllipse(dot_x, dot_y, dot_size, dot_size)
            
            painter.setPen(QPen(QColor(60, 60, 60)))
            font = QFont("Microsoft YaHei", 9)
            painter.setFont(font)
            painter.drawText(dot_x + dot_size + 8, dot_y + dot_size - 2, self.site_name)
        else:
            painter.drawEllipse(0, 0, self.width(), self.height())

class DownloadButton(QPushButton):
    """自定义下载按钮"""
    
    def __init__(self, text="下载", parent=None):
        super().__init__(text, parent)
        self.btn_status = "idle"
        self.animation_offset = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_animation)
        self.setFixedSize(80, 32)
        self.setCursor(Qt.PointingHandCursor)
        
    def set_status(self, status):
        self.btn_status = status
        if status == "downloading":
            self.timer.start(50)
            self.setText("下载中")
        elif status == "success":
            self.timer.stop()
            self.setText("已下载")
            self.setEnabled(False)
        elif status == "local_success":
            # 本地文件已打开：显示"已打开"以区分网络下载
            self.timer.stop()
            self.setText("已打开")
            self.setEnabled(False)
        elif status == "failed":
            self.timer.stop()
            self.setText("失败")
        elif status == "disabled":
            self.timer.stop()
            self.setText("不可下载")
            self.setEnabled(False)
            self.setCursor(Qt.ForbiddenCursor)
        elif status == "disabled_batch":
            self.timer.stop()
            self.setText("不启用")
            self.setEnabled(False)
            self.setCursor(Qt.ForbiddenCursor)
        elif status == "not_provided":
            self.timer.stop()
            self.setText("不提供")
            self.setEnabled(False)
            self.setCursor(Qt.ForbiddenCursor)
        elif status == "download_off":
            self.timer.stop()
            self.setText("未开启")
            self.setEnabled(False)
            self.setCursor(Qt.ForbiddenCursor)
        elif status == "detecting":
            # 按需探测下载源中：走流光动画，暂不可点
            self.timer.start(50)
            self.setText("检测中")
            self.setEnabled(False)
            self.setCursor(Qt.WaitCursor)
        elif status == "not_found":
            self.timer.stop()
            self.setText("未找到资源")
            self.setEnabled(False)
            self.setCursor(Qt.ForbiddenCursor)
        elif status == "local_idle":
            # 已在文档管理目录命中，待用户点击：按钮文字提示"本地打开"
            self.timer.stop()
            self.setText("本地打开")
            self.setEnabled(True)
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.timer.stop()
            self.setText("下载")
            self.setEnabled(True)
            self.setCursor(Qt.PointingHandCursor)

        self.update()
    
    def update_animation(self):
        self.animation_offset = (self.animation_offset + 5) % 200
        self.update()
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        rect = self.rect()
        is_disabled = not self.isEnabled()
        
        if self.btn_status == "downloading":
            gradient = QLinearGradient(self.animation_offset - 200, 0,
                                       self.animation_offset + 200, 0)
            gradient.setColorAt(0, _theme.success_qcolor())
            gradient.setColorAt(0.5, QColor(90, 170, 120))
            gradient.setColorAt(1, _theme.success_qcolor())
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 6, 6)
            text_color = Qt.white
        elif self.btn_status == "detecting":
            gradient = QLinearGradient(self.animation_offset - 200, 0,
                                       self.animation_offset + 200, 0)
            gradient.setColorAt(0, _theme.primary_qcolor())
            gradient.setColorAt(0.5, QColor(120, 170, 220))
            gradient.setColorAt(1, _theme.primary_qcolor())
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 6, 6)
            text_color = Qt.white
        elif self.btn_status == "success":
            painter.setBrush(QBrush(_theme.success_qcolor()))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 6, 6)
            text_color = Qt.white
        elif self.btn_status == "local_success":
            painter.setBrush(QBrush(_theme.local_qcolor()))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 6, 6)
            text_color = Qt.white
        elif self.btn_status == "failed":
            painter.setBrush(QBrush(_theme.danger_qcolor()))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 6, 6)
            text_color = Qt.white
        elif self.btn_status == "disabled_batch":
            # 批量/汉字名称查询：红色提示「不启用」
            danger = _theme.danger_qcolor()
            soft = QColor(danger.red(), danger.green(), danger.blue(), 28)
            painter.setBrush(QBrush(soft))
            painter.setPen(QPen(danger, 1))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)
            text_color = danger
        elif is_disabled:
            # 禁用状态：不画底色（行 hover 可透出），只画浅灰文字+虚线边框
            painter.setPen(QPen(QColor(189, 189, 189), 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)
            text_color = QColor(189, 189, 189)
        elif self.btn_status == "local_idle":
            painter.setBrush(QBrush(_theme.local_qcolor()))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 6, 6)
            text_color = Qt.white
        else:
            painter.setBrush(QBrush(_theme.success_qcolor()))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 6, 6)
            text_color = Qt.white
        
        # 绘制文字
        painter.setPen(QPen(text_color))
        font = QFont("Microsoft YaHei", 9)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, self.text())

class BubbleNotification(QWidget):
    """液态气泡提示"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.hide)
        self.message = ""
        self.bg_color = _theme.qcolor(_theme.ACCENT, 230)
        
    def show_message(self, message, msg_type="info", duration=3000):
        """显示消息"""
        self.message = message
        
        # 设置颜色
        if msg_type == "error":
            self.bg_color = QColor(*_theme.DANGER_RGB, 230)
        elif msg_type == "success":
            self.bg_color = QColor(*_theme.SUCCESS_RGB, 230)
        elif msg_type == "warning":
            self.bg_color = QColor(*_theme.WARNING_RGB, 230)
        else:
            self.bg_color = _theme.qcolor(_theme.ACCENT, 230)
        
        # 计算大小
        font = QFont("Microsoft YaHei", 10)
        fm = QFontMetrics(font)
        text_width = fm.horizontalAdvance(message) + 40
        self.setFixedSize(max(200, text_width), 50)
        
        # 定位到父窗口居中
        if self.parent():
            parent_rect = self.parent().rect()
            x = (parent_rect.width() - self.width()) // 2
            y = (parent_rect.height() - self.height()) // 2
            self.move(self.parent().mapToGlobal(QPoint(x, y)))
        
        self.show()
        self.raise_()
        # 停止旧定时器再启动新的，防止多次调用时定时器泄漏
        self.timer.stop()
        self.timer.start(duration)
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # 绘制气泡背景
        painter.setBrush(QBrush(self.bg_color))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 20, 20)
        
        # 绘制文字
        painter.setPen(QPen(Qt.white))
        font = QFont("Microsoft YaHei", 10)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, self.message)


# ============================================================================
# 液态玻璃按钮（Windows 原生 GDI 自绘）
# ----------------------------------------------------------------------------
# 纯 QPainter（渐变 + 高光 + 描边）模拟液态玻璃质感，不调用任何 Aero 透明/
# 不依赖 Aero 毛玻璃接口，走 raster/GDI 稳定且无花屏。
# 三态：普通（低通透渐变、柔和高光）/ Hover（高光提亮、明度 +10%、投影加深）
#      / 按下（高光下移至下半区、整体加深、顶部内凹阴影）。
# 自带底部投影（预留 shadow_gap 像素），不依赖 QGraphicsDropShadowEffect，
# 使 grab() 预览与真机渲染一致。
# ============================================================================
def paint_glass_button(painter, rect, base, state, text, radius=8):
    """在 rect 内绘制一枚液态玻璃按钮。

    painter : 已 begin 的 QPainter
    rect    : QRectF 绘制区域
    base    : QColor 按钮基色
    state   : 'normal' / 'hover' / 'pressed' / 'disabled'
    text    : 按钮文字
    radius  : 圆角半径（矢量，锐利无模糊）
    """
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)

    shadow_gap = 3
    # 按钮主体区域（底部预留投影空间，四周留 1px 抗锯齿余量）
    body = QRectF(rect.left() + 1, rect.top() + 1,
                  rect.width() - 2, rect.height() - shadow_gap - 1)
    if state == 'pressed':
        # 按下：整体下沉 1px，模拟被按入
        body = QRectF(body.left(), body.top() + 1, body.width(), body.height() - 1)

    # --- 1. 外底投影（Hover 加深、按下收紧）---
    if state == 'hover':
        sh_alpha, expand = 70, 2
    elif state == 'pressed':
        sh_alpha, expand = 45, 0
    else:
        sh_alpha, expand = 45, 1
    shadow_rect = QRectF(body.left() - expand, body.top() + 2,
                         body.width() + expand * 2, body.height())
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(20, 45, 75, sh_alpha))
    painter.drawRoundedRect(shadow_rect, radius, radius)

    # --- 2. 主体垂直渐变 ---
    if state == 'disabled':
        d = QColor(150, 158, 166)
        top, mid, bot = d.lighter(112), d.lighter(103), d.darker(108)
    elif state == 'hover':
        top, mid, bot = base.lighter(135), base.lighter(114), base.darker(104)  # 明度 +10%
    elif state == 'pressed':
        top, mid, bot = base.darker(112), base.darker(106), base.lighter(106)   # 整体加深
    else:  # normal：低通透渐变
        top, mid, bot = base.lighter(122), base.lighter(104), base.darker(112)

    grad = QLinearGradient(body.topLeft(), body.bottomLeft())
    grad.setColorAt(0.0, top)
    grad.setColorAt(0.5, mid)
    grad.setColorAt(1.0, bot)

    path = QPainterPath()
    path.addRoundedRect(body, radius, radius)
    painter.setPen(Qt.NoPen)
    painter.setBrush(grad)
    painter.drawPath(path)

    # --- 3. 高光带 ---
    painter.setClipPath(path)
    if state == 'pressed':
        # 高光下移至下半区
        hi = QRectF(body.left(), body.center().y(), body.width(), body.height() / 2)
        hg = QLinearGradient(hi.topLeft(), hi.bottomLeft())
        hg.setColorAt(0.0, QColor(255, 255, 255, 0))
        hg.setColorAt(1.0, QColor(255, 255, 255, 90))
        painter.setPen(Qt.NoPen)
        painter.setBrush(hg)
        painter.drawRect(hi)
        # 顶部内凹阴影
        ig = QRectF(body.left(), body.top(), body.width(), body.height() * 0.45)
        igg = QLinearGradient(ig.topLeft(), ig.bottomLeft())
        igg.setColorAt(0.0, QColor(0, 0, 0, 60))
        igg.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setBrush(igg)
        painter.drawRect(ig)
    else:
        # 上半部柔和高光（Hover 提亮）
        hi = QRectF(body.left(), body.top(), body.width(), body.height() * 0.5)
        if state == 'hover':
            a = 150
        elif state == 'disabled':
            a = 80
        else:
            a = 110
        hg = QLinearGradient(hi.topLeft(), hi.bottomLeft())
        hg.setColorAt(0.0, QColor(255, 255, 255, a))
        hg.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(hg)
        painter.drawRect(hi)
    painter.setClipping(False)

    # --- 4. 描边 + 顶部内高光线（锐利边缘）---
    edge = base.darker(150) if state != 'disabled' else QColor(120, 128, 136)
    pen = QPen(edge)
    pen.setWidthF(1.0)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(path)
    painter.setPen(QPen(QColor(255, 255, 255, 40 if state == 'pressed' else 120)))
    painter.drawLine(QPointF(body.left() + radius, body.top() + 1.2),
                     QPointF(body.right() - radius, body.top() + 1.2))

    # --- 5. 文字 ---
    painter.setPen(QColor(255, 255, 255) if state != 'disabled' else QColor(236, 239, 242))
    f = QFont("Microsoft YaHei", 10)
    f.setBold(True)
    painter.setFont(f)
    tr = body if state != 'pressed' else QRectF(body.left(), body.top() + 1,
                                                body.width(), body.height())
    painter.drawText(tr, Qt.AlignCenter, text)

    painter.restore()


class GlassButton(QPushButton):
    """液态玻璃按钮（GDI 自绘，三态）。

    用法：GlassButton("立即查询") 或 GlassButton("停止", danger_qcolor())
    仅通过 base_color 区分语义色（查询蓝/确认深绿/警告砖红等），
    三态外观由 paint_glass_button 统一渲染，风格稳重简约。
    """

    def __init__(self, text="", base_color=None, parent=None):
        super().__init__(text, parent)
        self._base = base_color if base_color is not None else _theme.primary_qcolor()
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(36)
        self.setFlat(True)
        self.setAttribute(Qt.WA_Hover, True)

    def setBaseColor(self, color):
        self._base = color
        self.update()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def _state(self):
        if not self.isEnabled():
            return 'disabled'
        if self.isDown():
            return 'pressed'
        if self._hover:
            return 'hover'
        return 'normal'

    def paintEvent(self, event):
        painter = QPainter(self)
        paint_glass_button(painter, QRectF(self.rect()), self._base,
                           self._state(), self.text())


def ask_yes_no(parent, title, text, default_yes=False):
    """中文「是 / 否」确认框。返回 True 表示点了「是」。"""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Question)
    box.setWindowTitle(title)
    box.setText(text)
    yes_btn = box.addButton("是", QMessageBox.YesRole)
    no_btn = box.addButton("否", QMessageBox.NoRole)
    box.setDefaultButton(yes_btn if default_yes else no_btn)
    box.exec_()
    return box.clickedButton() == yes_btn


class StdRowPeekTip(QFrame):
    """鼠标悬停时半透明浮层：显示标准编号与名称。"""

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        # 不用 WA_TranslucentBackground：Win 上样式表底色常画不出来，白字会看不见
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)
        self._no = QLabel()
        self._name = QLabel()
        for lab in (self._no, self._name):
            lab.setWordWrap(True)
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._no)
        lay.addWidget(self._name)
        self.setStyleSheet("""
            QFrame {
                background-color: #1e3248;
                border-radius: 10px;
                border: 1px solid #6a849c;
            }
            QLabel {
                color: #ffffff;
                font-size: 13px;
                font-family: "Microsoft YaHei";
                background: transparent;
            }
        """)
        self.hide()

    def show_info(self, std_no, std_name, global_pos):
        no_text = f"编号：{(std_no or '').strip() or '—'}"
        name_text = f"名称：{(std_name or '').strip() or '—'}"
        self._no.setText(no_text)
        self._name.setText(name_text)

        screen = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        # 按内容测宽：短名收窄、长名加宽，且不超过屏幕可用宽度
        fm = QFontMetrics(self._name.font())
        pad = 28  # 左右边距合计

        def _text_w(s):
            if hasattr(fm, 'horizontalAdvance'):
                return fm.horizontalAdvance(s)
            return fm.width(s)

        need = max(_text_w(no_text), _text_w(name_text)) + pad
        min_w, max_w = 220, max(360, int(geo.width() * 0.72))
        tip_w = max(min_w, min(need, max_w))
        for lab in (self._no, self._name):
            lab.setMinimumWidth(tip_w - pad)
            lab.setMaximumWidth(tip_w - pad)
        self.setFixedWidth(tip_w)
        self.adjustSize()

        x = global_pos.x() + 14
        y = global_pos.y() + 18
        if x + self.width() > geo.right():
            x = global_pos.x() - self.width() - 8
        if y + self.height() > geo.bottom():
            y = global_pos.y() - self.height() - 8
        self.move(max(geo.left(), x), max(geo.top(), y))
        self.show()
        self.raise_()


class _StdRowHelperFilter(QObject):
    """视口离开时隐藏浮层。"""

    def __init__(self, tip, parent=None):
        super().__init__(parent)
        self._tip = tip

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Leave, QEvent.HoverLeave):
            self._tip.hide()
        return False


def install_std_row_helpers(table, get_info):
    """为表格安装：悬停半透明提示 + 右键复制编号/名称。

    get_info(row) -> (std_no, std_name) 或 None
    """
    if table is None or not callable(get_info):
        return
    tip = StdRowPeekTip(table.window())
    table._std_peek_tip = tip
    table._std_peek_get = get_info
    table.setMouseTracking(True)
    table.viewport().setMouseTracking(True)

    # 避免重复安装 eventFilter
    old_filt = getattr(table, '_std_peek_filter', None)
    if old_filt is not None:
        table.viewport().removeEventFilter(old_filt)
    filt = _StdRowHelperFilter(tip, table)
    table.viewport().installEventFilter(filt)
    table._std_peek_filter = filt

    if getattr(table, '_std_helpers_ready', False):
        return

    def _on_entered_proxy(row, _col):
        fn = getattr(table, '_std_peek_get', None)
        tip_w = getattr(table, '_std_peek_tip', None)
        if not callable(fn) or tip_w is None:
            return
        info = fn(row)
        if not info:
            tip_w.hide()
            return
        no, name = info[0], info[1]
        if not (no or name):
            tip_w.hide()
            return
        tip_w.show_info(no, name, QCursor.pos())

    def _on_menu(pos):
        idx = table.indexAt(pos)
        if not idx.isValid():
            return
        fn = getattr(table, '_std_peek_get', None)
        if not callable(fn):
            return
        info = fn(idx.row())
        if not info:
            return
        no = (info[0] or '').strip()
        name = (info[1] or '').strip()
        if not no and not name:
            return
        menu = QMenu(table)
        act_no = menu.addAction("复制标准编号") if no else None
        act_name = menu.addAction("复制标准名称") if name else None
        act_both = menu.addAction("复制编号和名称") if (no and name) else None
        chosen = menu.exec_(table.viewport().mapToGlobal(pos))
        clip = QApplication.clipboard()
        if act_no is not None and chosen == act_no:
            clip.setText(no)
        elif act_name is not None and chosen == act_name:
            clip.setText(name)
        elif act_both is not None and chosen == act_both:
            clip.setText(f"{no}\n{name}")

    table.cellEntered.connect(_on_entered_proxy)
    table.setContextMenuPolicy(Qt.CustomContextMenu)
    table.customContextMenuRequested.connect(_on_menu)
    table._std_helpers_ready = True

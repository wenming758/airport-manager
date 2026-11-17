# -*- coding: utf-8 -*-
"""功能叠加层（overlay）。

原程序只有编译好的 exe，没有源码。为 **100% 保留原有界面与风格**，
这里不重写界面代码，只在原程序之上叠加功能：

1) 分组管理 —— 新建 / 重命名 / 删除分组；
2) 卡片「标记失效 / 恢复」—— 点击后卡片文字加删除线并归入「失效链接」分组，
   标题行右侧显示醒目的红色「已失效」徽标：
     🚫 = 人为标记失效        🤖 = 系统检测判定失效
3) 排列方式 —— 名称 / 已失效优先 / 正常优先 / 添加时间 / 流量大小；
4) 代理检测 —— 在左侧「检测设置」里填写代理（HTTP / SOCKS5）。
   每条链接默认直连，直连失败（超时/连接被重置/SSL 等）时自动改用代理重试；
   记住每条链接是否必须先走代理，下次检测直接优先用代理，避免反复超时。
5) 检测成功后自动解除「已失效」标记（恢复原分组）。
6) 检测更新 —— 比对 Gitee 项目 Releases 上的最新版本号，默认启动时自动检测。

此外还修掉了原程序自身导致「检测不成功」的两个缺陷（不改界面）：
  a) 订阅里 expire 时间戳过大（如 3126 年）时 datetime.fromtimestamp 会抛
     OSError，整条订阅被判为「检测异常」；
  b) 部分机场直接返回完整 Clash 配置（YAML），原版解析不出节点，报「未解析到节点」。

除以上内容外，原程序的界面、配色、布局、交互一律未做改动。
"""
import datetime as dt
import functools
import json
import re
import threading
import webbrowser

from PySide6.QtCore import QCollator, QLocale, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

INVALID_GROUP = "失效链接"      # 系统保留分组：存放被标记为“失效”的订阅
DEFAULT_GROUP = "默认分组"      # 与原生逻辑一致的回退分组名

APP_VERSION = "1.1.0"
UPDATE_API = ("https://gitee.com/api/v5/repos/wenming760/"
              "airport-manager/releases/latest")
UPDATE_PAGE = "https://gitee.com/wenming760/airport-manager/releases"

SORT_MODES = ["默认排序", "名称", "已失效优先", "正常优先", "添加时间", "流量大小"]
SORT_HINTS = {
    "默认排序": "最新的订阅排在最上面",
    "名称": "按名称排序（中文按拼音），同名时最新添加的在前",
    "已失效优先": "失效的排在最前面，同组内后添加的在前",
    "正常优先": "正常的排在最前面，其余按添加时间倒序（后添加的在前）",
    "添加时间": "最近添加的排在最前面",
    "流量大小": "套餐流量大的排在最前面，流量相同则后添加的在前",
}

# 运行期上下文（由 install() 注入）
_CTX = {}

# 每条检测线程当前该不该走代理（由 requests 垫片读取，线程安全）
_TLS = threading.local()
_REAL_REQUESTS = None
_ORIG_CHECK_ONE = None


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------
def _msg(parent, text, title="ℹ️ 提示"):
    QMessageBox.information(parent, title, text)


def _group_of(sub):
    return sub.get("group") or DEFAULT_GROUP


def _item_group_name(text):
    """从分组列表项文本（如 '📁 默认分组  (3)'）还原出分组名。"""
    return text.replace("📁", "").replace("📂", "").strip().split("(")[0].strip()


def _sub_status(sub):
    return str(sub.get("status") or "").lower()


def _is_system_invalid(sub):
    """系统检测判定失效（不是用户手动标记的）。"""
    if sub.get("invalid"):
        return False
    return _sub_status(sub) in ("dead", "invalid")


def _is_invalid_any(sub):
    return bool(sub.get("invalid")) or _is_system_invalid(sub)


# --------------------------------------------------------------------------
# 排列方式
# --------------------------------------------------------------------------
_COLLATOR = None


def _collator():
    global _COLLATOR
    if _COLLATOR is None:
        try:
            c = QCollator(QLocale(QLocale.Chinese, QLocale.China))
            c.setCaseSensitivity(Qt.CaseInsensitive)
            c.setNumericMode(True)
            _COLLATOR = c
        except Exception:
            _COLLATOR = False
    return _COLLATOR or None


def _name_of(sub):
    return str(sub.get("name") or sub.get("url") or "")


def _name_sorted(subs):
    """按名称排序；同名时保持传入顺序（调用方先按添加时间倒序）。"""
    col = _collator()
    if col is None:
        return sorted(subs, key=lambda s: _name_of(s).casefold())
    return sorted(subs, key=functools.cmp_to_key(
        lambda a, b: col.compare(_name_of(a), _name_of(b))))


def _time_sorted(subs):
    """按添加时间倒序：后添加的在前。"""
    return sorted(subs, key=_added_ts, reverse=True)


def _added_ts(sub):
    """添加时间：原程序的 id 形如 '1791382063-2'，前缀是写入时的 Unix 时间戳。"""
    head = str(sub.get("id") or "").split("-")[0]
    if head.isdigit():
        v = int(head)
        if 1000000000 <= v <= 3000000000:
            return v
    for key in ("created_at", "add_time", "checked_at"):
        value = sub.get(key)
        if not isinstance(value, str) or not value:
            continue
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return dt.datetime.strptime(value[:19], fmt).timestamp()
            except Exception:
                continue
    return 0.0


def _traffic_gb(sub):
    for key in ("total_traffic_gb", "remain_gb", "used_traffic_gb"):
        value = sub.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    text = str(sub.get("traffic") or "")
    m = re.search(r"(?:总量|total)\s*([\d.]+)\s*(TB|GB|G|T)", text, re.I)
    if not m:
        m = re.search(r"([\d.]+)\s*(TB|GB|G|T)\b", text, re.I)
    if m:
        try:
            return float(m.group(1)) * (1024 if m.group(2).lower() in ("tb", "t") else 1)
        except Exception:
            pass
    return -1.0


def _json_safe(obj):
    """将 datetime 等不可 JSON 化的值换成字符串。

    原程序只转换了订阅顶层的 checked_at / expire_at，但检测结果里的
    sub_userinfo.expire_at 仍是 datetime；保存时 json.dump 抛异常，
    整个 on_done 中断，表现为「点全部检测没反应」。
    """
    if isinstance(obj, dt.datetime):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(obj, dt.date):
        return obj.strftime("%Y-%m-%d")
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            obj[k] = _json_safe(v)
        return obj
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            obj[i] = _json_safe(v)
        return obj
    return obj


def _sort_subs(win, subs):
    mode = getattr(win, "cb_sort", None).currentText() if hasattr(win, "cb_sort") else "默认排序"
    subs = list(subs)
    if mode == "名称":
        # 同名时后添加的在前
        return _name_sorted(_time_sorted(subs))
    if mode == "已失效优先":
        out = []
        for want_invalid in (True, False):
            group = [s for s in subs if _is_invalid_any(s) == want_invalid]
            out.extend(_time_sorted(group))
        return out
    if mode == "正常优先":
        alive = [s for s in subs if _sub_status(s) == "alive"]
        rest = [s for s in subs if _sub_status(s) != "alive"]
        return _time_sorted(alive) + _time_sorted(rest)
    if mode == "添加时间":
        return _time_sorted(subs)
    if mode == "流量大小":
        # 流量相同则后添加的在前
        return sorted(_time_sorted(subs), key=_traffic_gb, reverse=True)
    # 默认排序：按添加时间倒序，最新添加的在最上面
    return _time_sorted(subs)


# --------------------------------------------------------------------------
# 代理检测
# --------------------------------------------------------------------------
NET_FAIL_MARKERS = (
    "timeout", "timed out", "connection", "connectionerror", "aborted",
    "reset", "ssl", "proxyerror", "proxy", "getaddrinfo", "max retries",
    "unreachable", "refused", "tunnel connection failed",
    "errno 10054", "errno 10060", "errno 10061", "errno 11001",
    "连接失败", "无法连接", "网络错误", "网络不可达",
)


def _is_net_fail(res):
    """失败是否属于「网络层不通」（值得换代理重试）。"""
    if not isinstance(res, dict):
        return True
    if _sub_status(res) == "alive":
        return False
    text = str(res.get("status_text") or "")
    if "连接失败" in text:
        return True
    blob = ("%s %s" % (res.get("error") or "", text)).lower()
    return any(m in blob for m in NET_FAIL_MARKERS)


class _ProxyRequests:
    """requests 垫片：每条检测线程各自决定直连还是走代理。

    原版 check_one 调用 requests.get(..., proxies=PROXIES)，PROXIES 是模块级
    全局字典，多线程下会互相覆盖。这里丢掉原参数，改用线程局部状态，
    并关闭 trust_env，保证「直连」真的是直连（不受系统代理环境变量影响）。
    """

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name):
        real = object.__getattribute__(self, "_real")
        if name == "get":
            return self._get
        return getattr(real, name)

    def _get(self, url, **kwargs):
        kwargs.pop("proxies", None)
        proxies = getattr(_TLS, "proxies", None)
        session = _REAL_REQUESTS.Session()
        session.trust_env = False
        try:
            if proxies:
                session.proxies.update(proxies)
            return session.get(url, **kwargs)
        finally:
            session.close()


def _proxy_check_one(item, timeout=15, **kwargs):
    """直连优先；网络层失败时自动换代理重试，并记住该链接是否需要代理。"""
    proxy_map = _CTX.get("proxy_map") or {}
    if not proxy_map or not isinstance(item, dict):
        _TLS.proxies = None
        return _ORIG_CHECK_ONE(item, timeout, **kwargs)

    prefer_proxy = bool(item.get("needs_proxy"))
    try:
        _TLS.proxies = proxy_map if prefer_proxy else None
        res = _ORIG_CHECK_ONE(item, timeout, **kwargs)
        if not _is_net_fail(res):
            if isinstance(res, dict):
                res["needs_proxy"] = prefer_proxy
                res["used_proxy"] = prefer_proxy
            return res

        _TLS.proxies = None if prefer_proxy else proxy_map
        res2 = _ORIG_CHECK_ONE(item, timeout, **kwargs)
        if not _is_net_fail(res2):
            if isinstance(res2, dict):
                res2["needs_proxy"] = not prefer_proxy
                res2["used_proxy"] = not prefer_proxy
            return res2
        return res2
    finally:
        _TLS.proxies = None


def _proxy_ui_text(win):
    cfg = _CTX.get("proxy_cfg") or {}
    proxy_map = _CTX.get("proxy_map") or {}
    mode = cfg.get("mode") or "off"
    if mode == "custom" and proxy_map:
        return "代理：%s（直连失败自动改用代理）" % (proxy_map.get("https") or "")
    if mode == "system" and proxy_map:
        return "代理：跟随系统 %s" % (proxy_map.get("https") or "")
    return "代理：未启用（全部直连检测）"


def _sync_proxy_ui(win):
    lbl = getattr(win, "lbl_proxy", None)
    if lbl is not None:
        lbl.setText(_proxy_ui_text(win))
        lbl.setToolTip("填写代理后，每条链接先直连；"
                       "直连失败（超时/连接被重置等）会自动改用代理重试。")
    btn = getattr(win, "btn_proxy_full", None)
    if btn is not None:
        btn.setText("🌐 填写代理…")


# --------------------------------------------------------------------------
# 分组列表刷新（在原逻辑基础上，额外显示“空分组”和保留分组）
# --------------------------------------------------------------------------
def refresh_groups(self):
    """与原版输出格式完全一致：'📁 名称  (数量)' + 末尾 '📂 全部  (总数)'。"""
    gl = self.group_list
    gl.blockSignals(True)
    gl.clear()

    counts = _group_counts(self)
    for g in list(self.data.get("groups") or []):
        if isinstance(g, str) and g:
            counts.setdefault(g, 0)
    counts.setdefault(INVALID_GROUP, 0)

    for name, cnt in sorted(counts.items(), key=lambda x: x[0]):
        gl.addItem(QListWidgetItem("📁 %s  (%d)" % (name, cnt)))
    gl.addItem(QListWidgetItem("📂 全部  (%d)" % len(self.data.get("subs", []))))
    gl.setCurrentRow(gl.count() - 1)
    gl.blockSignals(False)


def _refresh_groups_keep(self):
    """刷新分组列表但保留用户当前选中的分组（避免跳回“全部”）。"""
    cur = self.current_group()
    refresh_groups(self)
    gl = self.group_list
    gl.blockSignals(True)
    row = gl.count() - 1
    for i in range(gl.count()):
        if _item_group_name(gl.item(i).text()) == cur:
            row = i
            break
    gl.setCurrentRow(row)
    gl.blockSignals(False)


def _saved_groups(win):
    """仅用户手动创建（可能暂无订阅）的分组名列表。"""
    return [g for g in (win.data.get("groups") or []) if isinstance(g, str) and g]


def _save_groups(self, groups):
    self.data["groups"] = [n for n in groups if n]
    _CTX["store"].save(self.data)
    _refresh_groups_keep(self)
    self.refresh_table()


def _group_counts(win):
    counts = {}
    for s in win.data.get("subs", []):
        g = _group_of(s)
        counts[g] = counts.get(g, 0) + 1
    return counts


def _all_group_names(win):
    """全部已存在的分组名：订阅里出现过的 + 手动建过（可能为空）的 + 保留分组。"""
    names = []
    for s in win.data.get("subs", []):
        g = _group_of(s)
        if g not in names:
            names.append(g)
    for g in list(win.data.get("groups") or []):
        if isinstance(g, str) and g and g not in names:
            names.append(g)
    if INVALID_GROUP not in names:
        names.append(INVALID_GROUP)
    return names


# --------------------------------------------------------------------------
# “失效”功能
# --------------------------------------------------------------------------
def _set_invalid(self, url, invalid):
    sub = self.find_sub(url)
    if sub is None:
        return
    if invalid:
        if _group_of(sub) != INVALID_GROUP:
            sub["_prev_group"] = _group_of(sub)
        sub["invalid"] = True
        sub["group"] = INVALID_GROUP
    else:
        sub["invalid"] = False
        prev = sub.pop("_prev_group", None)
        sub["group"] = prev or DEFAULT_GROUP
    try:
        _CTX["store"].save(self.data)
    except Exception as exc:
        QMessageBox.warning(self, "⚠️ 保存失败",
                            "失效标记没能写入数据文件：\n%s\n\n"
                            "请检查程序所在目录（或 data 目录）是否可写。" % exc)
        return
    _refresh_groups_keep(self)
    self.refresh_table()
    _sync_invalid_btn(self)
    name = sub.get("name", "")
    if invalid:
        self.status.setText("🚫 已标记失效「%s」— 已归入「%s」分组" % (name, INVALID_GROUP))
    else:
        self.status.setText("♻️ 已恢复「%s」" % name)


def _toggle_selected(self):
    url = getattr(self, "selected_url", None)
    sub = self.find_sub(url) if url else None
    if sub is None:
        self.status.setText("⚠️ 请先点击一张卡片，再点「失效 / 恢复」")
        return
    _set_invalid(self, sub.get("url"), not bool(sub.get("invalid")))


def _sync_invalid_btn(self):
    btn = getattr(self, "btn_invalid", None)
    if btn is None:
        return
    url = getattr(self, "selected_url", None)
    sub = self.find_sub(url) if url else None
    if sub is not None and sub.get("invalid"):
        btn.setText("♻️ 恢复")
        btn.setToolTip("把当前选中的链接恢复为正常")
    else:
        btn.setText("🚫 失效")
        btn.setToolTip("把当前选中的链接标记为失效")


def _auto_clear_invalid(self):
    """检测结果正常的链接，自动去掉「已失效」标记并回到原分组。"""
    results = getattr(self, "_last_results", None) or []
    alive = {r.get("url") for r in results
             if isinstance(r, dict) and str(r.get("status")) == "alive"}
    if not alive:
        return []
    cleared = []
    for s in self.data.get("subs", []):
        if s.get("invalid") and s.get("url") in alive:
            s["invalid"] = False
            s["group"] = s.pop("_prev_group", None) or DEFAULT_GROUP
            cleared.append(s.get("name") or s.get("url") or "")
    if cleared:
        try:
            _CTX["store"].save(self.data)
        except Exception:
            pass
    return cleared


def _set_renewable(self, url, renewable):
    sub = self.find_sub(url)
    if sub is None:
        return
    sub["renewable"] = bool(renewable)
    try:
        _CTX["store"].save(self.data)
    except Exception:
        pass
    self.status.setText(
        ("🔁 已标记「%s」为可复购" if renewable else "🔁 已标记「%s」为不可复购")
        % (sub.get("name") or url))


def _wire_cards(self):
    """把每张卡片上的“标记失效 / 恢复”信号接到主窗口。"""
    for card in list(getattr(self.card_list, "_cards", []) or []):
        if not hasattr(card, "invalid_toggled") or getattr(card, "_inv_wired", False):
            continue
        card._inv_wired = True
        card.invalid_toggled.connect(
            lambda url, inv, w=self: _set_invalid(w, url, inv))
        if hasattr(card, "renewable_toggled"):
            card.renewable_toggled.connect(
                lambda url, val, w=self: _set_renewable(w, url, val))


# --------------------------------------------------------------------------
# 分组管理对话框
# --------------------------------------------------------------------------
class _InputDialog(QDialog):
    """与程序内其它对话框风格一致的文本输入框。"""

    def __init__(self, parent, title, label, default=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedWidth(420)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addWidget(QLabel(label))
        self.edit = QLineEdit(default)
        self.edit.selectAll()
        lay.addWidget(self.edit)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("💾 保存")
        box.button(QDialogButtonBox.Ok).setObjectName("Primary")
        box.button(QDialogButtonBox.Cancel).setText("❌ 取消")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.edit.setFocus()

    def value(self):
        return self.edit.text().strip()


class GroupManagerDialog(QDialog):
    """分组管理：新建 / 重命名 / 删除。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.win = parent
        self.setWindowTitle("🗂️ 分组管理")
        self.setFixedWidth(460)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addWidget(QLabel("选择分组后可重命名或删除："))

        self.list = QListWidget()
        self.list.setMinimumHeight(240)
        lay.addWidget(self.list)

        row = QHBoxLayout()
        self.btn_new = QPushButton("➕ 新建分组")
        self.btn_ren = QPushButton("✏️ 重命名")
        self.btn_del = QPushButton("🗑️ 删除")
        self.btn_del.setObjectName("Danger")
        row.addWidget(self.btn_new)
        row.addWidget(self.btn_ren)
        row.addWidget(self.btn_del)
        row.addStretch(1)
        lay.addLayout(row)

        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.button(QDialogButtonBox.Close).setText("关闭")
        box.rejected.connect(self.reject)
        lay.addWidget(box)

        self.btn_new.clicked.connect(lambda: (_new_group(self.win), self.reload()))
        self.btn_ren.clicked.connect(lambda: (_rename_group(self.win), self.reload()))
        self.btn_del.clicked.connect(lambda: (_delete_group(self.win), self.reload()))
        self.reload()

    def reload(self):
        self.list.clear()
        counts = _group_counts(self.win)
        for name in _all_group_names(self.win):
            label = "📁 %s  (%d)" % (name, counts.get(name, 0))
            if name == INVALID_GROUP:
                label += "   · 系统保留"
            self.list.addItem(QListWidgetItem(label))
        if self.list.count() and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)

    def selected_group(self):
        it = self.list.currentItem()
        if it is None:
            return None
        return _item_group_name(it.text())


# --------------------------------------------------------------------------
# 分组操作
# --------------------------------------------------------------------------
def _new_group(self):
    dlg = _InputDialog(self, "➕ 新建分组", "分组名称：", "")
    if dlg.exec() != QDialog.Accepted:
        return
    name = dlg.value()
    if not name:
        return
    if name in _all_group_names(self):
        _msg(self, "分组「%s」已存在。" % name)
        return
    groups = _saved_groups(self)
    groups.append(name)
    _save_groups(self, groups)


def _rename_group(self):
    cur = self.current_group()
    if cur == "全部":
        _msg(self, "请先在左侧列表里选中一个分组。")
        return
    if cur == INVALID_GROUP:
        _msg(self, "「%s」是系统保留分组，不能重命名。" % INVALID_GROUP)
        return
    dlg = _InputDialog(self, "✏️ 重命名分组", "新的分组名称：", cur)
    if dlg.exec() != QDialog.Accepted:
        return
    new = dlg.value()
    if not new or new == cur:
        return
    if new in _all_group_names(self):
        _msg(self, "分组「%s」已存在。" % new)
        return
    for s in self.data.get("subs", []):
        if _group_of(s) == cur:
            s["group"] = new
    _save_groups(self, [new if n == cur else n for n in _saved_groups(self)])


def _delete_group(self):
    cur = self.current_group()
    if cur == "全部":
        _msg(self, "请先在左侧列表里选中一个分组。")
        return
    if cur == INVALID_GROUP:
        _msg(self, "「%s」是系统保留分组，不能删除。" % INVALID_GROUP)
        return
    n = sum(1 for s in self.data.get("subs", []) if _group_of(s) == cur)
    text = "确定删除分组「%s」吗？" % cur
    if n:
        text += "\n其中的 %d 条订阅将移动到「%s」。" % (n, DEFAULT_GROUP)
    if QMessageBox.question(self, "🗑️ 确认删除", text) != QMessageBox.Yes:
        return
    for s in self.data.get("subs", []):
        if _group_of(s) == cur:
            s["group"] = DEFAULT_GROUP
    _save_groups(self, [x for x in _saved_groups(self) if x != cur])


def _open_group_manager(self):
    GroupManagerDialog(self).exec()
    _refresh_groups_keep(self)
    self.refresh_table()


# --------------------------------------------------------------------------
# 卡片：失效按钮 + 醒目的「已失效」徽标
# --------------------------------------------------------------------------
TOGGLE_QSS = (
    "QPushButton{color:#2f6fd0;font-size:11px;background:rgba(63,140,255,30);"
    "border:1px solid rgba(63,140,255,95);border-radius:10px;padding:1px 9px;}"
    "QPushButton:hover{background:rgba(63,140,255,80);color:#1d5bbd;}"
    "QPushButton:pressed{background:rgba(63,140,255,130);}"
)
BADGE_MANUAL_QSS = (
    "QLabel{color:#c81e1e;font-size:11px;font-weight:700;"
    "background:#ffe4e6;border:1px solid #fda4af;border-radius:9px;padding:1px 7px;}"
)
BADGE_SYSTEM_QSS = (
    "QLabel{color:#d92d20;font-size:11px;font-weight:700;"
    "background:#fef3f2;border:1px solid #fecaca;border-radius:9px;padding:1px 7px;}"
)
PKG_QSS = "QLabel{color:#5b6b7b;font-size:11px;background:transparent;}"
RENEW_QSS = (
    "QPushButton{font-size:11px;color:#8a97a6;background:rgba(140,150,165,22);"
    "border:1px solid rgba(140,150,165,70);border-radius:9px;padding:0 8px;}"
    "QPushButton:hover{background:rgba(140,150,165,55);}"
    "QPushButton:checked{color:#0f9d58;background:#e6f6ee;"
    "border:1px solid #9fd9bd;font-weight:700;}"
)


def _fmt_gb(v):
    try:
        g = float(v)
    except Exception:
        return ""
    if g <= 0:
        return ""
    return "%dG" % g if abs(g - round(g)) < 0.05 else "%.1fG" % g


def _pkg_text(sub):
    """卡片标题行里的套餐信息：流量 + 到期月-日（不显示年份）。"""
    ui = sub.get("sub_userinfo") or {}
    total = ui.get("total_gb")
    if total is None:
        total = sub.get("total_traffic_gb")
    parts = []
    gb = _fmt_gb(total)
    if gb:
        parts.append("📦 " + gb)
    exp = ui.get("expire_at") or sub.get("expire_at")
    month_day = ""
    if isinstance(exp, (int, float)) and exp > 0:
        d = _safe_fromtimestamp(exp)
        if d is not None:
            month_day = d.strftime("%m-%d")
    else:
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(exp or ""))
        if m:
            month_day = "%02d-%02d" % (int(m.group(2)), int(m.group(3)))
    if month_day:
        parts.append("📅 " + month_day)
    return " · ".join(parts)


def _pkg_tip(sub):
    ui = sub.get("sub_userinfo") or {}
    tip = []
    gb = _fmt_gb(ui.get("total_gb") if ui.get("total_gb") is not None
                 else sub.get("total_traffic_gb"))
    if gb:
        tip.append("套餐流量：%s" % gb)
    exp = ui.get("expire_at") or sub.get("expire_at")
    if exp:
        if isinstance(exp, (int, float)):
            d = _safe_fromtimestamp(exp)
            exp = d.strftime("%Y-%m-%d %H:%M:%S") if d else exp
        tip.append("到期时间：%s" % exp)
    return "\n".join(tip) or "暂无套餐信息（重新检测后自动获取）"


def _patch_cards(cards, main):
    class SubCard(cards.SubCard):
        invalid_toggled = Signal(str, bool)
        renewable_toggled = Signal(str, bool)

        def __init__(self, sub, parent=None):
            super().__init__(sub, parent)
            self._sub = sub
            self._invalid = bool(sub.get("invalid"))
            self._system_invalid = _is_system_invalid(sub)

            top = self.layout().itemAt(0).layout()
            self._status_lbl = top.itemAt(top.count() - 1).widget() if top.count() else None

            # 用真正的按钮代替小号文字链接：命中区域更大、按下有反馈
            self._toggle = QPushButton("标记失效")
            self._toggle.setCursor(Qt.PointingHandCursor)
            self._toggle.setFocusPolicy(Qt.NoFocus)
            self._toggle.setFixedHeight(22)
            self._toggle.setStyleSheet(TOGGLE_QSS)
            self._toggle.clicked.connect(self._on_toggle_click)
            top.insertWidget(max(0, top.count() - 1), self._toggle)

            self._badge = QLabel()
            self._badge.setAlignment(Qt.AlignCenter)
            self._badge.setFixedHeight(20)
            self._badge.hide()
            top.insertWidget(max(0, top.count() - 1), self._badge)

            # 「可复购」选项 + 套餐信息，都排在「标记失效」前面
            idx = top.indexOf(self._toggle)
            self._renew = QPushButton()
            self._renew.setCheckable(True)
            self._renew.setCursor(Qt.PointingHandCursor)
            self._renew.setFocusPolicy(Qt.NoFocus)
            self._renew.setFixedHeight(20)
            self._renew.setStyleSheet(RENEW_QSS)
            self._renew.setChecked(bool(sub.get("renewable")))
            self._renew.setText("🔁 可复购" if self._renew.isChecked() else "🔁 不可复购")
            self._renew.setToolTip(
                "已标记为可复购（点击切换）" if self._renew.isChecked()
                else "点击标记为“可复购”")
            self._renew.toggled.connect(self._on_renew_toggled)
            top.insertWidget(idx, self._renew)

            self._pkg = QLabel()
            self._pkg.setStyleSheet(PKG_QSS)
            self._pkg.setFocusPolicy(Qt.NoFocus)
            top.insertWidget(idx + 1, self._pkg)
            self._refresh_pkg()
            self._sync_note_labels()
            self._apply_invalid()

        # -- 交互 --
        def _on_toggle_click(self, _checked=False):
            self.invalid_toggled.emit(self.url, not self._invalid)

        def _on_renew_toggled(self, checked):
            self._renew.setText("🔁 可复购" if checked else "🔁 不可复购")
            self._renew.setToolTip(
                "已标记为可复购（点击切换）" if checked
                else "点击标记为“可复购”")
            self.renewable_toggled.emit(self.url, checked)

        def _refresh_pkg(self):
            text = _pkg_text(self._sub)
            self._pkg.setText(text)
            self._pkg.setToolTip(_pkg_tip(self._sub))
            self._pkg.setVisible(bool(text))

        def set_selected(self, on):
            super().set_selected(on)
            self._apply_invalid()

        # -- 外观 --
        def _apply_invalid(self):
            inv = self._invalid
            for lb in self.findChildren(QLabel):
                if lb is self._badge or lb is self._pkg or isinstance(lb, cards.StatusDot):
                    continue
                f = lb.font()
                if f.strikeOut() != inv:
                    f.setStrikeOut(inv)
                    lb.setFont(f)

            self._toggle.setText("恢复" if inv else "标记失效")
            self._toggle.setToolTip(
                "点击恢复到原分组" if inv
                else "点击标记为失效：卡片文字加删除线并归入「%s」分组" % INVALID_GROUP)

            show = inv or self._system_invalid
            if show:
                if inv:
                    self._badge.setText("🚫 已失效")
                    self._badge.setToolTip(
                        "人为标记失效 · 已归入「%s」分组\n"
                        "重新检测正常后会自动恢复" % INVALID_GROUP)
                    self._badge.setStyleSheet(BADGE_MANUAL_QSS)
                else:
                    reason = self._sub.get("status_text") or "检测失败"
                    self._badge.setText("🤖 已失效")
                    self._badge.setToolTip(
                        "系统检测判定失效（%s）\n"
                        "重新检测正常后此徽标会自动消失" % reason)
                    self._badge.setStyleSheet(BADGE_SYSTEM_QSS)
                self._badge.show()
            else:
                self._badge.hide()
            if self._status_lbl is not None:
                self._status_lbl.setVisible(not show)

        def _sync_note_labels(self):
            """备注/网址原本点一下就复制到剪贴板，这里改成只选中卡片。"""
            for lb in self.findChildren(cards.NoteLabel):
                lb.setCursor(Qt.ArrowCursor)
                note = getattr(lb, "_note", "")
                if note:
                    lb.setToolTip(note)

    def _note_click_select_only(self, event):
        """点备注文字不再复制，改为选中所在卡片。"""
        if event.button() != Qt.LeftButton:
            return
        p = self.parent()
        while p is not None and not isinstance(p, cards.SubCard):
            p = p.parent()
        if p is None:
            return
        p.clicked.emit(p.url)
        event.accept()

    cards.SubCard = SubCard
    main.SubCard = SubCard
    try:
        cards.NoteLabel.mousePressEvent = _note_click_select_only
    except Exception:
        pass


# --------------------------------------------------------------------------
# 检测：修掉原版两个会导致「检测不成功」的缺陷
# --------------------------------------------------------------------------
def _safe_fromtimestamp(ts):
    """把时间戳转成 datetime；超出平台支持范围时不再抛异常。

    原版直接 datetime.fromtimestamp(ts)，遇到 3126 年这种超远期 expire
    会抛 OSError(Errno 22)，导致整条订阅被判为「检测异常」。
    """
    try:
        return dt.datetime.fromtimestamp(ts)
    except Exception:
        pass
    try:      # 退回纯算术换算（可支持到 9999 年）
        return dt.datetime(1970, 1, 1) + dt.timedelta(seconds=int(ts))
    except Exception:
        return None


def _patch_checker(checker):
    # --- a) _parse_userinfo：expire 时间戳过大不再崩溃 ---
    _orig_userinfo = checker._parse_userinfo

    def _parse_userinfo(raw):
        try:
            return _orig_userinfo(raw)
        except Exception:
            out = {}
            raw = raw or ""
            try:
                m = re.search(r"upload=(\d+)", raw)
                if m:
                    out["used_gb"] = round(int(m.group(1)) / 1073741824, 2)
                m = re.search(r"download=(\d+)", raw)
                if m:
                    out["used_gb"] = round(
                        out.get("used_gb", 0) + int(m.group(1)) / 1073741824, 2)
                m = re.search(r"total=(\d+)", raw)
                if m:
                    out["total_gb"] = round(int(m.group(1)) / 1073741824, 2)
                m = re.search(r"expire=(\d+)", raw)
                if m:
                    ts = int(m.group(1))
                    if ts > 0:
                        v = _safe_fromtimestamp(ts)
                        if v is not None:
                            out["expire_at"] = v
            except Exception:
                pass
            return out

    checker._parse_userinfo = _parse_userinfo

    # --- b) decode_body：补上完整 Clash 配置（YAML / JSON）的节点解析 ---
    _orig_decode_body = checker.decode_body

    def decode_body(text):
        nodes, raw = _orig_decode_body(text)
        # 原版 JSON 分支只认 protocol、且不取 server/port，结果节点不可用；
        # 这里判断「解析出的节点是否可用」，不可用就用自研解析兜底。
        if nodes and any(n.get("server") for n in nodes):
            return nodes, raw
        extra = _clash_config_nodes(text)
        if extra:
            return extra, raw
        return nodes, raw

    checker.decode_body = decode_body


def _clash_proxies(text):
    """从完整 Clash 配置里取出 proxies 列表（支持 YAML 与 JSON）。"""
    s = (text or "").strip()
    if not s:
        return None
    if s[0] in "[{":
        try:
            data = json.loads(s)
        except Exception:
            return None
        if isinstance(data, dict):
            p = data.get("proxies") or data.get("outbounds")
            if isinstance(p, list) and p:
                return [x for x in p if isinstance(x, dict)]
        return None
    try:
        import yaml
        data = yaml.safe_load(s)
    except Exception:
        return None
    if isinstance(data, dict):
        p = data.get("proxies")
        if isinstance(p, list) and p:
            return [x for x in p if isinstance(x, dict)]
    return None


def _clash_config_nodes(text):
    """把 Clash 配置里的 proxies 转成程序内部的节点结构。"""
    proxies = _clash_proxies(text)
    if not proxies:
        return None
    out = []
    for p in proxies:
        proto = str(p.get("type") or p.get("protocol") or "").strip().lower()
        server = p.get("server")
        if not proto or not server:
            continue
        try:
            port = int(p.get("port"))
        except Exception:
            port = None
        out.append({
            "name": str(p.get("name") or p.get("tag") or server),
            "type": proto,
            "server": str(server),
            "port": port,
            "expire": None,
            "raw": "",
        })
    return out or None


# --------------------------------------------------------------------------
# 卡片右键菜单：加一个「标记失效 / 恢复」入口
# --------------------------------------------------------------------------
def _patch_menu(main):
    MW = main.MainWindow

    def table_menu(self, pos):
        menu = QMenu(self)
        for label, slot in (
            ("✏️ 编辑", self.edit_sub),
            ("🧪 检测此条", self.check_one),
            ("📋 复制链接", self.copy_url),
            ("🔗 复制订阅内容", self.copy_clash),
            ("🌐 打开链接", self.open_url),
        ):
            act = QAction(label, menu)
            act.triggered.connect(slot)
            menu.addAction(act)
        menu.addSeparator()
        # 注意：原版 self.selected() 返回的是「url 字符串列表」，不是订阅字典；
        # 必须用 selected_url + find_sub 才能拿到当前选中的订阅。
        cur_url = getattr(self, "selected_url", None)
        sub = self.find_sub(cur_url) if cur_url else None
        if sub is not None:
            inv = bool(sub.get("invalid"))
            act = QAction("♻️ 恢复为正常" if inv else "🚫 标记为失效", menu)
            act.triggered.connect(
                lambda _=False, w=self, u=sub.get("url"), v=not inv: _set_invalid(w, u, v))
            menu.addAction(act)
            menu.addSeparator()
        act = QAction("🗑️ 删除", menu)
        act.triggered.connect(self.del_sub)
        menu.addAction(act)
        menu.exec(self.card_list.mapToGlobal(pos))

    MW.table_menu = table_menu


# --------------------------------------------------------------------------
# 代理设置入口（左侧检测设置里）
# --------------------------------------------------------------------------
def _install_proxy_row(self, w):
    box = None
    for b in w.findChildren(QGroupBox):
        if "检测设置" in b.title():
            box = b
            break
    if box is None or not isinstance(box.layout(), QGridLayout):
        return
    lay = box.layout()

    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(6)
    self.btn_proxy_full = QPushButton("🌐 填写代理…")
    self.btn_proxy_off = QPushButton("🚫 直连")
    for b in (self.btn_proxy_full, self.btn_proxy_off):
        b.setFixedHeight(24)
        b.setStyleSheet("QPushButton{font-size:11px;padding:0 8px;}")
        b.setCursor(Qt.PointingHandCursor)
        h.addWidget(b)
    h.addStretch(1)
    lay.addWidget(row, lay.rowCount(), 0, 1, 3)

    self.lbl_proxy = QLabel("")
    self.lbl_proxy.setWordWrap(True)
    self.lbl_proxy.setStyleSheet("font-size:10px;color:#7a8899;")
    lay.addWidget(self.lbl_proxy, lay.rowCount(), 0, 1, 3)

    upd_row = QWidget()
    hu = QHBoxLayout(upd_row)
    hu.setContentsMargins(0, 2, 0, 0)
    hu.setSpacing(6)
    self.cb_auto_update = QCheckBox("☁️ 自动检测更新")
    self.cb_auto_update.setCursor(Qt.PointingHandCursor)
    self.cb_auto_update.setStyleSheet("QCheckBox{font-size:11px;color:#44505c;}")
    self.cb_auto_update.setToolTip("启动时自动检查 Gitee 上的最新版本（默认开启）")
    self.cb_auto_update.setChecked(_update_enabled(self))
    self.btn_update_now = QPushButton("检测更新")
    self.btn_update_now.setFixedHeight(22)
    self.btn_update_now.setStyleSheet("QPushButton{font-size:11px;padding:0 8px;}")
    self.btn_update_now.setCursor(Qt.PointingHandCursor)
    self.btn_update_now.setToolTip("立即检查 Gitee 上是否有新版本")
    hu.addWidget(self.cb_auto_update)
    hu.addStretch(1)
    hu.addWidget(self.btn_update_now)
    lay.addWidget(upd_row, lay.rowCount(), 0, 1, 3)

    self.btn_proxy_full.clicked.connect(self.open_proxy)
    self.btn_proxy_off.clicked.connect(self.disable_proxy)
    self.cb_auto_update.toggled.connect(self._set_auto_update)
    self.btn_update_now.clicked.connect(self.check_updates)


def _apply_proxy(self):
    """把代理配置记录到上下文，供逐条检测时按需使用；不改进程环境变量。"""
    cfg = self.data.get("proxy") or {}
    try:
        _CTX["checker"].set_proxies({})
    except Exception:
        pass
    proxy_map = {}
    try:
        proxy_map = _CTX["proxy"].resolve(cfg) or {}
    except Exception:
        proxy_map = {}
    _CTX["proxy_map"] = dict(proxy_map)
    _CTX["proxy_cfg"] = dict(cfg)

    btn = getattr(self, "btn_proxy", None)
    if btn is not None:
        mode = cfg.get("mode") or "off"
        if mode == "custom" and proxy_map:
            btn.setText("🌐 自定义代理")
            btn.setToolTip(str(_CTX["proxy"].describe(cfg)))
        elif mode == "system" and proxy_map:
            btn.setText("🌐 系统代理")
            btn.setToolTip(str(_CTX["proxy"].describe(cfg)))
        else:
            btn.setText("🌐 代理")
            btn.setToolTip("代理设置（仅本程序生效）")
    _sync_proxy_ui(self)


def _disable_proxy(self):
    self.data["proxy"] = {"mode": "off"}
    _CTX["proxy_map"] = {}
    _CTX["proxy_cfg"] = {"mode": "off"}
    try:
        _CTX["store"].save(self.data)
        _CTX["checker"].set_proxies({})
    except Exception:
        pass
    btn = getattr(self, "btn_proxy", None)
    if btn is not None:
        btn.setText("🌐 代理")
        btn.setToolTip("当前不使用任何代理，直接连接")
    _sync_proxy_ui(self)
    self.status.setText("🚫 已关闭代理，全部链接改为直连检测")


# --------------------------------------------------------------------------
# 工具栏：排列方式 + 底栏「失效 / 恢复」按钮
# --------------------------------------------------------------------------
def _on_sort_changed(self, text):
    try:
        self.data.setdefault("settings", {})["sort_mode"] = text
        _CTX["store"].save(self.data)
    except Exception:
        pass
    self.refresh_table()


def _install_center_extras(self, w):
    try:
        outer = w.layout()
        bar = outer.itemAt(0).layout()
        if bar is not None:
            self.cb_sort = QComboBox()
            self.cb_sort.setFixedWidth(126)
            for name in SORT_MODES:
                self.cb_sort.addItem(name)
                self.cb_sort.setItemData(self.cb_sort.count() - 1,
                                         SORT_HINTS.get(name, ""), Qt.ToolTipRole)
            self.cb_sort.setToolTip("排列方式")
            saved = (self.data.get("settings") or {}).get("sort_mode")
            if saved in SORT_MODES:
                self.cb_sort.setCurrentIndex(SORT_MODES.index(saved))
            self.cb_sort.currentTextChanged.connect(self._on_sort_changed)
            bar.insertWidget(max(0, bar.count() - 1), self.cb_sort)
    except Exception:
        pass

    try:
        outer = w.layout()
        bottom = outer.itemAt(outer.count() - 1).layout()
        if bottom is not None:
            self.btn_invalid = QPushButton("🚫 失效")
            self.btn_invalid.setFixedHeight(26)
            self.btn_invalid.setCursor(Qt.PointingHandCursor)
            self.btn_invalid.setStyleSheet(
                "QPushButton{font-size:12px;padding:0 10px;color:#c0392b;"
                "border-color:#f0c4bd;}")
            self.btn_invalid.clicked.connect(self._toggle_selected)
            bottom.insertWidget(max(0, bottom.count() - 1), self.btn_invalid)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 右侧「订阅详情 / 节点信息列表」布局
# --------------------------------------------------------------------------
def _install_detail_layout(self):
    """详情区可滚动，节点列表占用剩余高度；面板宽度随窗口自适应。"""
    d = getattr(self, "detail", None)
    if d is None:
        return
    lay = d.layout()
    meta = getattr(d, "meta_host", None)
    if lay is not None and meta is not None and getattr(d, "_meta_scroll", None) is None:
        sc = QScrollArea(d)
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sc.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        sc.setStyleSheet("QScrollArea{background:transparent;border:none;}"
                         "QScrollArea>QWidget>QWidget{background:transparent;}")
        idx = lay.indexOf(meta)
        if idx >= 0:
            lay.removeWidget(meta)
            sc.setWidget(meta)
            lay.insertWidget(idx, sc)
            d._meta_scroll = sc
    if lay is not None:
        for i in range(lay.count()):
            lay.setStretch(i, 0)
        # node_area（节点卡片滚动区）和 node_view（无节点时的提示）二者只显示一个
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None and w is getattr(d, "node_area", None):
                lay.setStretch(i, 1)
                w.setMinimumHeight(220)
        lay.setStretch(lay.count() - 1, 1)
    nv = getattr(d, "node_view", None)
    if nv is not None:
        nv.setMinimumHeight(220)
    d.setMinimumWidth(340)
    d.setMaximumWidth(680)
    _fit_detail_grid(self)
    _sync_detail_heights(self)


def _fit_detail_grid(self):
    """详情网格随面板宽度收缩：中间值列换行，右侧「复制」按钮不会被挤出可视区。

    原网格按最长内容算出 562px 最小宽度，面板只有 ~300px，第三列（复制按钮）
    会被裁掉且横向滚动条不可见，表现为“详情里没有复制按钮”。
    """
    d = getattr(self, "detail", None)
    meta = getattr(d, "meta_host", None) if d is not None else None
    if meta is None:
        return
    meta.setMinimumWidth(0)
    pol = meta.sizePolicy()
    if pol.horizontalPolicy() != QSizePolicy.Ignored:
        pol.setHorizontalPolicy(QSizePolicy.Ignored)
        meta.setSizePolicy(pol)
    grid = meta.findChild(QGridLayout)
    if grid is None:
        return
    grid.setColumnStretch(0, 0)
    grid.setColumnStretch(1, 1)
    grid.setColumnStretch(2, 0)
    for i in range(grid.count()):
        w = grid.itemAt(i).widget()
        if w is None:
            continue
        if grid.getItemPosition(i)[1] != 1:
            continue
        w.setMinimumWidth(0)
        p = w.sizePolicy()
        if p.horizontalPolicy() != QSizePolicy.Ignored:
            p.setHorizontalPolicy(QSizePolicy.Ignored)
            w.setSizePolicy(p)


def _sync_detail_heights(self):
    """详情区最多占面板 40%，剩余高度全部留给节点列表。"""
    d = getattr(self, "detail", None)
    sc = getattr(d, "_meta_scroll", None) if d is not None else None
    if sc is None:
        return
    h = d.height() if d.height() > 80 else 570
    if getattr(d, "sub", None) is None:
        sc.setFixedHeight(34)
        return
    sc.setFixedHeight(max(110, min(int(h * 0.35), h - 310)))


def _apply_pane_sizes(self):
    """把右侧详情面板调到能正常显示节点列表的宽度，并保证中间卡片区不被挤压。"""
    try:
        sp = self.findChild(QSplitter)
        if sp is None or sp.count() < 3:
            return
        total = sum(sp.sizes())
        if total <= 0:
            return
        left = max(230, int(total * 0.23))
        detail = max(340, int(total * 0.31))
        center = total - left - detail
        if center < 430:
            center = 430
            detail = max(340, total - left - center)
        sp.setSizes([left, center, detail])
    except Exception:
        pass


# --------------------------------------------------------------------------
# 挂载
# --------------------------------------------------------------------------
def _install_ui(self):
    """在 ⚙️ 菜单里加“分组管理”，并给分组列表加右键菜单。"""
    for btn in self.findChildren(QToolButton):
        menu = btn.menu()
        if menu is None:
            continue
        acts = menu.actions()
        anchor = acts[-1] if acts else None
        sep = menu.addSeparator()
        act_upd = QAction("☁️ 检测更新…", menu)
        act_upd.triggered.connect(lambda _=False, w=self: _check_updates(w))
        sep2 = menu.addSeparator()
        act = QAction("🗂️ 分组管理…", menu)
        act.triggered.connect(lambda _=False, w=self: _open_group_manager(w))
        if anchor is not None:
            for a in (sep, act_upd, sep2, act):
                menu.insertAction(anchor, a)
        break

    gl = self.group_list
    gl.setContextMenuPolicy(Qt.CustomContextMenu)
    gl.customContextMenuRequested.connect(
        lambda pos, w=self: _group_context_menu(w, pos))


def _group_context_menu(self, pos):
    menu = QMenu(self)
    menu.addAction("➕ 新建分组", lambda: _new_group(self))
    menu.addAction("✏️ 重命名分组", lambda: _rename_group(self))
    menu.addAction("🗑️ 删除分组", lambda: _delete_group(self))
    menu.addSeparator()
    menu.addAction("🗂️ 分组管理…", lambda: _open_group_manager(self))
    menu.exec(self.group_list.mapToGlobal(pos))


# --------------------------------------------------------------------------
# 检测更新（更新源：Gitee 项目的 Releases）
# --------------------------------------------------------------------------
def _version_tuple(text):
    """'v1.2.3' / '1.2.3-beta' -> (1, 2, 3)。"""
    nums = re.findall(r"\d+", str(text or ""))
    out = [int(n) for n in nums[:4]]
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def _update_enabled(win):
    try:
        return bool((win.data.get("settings") or {}).get("auto_update", True))
    except Exception:
        return True


class _UpdateBridge(QObject):
    """跨线程回传检测结果；信号自动排队到界面线程。"""

    finished = Signal(object, str)

    def __init__(self, win, manual):
        super().__init__(win)
        self.win = win
        self.manual = manual
        self.finished.connect(self._handle)

    def _handle(self, payload, err):
        _on_update_result(self.win, payload, err, self.manual)


def _fetch_latest_release():
    req = _REAL_REQUESTS
    if req is None:
        import requests as req
    sess = req.Session()
    sess.trust_env = False
    headers = {"User-Agent": "airport-manager/%s" % APP_VERSION}
    try:
        resp = sess.get(UPDATE_API, timeout=10, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        proxies = _CTX.get("proxy_map") or {}
        if not proxies:
            raise
        resp = sess.get(UPDATE_API, timeout=10, headers=headers,
                        proxies=proxies)
        resp.raise_for_status()
        return resp.json()


def _update_worker(bridge):
    try:
        bridge.finished.emit(_fetch_latest_release(), "")
    except Exception as exc:
        bridge.finished.emit(None, "%s" % (exc or exc.__class__.__name__))


def _start_update_check(win, manual=False):
    if getattr(win, "_upd_busy", False):
        if manual:
            try:
                win.status.setText("☁️ 正在检测更新，请稍候…")
            except Exception:
                pass
        return
    win._upd_busy = True
    if manual:
        try:
            win.status.setText("☁️ 正在检测更新…（更新源：Gitee）")
        except Exception:
            pass
    bridge = _UpdateBridge(win, manual)
    win._upd_bridge = bridge  # 防止被回收
    threading.Thread(target=_update_worker, args=(bridge,), daemon=True).start()


def _release_notes(body, limit=600):
    text = re.sub(r"\r\n?", "\n", str(body or "")).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _on_update_result(win, payload, err, manual=False):
    win._upd_busy = False
    win._upd_bridge = None
    if err or not isinstance(payload, dict):
        try:
            win.status.setText("☁️ 检测更新失败：%s" % (err or "返回数据异常"))
        except Exception:
            pass
        if manual:
            QMessageBox.warning(
                win, "☁️ 检测更新",
                "没能连上 Gitee 更新源，请稍后再试。\n\n%s\n\n"
                "也可以手动打开下载页：\n%s" % (err or "", UPDATE_PAGE))
        return

    tag = str(payload.get("tag_name") or "").strip()
    latest = _version_tuple(tag)
    current = _version_tuple(APP_VERSION)
    if not tag or latest <= current:
        try:
            win.status.setText("✅ 已是最新版本 v%s" % APP_VERSION)
        except Exception:
            pass
        if manual:
            _msg(win, "当前版本 v%s，已是最新版本。\n\n"
                      "更新源：Gitee Releases\n%s" % (APP_VERSION, UPDATE_PAGE),
                 "☁️ 检测更新")
        return

    skipped = None
    try:
        skipped = (win.data.get("settings") or {}).get("update_skip")
    except Exception:
        pass
    if not manual and skipped == tag:
        try:
            win.status.setText("☁️ 发现新版本 %s（已选择稍后提醒）" % tag)
        except Exception:
            pass
        return

    exe_url = ""
    for a in payload.get("assets") or []:
        if str(a.get("name") or "").lower().endswith(".exe"):
            exe_url = str(a.get("browser_download_url") or "")
            break
    page = str(payload.get("html_url") or exe_url or UPDATE_PAGE)

    box = QMessageBox(win)
    box.setWindowTitle("☁️ 发现新版本")
    box.setIcon(QMessageBox.Information)
    title = str(payload.get("name") or tag).strip()
    lines = ["发现新版本 %s（当前 v%s）" % (tag, APP_VERSION)]
    if title and title != tag:
        lines.append(title)
    notes = _release_notes(payload.get("body"))
    if notes:
        lines += ["", notes]
    lines += ["", "更新源：Gitee · %s" % UPDATE_PAGE]
    box.setText("\n".join(lines))
    go = box.addButton("⬇️ 去下载", QMessageBox.AcceptRole)
    box.addButton("稍后提醒", QMessageBox.RejectRole)
    box.setDefaultButton(go)
    box.exec()

    if box.clickedButton() is go:
        try:
            webbrowser.open(exe_url or page)
        except Exception:
            pass
        try:
            win.status.setText("☁️ 已在浏览器打开 v%s 的下载地址" % tag)
        except Exception:
            pass
    else:
        try:
            win.data.setdefault("settings", {})["update_skip"] = tag
            _CTX["store"].save(win.data)
        except Exception:
            pass
        try:
            win.status.setText("☁️ 已忽略 %s 的提醒，可在 ⚙ 菜单手动检测" % tag)
        except Exception:
            pass


def _maybe_auto_check(win):
    if _update_enabled(win):
        _start_update_check(win, manual=False)


def _set_auto_update(self, checked):
    try:
        self.data.setdefault("settings", {})["auto_update"] = bool(checked)
        _CTX["store"].save(self.data)
    except Exception:
        pass
    if checked:
        self.status.setText("☁️ 已开启自动检测更新：启动时检查 Gitee 最新版本")
        QTimer.singleShot(300, lambda w=self: _start_update_check(w, False))
    else:
        self.status.setText("☁️ 已关闭自动检测更新（仍可手动检测）")


def _check_updates(self):
    _start_update_check(self, manual=True)


def install(cards, store, proxy, checker, main):
    """由 entry.py 调用：把新功能叠加到原程序上。"""
    global _REAL_REQUESTS, _ORIG_CHECK_ONE

    _CTX.update(cards=cards, store=store, proxy=proxy,
                checker=checker, main=main, proxy_map={}, proxy_cfg={})

    # 让 check_one 里的网络请求走「直连 / 代理」线程局部开关
    _REAL_REQUESTS = checker.requests
    checker.requests = _ProxyRequests(_REAL_REQUESTS)
    _ORIG_CHECK_ONE = checker.check_one
    checker.check_one = _proxy_check_one

    _patch_cards(cards, main)
    _patch_checker(checker)
    _patch_menu(main)

    MW = main.MainWindow
    MW.refresh_groups = refresh_groups
    MW._refresh_groups_keep = _refresh_groups_keep
    MW._set_invalid = _set_invalid
    MW._toggle_selected = _toggle_selected
    MW._on_sort_changed = _on_sort_changed
    MW._set_auto_update = _set_auto_update
    MW.check_updates = _check_updates

    _orig_init = MW.__init__
    _orig_build_left = MW._build_left
    _orig_build_center = MW._build_center
    _orig_refresh_table = MW.refresh_table
    _orig_visible_subs = MW.visible_subs
    _orig_select_sub = MW.select_sub
    _orig_apply_proxy = MW.apply_proxy
    _orig_disable_proxy = MW.disable_proxy
    _orig_on_done = MW.on_done
    _orig_finish_check = MW.finish_check
    _orig_resize = MW.resizeEvent
    _orig_check_all = MW.check_all

    def __init__(self):
        _orig_init(self)
        try:
            # 无边框窗口默认没有 WS_MINIMIZEBOX，点任务栏图标无法最小化/还原
            need = Qt.WindowMinimizeButtonHint | Qt.WindowSystemMenuHint
            if (self.windowFlags() & need) != need:
                self.setWindowFlags(self.windowFlags() | need)
        except Exception:
            pass
        try:
            scr = self.screen().availableGeometry()
            w = min(max(self.width(), 1180), scr.width() - 20)
            h = min(max(self.height(), 740), scr.height() - 20)
            if w > self.width() or h > self.height():
                self.resize(w, h)
        except Exception:
            pass
        _install_ui(self)
        _install_detail_layout(self)
        QTimer.singleShot(0, lambda w=self: (_apply_pane_sizes(w),
                                             _sync_detail_heights(w)))
        _sync_proxy_ui(self)
        _sync_invalid_btn(self)
        if _update_enabled(self):
            QTimer.singleShot(1600, lambda w=self: _maybe_auto_check(w))

    def _build_left(self):
        w = _orig_build_left(self)
        try:
            _install_proxy_row(self, w)
        except Exception:
            import traceback
            traceback.print_exc()
        return w

    def _build_center(self):
        w = _orig_build_center(self)
        try:
            _install_center_extras(self, w)
        except Exception:
            import traceback
            traceback.print_exc()
        return w

    def refresh_table(self):
        _orig_refresh_table(self)
        _wire_cards(self)
        _sync_invalid_btn(self)
        _sync_detail_heights(self)

    def visible_subs(self):
        subs = list(_orig_visible_subs(self) or [])
        return _sort_subs(self, subs)

    def select_sub(self, url):
        sub = _orig_select_sub(self, url)
        _sync_invalid_btn(self)
        _sync_detail_heights(self)
        return sub

    def apply_proxy(self):
        try:
            _apply_proxy(self)
        except Exception:
            import traceback
            traceback.print_exc()

    def disable_proxy(self):
        _disable_proxy(self)

    def resizeEvent(self, e):
        _orig_resize(self, e)
        _sync_detail_heights(self)

    def check_all(self, urls):
        w = getattr(self, "worker", None)
        t = getattr(self, "thread", None)
        if w is not None and (t is None or not t.isRunning()):
            # 上一轮线程异常退出后 worker 残留，会让按钮彻底失效
            self.worker = None
            self.thread = None
            self.stop_event = None
            try:
                self.progress.hide()
            except Exception:
                pass
        elif w is not None:
            # 正在检测时给一条可见提示，避免弹窗藏在窗口后面像“没反应”
            try:
                pr = getattr(self, "progress", None)
                self.status.setText("⏳ 正在检测中… %d/%d，请稍候" %
                                    (pr.value() if pr else 0,
                                     pr.maximum() if pr else 0))
            except Exception:
                pass
            return
        return _orig_check_all(self, urls)

    def on_done(self, results):
        try:
            _json_safe(results)
            _json_safe(self.data)
        except Exception:
            pass
        self._last_results = results
        try:
            _orig_on_done(self, results)
        except Exception:
            import traceback
            traceback.print_exc()
            try:
                self.finish_check(False)
            except Exception:
                traceback.print_exc()

    def finish_check(self, *args, **kwargs):
        stopped = bool(args[0]) if args else bool(kwargs.get("stopped"))
        cleared = [] if stopped else _auto_clear_invalid(self)
        _orig_finish_check(self, *args, **kwargs)
        _sync_invalid_btn(self)
        _sync_detail_heights(self)
        QTimer.singleShot(0, lambda w=self: _sync_detail_heights(w))
        try:
            if not stopped:
                results = getattr(self, "_last_results", None) or []
                via = sum(1 for r in results
                          if isinstance(r, dict) and r.get("used_proxy"))
                if via:
                    self.status.setText(
                        self.status.text() + " · 🌐 %d 条经代理" % via)
                if cleared:
                    self.status.setText(
                        self.status.text() + " · ♻️ 自动恢复：" + "、".join(cleared))
        except Exception:
            pass

    MW.__init__ = __init__
    MW._build_left = _build_left
    MW._build_center = _build_center
    MW.refresh_table = refresh_table
    MW.visible_subs = visible_subs
    MW.select_sub = select_sub
    MW.apply_proxy = apply_proxy
    MW.disable_proxy = disable_proxy
    MW.on_done = on_done
    MW.finish_check = finish_check
    MW.resizeEvent = resizeEvent
    MW.check_all = check_all

    try:
        _apply_proxy_ctx_only(proxy, store)
    except Exception:
        pass


def _apply_proxy_ctx_only(proxy, store):
    """窗口创建前先把代理配置读进上下文（apply_proxy 也会再刷新一次）。"""
    try:
        data = store.load()
        cfg = data.get("proxy") or {}
        _CTX["proxy_cfg"] = dict(cfg)
        _CTX["proxy_map"] = dict(proxy.resolve(cfg) or {})
    except Exception:
        pass

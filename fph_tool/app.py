"""FPH 仪表设置工具 GUI。

R&S Spectrum Rider FPH 参数配置 + 迹线读取/绘制 + 2.4G 信道预设。
所有耗时操作（VISA/SCPI）在后台工作线程串行执行，UI 通过队列刷新。
"""
from __future__ import annotations

import csv
import math
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .instrument import (DEFAULT_RESOURCE, FphInstrument, FphSettings,
                         InstrumentError, trace_stats)

APP_TITLE = "FPH 仪表设置工具 - R&S Spectrum Rider"
WIFI24_CHANNELS = {ch: 2407.0 + 5.0 * ch for ch in range(1, 15)}  # ch1..ch14
RBW_CHOICES_KHZ = [10, 30, 100, 300, 1000, 3000]
ATTEN_CHOICES_DB = [0, 5, 10, 15, 20, 25, 30]
# UI 迹线方式 -> 仪表 DISP:TRAC:MODE（下发到仪表，回读校验）
TRACE_MODE_TO_SCPI = {"单扫": "WRITE", "峰值保持": "MAXH", "软件平均": "AVER"}
SCPI_TO_TRACE_MODE = {"WRITE": "单扫", "WRIT": "单扫",
                      "MAXH": "峰值保持", "AVER": "软件平均", "AVERAGE": "软件平均"}


class FphApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1280x820")
        self.root.minsize(1080, 640)

        self.instr = FphInstrument(self._thread_log)
        self.jobs: queue.Queue[Callable[[], None]] = queue.Queue()
        self.ui_events: queue.Queue[Callable[[], None]] = queue.Queue()
        self.busy = False
        self.auto_refresh = False
        self._trace_data: tuple[float, float, list[float]] | None = None

        self._build_ui()
        self.root.after(80, self._poll_ui)
        threading.Thread(target=self._worker, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI 构建
    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Section.TLabelframe.Label", font=("", 10, "bold"))
        style.configure("Big.TLabel", font=("", 11, "bold"))

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        self._build_connection_bar()
        main = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        main.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        top = ttk.PanedWindow(main, orient=tk.HORIZONTAL)
        log = ttk.Labelframe(main, text=" 通信日志 ")
        main.add(top, weight=4)
        main.add(log, weight=1)
        style.configure("Sash.TPanedwindow", sashwidth=6)

        self._build_settings_panel(top)
        self._build_trace_panel(top)
        self._build_log_panel(log)

    def _build_connection_bar(self) -> None:
        bar = ttk.Labelframe(self.root, text=" 连接 ")
        bar.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        bar.columnconfigure(6, weight=1)

        ttk.Label(bar, text="资源串:").grid(row=0, column=0, padx=(6, 2))
        self.resource_var = tk.StringVar(value=DEFAULT_RESOURCE)
        resource_entry = ttk.Entry(bar, textvariable=self.resource_var, width=30)
        resource_entry.grid(row=0, column=1, padx=2)

        self.connect_btn = ttk.Button(bar, text="连接", command=self.on_connect)
        self.connect_btn.grid(row=0, column=2, padx=2)
        self.disconnect_btn = ttk.Button(bar, text="断开", command=self.on_disconnect,
                                         state=tk.DISABLED)
        self.disconnect_btn.grid(row=0, column=3, padx=2)
        ttk.Button(bar, text="读取当前参数", command=self.on_read_status
                   ).grid(row=0, column=4, padx=2)
        self.restore_btn = ttk.Button(bar, text="恢复连接时设置", command=self.on_restore,
                                      state=tk.DISABLED)
        self.restore_btn.grid(row=0, column=5, padx=2)

        self.idn_var = tk.StringVar(value="未连接")
        ttk.Label(bar, textvariable=self.idn_var, style="Big.TLabel", foreground="#0a7a0a"
                  ).grid(row=0, column=6, sticky="w", padx=10)
        self.idn_label = None  # 兼容外部查询

    # ------------------------------------------------------------- 参数设置区
    def _build_settings_panel(self, parent: ttk.PanedWindow) -> None:
        panel = ttk.Frame(parent)
        parent.add(panel, weight=0)

        canvas = tk.Canvas(panel, highlightthickness=0)
        vbar = ttk.Scrollbar(panel, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vbar.set, width=430)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_mousewheel(event: Any) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel, add="+")

        col = ttk.Frame(inner)
        col.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        col.columnconfigure(1, weight=1)

        # --- 信道预设 ---
        sect = ttk.Labelframe(col, text=" 2.4G 信道一键预设 ", style="Section.TLabelframe")
        sect.pack(fill=tk.X, pady=(0, 6))
        sect.columnconfigure(2, weight=1)
        ttk.Label(sect, text="信道:").grid(row=0, column=0, padx=(6, 2), pady=4)
        self.channel_var = tk.IntVar(value=1)
        ch_combo = ttk.Combobox(sect, textvariable=self.channel_var, width=5,
                                values=list(WIFI24_CHANNELS), state="readonly")
        ch_combo.grid(row=0, column=1, padx=2)
        ch_combo.bind("<<ComboboxSelected>>", lambda e: self._update_channel_label())
        self.channel_freq_var = tk.StringVar(value="2412.0 MHz")
        ttk.Label(sect, textvariable=self.channel_freq_var, style="Big.TLabel",
                  foreground="#a030a0").grid(row=0, column=2, sticky="w", padx=6)
        ttk.Button(sect, text="设到该信道", command=self.on_channel_preset
                   ).grid(row=0, column=3, padx=6)
        ttk.Label(sect, text="把中心频率设为该信道频率（Span 等按下方各栏当前值一并下发）。",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=4,
                                            sticky="w", padx=6, pady=(0, 4))

        # --- 频率 ---
        sect = ttk.Labelframe(col, text=" 频率 / 扫描 ", style="Section.TLabelframe")
        sect.pack(fill=tk.X, pady=(0, 6))
        sect.columnconfigure(1, weight=1)
        ttk.Label(sect, text="中心频率 (MHz):").grid(row=0, column=0, sticky="w", padx=6, pady=2)
        self.centre_var = tk.StringVar(value="2412")
        ttk.Entry(sect, textvariable=self.centre_var, width=12).grid(row=0, column=1, sticky="w")
        ttk.Label(sect, text="Span (MHz):").grid(row=1, column=0, sticky="w", padx=6, pady=2)
        self.span_var = tk.StringVar(value="40")
        ttk.Entry(sect, textvariable=self.span_var, width=12).grid(row=1, column=1, sticky="w")
        ttk.Label(sect, text="扫描时间 (s):").grid(row=2, column=0, sticky="w", padx=6, pady=2)
        self.sweep_time_auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(sect, text="自动", variable=self.sweep_time_auto_var
                        ).grid(row=2, column=1, padx=2)
        self.sweep_time_var = tk.StringVar(value="")
        ttk.Entry(sect, textvariable=self.sweep_time_var, width=10
                  ).grid(row=2, column=2, sticky="w", padx=2)
        ttk.Label(sect, text="秒（空=沿用仪表当前值；手动值不能小于 RBW 决定的物理最短扫描时间，"
                             "过小会被仪表拒绝/自动调整）", style="Hint.TLabel", wraplength=380,
                  justify="left").grid(row=3, column=0, columnspan=3, sticky="w",
                                       padx=6, pady=(0, 4))
        self.axis_var = tk.StringVar(value="实际范围: -")
        ttk.Label(sect, textvariable=self.axis_var, style="Hint.TLabel"
                  ).grid(row=4, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 4))

        # --- 带宽 ---
        sect = ttk.Labelframe(col, text=" 分辨率/视频带宽 (RBW/VBW) ", style="Section.TLabelframe")
        sect.pack(fill=tk.X, pady=(0, 6))
        sect.columnconfigure(2, weight=1)
        ttk.Label(sect, text="RBW:").grid(row=0, column=0, padx=(6, 2), pady=2)
        self.rbw_auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sect, text="自动", variable=self.rbw_auto_var
                        ).grid(row=0, column=1, padx=2)
        self.rbw_var = tk.StringVar(value="100")
        rbw_combo = ttk.Combobox(sect, textvariable=self.rbw_var, width=8,
                                 values=[str(v) for v in RBW_CHOICES_KHZ])
        rbw_combo.grid(row=0, column=2, sticky="w", padx=2)
        ttk.Label(sect, text="kHz", style="Hint.TLabel").grid(row=0, column=3, sticky="w")
        ttk.Label(sect, text="VBW:").grid(row=1, column=0, padx=(6, 2), pady=2)
        self.vbw_auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sect, text="自动", variable=self.vbw_auto_var
                        ).grid(row=1, column=1, padx=2)
        self.vbw_var = tk.StringVar(value="300")
        vbw_combo = ttk.Combobox(sect, textvariable=self.vbw_var, width=8,
                                 values=[str(v) for v in RBW_CHOICES_KHZ])
        vbw_combo.grid(row=1, column=2, sticky="w", padx=2)
        ttk.Label(sect, text="kHz", style="Hint.TLabel").grid(row=1, column=3, sticky="w")
        ttk.Label(sect, text="自动模式会由仪表按 span 自选；手动会先关 AUTO 再写值。",
                  style="Hint.TLabel").grid(row=2, column=0, columnspan=4,
                                            sticky="w", padx=6, pady=(0, 4))

        # --- 电平 ---
        sect = ttk.Labelframe(col, text=" 电平 / 衰减 ", style="Section.TLabelframe")
        sect.pack(fill=tk.X, pady=(0, 6))
        sect.columnconfigure(1, weight=1)
        ttk.Label(sect, text="参考电平 (dBm):").grid(row=0, column=0, sticky="w", padx=6, pady=2)
        self.rlev_var = tk.StringVar(value="0")
        ttk.Entry(sect, textvariable=self.rlev_var, width=10).grid(row=0, column=1, sticky="w")
        ttk.Label(sect, text="输入衰减 (dB):").grid(row=1, column=0, sticky="w", padx=6, pady=2)
        self.atten_auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sect, text="自动", variable=self.atten_auto_var
                        ).grid(row=1, column=1, sticky="w")
        self.atten_var = tk.StringVar(value="15")
        att_combo = ttk.Combobox(sect, textvariable=self.atten_var, width=6,
                                 values=[str(v) for v in ATTEN_CHOICES_DB])
        att_combo.grid(row=1, column=2, sticky="w", padx=2)
        ttk.Label(sect, text="注意: 参考电平与输入衰减耦合（如 ATT=0 时 RLEV=0 会被拒绝，"
                             "工具会自动改开自动衰减重试）。", style="Hint.TLabel", wraplength=380,
                  justify="left").grid(row=2, column=0, columnspan=3, sticky="w",
                                       padx=6, pady=(0, 4))

        # --- 触发/平均 ---
        sect = ttk.Labelframe(col, text=" 触发 / 迹线平均 ", style="Section.TLabelframe")
        sect.pack(fill=tk.X, pady=(0, 6))
        sect.columnconfigure(1, weight=1)
        ttk.Label(sect, text="触发源:").grid(row=0, column=0, sticky="w", padx=6, pady=2)
        self.trigger_var = tk.StringVar(value="IMM")
        ttk.Combobox(sect, textvariable=self.trigger_var, width=8, state="readonly",
                     values=["IMM", "EXT"]).grid(row=0, column=1, sticky="w")
        ttk.Label(sect, text="迹线方式:").grid(row=1, column=0, sticky="w", padx=6, pady=2)
        self.trace_mode_var = tk.StringVar(value="峰值保持")
        ttk.Combobox(sect, textvariable=self.trace_mode_var, width=10, state="readonly",
                     values=["单扫", "峰值保持", "软件平均"]).grid(row=1, column=1, sticky="w")
        ttk.Label(sect, text="次数:").grid(row=1, column=2, sticky="w", padx=(10, 2))
        self.sweep_count_var = tk.IntVar(value=8)
        ttk.Spinbox(sect, textvariable=self.sweep_count_var, from_=1, to=200, width=6
                    ).grid(row=1, column=3, sticky="w")
        ttk.Label(sect, text="迹线方式会下发到仪表（单扫→WRITE / 峰值保持→MAXH / 软件平均→AVER）。"
                             "突发发包信号(占空比低)在扫频分析仪上呈随机梳状尖峰：软件平均会把它抹低"
                             "——这是突发信号特性，不是仪表故障。看包络/量功率请用「峰值保持」"
                             "(仪表 MAXH 保持包络，工具再逐点取最大)。",
                  style="Hint.TLabel", wraplength=380, justify="left"
                  ).grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))

        # --- 应用 ---
        btns = ttk.Frame(col)
        btns.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(btns, text="应用全部参数（写入并回读校验）", command=self.on_apply
                   ).pack(side=tk.LEFT, padx=(4, 4), fill=tk.X, expand=True)
        ttk.Button(btns, text="填入 TX 测试默认值", command=self.on_tx_defaults
                   ).pack(side=tk.LEFT, padx=4)

        # --- 测量辅助 ---
        sect = ttk.Labelframe(col, text=" 测量辅助 ", style="Section.TLabelframe")
        sect.pack(fill=tk.X, pady=(0, 6))
        sect.columnconfigure(1, weight=1)
        ttk.Label(sect, text="外部衰减 (dB):").grid(row=0, column=0, sticky="w", padx=6, pady=2)
        self.ext_att_var = tk.DoubleVar(value=13.0)
        ttk.Entry(sect, textvariable=self.ext_att_var, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(sect, text="信道带宽 (MHz):").grid(row=1, column=0, sticky="w", padx=6, pady=2)
        self.ch_bw_var = tk.DoubleVar(value=20.0)
        ttk.Entry(sect, textvariable=self.ch_bw_var, width=8).grid(row=1, column=1, sticky="w")
        ttk.Label(sect, text="外部衰减=DUT 到仪表入口的固定衰减器/线缆损耗，"
                             "用于把读数换算成 DUT 天线口功率。",
                  style="Hint.TLabel", wraplength=380, justify="left"
                  ).grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 4))

    # ------------------------------------------------------------- 迹线显示区
    def _build_trace_panel(self, parent: ttk.PanedWindow) -> None:
        panel = ttk.Frame(parent)
        parent.add(panel, weight=1)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)

        bar = ttk.Frame(panel)
        bar.grid(row=0, column=0, sticky="ew", padx=4, pady=2)
        ttk.Button(bar, text="读取迹线", command=self.on_read_trace).pack(side=tk.LEFT, padx=2)
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="自动刷新", variable=self.auto_var,
                        command=self._toggle_auto).pack(side=tk.LEFT, padx=(10, 2))
        ttk.Label(bar, text="间隔(s):").pack(side=tk.LEFT, padx=(6, 0))
        self.interval_var = tk.IntVar(value=5)
        ttk.Spinbox(bar, textvariable=self.interval_var, from_=1, to=120, width=5
                    ).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="保存 CSV", command=self.on_save_csv).pack(side=tk.LEFT, padx=10)
        self.trace_info_var = tk.StringVar(value="尚未读取迹线")
        ttk.Label(bar, textvariable=self.trace_info_var, style="Big.TLabel"
                  ).pack(side=tk.RIGHT, padx=8)

        self.canvas = tk.Canvas(panel, bg="white", highlightthickness=1,
                                highlightbackground="#bbbbbb")
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self.canvas.bind("<Configure>", lambda e: self._draw_trace())
        self._draw_empty()

    def _build_log_panel(self, parent: ttk.Widget) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.log_text = tk.Text(parent, height=8, state=tk.DISABLED, wrap="none",
                                font=("Consolas", 9))
        ybar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.log_text.yview)
        xbar = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        for tag, color in (("发送", "#0055cc"), ("接收", "#007700"), ("状态", "#555555"),
                           ("错误", "#cc0000"), ("注意", "#cc7700"), ("结果", "#800080")):
            self.log_text.tag_configure(tag, foreground=color)
        btns = ttk.Frame(parent)
        btns.grid(row=2, column=0, sticky="ew", pady=2)
        ttk.Button(btns, text="保存日志", command=self.on_save_log).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="清空", command=lambda: self._clear_log()).pack(side=tk.LEFT, padx=4)

    # ------------------------------------------------------------------ 线程层
    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                break
            try:
                job()
            except InstrumentError as exc:
                self._thread_log("错误", str(exc))
            except Exception as exc:  # noqa: BLE001
                self._thread_log("错误", f"未处理异常: {type(exc).__name__}: {exc}")
            finally:
                self._post(lambda: setattr(self, "busy", False))
                self._post(self._update_buttons)

    def _submit(self, job: Callable[[], None]) -> bool:
        if self.busy:
            self.append_log("注意", "有操作正在执行，请稍候")
            return False
        self.busy = True
        self.jobs.put(job)
        self._update_buttons()
        return True

    def _thread_log(self, tag: str, message: str) -> None:
        self._post(lambda: self.append_log(tag, message))

    def _post(self, fn: Callable[[], None]) -> None:
        self.ui_events.put(fn)

    def _poll_ui(self) -> None:
        try:
            while True:
                fn = self.ui_events.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.root.after(80, self._poll_ui)

    # ------------------------------------------------------------------ 日志
    def append_log(self, tag: str, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{stamp}] [{tag}] {message}\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def on_save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".log", initialfile=f"fph_log_{datetime.now():%Y%m%d_%H%M%S}.log")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.log_text.get("1.0", tk.END))
        self.append_log("状态", f"日志已保存到 {path}")

    # ------------------------------------------------------------------ 连接
    def _update_buttons(self) -> None:
        connected = self.instr.connected
        state = tk.NORMAL if (connected and not self.busy) else tk.DISABLED
        self.connect_btn.configure(state=tk.DISABLED if connected or self.busy else tk.NORMAL)
        self.disconnect_btn.configure(state=state)
        self.restore_btn.configure(state=state)

    def on_connect(self) -> None:
        resource = self.resource_var.get().strip()

        def work() -> None:
            idn = self.instr.connect(resource)
            self.instr.snapshot()

            def ok() -> None:
                self.idn_var.set(idn)
                self.append_log("结果", f"连接成功: {idn}")
                self._update_buttons()
                self.on_read_status()  # 顺带回读填充
            self._post(ok)
        if self._submit(work):
            self.append_log("状态", f"正在连接 {resource} ...")

    def on_disconnect(self) -> None:
        def work() -> None:
            self.instr.disconnect()

            def ok() -> None:
                self.idn_var.set("未连接")
                self.append_log("状态", "已断开连接")
                self._update_buttons()
            self._post(ok)
        self._submit(work)

    def on_restore(self) -> None:
        def work() -> None:
            self.instr.restore()
        self._submit(work)

    # ------------------------------------------------------------------ 参数
    def _collect_settings(self, centre_mhz: float | None = None) -> tuple[FphSettings | None, str]:
        try:
            centre = centre_mhz if centre_mhz is not None else float(self.centre_var.get())
            span = float(self.span_var.get())
            rlev = float(self.rlev_var.get())
            atten = float(self.atten_var.get())
            sweep = int(self.sweep_count_var.get())
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("参数错误", f"数值输入有误: {exc}", parent=self.root)
            return None, ""
        if span <= 0 or centre <= 0:
            messagebox.showerror("参数错误", "中心频率/Span 必须为正数", parent=self.root)
            return None, ""
        sweep_time_s = None
        if not self.sweep_time_auto_var.get():
            raw_t = self.sweep_time_var.get().strip()
            if raw_t:
                try:
                    sweep_time_s = float(raw_t)
                except ValueError:
                    messagebox.showerror("参数错误", "扫描时间数值有误", parent=self.root)
                    return None, ""
                if sweep_time_s <= 0:
                    messagebox.showerror("参数错误", "扫描时间必须为正数", parent=self.root)
                    return None, ""
        rbw_hz = vbw_hz = None
        try:
            if not self.rbw_auto_var.get():
                rbw_hz = float(self.rbw_var.get()) * 1e3
            if not self.vbw_auto_var.get():
                vbw_hz = float(self.vbw_var.get()) * 1e3
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("参数错误", f"RBW/VBW 数值有误: {exc}", parent=self.root)
            return None, ""
        mode = self.trace_mode_var.get()
        s = FphSettings(
            centre_hz=centre * 1e6,
            span_hz=span * 1e6,
            rbw_auto=True if self.rbw_auto_var.get() else False,
            rbw_hz=rbw_hz,
            vbw_auto=True if self.vbw_auto_var.get() else False,
            vbw_hz=vbw_hz,
            ref_level_dbm=rlev,
            atten_auto=True if self.atten_auto_var.get() else False,
            atten_db=atten if not self.atten_auto_var.get() else None,
            trigger=self.trigger_var.get(),
            trace_mode=TRACE_MODE_TO_SCPI[mode],  # 下发到仪表：单扫→WRITE / 峰值保持→MAXH / 软件平均→AVER
            sweep_count=max(1, min(200, sweep)),
            sweep_time_auto=self.sweep_time_auto_var.get(),
            sweep_time_s=sweep_time_s,
        )
        mode_desc = mode + (f"x{s.sweep_count}" if mode != "单扫" else "")
        return s, f"CENT={centre:g}MHz SPAN={span:g}MHz RBW={'AUTO' if rbw_hz is None else f'{rbw_hz/1e3:g}k'}" \
                 f" VBW={'AUTO' if vbw_hz is None else f'{vbw_hz/1e3:g}k'} RLEV={rlev:g}dBm " \
                 f"ATT={'AUTO' if self.atten_auto_var.get() else f'{atten:g}dB'} " \
                 f"TRIG={self.trigger_var.get()} 迹线={mode_desc}" \
                 f" SWE:TIME={'AUTO' if sweep_time_s is None else f'{sweep_time_s:g}s'}"

    def _apply_job(self, centre_mhz: float | None, note: str) -> None:
        settings, desc = self._collect_settings(centre_mhz)
        if settings is None:
            return

        def work() -> None:
            self._thread_log("发送", f"应用参数: {desc}")
            results = self.instr.apply(settings)
            ok_cnt = sum(1 for _, _, v in results if v == "OK")
            wait = 2.0  # set_tx 有 ~3s 启动延迟，此处先等 2s，读取侧再等
            self._thread_log("状态", f"写入 {len(results)} 项 / {ok_cnt} 项回读一致；"
                                      f"等待 {wait:.1f}s 扫描稳定…")

            time.sleep(wait)
            st = self.instr.read_status()

            def ok() -> None:
                self._fill_status(st)
                self.append_log("结果", f"参数应用完成（{ok_cnt}/{len(results)} 项 OK）")
            self._post(ok)
        self._submit(work)

    def on_apply(self) -> None:
        self._apply_job(None, "")

    def on_channel_preset(self) -> None:
        ch = self.channel_var.get()
        if ch not in WIFI24_CHANNELS:
            messagebox.showerror("参数错误", "请选择 1-14 信道", parent=self.root)
            return
        freq = WIFI24_CHANNELS[ch]
        self.centre_var.set(f"{freq:g}")
        self._update_channel_label()
        self._apply_job(freq, f"信道{ch}")

    def _update_channel_label(self) -> None:
        ch = self.channel_var.get()
        self.channel_freq_var.set(f"{WIFI24_CHANNELS.get(ch, 0):g} MHz")

    def on_tx_defaults(self) -> None:
        """2.4G TX 传导测试默认参数（含 13dB 外部衰减器场景的安全衰减设置）。"""
        ch = self.channel_var.get()
        self.centre_var.set(f"{WIFI24_CHANNELS.get(ch, 2412.0):g}")
        self.span_var.set("40")
        self.rbw_auto_var.set(False); self.rbw_var.set("100")
        self.vbw_auto_var.set(False); self.vbw_var.set("300")
        # 真机标定：功率档0 时入口约 -4dBm；ATT=0/RLEV=-10 会报中频过载(IFOVL)
        self.rlev_var.set("0")
        self.atten_auto_var.set(False); self.atten_var.set("15")
        self.trigger_var.set("IMM")
        self.trace_mode_var.set("峰值保持"); self.sweep_count_var.set(8)
        self.sweep_time_auto_var.set(True); self.sweep_time_var.set("")
        self._update_channel_label()
        self.append_log("状态", "已填入 TX 测试默认参数（40MHz span / RBW100k / VBW300k / "
                                "RLEV0 / ATT15 / 峰值保持x8 / 扫描时间自动；ATT15 用于 "
                                "13dB 外部衰减下防中频过载），点击“应用全部参数”下发")

    def on_read_status(self) -> None:
        def work() -> None:
            st = self.instr.read_status()

            def ok() -> None:
                self._fill_status(st)
                self.append_log("结果", "当前参数已回读")
            self._post(ok)
        self._submit(work)

    def _fill_status(self, st: Any) -> None:
        def mhz(v: float | None) -> str:
            return f"{v / 1e6:.4g} MHz" if v is not None else "-"
        self.idn_var.set(st.idn or self.idn_var.get())
        self.centre_var.set(f"{(st.centre_hz or 0) / 1e6:g}")
        self.span_var.set(f"{(st.span_hz or 0) / 1e6:g}")
        self.axis_var.set(f"实际范围: {mhz(st.start_hz)} ~ {mhz(st.stop_hz)} | "
                          f"扫描 {st.sweep_points and int(st.sweep_points) or '?'} 点 / "
                          f"{st.sweep_time_s or 0:.3f} s")
        if st.rbw_auto is not None:
            self.rbw_auto_var.set(st.rbw_auto)
        if st.rbw_hz:
            self.rbw_var.set(f"{st.rbw_hz / 1e3:g}")
        if st.vbw_auto is not None:
            self.vbw_auto_var.set(st.vbw_auto)
        if st.vbw_hz:
            self.vbw_var.set(f"{st.vbw_hz / 1e3:g}")
        if st.ref_level_dbm is not None:
            self.rlev_var.set(f"{st.ref_level_dbm:g}")
        if st.atten_auto is not None:
            self.atten_auto_var.set(st.atten_auto)
        if st.atten_db is not None:
            self.atten_var.set(f"{st.atten_db:g}")
        if st.trigger:
            self.trigger_var.set(st.trigger if st.trigger in ("IMM", "EXT") else "IMM")
        if st.sweep_count:
            self.sweep_count_var.set(max(1, min(200, int(st.sweep_count))))
        if st.sweep_time_auto is not None:
            self.sweep_time_auto_var.set(st.sweep_time_auto)
        if st.sweep_time_s:
            self.sweep_time_var.set(f"{st.sweep_time_s:g}")
        if st.trace_mode:
            ui_mode = SCPI_TO_TRACE_MODE.get(st.trace_mode.strip().upper())
            if ui_mode:
                self.trace_mode_var.set(ui_mode)
        self.append_log("接收", f"回读: CENT={mhz(st.centre_hz)} SPAN={mhz(st.span_hz)} "
                                f"RBW={mhz(st.rbw_hz)} VBW={mhz(st.vbw_hz)} "
                                f"RLEV={st.ref_level_dbm}dBm ATT={st.atten_db}dB "
                                f"MODE={st.trace_mode} "
                                f"SWE:TIME={'AUTO' if st.sweep_time_auto else (f'{st.sweep_time_s:g}s' if st.sweep_time_s else '-')}")

    # ------------------------------------------------------------------ 迹线
    def _toggle_auto(self) -> None:
        self.auto_refresh = self.auto_var.get()
        if self.auto_refresh:
            self.on_read_trace()

    def on_read_trace(self) -> None:
        mode = self.trace_mode_var.get()              # 单扫 / 峰值保持 / 软件平均
        n_read = max(1, int(self.sweep_count_var.get())) if mode != "单扫" else 1

        def work() -> None:
            time.sleep(self.instr.sweep_settle_seconds(1))
            traces = []
            for i in range(n_read):
                start, stop, vals = self.instr.read_trace()
                traces.append(vals)
                if i < n_read - 1:
                    time.sleep(0.35)
            if mode == "峰值保持":
                values = [max(b) for b in zip(*traces)]      # 突发发包信号的正确观测方式
            elif mode == "软件平均":
                values = [sum(b) / len(b) for b in zip(*traces)]  # 仅适合连续/稳态信号
            else:
                values = traces[0]
            self._post(lambda: self._show_trace(start, stop, values, mode))
        if self._submit(work):
            self.append_log("状态", f"读取迹线中…（{mode}{'x%d' % n_read if n_read > 1 else ''}）")

    def _show_trace(self, start: float, stop: float, values: list[float],
                    mode: str = "单扫") -> None:
        self._trace_data = (start, stop, values)
        try:
            centre = float(self.centre_var.get()) * 1e6
        except (ValueError, tk.TclError):
            centre = (start + stop) / 2
        try:
            ch_bw = float(self.ch_bw_var.get()) * 1e6
        except (ValueError, tk.TclError):
            ch_bw = 20e6
        try:
            ext_att = float(self.ext_att_var.get())
        except (ValueError, tk.TclError):
            ext_att = 0.0
        stats = trace_stats(start, stop, values, centre, ch_bw, ext_att)
        peak_mhz = stats["peak_hz"] / 1e6
        ch_p = stats.get("channel_power_dbm")
        dut_p = stats.get("dut_channel_power_dbm")
        # 过载预警：峰值接近参考电平提示加大衰减
        overload_hint = ""
        try:
            rlev = float(self.rlev_var.get())
            if stats["peak_dbm"] >= rlev - 3.0:
                overload_hint = " ⚠ 峰值接近/超过参考电平，可能中频过载，请加大输入衰减(ATT≥15)或抬高参考电平"
                self.append_log("注意", f"峰值 {stats['peak_dbm']:.1f}dBm 接近参考电平 {rlev:g}dBm，"
                                        f"可能过载失真，读数不可信——请加大输入衰减")
        except (ValueError, tk.TclError):
            pass
        mode_note = "（峰值保持包络=突发信号的包络功率上限，非平均功率）" if mode == "峰值保持" else ""
        info = (f"[{mode}] 峰值 {stats['peak_dbm']:.2f} dBm @ {peak_mhz:.3f} MHz | "
                f"信道功率 {ch_p:.2f} dBm | DUT口(含{ext_att:g}dB补偿) "
                f"{dut_p:.2f} dBm{mode_note}" if ch_p is not None and math.isfinite(ch_p)
                else f"[{mode}] 峰值 {stats['peak_dbm']:.2f} dBm @ {peak_mhz:.3f} MHz")
        info += overload_hint
        self.trace_info_var.set(info)
        self.append_log("结果", f"迹线 {stats['points']} 点 ({start / 1e6:.4g}~{stop / 1e6:.4g} MHz)；{info}")
        self._draw_trace()
        if self.auto_refresh:
            self.root.after(max(1, self.interval_var.get()) * 1000,
                            lambda: self.on_read_trace() if self.auto_refresh else None)

    def _draw_empty(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 120)
        c.create_text(w // 2, h // 2, text="连接仪表后点击「读取迹线」",
                      fill="#999999", font=("", 12))

    def _draw_trace(self) -> None:
        if not self._trace_data:
            self._draw_empty()
            return
        start, stop, values = self._trace_data
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), 300)
        h = max(c.winfo_height(), 200)
        ml, mr, mt, mb = 62, 16, 14, 34
        pw, ph = w - ml - mr, h - mt - mb
        n = len(values)
        vmin, vmax = min(values), max(values)
        if vmax - vmin < 10:
            vmax, vmin = vmin + 10, vmin - 0
        pad = (vmax - vmin) * 0.08
        vmax += pad
        vmin -= pad
        vmax = math.ceil(vmax / 10.0) * 10
        vmin = math.floor(vmin / 10.0) * 10

        def X(i: int) -> float:
            return ml + pw * i / (n - 1)

        def Y(v: float) -> float:
            return mt + ph * (1 - (v - vmin) / (vmax - vmin))

        # 网格与刻度
        for k in range(11):
            v = vmin + (vmax - vmin) * k / 10
            y = Y(v)
            c.create_line(ml, y, ml + pw, y, fill="#eeeeee" if k else "#999999")
            c.create_text(ml - 6, y, text=f"{v:.0f}", anchor="e", fill="#666666")
        for k in range(9):
            f = start + (stop - start) * k / 8
            x = ml + pw * k / 8
            c.create_line(x, mt, x, mt + ph, fill="#eeeeee")
            c.create_text(x, mt + ph + 8, text=f"{f / 1e6:.4g}", fill="#666666")
        c.create_text(ml + pw / 2, mt + ph + 24, text="频率 (MHz)", fill="#444444")
        c.create_text(14, mt + ph / 2, text="功率 (dBm)", fill="#444444", angle=90)

        # 迹线
        points = []
        for i, v in enumerate(values):
            points.extend((X(i), Y(v)))
        c.create_line(*points, fill="#cc2222", width=1)

        # 峰值标记
        pi = max(range(n), key=lambda i: values[i])
        px, py = X(pi), Y(values[pi])
        c.create_oval(px - 4, py - 4, px + 4, py + 4, outline="#cc0000", width=2)
        label = f"{values[pi]:.1f} dBm @ {((start + (stop - start) * pi / (n - 1)) / 1e6):.3f} MHz"
        lx = min(px + 8, ml + pw - 150)
        c.create_text(lx, py - 10, text=label, anchor="w", fill="#cc0000", font=("", 9, "bold"))

        # 中心频率竖线
        try:
            centre = float(self.centre_var.get()) * 1e6
            if start <= centre <= stop:
                cx = ml + pw * (centre - start) / (stop - start)
                c.create_line(cx, mt, cx, mt + ph, fill="#8888cc", dash=(4, 3))
                c.create_text(cx, mt + 6, text="CENT", fill="#6666aa", font=("", 8))
        except (ValueError, tk.TclError):
            pass

    def on_save_csv(self) -> None:
        if not self._trace_data:
            messagebox.showinfo("提示", "尚未读取迹线", parent=self.root)
            return
        start, stop, values = self._trace_data
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=f"fph_trace_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        n = len(values)
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["index", "freq_hz", "freq_mhz", "power_dbm"])
            for i, v in enumerate(values):
                f = start + (stop - start) * i / (n - 1)
                writer.writerow([i, f"{f:.1f}", f"{f / 1e6:.6f}", f"{v:.2f}"])
        self.append_log("结果", f"迹线已保存: {path}（{n} 点）")

    # ------------------------------------------------------------------ 退出
    def _on_close(self) -> None:
        try:
            if self.instr.connected:
                try:
                    self.instr.restore()
                except Exception:
                    pass
                self.instr.disconnect()
        finally:
            self.jobs.put(None)
            self.root.destroy()

    def run(self) -> None:
        self.append_log("状态", "FPH 仪表设置工具启动；默认资源串 TCPIP::192.168.29.2::INSTR "
                                "(VXI-11, 纯 Python 后端)")
        self.root.mainloop()

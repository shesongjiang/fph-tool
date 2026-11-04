"""R&S Spectrum Rider FPH SCPI 仪表封装。

所有命令均已在真机 (1321.1111K13, FW V2.10) 上验证：
- FREQ:CENT/SPAN (Hz), FREQ:START?/STOP?
- BAND:RES / BAND:VID (Hz) 及 :AUTO
- DISP:TRAC:Y:RLEV, INP:ATT 及 :AUTO (RLEV 与 ATT 耦合: ATT=0 时 RLEV 会被 -222 拒绝)
- TRIG:SOUR IMM, DISP:TRAC:MODE AVER, SWE:COUN
- TRAC:DATA? TRACE1 (711 点), SWE:POIN?, SWE:TIME?
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import pyvisa

DEFAULT_RESOURCE = "TCPIP::192.168.29.2::INSTR"
DEFAULT_BACKEND = "@py"
TIMEOUT_MS = 6000


class InstrumentError(RuntimeError):
    pass


@dataclass
class FphSettings:
    """一次可下发的仪表参数集合（None 表示不修改该项）。"""
    centre_hz: float | None = None
    span_hz: float | None = None
    rbw_auto: bool | None = None
    rbw_hz: float | None = None
    vbw_auto: bool | None = None
    vbw_hz: float | None = None
    ref_level_dbm: float | None = None
    atten_auto: bool | None = None
    atten_db: float | None = None
    trigger: str | None = None          # IMM / EXT
    trace_mode: str | None = None       # DISP:TRAC:MODE WRITE / MAXH / AVER
    sweep_count: int | None = None
    sweep_time_auto: bool | None = None  # SWE:TIME:AUTO（True 自动 / False 手动）
    sweep_time_s: float | None = None    # SWE:TIME 手动扫描时间（秒）


@dataclass
class FphStatus:
    """仪表当前状态回读。"""
    idn: str = ""
    centre_hz: float | None = None
    span_hz: float | None = None
    start_hz: float | None = None
    stop_hz: float | None = None
    rbw_hz: float | None = None
    rbw_auto: bool | None = None
    vbw_hz: float | None = None
    vbw_auto: bool | None = None
    ref_level_dbm: float | None = None
    atten_db: float | None = None
    atten_auto: bool | None = None
    trigger: str = ""
    sweep_count: int | None = None
    sweep_points: int | None = None
    sweep_time_s: float | None = None
    sweep_time_auto: bool | None = None
    trace_mode: str = ""
    raw: dict[str, str] = field(default_factory=dict)


def _to_hz(value: str) -> float | None:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


class FphInstrument:
    """单线程使用的 FPH SCPI 控制器（GUI 侧由工作线程串行调用）。"""

    def __init__(self, log: Callable[[str, str], None]) -> None:
        self.log = log
        self.rm: pyvisa.ResourceManager | None = None
        self.inst: pyvisa.MessageBasedResource | None = None
        self._snapshot: dict[str, str] | None = None

    # -- 连接 ---------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.inst is not None

    def connect(self, resource: str = DEFAULT_RESOURCE, timeout_ms: int = TIMEOUT_MS) -> str:
        self.disconnect()
        self.rm = pyvisa.ResourceManager(DEFAULT_BACKEND)
        self.inst = self.rm.open_resource(resource, timeout=timeout_ms)
        self.inst.read_termination = "\n"
        idn = self.query("*IDN?").strip()
        if not idn:
            raise InstrumentError("仪表无 *IDN? 回复，请检查网络连接")
        self.log("状态", f"已连接 {resource}: {idn}")
        return idn

    def disconnect(self) -> None:
        if self.inst is not None:
            try:
                self.inst.close()
            except Exception:
                pass
            self.inst = None
        if self.rm is not None:
            try:
                self.rm.close()
            except Exception:
                pass
            self.rm = None

    # -- 底层读写 -----------------------------------------------------------
    def _check_inst(self) -> None:
        if self.inst is None:
            raise InstrumentError("仪表未连接")

    def write(self, command: str) -> None:
        self._check_inst()
        self.inst.write(command)

    def query(self, command: str, tries: int = 2) -> str:
        self._check_inst()
        last: Exception | None = None
        for attempt in range(tries):
            try:
                return self.inst.query(command).strip()
            except Exception as exc:  # 超时重试一次
                last = exc
                self.log("状态", f"查询 {command} 超时，重试 {attempt + 1}/{tries}")
                try:
                    self.inst.clear()
                except Exception:
                    pass
        raise InstrumentError(f"指令 {command} 失败: {last}")

    def query_float(self, command: str) -> float | None:
        return _to_hz(self.query(command))

    def syst_err(self) -> str:
        try:
            return self.query("SYST:ERR?", tries=1)
        except InstrumentError:
            return "?"

    # -- 状态快照 / 恢复 -----------------------------------------------------
    SNAPSHOT_KEYS = (
        "FREQ:CENT?", "FREQ:SPAN?", "DISP:TRAC:Y:RLEV?",
        "BAND:RES:AUTO?", "BAND:RES?", "BAND:VID:AUTO?", "BAND:VID?",
        "INP:ATT:AUTO?", "INP:ATT?", "TRIG:SOUR?", "DISP:TRAC:MODE?",
        "SWE:COUN?", "SWE:TIME?", "SWE:TIME:AUTO?",
    )

    def snapshot(self) -> dict[str, str]:
        snap: dict[str, str] = {}
        for key in self.SNAPSHOT_KEYS:
            try:
                snap[key] = self.query(key, tries=1)
            except InstrumentError:
                snap[key] = ""
        self._snapshot = snap
        self.log("状态", "已保存仪表当前设置快照（连接时）")
        return snap

    def restore(self) -> int:
        if not self._snapshot:
            raise InstrumentError("没有可恢复的快照（需先连接）")
        ok = 0
        for key, value in self._snapshot.items():
            if not value:
                continue
            base = key.rstrip("?")
            try:
                self.write(f"{base} {value}")
                ok += 1
            except Exception as exc:
                self.log("错误", f"恢复 {base} <- {value} 失败: {exc}")
        self.write("*WAI")
        time.sleep(0.5)
        self.log("状态", f"已恢复连接时的仪表设置（{ok} 项）")
        return ok

    # -- 参数下发 -----------------------------------------------------------
    def apply(self, s: FphSettings) -> list[tuple[str, str, str]]:
        """按可靠顺序下发参数；每项 (命令, 回读值, 结论)。RLEV 失败时自动开衰减重试。"""
        results: list[tuple[str, str, str]] = []

        def step(command: str, readback_cmd: str) -> None:
            self.write(command)
            rb = self.query(readback_cmd)
            err = self.syst_err()
            verdict = "OK" if err.startswith('0,') or err.startswith('0 ') else f"SYST:ERR {err}"
            results.append((command, rb, verdict))
            if verdict != "OK":
                self.log("注意", f"{command} -> 回读 {rb}，{err}")

        if s.trigger:
            step(f"TRIG:SOUR {s.trigger}", "TRIG:SOUR?")
        # 带宽：先关 AUTO 再写值
        if s.rbw_auto is False and s.rbw_hz is not None:
            step("BAND:RES:AUTO 0", "BAND:RES:AUTO?")
            step(f"BAND:RES {int(s.rbw_hz)}", "BAND:RES?")
        elif s.rbw_auto is True:
            step("BAND:RES:AUTO 1", "BAND:RES:AUTO?")
        if s.vbw_auto is False and s.vbw_hz is not None:
            step("BAND:VID:AUTO 0", "BAND:VID:AUTO?")
            step(f"BAND:VID {int(s.vbw_hz)}", "BAND:VID?")
        elif s.vbw_auto is True:
            step("BAND:VID:AUTO 1", "BAND:VID:AUTO?")
        # 衰减先于参考电平（RLEV 与 ATT 耦合）
        if s.atten_auto is True:
            step("INP:ATT:AUTO 1", "INP:ATT:AUTO?")
        elif s.atten_auto is False:
            step("INP:ATT:AUTO 0", "INP:ATT:AUTO?")
            if s.atten_db is not None:
                step(f"INP:ATT {s.atten_db:g}", "INP:ATT?")
        if s.ref_level_dbm is not None:
            self.write(f"DISP:TRAC:Y:RLEV {s.ref_level_dbm:g}")
            rb = self.query("DISP:TRAC:Y:RLEV?")
            err = self.syst_err()
            if not (err.startswith('0,') or err.startswith('0 ')):
                # RLEV 与衰减耦合被拒：自动开启衰减后重试一次
                self.log("注意", f"RLEV {s.ref_level_dbm:g} 被拒({err})，尝试自动衰减后重试")
                self.write("INP:ATT:AUTO 1")
                self.write(f"DISP:TRAC:Y:RLEV {s.ref_level_dbm:g}")
                rb = self.query("DISP:TRAC:Y:RLEV?")
                err = self.syst_err()
            results.append((f"DISP:TRAC:Y:RLEV {s.ref_level_dbm:g}", rb,
                            "OK" if err.startswith('0,') or err.startswith('0 ') else f"SYST:ERR {err}"))
        if s.span_hz is not None:
            step(f"FREQ:SPAN {int(s.span_hz)}", "FREQ:SPAN?")
        if s.centre_hz is not None:
            step(f"FREQ:CENT {int(s.centre_hz)}", "FREQ:CENT?")
        if s.trace_mode:
            step(f"DISP:TRAC:MODE {s.trace_mode}", "DISP:TRAC:MODE?")
        if s.sweep_time_auto is True:
            step("SWE:TIME:AUTO 1", "SWE:TIME:AUTO?")
        elif s.sweep_time_auto is False:
            step("SWE:TIME:AUTO 0", "SWE:TIME:AUTO?")
            if s.sweep_time_s is not None:
                step(f"SWE:TIME {s.sweep_time_s:g}", "SWE:TIME?")
        elif s.sweep_time_s is not None:
            # 未显式指定 AUTO：先关自动再写值，避免 AUTO 打开时写入被拒
            step("SWE:TIME:AUTO 0", "SWE:TIME:AUTO?")
            step(f"SWE:TIME {s.sweep_time_s:g}", "SWE:TIME?")
        if s.sweep_count is not None:
            step(f"SWE:COUN {int(s.sweep_count)}", "SWE:COUN?")
        self.write("*WAI")
        return results

    # -- 状态回读 -----------------------------------------------------------
    def read_status(self) -> FphStatus:
        st = FphStatus()
        st.idn = self.query("*IDN?")
        st.centre_hz = self.query_float("FREQ:CENT?")
        st.span_hz = self.query_float("FREQ:SPAN?")
        st.start_hz = self.query_float("FREQ:START?")
        st.stop_hz = self.query_float("FREQ:STOP?")
        st.rbw_auto = _auto(self.query("BAND:RES:AUTO?"))
        st.rbw_hz = self.query_float("BAND:RES?")
        st.vbw_auto = _auto(self.query("BAND:VID:AUTO?"))
        st.vbw_hz = self.query_float("BAND:VID?")
        st.ref_level_dbm = self.query_float("DISP:TRAC:Y:RLEV?")
        st.atten_auto = _auto(self.query("INP:ATT:AUTO?"))
        st.atten_db = self.query_float("INP:ATT?")
        st.trigger = self.query("TRIG:SOUR?")
        st.sweep_count = self.query_float("SWE:COUN?")
        st.sweep_points = self.query_float("SWE:POIN?")
        st.sweep_time_s = self.query_float("SWE:TIME?")
        st.sweep_time_auto = _auto(self.query("SWE:TIME:AUTO?"))
        st.trace_mode = self.query("DISP:TRAC:MODE?")
        for key in self.SNAPSHOT_KEYS:
            try:
                st.raw[key] = self.query(key, tries=1)
            except InstrumentError:
                st.raw[key] = ""
        return st

    # -- 迹线 -----------------------------------------------------------------
    def read_trace(self) -> tuple[float, float, list[float]]:
        """返回 (起始频率Hz, 终止频率Hz, 功率dBm列表)。"""
        start = self.query_float("FREQ:START?")
        stop = self.query_float("FREQ:STOP?")
        if start is None or stop is None or stop <= start:
            raise InstrumentError(f"频率轴异常: START={start} STOP={stop}")
        raw = self.query("TRAC:DATA? TRACE1", tries=3)
        values: list[float] = []
        for token in raw.replace("\n", "").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                values.append(float(token))
            except ValueError:
                continue
        if len(values) < 100:
            raise InstrumentError(f"迹线点数异常({len(values)}), 原始数据: {raw[:120]!r}")
        bad = [v for v in values if not math.isfinite(v)]
        if bad:
            raise InstrumentError(f"迹线含 {len(bad)} 个非数值点")
        return start, stop, values

    # -- 测量辅助 -------------------------------------------------------------
    def sweep_settle_seconds(self, sweeps: int = 1) -> float:
        t = self.query_float("SWE:TIME?")
        return max(1.0, t * sweeps + 0.5)


def _auto(value: str) -> bool | None:
    v = value.strip().upper()
    if v in ("1", "ON", "TRUE"):
        return True
    if v in ("0", "OFF", "FALSE"):
        return False
    return None


def band_power_dbm(freqs: list[float], powers: list[float],
                   lo: float, hi: float) -> float:
    """[lo, hi] 区间线性积分功率 (dBm)。"""
    total = sum(10 ** (p / 10.0) for f, p in zip(freqs, powers) if lo <= f <= hi)
    return 10 * math.log10(total) if total > 0 else float("-inf")


def trace_stats(start: float, stop: float, values: list[float],
                centre_hz: float | None, channel_bw_hz: float | None,
                external_att_db: float) -> dict[str, Any]:
    """迹线统计：峰值 / 信道功率 / DUT 口功率(含外部衰减补偿)。"""
    n = len(values)
    freqs = [start + (stop - start) * i / (n - 1) for i in range(n)]
    peak_i = max(range(n), key=lambda i: values[i])
    stats: dict[str, Any] = {
        "points": n,
        "start_hz": start,
        "stop_hz": stop,
        "peak_dbm": values[peak_i],
        "peak_hz": freqs[peak_i],
        "external_att_db": external_att_db,
    }
    if centre_hz is not None and channel_bw_hz is not None:
        ch = band_power_dbm(freqs, values,
                            centre_hz - channel_bw_hz / 2,
                            centre_hz + channel_bw_hz / 2)
        stats["channel_power_dbm"] = ch
        stats["dut_channel_power_dbm"] = (ch + external_att_db
                                          if math.isfinite(ch) else None)
        # 削顶检测：峰值贴近参考电平(±1dB)提示
    stats["dut_peak_dbm"] = values[peak_i] + external_att_db
    return stats

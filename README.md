# FPH 仪表设置工具

R&S Spectrum Rider FPH 频谱仪远程控制工具（Windows GUI，Tkinter）。

- SCPI over VXI-11（pyvisa-py 纯 Python 后端，免装 VISA 驱动）
- 参数下发（频率 / Span / 扫描时间 / RBW / VBW / 参考电平 / 衰减 / 触发 / 迹线方式 / 平均次数），写入→回读→SYST:ERR 三重自检
- 迹线方式下发到仪表：单扫→WRITE / 峰值保持→MAXH / 软件平均→AVER
- 2.4G 信道一键预设（ch1–14）
- 迹线读取 / 绘制 / CSV 导出 / 自动刷新
- 连接时保存仪表设置快照，可一键恢复

真机验证：R&S Spectrum Rider FPH 1321.1111K13，FW V2.10
默认资源串：`TCPIP::192.168.29.2::INSTR`

## 运行

```bash
python main.py
```

## 构建 EXE（PyInstaller）

```bash
python build.py
```

输出：`dist/FPH仪表设置工具.exe`

依赖：pyvisa、pyvisa-py（运行时）；PyInstaller（构建时）。

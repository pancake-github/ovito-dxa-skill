# 任务清单与命令接口

辅助脚本面向 Python 3.10+，使用可导入 OVITO、NumPy、psutil 的 Python 环境。Notebook 入口通过 `%%writefile DXA_Multiprocessing.py` 和 `%run DXA_Multiprocessing.py` 调用可导入的 `run_dxa.main(argv=None)`，Windows 的进程池 worker 定义在 `run_dxa.py`，不要放入 Notebook 临时 `__main__`。`inspect_inputs.py` 只读检查元数据，不执行 DXA，将报告打印到标准输出；`run_dxa.py --config FILE [--mode baseline|benchmark|run|resume]` 读取 JSON 清单，省略 mode 默认为 run。相对路径相对于清单所在目录解析。脚本不会安装或升级依赖。

## 确认后的清单

顶层字段：`schema_version` 必须为 1；`confirmed` 必须为 true（表示使用者已确认科学设置及分组，不是工具代替使用者确认）；`output_dir` 是独立结果目录；`cases` 是非空列表；`resources` 为资源配置。

每个 case：
- `id`：唯一标识，仅英文字母、数字、下划线、连字符与点，不能为 `.` 或 `..`。
- `sources`：按确认顺序排列的非空文件路径列表；不允许未展开的通配符。单个文件也必须放在列表中。多文件表示同一组数据；独立数据放在不同 case。
- `input_format`：`lammps/data` 或 `lammps/dump`。第一版不接受 LAMMPS `%` 处理器分片。
- `atom_style`：data 无 Atoms 注释提示时必须明确；`atom_substyles` 用于 hybrid。
- `frames`：显式非负整数列表，或 `{"start":0,"stop":null,"step":1}`；stop 不包含在范围内，null 表示轨迹末尾。不得空集、越界或重复。
- `lattice`：`BCC`、`FCC`、`HCP`、`CubicDiamond` 或 `HexagonalDiamond`。
- `selection`：`{"mode":"all"}` 或 `{"mode":"types","type_ids":[1]}`。后者仅将已确认类型送入 DXA，不删除其他原子；不得凭元素名自动选择。
- `pbc`：dump 使用 null 保留文件中每帧边界，也可显式提供三个布尔值进行已确认的覆盖；data 必须提供三个布尔值。
- `units`：`length` 必须明确为 `angstrom`、`nm`、`m`、`reduced` 或 `unknown`；`time` 为已确认标签或 null。unknown/reduced 不进行 SI 换算。
- `time_mapping`：可省略；若提供，含正数 `timestep_size`、整数 `origin_step`、数值 `origin_time`。优先保留输入的 Time，缺失时才按 Timestep 映射；没有模拟步就不虚构物理时间。
- `dxa`：显式覆盖参数的对象，可为空（采用并记录当前版本实际默认值）；仅接受运行器支持的 DXA 搜索、平滑、粗化及完美位错选项。不允许静默忽略未知参数。
- `density`：`{"mode":"none"}`，或 `{"mode":"cell","confirmed":true}`。cell 表示已确认统计线长与整个盒体体积适配；含真空、局部空间选区和复杂有效体积先走专用方法，不仅扣分母。
- `output`：`{"level":"statistics"}` 或 `{"level":"geometry"}`。geometry 添加逐线记录及 CA 文件。

资源配置：`workers` 为 `"auto"`（默认）或正整数上限；`ovito_threads` 为正整数或 `"auto"`（默认 1）。显式 `ovito_threads="auto"` 在选择进程数时按每 worker 1 线程估算，实际仅有 1 个 worker 时使用 CPU 预算，多 worker 时每个 worker 仍为 1 线程。`chunk_size` 默认 1；`max_pending_factor` 默认 2；`reserve_cpu_fraction` 默认 0.1；`reserve_memory_fraction` 默认 0.25；`benchmark_max_workers` 默认 4；`benchmark_max_frames` 默认 6。

默认 CPU 预算是 `max(1, floor(当前进程可用逻辑 CPU × 0.9))`。实际 worker 数按 CPU 预算／线程数、`ceil(待计算帧数 / chunk_size)`、可用内存预算／单 worker 估算、平台上限及显式 `workers` 正整数上限（若有）取最小值，各除法上限向下取整；Windows 平台最多 61 个，其他平台上限为可用 CPU。每 worker 估算为 `384 MiB + 4096 B × 估计最大粒子数`，采用待处理工况首帧和可覆盖文本 dump 所有原子数头的最大值。内存预算基于当前可用内存的 75%，该数已经反映父进程占用，不再次扣减；总 RSS 展示估算为 `父 RSS + worker 数 × 单 worker 估算`。估算不是实测峰值保证，未覆盖格式的粒子数可能被低估。内存不足 1 worker 时明确停止；其他限制可缩减指定的 worker 上限并记录原因。恢复时先筛去成功帧，再重新读取资源，只估算仍待处理的工况。

默认直接处理所选帧，不进行额外串行 DXA 基线。独立单构型或资源限制可能导致实际仅 1 个 worker，这不是基线对照；可选 benchmark 才试跑代表帧。运行开始时打印资源选择，`execution.json.resource_plan` 记录请求和实际值、各上限、限制原因及估算覆盖范围。

## 输出与验收语义

- run/resume 共享 `output_dir/run`；baseline 写入 `output_dir/baseline`；benchmark 写入 `output_dir/benchmark`。新的运行不覆盖已有结果；resume 只接续可验证兼容的 run。
- 默认 run 执行所有已选择帧，不自动运行 baseline 或 benchmark；仅在用户要求对照时，baseline 和 benchmark 才抽取每组所选范围首、中、末的去重候选帧，并全局均匀限额至 `benchmark_max_frames`，未抽到的工况不表示经过验证；绝不自动对全量数据先做串行基线。
- `frames.csv`：case_id、frame、timestep、time、total_line_length、cell_volume、density、density_m2、recognized_fraction、status、error、worker_pid 等；长度与体积保留原始单位。状态区分成功非零、成功零位错、无晶态原子以及失败；缺失数值留空，不用 0 冒充失败。worker_pid 是实际计算该帧的操作系统进程号，仅用于诊断，不是持续的科学身份，也不参与结果一致性比较。
- `families.csv` 使用 case_id、frame、family、length 长表，不将 BCC 分类写死；geometry 的逐线标识只在该帧内有效。
- 每帧 JSON 成功原子落盘后才可作为检查点；CA 必须先完整写入。记录源文件 SHA256、帧映射、分析参数、OVITO/Python 版本及运行配置。resume 拒绝来源、科学设置或版本变化，也拒绝缺失/损坏的几何结果冒充完成。
- 用户按需执行 benchmark 时，输出 JSON、CSV 及可直接打开的独立 HTML 报告，呈现完整候选耗时、峰值 RSS、相对于单进程基线的倍率、结果一致性和推荐配置。HTML 用原生 SVG/表格，无外部依赖，无 Matplotlib 或绘图插件依赖。总耗时包含导入、读取、计算、结果传输及落盘；共同预检/哈希开销单列。
- 对照容差 rtol=1e-6、atol=1e-8；已知物理单位的长度、体积、密度分别换算为 Å、Å³、Å⁻² 后比较，无量纲比例单独比较。这样不会因使用 m 导致纳米尺度差异被绝对容差掩盖。unknown/reduced 保留原单位并记录限制。此转换仅用于验收，原始输出单位不变。不比较随机线 ID 或方向；超限不得自动放宽。仅代表帧通过数值与资源检查的候选可被推荐；全部无晶态原子不能成为合格推荐。最快的是本次样本结果，不是整条轨迹保证。

实际可运行的完整清单由仓库 `tests/make_examples.py` 随合成数据生成，没有虚构文件路径。

# OVITO DXA 优化指导技能

面向单组或多组 LAMMPS 数据的 DXA 位错提取与性能优化技能。技能先检查数据、询问必要的科学设置，再用代表帧比较串行和并行；不会假定使用者的数据按速率、取向或任何固定工况组合排列。

一组数据可以是一个静态构型，也可以是一条由一个或多个文件组成的轨迹。多个文件是否属于同一组由使用者确认。已有 DXA 脚本也可作为输入，先审查分析定义，再保留所需输出进行优化。

## 内容与适用边界

- 技能入口：[SKILL.md](skills/ovito-dxa/SKILL.md)。
- 输入检查：检查解释器、资源、源文件、帧及必要属性，不执行 DXA。
- DXA 运行：支持串行基线、基准比较、正式运行和兼容性检查后的续算。
- 输出：统计 CSV 长表、运行元数据与检查点；按需导出逐线属性和 CA 位错网络。
- 性能报告：独立 HTML、JSON 与 CSV，展示真实耗时、峰值 RSS、结果一致性及本次样本的推荐配置。没有提速也照实显示。
- 溶质分析：提供按需询问、专用代码生成及验证规则；第一版不内置通用氢距离、富集或位错追踪算法。

内置运行器处理 LAMMPS data/dump。其他格式先确认 OVITO 读者能力，再单独适配。参考晶格支持由实际 OVITO 版本决定；不对一般晶界、多相界面或不支持的晶格作越界解释。

## 安装技能

在支持 `skill-installer` 的 Codex 中，可以直接请求：

> 使用 skill-installer，从 https://github.com/pancake-github/ovito-dxa-skill/tree/main/skills/ovito-dxa 安装 ovito-dxa 技能。

安装目标是仓库中的 `skills/ovito-dxa` 目录，包含入口、参考资料及两个辅助脚本。安装后按宿主要求重新加载技能，再使用 `$ovito-dxa`。也可将该完整目录放入宿主的技能发现目录；不同机器不共享任何固定盘符。

技能不会替使用者安装或升级科学计算环境。请使用已有且能导入 `ovito`、`numpy`、`psutil` 的 Python 3.10 或更新解释器。OVITO 的版本能力需先检查；当前验收目标为 Windows、Python 3.10.19、OVITO 3.14.1。Linux 采用兼容实现，但测试状态以验收记录为准。

## 可运行的合成示例

在已正常激活 OVITO 环境的终端中获取仓库。以下命令使用仓库自身的路径，不指向私有研究数据：

```powershell
git clone https://github.com/pancake-github/ovito-dxa-skill.git # 获取技能和公开合成示例生成器。
Set-Location ovito-dxa-skill # 将当前目录切换到本仓库根目录。
python tests/make_examples.py --output examples/generated # 生成实际数据及与它们对应的完整清单。
```

生成器提供 `static.json`（单构型）、`task.json`（多组数据）和 `sequence.json`（分帧序列）。合成案例中的晶格、单位及周期边界仅适用于这些演示模型，不是使用真实数据时的默认值。

先进行只读检查，查看分组及缺失信息：

```powershell
python skills/ovito-dxa/scripts/inspect_inputs.py --paths examples/generated/bcc.dump examples/generated/fcc.data # 检查两组有效示例，不执行 DXA。
```

确认任务清单后比较代表样本：

```powershell
python skills/ovito-dxa/scripts/run_dxa.py --config examples/generated/task.json --mode benchmark # 比较同一组代表帧的串行与并行配置。
```

性能报告保存在清单 `output_dir` 下的 `benchmark` 目录，可在浏览器直接打开 HTML，无联网或绘图依赖。查看结果一致性、内存和耗时后，再根据用户授权运行完整选定帧：

```powershell
python skills/ovito-dxa/scripts/run_dxa.py --config examples/generated/task.json --mode run # 正式处理清单中选定的全部帧。
```

中断后使用相同清单和 `--mode resume`。输入、科学设置或软件版本发生变化时，不应复用旧结果；新建独立输出目录。运行器不会为了重跑自动删除已有结果。

## Notebook 入口

可直接打开 [分步 Notebook](examples/dxa_walkthrough.ipynb)，逐单元运行合成示例并查看性能报告。

从仓库根目录、已正常激活并能导入 OVITO 的环境启动 Notebook。Notebook 保留为操作界面，计算由独立解释器执行；`%run` 本身不提供并行，也不保证 Windows 进程池兼容。

```python
import sys  # 读取当前已验证 Notebook 环境的 Python 解释器路径。
import subprocess  # 从 Notebook 启动独立分析进程，避免将交互函数直接交给进程池。
from pathlib import Path  # 以跨平台方式构造实际文件路径。
repo = Path.cwd().resolve()  # 本示例要求 Notebook 当前目录是已克隆的仓库根目录。
runner = repo / "skills" / "ovito-dxa" / "scripts" / "run_dxa.py"  # 指向仓库中的真实运行脚本。
config = repo / "examples" / "generated" / "task.json"  # 指向上一步生成器已经创建的完整任务清单。
assert runner.is_file() and config.is_file(), "请先在仓库根目录生成合成示例。"  # 缺少真实文件时停止，避免猜测或虚构路径。
subprocess.run([sys.executable, str(runner), "--config", str(config), "--mode", "benchmark"], cwd=repo, check=True)  # 使用当前解释器运行基准比较，失败时明确抛出异常。
```

真实项目的清单应在预检与使用者确认后生成，不能直接把演示清单中的科学设置套到另一体系。完整字段和模式见 [任务清单](skills/ovito-dxa/references/task-manifest.md)。

## 阅读报告

倍率为串行基线耗时除以候选耗时，小于 1 表示候选更慢。默认基线是**此辅助脚本的单进程实现**，不是博客原版或使用者的旧代码；若没有对旧代码实测，不能报告“相对于旧代码提升”。

计时包含候选导入、读取、计算、传输和落盘；共同检查及哈希成本单列。峰值 RSS 是进程树定时采样估计，可能遗漏瞬时峰值，也可能重复计算共享页。首、中、末样本的最快配置不保证在整条轨迹上最快。

总线长、分类长度及定义明确的密度按固定容差比较；结果不一致的候选不被推荐。错误帧、无晶态原子与成功的零位错分别显示。线 ID 和方向可能变化，不能当作跨帧追踪。

## 当前验收状态

2026-11-09：已在 Windows、Python 3.10.19、OVITO 3.14.1 完成 12 项自动测试、Notebook 全部 6 个代码单元、两种独立技能行为场景，以及本机非零螺位错构型的串并行与 CA 回读验证。完整记录及边界见 [VALIDATION.md](VALIDATION.md)。Linux、HCP/diamond 实际构型尚未实测；不保证任何输入都提速。

在已激活的 OVITO 环境中可复现自动测试：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v # 生成临时合成输入并验证实际CLI与故障处理，不读取私人数据。
```

## 方法来源与许可

使用 [OVITO DXA 官方说明](https://docs.ovito.org/reference/pipelines/modifiers/dislocation_analysis.html)、[OVITO 性能指南](https://www.ovito.org/docs/current/python/introduction/scripting/performance.html)及 [Python 进程池文档](https://docs.python.org/3/library/concurrent.futures.html#processpoolexecutor)。问题背景参考[用户提供的 CSDN 文章](https://blog.csdn.net/wcget123/article/details/135202165)，本仓库独立实现代码，没有复制博客代码。

本仓库以 [MIT](LICENSE) 许可发布。OVITO 及其他依赖各自的许可仍适用；发表研究结果时应按照 OVITO 官方页面引用 DXA 算法论文。公共示例使用合成数据，私有研究数据不随仓库发布。

# OVITO DXA 优化指导技能

面向单组或多组 LAMMPS 数据的 DXA 位错提取与性能优化技能。技能先检查数据并确认必要的科学设置，默认直接运行多进程；仅在用户要求性能比较时对代表帧执行对照；不会假定使用者的数据按速率、取向或任何固定工况组合排列。

一组数据可以是一个静态构型，也可以是一条由一个或多个文件组成的轨迹。多个文件是否属于同一组由使用者确认。已有 DXA 脚本也可作为输入，先审查分析定义，再保留所需输出进行优化。

## 内容与适用边界

- 技能入口：[SKILL.md](skills/ovito-dxa/SKILL.md)。
- 输入检查：检查解释器、资源、源文件、帧及必要属性，不执行 DXA。
- DXA 运行：支持默认多进程正式运行、兼容性检查后的续算，以及按需对代表帧进行性能比较。
- 输出：统计 CSV 长表、运行元数据与检查点；按需导出逐线属性和 CA 位错网络。
- 性能报告：独立 HTML、JSON 与 CSV，按需展示代表帧的真实耗时、峰值 RSS、结果一致性及本次样本的推荐配置。没有实测或未通过比较时不声称提速。
- 溶质分析：提供按需询问、专用代码生成及验证规则；第一版不内置通用氢距离、富集或位错追踪算法。

内置运行器处理 LAMMPS data/dump。其他格式先确认 OVITO 读者能力，再单独适配。参考晶格支持由实际 OVITO 版本决定；不对一般晶界、多相界面或不支持的晶格作越界解释。

## 安装技能

在支持 `skill-installer` 的 Codex 中，可以直接请求：

> 使用 skill-installer，从 https://github.com/pancake-github/ovito-dxa-skill/tree/v0.1.1/skills/ovito-dxa 安装 ovito-dxa 技能。

安装版本为 `v0.1.1`，安装目标是仓库中的 `skills/ovito-dxa` 目录，包含入口、参考资料及两个辅助脚本。安装后按宿主要求重新加载技能，再使用 `$ovito-dxa`。也可将该完整目录放入宿主的技能发现目录；不同机器不共享任何固定盘符。

技能不会替使用者安装或升级科学计算环境。请使用已有且能导入 `ovito`、`numpy`、`psutil` 的 Python 3.10 或更新解释器。OVITO 的版本能力需先检查；当前验收目标为 Windows、Python 3.10.19、OVITO 3.14.1。Linux 采用兼容实现，但测试状态以验收记录为准。

## 可运行的合成示例

在已正常激活 OVITO 环境的终端中获取仓库。以下命令使用仓库自身的路径，不指向私有研究数据：

```powershell
git clone --branch v0.1.1 --single-branch https://github.com/pancake-github/ovito-dxa-skill.git # 获取指定版本和公开合成示例生成器。
Set-Location ovito-dxa-skill # 将当前目录切换到本仓库根目录。
python tests/make_examples.py --output examples/generated # 生成实际数据及与它们对应的完整清单。
```

生成器提供 `static.json`（单构型）、`task.json`（多组数据）和 `sequence.json`（分帧序列）。合成案例中的晶格、单位及周期边界仅适用于这些演示模型，不是使用真实数据时的默认值。

先进行只读检查，查看分组及缺失信息：

```powershell
python skills/ovito-dxa/scripts/inspect_inputs.py --paths examples/generated/bcc.dump examples/generated/fcc.data # 检查两组有效示例，不执行 DXA。
```

确认任务清单后直接处理全部选定帧，默认不先执行串行基线或性能比较：

```powershell
python skills/ovito-dxa/scripts/run_dxa.py --config examples/generated/task.json # 默认使用 run 模式及清单中的资源设置。
```

默认资源起点为 2 个 worker、每个 worker 使用 1 个 OVITO 线程；实际数量受到可用 CPU 和任务数等限制。只有一个独立构型或只允许使用 1 个 CPU 时，退为 1 个 worker 是资源或任务限制，不是额外串行基线。用户确实需要比较性能时，才可选用 benchmark；它从每组所选范围选首、中、末去重候选帧，默认再全局均匀限额至 6 帧；未抽到的工况并未因此完成性能验证，不对全部帧执行串行基线。

```powershell
python skills/ovito-dxa/scripts/run_dxa.py --config examples/generated/task.json --mode benchmark # 仅按需比较代表帧，结果写入 benchmark 目录。
```

benchmark 的 HTML 报告可在浏览器直接打开，无联网或绘图依赖；未执行或对照失败时不能声称提速。
中断后使用相同清单和 `--mode resume`。输入、科学设置或软件版本发生变化时，不应复用旧结果；新建独立输出目录。运行器不会为了重跑自动删除已有结果。

## Notebook 入口

从仓库根目录、已正常激活并能导入 OVITO 的环境启动 Notebook。为保证每次演示有独立结果目录，第一个单元生成全新的合成输入，并复制执行所需的两个运行器模块：

```python
from datetime import datetime  # 用当前时间生成新的演示目录名称。
from pathlib import Path  # 处理当前仓库与示例目录。
import shutil  # 复制仓库中的运行器文件。
import subprocess  # 调用示例生成器创建真实演示数据。
import sys  # 使用当前 Notebook 的 Python 解释器。
repo = Path.cwd().resolve()  # 此单元要求当前目录为已克隆的仓库根目录。
generator = repo / "tests" / "make_examples.py"  # 指向仓库自带的示例生成器。
assert generator.is_file(), "请先将 Notebook 工作目录切换到仓库根目录。"  # 在找不到实际生成器时停止。
work_dir = repo / "examples" / ("notebook_generated_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))  # 创建本次独立的示例工作路径。
subprocess.run([sys.executable, str(generator), "--output", str(work_dir)], check=True)  # 生成可实际读取的数据和任务清单。
runtime_dir = work_dir / "dxa_runtime"  # 设置此次演示的运行器目录。
runtime_dir.mkdir()  # 创建新的运行器目录，不覆盖已有内容。
for name in ("run_dxa.py", "inspect_inputs.py"):  # 依次复制运行器与其输入检查模块。
    shutil.copy2(repo / "skills" / "ovito-dxa" / "scripts" / name, runtime_dir / name)  # 保留模块名称供子进程导入。
```

第二个单元进入刚生成的目录：

```python
%cd -q $work_dir
```

第三个单元用 IPython 单元魔法命令写入薄入口。工作函数由相邻 `dxa_runtime` 中可导入的 `run_dxa.py` 提供：

```python
%%writefile DXA_Multiprocessing.py
__spec__ = None  # 将 IPython 重复执行的入口明确标为脚本，供 Windows spawn 读取。
from pathlib import Path  # 读取本次生成的示例目录。
import sys  # 设置模块搜索路径，并保留可选命令行参数。
import multiprocessing as mp  # 使用 Windows 标准入口保护。
if __name__ == "__main__":  # 只在入口程序中启动计算，避免子进程重复启动。
    work_dir = Path(__file__).resolve().parent  # 使用脚本所在目录，不依赖后续工作目录变化。
    runtime_dir = work_dir / "dxa_runtime"  # 定位前一步复制的运行器模块。
    config = work_dir / "task.json"  # 使用前一步已实际生成的演示清单。
    if not (runtime_dir / "run_dxa.py").is_file() or not config.is_file():  # 确认运行器和清单确实存在。
        raise FileNotFoundError("请先运行生成示例目录的单元。")  # 缺少输入时停止，避免猜测路径。
    sys.path.insert(0, str(runtime_dir))  # 让父进程及 spawn 子进程可以导入运行器模块。
    import run_dxa  # 从可导入模块加载入口，worker 也由该模块提供。
    if Path(run_dxa.__file__).resolve() != (runtime_dir / "run_dxa.py").resolve():  # 防止同一内核缓存了其他目录的旧模块。
        raise RuntimeError("当前内核已加载另一份 run_dxa；请重启内核后从头执行。")  # 避免静默使用旧实现。
    mp.freeze_support()  # 保留 Windows 独立脚本入口保护。
    run_dxa.main(["--config", str(config), *sys.argv[1:]])  # 默认执行 run；可选参数按原样传给运行器。
```

第四个单元执行已保存的薄入口：

```python
%run DXA_Multiprocessing.py
```

`%%writefile` 用于写文件，`%run` 用于执行脚本；多进程来自运行器中的进程池，单靠 `%run` 不会创建并行。工作函数必须位于可导入的 `run_dxa.py` 模块，不能放在 Notebook 临时 `__main__` 中。实际环境仍应通过小规模运行检查导入、输出和结果状态。
真实项目的清单应在预检与使用者确认后生成，不能直接把演示清单中的科学设置套到另一体系。完整字段和模式见 [任务清单](skills/ovito-dxa/references/task-manifest.md)。

## 阅读报告

按需执行 benchmark 时，倍率为代表帧串行基线耗时除以候选耗时，小于 1 表示候选更慢。benchmark 的基线是**此辅助脚本的单进程实现**，不是博客原版或使用者的旧代码；若没有对旧代码实测，不能报告“相对于旧代码提升”。

计时包含候选导入、读取、计算、传输和落盘；共同检查及哈希成本单列。峰值 RSS 是进程树定时采样估计，可能遗漏瞬时峰值，也可能重复计算共享页。首、中、末候选帧经全局限额后的样本最快配置不保证在整条轨迹上最快；未抽到的工况不在本次性能验证范围内。

总线长、分类长度及定义明确的密度按固定容差比较；结果不一致的候选不被推荐。错误帧、无晶态原子与成功的零位错分别显示。线 ID 和方向可能变化，不能当作跨帧追踪。

## 当前验收状态

验收数据、实际测试配置及尚未覆盖的环境见 [VALIDATION.md](VALIDATION.md)。资源设置和 Notebook 入口变更后，应以对应版本的最新实测记录为准；不能把先前版本的结果直接视为新版本的验收。

在已激活的 OVITO 环境中可复现自动测试：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v # 生成临时合成输入并验证实际CLI与故障处理，不读取私人数据。
```

## 方法来源与许可

使用 [OVITO DXA 官方说明](https://docs.ovito.org/reference/pipelines/modifiers/dislocation_analysis.html)、[OVITO 性能指南](https://www.ovito.org/docs/current/python/introduction/scripting/performance.html)及 [Python 进程池文档](https://docs.python.org/3/library/concurrent.futures.html#processpoolexecutor)。问题背景参考[用户提供的 CSDN 文章](https://blog.csdn.net/wcget123/article/details/135202165)，本仓库独立实现代码，没有复制博客代码。

本仓库以 [MIT](LICENSE) 许可发布。OVITO 及其他依赖各自的许可仍适用；发表研究结果时应按照 OVITO 官方页面引用 DXA 算法论文。公共示例使用合成数据，私有研究数据不随仓库发布。

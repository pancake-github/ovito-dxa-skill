"""通过真实 CLI 与 OVITO 验证输入、计算、导出和续算的行为。"""  # 测试科学不变量而不是文案。
import copy  # 隔离不同测试的配置改动。
import csv  # 读取真实输出表格。
import json  # 构造完整任务清单。
import os  # 向独立进程传递 UTF-8 设置。
from pathlib import Path  # 解析公开仓库路径。
import subprocess  # 验证真实 CLI 和 spawn 行为。
import sys  # 使用与测试相同的已激活解释器。
import tempfile  # 隔离每次测试产生的数据。
import unittest  # 不额外依赖第三方测试框架。
from make_examples import generate  # 生成明确来源的合成数据。

ROOT = Path(__file__).resolve().parents[1]  # 仓库根目录。
RUNNER = ROOT / "skills/ovito-dxa/scripts/run_dxa.py"  # 运行器真实位置。
INSPECTOR = ROOT / "skills/ovito-dxa/scripts/inspect_inputs.py"  # 输入检查器真实位置。


class IntegrationTests(unittest.TestCase):  # 针对可观察输出进行端到端验收。
    @classmethod  # 每组测试只生成一份输入。
    def setUpClass(cls):  # 创建隔离目录及完整示例。
        cls.temporary = tempfile.TemporaryDirectory(prefix="ovito-dxa-tests-")  # 系统临时目录完全由本测试拥有。
        cls.folder = generate(Path(cls.temporary.name) / "inputs")  # 在空子目录生成示例。
        cls.base = json.loads((cls.folder / "task.json").read_text(encoding="utf-8"))  # 读取真实可运行清单。

    @classmethod  # 所有测试完成后清理自有临时目录。
    def tearDownClass(cls):  # 不删除任何用户目录。
        cls.temporary.cleanup()  # TemporaryDirectory 只清理自己创建的目录。

    def invoke(self, script, *args, success=True):  # 使用真实独立解释器执行CLI。
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")  # 让中英文报告编码确定。
        result = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True, encoding="utf-8", env=env, timeout=240)  # 保留失败输出用于诊断。
        if success:  # 成功路径必须返回零状态。
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)  # 返回码与输出共同作为证据。
        else:  # 负面测试必须明确失败。
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)  # 不允许静默吞掉错误。
        return result  # 返回报告供进一步检查。

    def config(self, name, value=None):  # 将清单绑定到独立输出目录。
        payload = copy.deepcopy(value if value is not None else self.base)  # 避免污染其他测试。
        payload["output_dir"] = str(Path(self.temporary.name) / name)  # 每个测试拥有独立结果。
        path = self.folder / f"test_{name}.json"  # 来源路径仍相对输入目录解析。
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")  # 写出已确认的完整合成任务。
        return path, Path(payload["output_dir"])  # 返回清单与输出目录。

    def rows(self, path):  # 读取 UTF-8 BOM 兼容的统计表。
        with path.open(encoding="utf-8-sig", newline="") as stream:  # 保留 CSV 字段语义。
            return list(csv.DictReader(stream))  # 返回小型测试结果。

    def test_inspect_valid_and_duplicate(self):  # 检查真实帧数和重复来源识别。
        result = self.invoke(INSPECTOR, "--paths", self.folder / "bcc.dump", self.folder / "bcc.dump", "--all-frames")  # 同一路径传入两次。
        report = json.loads(result.stdout)  # 读取机器可解析报告。
        self.assertEqual(report["inputs"][0]["frame_count"], 3)  # 单文件确有三个帧。
        self.assertTrue(report["duplicates"])  # 必须报告重复而不是重复分析。
        self.assertEqual(len(report["inputs"][0]["frames"]), 3)  # all-frames 确实检查每帧。

    def test_inspect_empty_and_truncated(self):  # 错误数据不能伪装成零位错。
        result = self.invoke(INSPECTOR, "--paths", self.folder / "empty.dump", self.folder / "truncated.dump", "--all-frames", success=False)  # 显式检查损坏输入。
        report = json.loads(result.stdout)  # 错误时也要保留结构化证据。
        self.assertTrue(all(item["status"] == "error" for item in report["inputs"]))  # 两种错误都应被检测。

    def test_inspect_gzip_and_style(self):  # 验证压缩轨迹与无类型提示的data。
        compressed = self.invoke(INSPECTOR, "--paths", self.folder / "bcc.dump.gz")  # 不手工解压输入。
        self.assertEqual(json.loads(compressed.stdout)["inputs"][0]["frame_count"], 3)  # 解码后帧数不变。
        explicit = self.invoke(INSPECTOR, "--paths", self.folder / "bcc_no_hint.data", "--atom-style", "atomic")  # 明确提供缺失的科学输入布局。
        self.assertTrue(json.loads(explicit.stdout)["inputs"][0]["questions"])  # data 周期边界与单位仍须确认。

    def test_manifest_requires_confirmation(self):  # 必要参数缺失时先停止。
        payload = copy.deepcopy(self.base)  # 从合法配置出发。
        payload["confirmed"] = False  # 模拟分组与科学设置尚未确认。
        path, out = self.config("unconfirmed", payload)  # 独立失败目录。
        self.invoke(RUNNER, "--config", path, "--mode", "run", success=False)  # 未确认不能执行。
        self.assertFalse((out / "run").exists())  # 不能留下貌似成功结果。

    def test_multicase_and_sequence_equivalence(self):  # 比较文件组织和并行方式不会改变结果。
        serial_payload = copy.deepcopy(self.base)  # 创建串行版本。
        serial_payload["resources"]["workers"] = 1  # 采用单进程参照。
        serial, serial_out = self.config("serial", serial_payload)  # 单文件多帧来源。
        self.invoke(RUNNER, "--config", serial, "--mode", "run")  # 执行全部有效帧。
        serial_rows = self.rows(serial_out / "run/frames.csv")  # 取得实测统计。
        self.assertEqual(len(serial_rows), 4)  # 三个BCC帧加一个FCC快照。
        self.assertTrue(all(row["status"] == "zero_dislocations" for row in serial_rows))  # 完整晶体应识别为正常零位错。
        self.assertTrue(all(float(row["recognized_fraction"]) > 0.99 for row in serial_rows))  # 零位错不能来自无法识别晶体。
        split = json.loads((self.folder / "sequence.json").read_text(encoding="utf-8"))  # 读取分帧版本。
        parallel, parallel_out = self.config("sequence", split)  # 使用两个工作进程。
        self.invoke(RUNNER, "--config", parallel, "--mode", "run")  # 执行分文件表示。
        parallel_rows = self.rows(parallel_out / "run/frames.csv")  # 取得同一轨迹的另一组织形式。
        bcc_rows = [row for row in serial_rows if row["case_id"] == "bcc_trajectory"]  # 仅比较对应案例。
        for a, b in zip(bcc_rows, parallel_rows):  # 保持逐帧时间信息对应。
            self.assertEqual((a["timestep"], a["time"], a["total_line_length"]), (b["timestep"], b["time"], b["total_line_length"]))  # 验证映射和科学量一致。

    def test_geometry_resume_and_parameter_guard(self):  # 检查几何完整性和续算兼容性。
        payload = json.loads((self.folder / "static.json").read_text(encoding="utf-8"))  # 使用轻量单构型。
        payload["cases"][0]["output"]["level"] = "geometry"  # 明确请求CA几何。
        path, out = self.config("geometry", payload)  # 创建独立运行目录。
        self.invoke(RUNNER, "--config", path, "--mode", "run")  # 首次分析与导出。
        ca = next((out / "run").rglob("*.ca"))  # 查找实际生成的网络。
        from ovito.io import import_file  # 仅测试进程导入，不干扰子进程线程初始化。
        data = import_file(str(ca)).compute()  # 用官方读取器再次加载。
        self.assertEqual(tuple(data.cell.pbc), (True, True, True))  # 周期信息必须保留。
        self.assertAlmostEqual(sum(line.length for line in data.dislocations.lines), 0.0)  # 完整晶体网络为空。
        self.invoke(RUNNER, "--config", path, "--mode", "resume")  # 无变化可以复用。
        checkpoint = next((out / "run/checkpoints").rglob("*.json"))  # 找到已经成功的测试检查点。
        damaged = json.loads(checkpoint.read_text(encoding="utf-8"))  # 读取检查点内容。
        damaged["total_line_length"] = 999.0  # 模拟仍可解析但内容损坏的JSON。
        checkpoint.write_text(json.dumps(damaged), encoding="utf-8")  # 不修改原始来源或摘要。
        self.invoke(RUNNER, "--config", path, "--mode", "resume")  # 摘要失配应触发重新计算。
        self.assertEqual(json.loads(checkpoint.read_text(encoding="utf-8"))["total_line_length"], 0.0)  # 不能复用伪造的成功数值。
        ca.unlink()  # 删除测试自有的几何文件来模拟不完整结果。
        self.invoke(RUNNER, "--config", path, "--mode", "resume")  # 缺几何必须重算而非跳过。
        self.assertTrue(ca.is_file())  # 导出恢复成功。
        changed = json.loads(path.read_text(encoding="utf-8"))  # 读取已执行配置。
        changed["cases"][0]["dxa"]["trial_circuit_length"] = 15  # 改动科学参数。
        path.write_text(json.dumps(changed), encoding="utf-8")  # 保留输出目录以测试保护。
        self.invoke(RUNNER, "--config", path, "--mode", "resume", success=False)  # 科学配置改变不能复用旧结果。

    def test_partial_failure_is_visible(self):  # 损坏帧必须有状态且不能冒充零值。
        payload = copy.deepcopy(self.base)  # 保留一条合成轨迹。
        payload["cases"] = [payload["cases"][0]]  # 不引入其他工况。
        payload["cases"][0]["sources"] = ["partial_failure.dump"]  # 中间帧含坏原子记录。
        path, out = self.config("partial", payload)  # 隔离失败结果。
        self.invoke(RUNNER, "--config", path, "--mode", "run", success=False)  # CLI应明确整体不完整。
        table = out / "run/frames.csv"  # 若读取器能索引轨迹，应保留逐帧状态。
        if table.exists():  # 部分格式可能在索引阶段直接拒绝损坏文件。
            rows = self.rows(table)  # 获取可用的失败证据。
            failures = [row for row in rows if row["status"] == "failed"]  # 不使用零线长判断失败。
            self.assertTrue(failures)  # 至少报告一个失败帧。
            self.assertTrue(all(row["total_line_length"] == "" for row in failures))  # 失败数值不得填零。

    def test_benchmark_artifacts(self):  # 基准必须包含真实数值对照和可打开报告。
        payload = copy.deepcopy(self.base)  # 限制基准候选以保持测试轻量。
        payload["resources"]["benchmark_max_workers"] = 2  # 覆盖串行与二进程。
        path, out = self.config("benchmark", payload)  # 每次使用新目录。
        self.invoke(RUNNER, "--config", path, "--mode", "benchmark")  # 执行完整计时路径。
        report = json.loads((out / "benchmark/report.json").read_text(encoding="utf-8"))  # 读取实际基准记录。
        self.assertTrue(report["recommendation"])  # 合成数据应至少有一个合格候选。
        self.assertTrue(all(row["consistent"] for row in report["candidates"]))  # 合格计算必须与基线一致。
        self.assertTrue((out / "benchmark/comparisons.csv").is_file())  # 保留可继续分析的数据表。
        page = (out / "benchmark/report.html").read_text(encoding="utf-8")  # 检查可视报告。
        self.assertIn("<svg", page)  # 报告包含原生性能图。

    def test_reader_index_truncation_is_rejected(self):  # 索引漏帧不能静默当作完整成功。
        self.invoke(INSPECTOR, "--paths", self.folder / "index_truncation.dump", "--all-frames", success=False)  # 对比文本帧头与实际发现帧数。
        payload = copy.deepcopy(self.base)  # 使用已确认的完整输入配置。
        payload["cases"] = [payload["cases"][0]]  # 只测试损坏轨迹。
        payload["cases"][0]["sources"] = ["index_truncation.dump"]  # 输入存在三个完整时间步头。
        path, out = self.config("index_truncation", payload)  # 使用独立错误输出位置。
        self.invoke(RUNNER, "--config", path, "--mode", "run", success=False)  # 不能只跑首帧并报告完成。


if __name__ == "__main__":  # 允许 unittest discover 或独立脚本执行。
    unittest.main(verbosity=2)  # 显示各验收场景和最终结果。

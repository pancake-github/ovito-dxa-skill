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

    def test_default_cli_runs_in_parallel_without_benchmark(self):  # 检查既不传执行模式也不配置资源时的真实入口。
        payload = copy.deepcopy(self.base)  # 保持合成案例及其已确认科学设置不变。
        payload.pop("resources", None)  # 完全省略资源字段，验证运行器自身默认值。
        path, out = self.config("defaults", payload)  # 隔离此次测试的输入清单和输出目录。
        self.invoke(RUNNER, "--config", path)  # 省略 --mode，直接执行真实 CLI。
        self.assertEqual({item.name for item in out.iterdir()}, {"run"})  # 不得隐式执行 baseline 或 benchmark。
        execution = json.loads((out / "run/execution.json").read_text(encoding="utf-8"))  # 读取真实采用的进程数及完成情况。
        plan = execution["resource_plan"]  # 使用运行器记录的实际资源预算核对自动进程数。
        self.assertEqual(plan["requested_workers"], "auto")  # 未指定资源时必须真正采用自动选择，而不是固定为两个进程。
        self.assertEqual(plan["scheduling"], "case_sequential")  # 默认先完成一个案例，再开始下一个案例。
        stages = plan["stages"]  # 各阶段分别记录真实资源约束和执行时间。
        self.assertEqual([stage["case_ids"] for stage in stages], [["bcc_trajectory"], ["fcc_snapshot"]])  # 案例顺序与确认清单一致，不能默认混池。
        self.assertEqual([stage["job_count"] for stage in stages], [3, 1])  # 第一案例三个帧，第二案例一个帧。
        self.assertEqual(execution["workers"], max(stage["workers"] for stage in stages))  # 顶层记录各阶段实际进程数的最大值。
        self.assertEqual(stages[0]["workers"], min(stages[0]["limits"][name] for name in ("cpu", "tasks", "memory", "platform")))  # BCC 阶段根据其本身的资源约束选择进程数。
        self.assertEqual(stages[0]["workers"], 3)  # 此测试机器资源充足时，三个 BCC 帧最多使用三个进程。
        self.assertEqual(stages[1]["workers"], 1)  # FCC 案例只有一个帧，应收窄为单进程。
        self.assertLessEqual(stages[0]["finished_unix"], stages[1]["started_unix"])  # 第二案例不得在第一案例结束前启动。
        self.assertEqual(execution["ovito_threads"], 1)  # 默认每个 OVITO 实例仅使用一个线程。
        self.assertEqual((execution["completed"], execution["failed"]), (4, 0))  # 三帧 BCC 和一帧 FCC 都已完成。
        rows = self.rows(out / "run/frames.csv")  # 从帧级输出验证进程号与结果状态。
        self.assertEqual(len(rows), 4)  # 每个输入帧恰有一条统计记录。
        self.assertTrue(all(row["status"] == "zero_dislocations" and int(row["worker_pid"]) > 0 for row in rows))  # 记录真实工作进程，零位错必须来自成功的晶体分析。
        fcc_checkpoint = next((out / "run/checkpoints/fcc_snapshot").glob("*.json"))  # 只删除测试自有结果中的一个小案例检查点。
        fcc_checkpoint.unlink()  # 模拟该帧尚未完成，而其他案例已经完成。
        self.invoke(RUNNER, "--config", path, "--mode", "resume")  # 真实续算仅需要重新计算一个 FCC 帧。
        resumed = json.loads((out / "run/execution.json").read_text(encoding="utf-8"))  # 获取续算时重新规划的资源。
        self.assertEqual((resumed["workers"], resumed["resource_plan"]["job_count"]), (1, 1))  # 单个待处理帧只使用一个进程。
        self.assertEqual(set(resumed["resource_plan"]["particle_estimates"]), {"fcc_snapshot"})  # 只有待处理案例参与本次内存估计。
        self.assertEqual([stage["case_ids"] for stage in resumed["resource_plan"]["stages"]], [["fcc_snapshot"]])  # 续算不应重新运行或创建已完成案例阶段。
        self.assertEqual(len(self.rows(out / "run/frames.csv")), 4)  # 重新汇总时必须保留已完成案例的三帧。
        self.invoke(RUNNER, "--config", path, "--mode", "resume")  # 已无待处理帧时再次恢复检查。
        empty = json.loads((out / "run/execution.json").read_text(encoding="utf-8"))  # 读取仅核验检查点的资源计划。
        self.assertEqual((empty["workers"], empty["resource_plan"]["job_count"]), (0, 0))  # 不为零任务启动空闲 worker。
        self.assertEqual(empty["resource_plan"]["stages"], [])  # 没有待处理帧时不能虚构案例执行阶段。
        self.assertEqual(len(self.rows(out / "run/frames.csv")), 4)  # 零任务恢复不应覆盖或清空已有的完整汇总。

    def test_default_single_frame_limits_worker_count(self):  # 单构型没有独立帧可供两个进程并行。
        payload = json.loads((self.folder / "static.json").read_text(encoding="utf-8"))  # 使用单帧 BCC 合成构型。
        payload.pop("resources", None)  # 不人为覆盖默认的进程配置。
        path, out = self.config("default_static", payload)  # 使用新的测试专属目录。
        self.invoke(RUNNER, "--config", path)  # 省略模式，走默认正式分析流程。
        execution = json.loads((out / "run/execution.json").read_text(encoding="utf-8"))  # 获取实际缩减后的资源配置。
        self.assertEqual((execution["workers"], execution["completed"], execution["failed"]), (1, 1, 0))  # 仅一帧时自动收窄为一个进程并成功处理。
        self.assertEqual({item.name for item in out.iterdir()}, {"run"})  # 单构型也不能隐式触发额外试跑。

    def test_cross_case_run_requires_matching_evidence(self):  # 默认逐工况，未经用户实测验证不能直接混池。
        payload = copy.deepcopy(self.base)  # 从有效的双案例测试清单出发。
        payload["resources"]["scheduling"] = "cross_case"  # 显式选择需要实测证据的调度模式。
        payload["resources"].pop("scheduling_evidence", None)  # 不提供小样本比较报告。
        path, out = self.config("cross_no_evidence", payload)  # 独立测试目录避免影响其他运行。
        result = self.invoke(RUNNER, "--config", path, success=False)  # 缺证据必须明确失败。
        self.assertIn("scheduling_evidence", result.stderr)  # 输出应指出缺少的证据字段。
        self.assertFalse((out / "run").exists())  # 不得创建貌似已经启动的正式运行目录。

    def test_optional_scheduling_evidence_controls_cross_case_run(self):  # 使用真实小样本报告决定是否允许跨工况运行。
        payload = copy.deepcopy(self.base)  # 保留两种已确认的独立合成案例。
        payload["resources"]["benchmark_max_frames"] = 2  # 两个案例各取一个样本，避免重型比较。
        path, out = self.config("scheduling", payload)  # 比较报告与正式运行共享同一份科学清单。
        self.invoke(RUNNER, "--config", path, "--mode", "scheduling")  # 仅显式请求时执行两策略双轮小样本对照。
        report_path = out / "scheduling/report.json"  # 读取实际生成且带摘要的报告。
        report = json.loads(report_path.read_text(encoding="utf-8"))  # 不篡改结果或伪造收益。
        self.assertEqual(len(report["sample_jobs"]), 2)  # 总共只抽取两个代表帧。
        self.assertEqual({job[0] for job in report["sample_jobs"]}, {"bcc_trajectory", "fcc_snapshot"})  # 两个案例均被采样。
        self.assertEqual([(row["round"], row["scheduling"]) for row in report["trials"]], [(1, "case_sequential"), (1, "cross_case"), (2, "cross_case"), (2, "case_sequential")])  # 第二轮交换策略执行顺序。
        self.assertTrue((out / "scheduling/report.html").is_file())  # 同时提供可视报告。
        payload["resources"]["scheduling"] = "cross_case"  # 明确请求以比较报告作为正式运行的依据。
        payload["resources"]["scheduling_evidence"] = str(report_path)  # 保留本次生成的原始报告路径。
        payload["output_dir"] = str(out)  # 保持比较时使用的测试专属输出目录。
        path.write_text(json.dumps(payload), encoding="utf-8")  # 不改动任何科学设置或源文件。
        if report["eligible_for_cross_case"]:  # 只有实测两轮满足条件时才应允许跨工况。
            self.invoke(RUNNER, "--config", path)  # 使用真实证据执行全部已确认帧。
            execution = json.loads((out / "run/execution.json").read_text(encoding="utf-8"))  # 查看正式运行的实际调度。
            self.assertEqual(execution["resource_plan"]["scheduling"], "cross_case")  # 确保确实采用获准的混池模式。
            self.assertEqual((execution["completed"], execution["failed"]), (4, 0))  # 不遗漏未抽样的正式帧。
        else:  # 结果不支持稳定收益时，必须拒绝而非伪造通过。
            self.invoke(RUNNER, "--config", path, success=False)  # 验证没有收益的真实报告不能授权混池。
            self.assertFalse((out / "run").exists())  # 不生成貌似成功的正式结果目录。

    def test_multicase_and_sequence_equivalence(self):  # 比较文件组织和并行方式不会改变结果。
        serial_payload = copy.deepcopy(self.base)  # 创建串行版本。
        serial_payload["resources"]["workers"] = 1  # 采用单进程参照。
        serial, serial_out = self.config("serial", serial_payload)  # 单文件多帧来源。
        self.invoke(RUNNER, "--config", serial, "--mode", "run")  # 执行全部有效帧。
        serial_execution = json.loads((serial_out / "run/execution.json").read_text(encoding="utf-8"))  # 读取显式单进程配置的实际执行情况。
        self.assertEqual(serial_execution["workers"], 1)  # 用户明确选择一个进程时不得强制改为默认的两个进程。
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
        self.assertEqual(report["context"]["sample_frame_count"], 4)  # 该示例共有四个代表帧，不应超过默认六帧限额。
        self.assertTrue(report["recommendation"])  # 合成数据应至少有一个合格候选。
        self.assertTrue(all(row["consistent"] for row in report["candidates"]))  # 合格计算必须与基线一致。
        self.assertTrue(all(row["workers"] * row["ovito_threads"] <= report["context"]["cpu_budget"] for row in report["candidates"]))  # 可选比较的实际配置也不能绕过 CPU 预留预算。
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

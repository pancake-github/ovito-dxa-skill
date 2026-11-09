"""验证单位相关的对照判定和进程池崩溃后的完整失败记录。"""  # 不运行 DXA。
import copy  # 隔离对照数据的改动。
from concurrent.futures import Future  # 模拟已经失败的工作任务。
from concurrent.futures.process import BrokenProcessPool  # 使用真实进程池故障类型。
import importlib.util  # 从独立脚本加载无 OVITO 副作用的工具函数。
import json  # 检查输入变化后写出的无效标记。
from pathlib import Path  # 定位仓库脚本。
import tempfile  # 隔离故障测试产物。
import unittest  # 使用标准库断言。
from unittest.mock import patch  # 仅替换进程池，不启动崩溃的真实子进程。

SCRIPT = Path(__file__).resolve().parents[1] / "skills/ovito-dxa/scripts/run_dxa.py"  # 定位被验收实现。
SPEC = importlib.util.spec_from_file_location("dxa_runner_test", SCRIPT)  # 构造独立模块说明。
DXA = importlib.util.module_from_spec(SPEC)  # 创建脚本模块对象。
SPEC.loader.exec_module(DXA)  # 加载函数定义但不运行命令入口。


class SafetyTests(unittest.TestCase):  # 覆盖独立审查发现的具体回归风险。
    def test_sample_jobs_preserves_first_middle_last_for_small_selection(self):  # 候选数在上限内时不额外删去代表帧。
        cases = [{"id": "long"}, {"id": "single"}]  # 分别覆盖长轨迹和单构型。
        frames = {"long": list(range(100)), "single": [7]}  # 100 帧应抽取三个代表帧，单帧仅出现一次。
        jobs = DXA._sample_jobs(cases, frames, 6)  # 按默认六帧上限选取。
        self.assertEqual(jobs, [("long", 0), ("long", 50), ("long", 99), ("single", 7)])  # 明确验证首、中、末和单帧去重。
        self.assertLess(len(jobs), sum(map(len, frames.values())))  # 长轨迹只取代表帧，不进行全帧试跑。

    def test_sample_jobs_respects_global_limit_across_cases(self):  # 多案例时六帧是全局上限，而非每案例上限。
        cases = [{"id": name} for name in ("a", "b", "c", "d")]  # 创建四个有独立帧序列的案例。
        frames = {case["id"]: list(range(100)) for case in cases}  # 每个案例都有首、中、末三个候选。
        jobs = DXA._sample_jobs(cases, frames, 6)  # 将十二个候选按全局顺序均匀限制到六个。
        self.assertEqual(jobs, [("a", 0), ("a", 99), ("b", 50), ("c", 0), ("c", 99), ("d", 99)])  # 验证确定性选取以及候选边界。
        self.assertEqual(len(set(jobs)), 6)  # 不重复，也不超过已确认的全局上限。

    def test_sample_jobs_handles_small_candidate_set_and_one_frame_limit(self):  # 同时检查少量候选和极小采样上限。
        cases = [{"id": "a"}, {"id": "b"}]  # 模拟两个不同来源的数据案例。
        frames = {"a": [1, 2], "b": [4]}  # 不足三帧时不能重复选同一帧。
        self.assertEqual(DXA._sample_jobs(cases, frames, 6), [("a", 1), ("a", 2), ("b", 4)])  # 候选数不足六时保留全部候选。
        self.assertEqual(DXA._sample_jobs(cases, frames, 1), [("a", 2)])  # 显式上限为一时只保留全局候选中点。

    def test_meter_scale_mismatch_is_not_accepted(self):  # 米制纳米结构不能被绝对容差掩盖数量级差异。
        row = {"case_id": "metric", "frame": 0, "status": "success", "total_line_length": 1e-9, "cell_volume": 1e-24, "density": None, "density_m2": None, "recognized_fraction": 1.0, "families": {"test": 1e-9}, "lines": []}  # 合成非零米制记录。
        changed = copy.deepcopy(row)  # 为候选结果创建独立副本。
        changed.update(total_line_length=8e-9, cell_volume=8e-24, families={"test": 8e-9})  # 注入明确不可接受的科学差异。
        cases = [{"id": "metric", "units": {"length": "m"}}]  # 提供比较所需的量纲信息。
        self.assertTrue(DXA._same_results([row], [copy.deepcopy(row)], cases))  # 完全相同的结果必须通过。
        self.assertFalse(DXA._same_results([row], [changed], cases))  # 不能将单位很小误判为数值相等。

    def test_changed_source_invalidates_run(self):  # 输入变化不能继续当成同一有效运行。
        with tempfile.TemporaryDirectory(prefix="ovito-dxa-source-") as folder:  # 仅使用自有临时文件。
            source = Path(folder) / "input.dump"  # 构造最小状态检查输入。
            source.write_text("original", encoding="utf-8")  # 创建初始文件内容。
            stats = {str(source): DXA._source_stat(source)}  # 保存运行开始时的身份信息。
            source.write_text("changed source", encoding="utf-8")  # 模拟文件在分析期间增长。
            out = Path(folder) / "result"  # 明确独立结果目录。
            with self.assertRaises(RuntimeError):  # 状态变化必须阻止成功结束。
                DXA._verify_sources(stats, out, {})  # 验证真实失效路径。
            self.assertTrue(json.loads((out / "manifest.json").read_text(encoding="utf-8"))["invalid_reason"])  # 续算时必须能识别本次运行无效。

    def test_broken_worker_preserves_all_job_failures(self):  # worker硬退出时，未执行帧也必须出现在结果中。
        class BrokenExecutor:  # 模拟损坏的进程池而不产生操作系统级故障。
            def __enter__(self):  # 兼容运行器的上下文管理。
                self.calls = 0  # 记录已提交次数。
                return self  # 提供池对象。

            def __exit__(self, *args):  # 不吞掉运行器未处理的异常。
                return False  # 保留异常语义。

            def submit(self, *args, **kwargs):  # 第一项失败，后续提交立即报池损坏。
                self.calls += 1  # 统计提交尝试。
                if self.calls > 1:  # 硬退出之后无法再次提交。
                    raise BrokenProcessPool("synthetic worker exit")  # 提供明确原因。
                result = Future()  # 创建已结束的任务对象。
                result.set_exception(BrokenProcessPool("synthetic worker exit"))  # 模拟worker异常终止。
                return result  # 让调度器进入真实失败处理分支。

        with tempfile.TemporaryDirectory(prefix="ovito-dxa-crash-") as folder:  # 故障产物仅写到测试自有目录。
            jobs = [("case", i) for i in range(3)]  # 包含尚未提交的后续帧。
            with patch.object(DXA.futures, "ProcessPoolExecutor", return_value=BrokenExecutor()):  # 替换唯一的进程池创建点。
                records, elapsed, peak = DXA._run_jobs([{"id": "case"}], jobs, Path(folder), 1, 1, 1, 1, "test-fingerprint")  # 执行有界调度的故障路径。
            self.assertEqual({(r["case_id"], r["frame"]) for r in records}, set(jobs))  # 每个原始任务都必须有明确结果。
            self.assertTrue(all(r["status"] == "failed" and r["error"] for r in records))  # 不能生成假成功或无原因缺失。
            self.assertTrue((Path(folder) / "frames.csv").is_file())  # 失败仍应生成可审查的汇总。


if __name__ == "__main__":  # 支持独立运行及unittest发现。
    unittest.main(verbosity=2)  # 显示具体回归场景。

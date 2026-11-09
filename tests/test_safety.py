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
    def test_benchmark_candidates_obey_same_resource_caps(self):  # 可选性能比较不能绕过正式运行的 CPU 预留和用户上限。
        resources = {'workers': 'auto', 'benchmark_max_workers': 8, 'reserve_cpu_fraction': 0.1, 'chunk_size': 1}  # 使用四核机器和较宽探索上限。
        candidates, budget = DXA._benchmark_candidates(resources, 20, 4, 61)  # 不启动 DXA，只构造实际将执行的候选。
        self.assertEqual(budget, 3)  # 四个逻辑 CPU 保留余量后只能使用三个线程预算。
        self.assertIn((3, 1), candidates)  # 非二次幂的合理上限也应被覆盖。
        self.assertTrue(all(workers * threads <= budget for workers, threads in candidates))  # 单进程内部线程和多进程都不超预算。
        restricted, _ = DXA._benchmark_candidates(dict(resources, workers=2), 20, 4, 61)  # 显式用户上限同样约束可选比较。
        self.assertTrue(all(workers <= 2 for workers, threads in restricted))  # 不创建超过授权资源上限的候选。
        chunked, _ = DXA._benchmark_candidates(dict(resources, chunk_size=20), 20, 4, 61)  # 一个任务块没有多进程空间。
        self.assertTrue(all(workers == 1 for workers, threads in chunked))  # 不把同一块重复提交以制造并行。
        self.assertEqual(DXA._benchmark_candidates(resources, 2, 1, 61)[0], [(1, 1)])  # 一核时去掉重复的相同配置，避免无效试跑。

    def resource_defaults(self, **changes):  # 构造资源选择器接受的完整配置，不依赖真实机器状态。
        resources = {"workers": "auto", "ovito_threads": 1, "chunk_size": 1, "reserve_cpu_fraction": 0.1, "reserve_memory_fraction": 0.25}  # 自动进程数及默认资源预留比例。
        resources.update(changes)  # 单个场景仅覆盖需要验证的配置。
        return resources  # 供纯函数资源测试使用。

    def plan_resources(self, resources=None, **changes):  # 用可控数值调用真实资源规划器。
        arguments = {"resources": resources or self.resource_defaults(), "job_count": 100, "cpu": 32, "available_memory": 100000, "parent_rss": 100, "per_worker_estimate": 1000, "platform_limit": 61}  # 默认场景由 CPU 决定进程数。
        arguments.update(changes)  # 按测试意图改变指定约束。
        return DXA._plan_resources(**arguments)  # 保留运行器的真实规划结果和限制原因。

    def test_auto_resources_respect_cpu_budget_and_threads(self):  # 用户指定的每进程线程数应占用对应 CPU 预算。
        plan = self.plan_resources(self.resource_defaults(ovito_threads=2), cpu=16)  # 16 个 CPU 预留 10%，得到 14 个可用线程。
        self.assertEqual((plan["cpu_available"], plan["cpu_budget"]), (16, 14))  # 预算按 floor 规则计算。
        self.assertEqual((plan["workers"], plan["ovito_threads"]), (7, 2))  # 14 个线程预算支持七个双线程进程。
        self.assertEqual(plan["limits"]["cpu"], 7)  # 计划必须记录该 CPU 进程上限。
        self.assertIn("cpu", plan["limiting_factors"])  # 使用者能够看到 CPU 是实际限制因素。

    def test_memory_budget_limits_workers_without_double_counting_parent(self):  # 当前可用内存已经排除了父进程占用。
        plan = self.plan_resources(available_memory=1024, parent_rss=8192, per_worker_estimate=256)  # 父进程 RSS 刻意设得很大以检验不重复扣除。
        self.assertEqual(plan["memory_budget_bytes"], 768)  # 可用内存只需扣除 25% 系统预留。
        self.assertEqual((plan["workers"], plan["limits"]["memory"]), (3, 3))  # 768 字节预算可容纳三个 256 字节 worker。
        self.assertIn("memory", plan["limiting_factors"])  # 明确报告内存收窄了并行度。
        self.assertEqual(plan["estimated_peak_bytes_before_start"], 8192 + 3 * 256)  # 进程树峰值估计仍应包含父进程 RSS。

    def test_task_chunks_limit_workers(self):  # 分块执行时并发上限取任务块数而非原始帧数。
        plan = self.plan_resources(self.resource_defaults(chunk_size=4), job_count=9)  # 9 帧按四帧一块形成三个任务块。
        self.assertEqual((plan["job_count"], plan["task_chunks"]), (9, 3))  # 保留真实帧数及向上取整后的块数。
        self.assertEqual((plan["workers"], plan["limits"]["tasks"]), (3, 3))  # 不创建超过任务块数量的进程。
        self.assertIn("tasks", plan["limiting_factors"])  # 限制来源必须可审查。

    def test_platform_and_explicit_worker_caps(self):  # 平台限制和用户指定的进程上限均不可突破。
        platform = self.plan_resources(platform_limit=2)  # 模拟平台仅允许两个工作进程。
        self.assertEqual((platform["workers"], platform["limits"]["platform"]), (2, 2))  # 平台约束生效。
        self.assertIn("platform", platform["limiting_factors"])  # 必须说明平台是限制因素。
        requested = self.plan_resources(self.resource_defaults(workers=4))  # 用户主动要求最多四个进程。
        self.assertEqual((requested["workers"], requested["requested_workers"]), (4, 4))  # 实际数量不得超过用户上限。
        self.assertIn("requested", requested["limiting_factors"])  # 用户约束应当记录在限制原因中。
        reduced = self.plan_resources(self.resource_defaults(workers=10), job_count=2)  # 用户上限高于独立任务量。
        self.assertEqual((reduced["workers"], reduced["requested_workers"]), (2, 10))  # 任务不足时收窄而不是强制启动十个进程。
        self.assertIn("tasks", reduced["limiting_factors"])  # 保留资源不足的可解释原因。

    def test_no_jobs_and_insufficient_memory(self):  # 零任务应是安全空操作，而不足一个 worker 的内存须报错。
        empty = self.plan_resources(job_count=0, available_memory=0)  # 模拟所有帧已完成且没有可用内存。
        self.assertEqual((empty["workers"], empty["job_count"], empty["task_chunks"]), (0, 0, 0))  # 不创建无意义的工作进程，也不报内存不足。
        with self.assertRaises(RuntimeError):  # 确有任务时，不得绕过内存限制硬开一个进程。
            self.plan_resources(available_memory=100, per_worker_estimate=1000)  # 一个进程的预估增量超出预算。

    def test_cpu_oversubscription_and_zero_reserve(self):  # 显式线程数超过 CPU 预算必须拒绝。
        with self.assertRaises(ValueError):  # 不能用单个超订阅进程绕过预算。
            self.plan_resources(self.resource_defaults(ovito_threads=15), cpu=16)  # 预留后只有十四个 CPU 线程预算。
        plan = self.plan_resources(self.resource_defaults(reserve_cpu_fraction=0), cpu=16)  # 用户允许全部 CPU 时不再额外预留。
        self.assertEqual((plan["cpu_budget"], plan["workers"]), (16, 16))  # 真实预算与选取的进程数均为十六。

    def test_auto_threads_depend_on_selected_worker_count(self):  # auto 线程在多进程与单进程下采用不同策略。
        parallel = self.plan_resources(self.resource_defaults(ovito_threads="auto"), job_count=3)  # 多任务允许三个独立进程。
        self.assertEqual((parallel["workers"], parallel["ovito_threads"]), (3, 1))  # 多进程时每个 OVITO 实例使用一个线程。
        single = self.plan_resources(self.resource_defaults(ovito_threads="auto"), job_count=1)  # 单帧只能选择一个 worker。
        self.assertEqual((single["workers"], single["ovito_threads"]), (1, 28))  # 单进程时可使用整个 CPU 预算。

    def test_particle_estimates_use_complete_headers_or_disclose_fallback(self):  # 粒子数可能随轨迹变化，不能一律假设首帧最大。
        cases = [{"id": "full", "sources": ["full_a", "full_b"]}, {"id": "partial", "sources": ["known", "unknown"]}, {"id": "static", "sources": ["data"]}]  # 覆盖完整文本、多源部分覆盖和单构型三种情况。
        metadata = {"full": {"particle_count_first_frame": 100, "num_frames": 10}, "partial": {"particle_count_first_frame": 500, "num_frames": 5}, "static": {"particle_count_first_frame": 77, "num_frames": 1}}  # 用首帧值代表读取器可直接观测的数据。
        scans = {"full_a": {"covered": True, "max_particle_count": 200}, "full_b": {"covered": True, "max_particle_count": 900}, "known": {"covered": True, "max_particle_count": 600}, "unknown": {"covered": False, "max_particle_count": None}}  # 测试完整扫描与未覆盖来源的区别。
        estimates = DXA._particle_estimates(cases, metadata, scans)  # 获取每个案例独立的粒子数与覆盖范围。
        self.assertEqual(estimates["full"], {"particle_count": 900, "scope": "all_text_dump_headers"})  # 完整文本扫描使用所有来源的最大值。
        self.assertEqual(estimates["partial"], {"particle_count": 600, "scope": "first_frame_and_available_headers"})  # 部分来源未覆盖时，不宣称已知整个轨迹峰值。
        self.assertEqual(estimates["static"], {"particle_count": 77, "scope": "single_frame"})  # 单构型使用读取到的首帧数量。

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

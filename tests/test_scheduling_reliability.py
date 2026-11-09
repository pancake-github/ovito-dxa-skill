"""不运行 DXA，验证分工况中断保留、资源错误和最终汇总内存判定。"""  # 仅使用模拟任务及临时输出。
from concurrent.futures import Future  # 构造已完成或已中断的任务，避免启动计算进程。
from contextlib import ExitStack, redirect_stderr  # 集中管理模拟资源及捕获预期错误说明。
import csv  # 读取真实生成的汇总表并核对帧身份。
import importlib.util  # 加载运行器函数定义而不执行命令入口。
import io  # 在内存中接收预期的次要写盘错误说明。
from pathlib import Path  # 定位运行器及测试自有的临时目录。
import tempfile  # 为每个故障场景建立隔离输出目录。
from types import SimpleNamespace  # 提供确定的 CPU 和内存观测值。
import unittest  # 使用标准库测试框架，不增加测试依赖。
from unittest.mock import patch  # 替换执行器、资源观测及采样，不调用 OVITO。
import psutil  # 仅模拟运行器已经使用的资源查询接口。

SCRIPT = Path(__file__).resolve().parents[1] / "skills/ovito-dxa/scripts/run_dxa.py"  # 指向仓库真实运行器。
SPEC = importlib.util.spec_from_file_location("dxa_scheduling_reliability", SCRIPT)  # 使用独立模块名避免污染其他测试。
DXA = importlib.util.module_from_spec(SPEC)  # 创建仅供本测试使用的模块对象。
SPEC.loader.exec_module(DXA)  # 只加载函数定义，不运行 main 或导入 OVITO。
MIB = 1024 * 1024  # 统一模拟内存的字节单位。
PARENT_RSS = 100 * MIB  # 固定父进程已占用内存，避免测试受真实机器负载影响。
AVAILABLE_MEMORY = 4096 * MIB  # 固定新增进程可使用的系统可用内存。


class SchedulingReliabilityTests(unittest.TestCase):  # 验证三个已发现的调度可靠性回归。
    def resources(self):  # 使用单 worker 使模拟任务的成功与中断顺序确定。
        return {"scheduling": "case_sequential", "workers": 1, "ovito_threads": 1, "chunk_size": 1, "max_pending_factor": 1, "reserve_cpu_fraction": 0.1, "reserve_memory_fraction": 0.25}  # 资源配置仍由真实规划器检查。

    def record(self, case_id, frame):  # 构造一帧可写入现有统计表的成功结果。
        return {"case_id": case_id, "frame": frame, "timestep": frame, "time": None, "total_line_length": 0.0, "cell_volume": 10.0, "density": None, "density_m2": None, "recognized_fraction": 1.0, "status": "zero_dislocations", "error": "", "families": {}, "lines": [], "worker_pid": 1234}  # 数值仅用于测试写表，不代表 DXA 实测。

    def rows(self, path):  # 读取实际输出，避免仅检查内部列表。
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:  # 兼容运行器 CSV 的 UTF-8 BOM。
            return list(csv.DictReader(stream))  # 返回小型测试记录用于断言。

    def fixed_resources(self, stack):  # 在调用者的 ExitStack 中注册确定的资源查询结果。
        process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=PARENT_RSS))  # 模拟父进程内存，不访问其他进程。
        stack.enter_context(patch.object(DXA, "_available_cpu", return_value=8))  # 为真实资源规划器提供八个可用逻辑 CPU。
        stack.enter_context(patch.object(psutil, "virtual_memory", return_value=SimpleNamespace(available=AVAILABLE_MEMORY)))  # 固定可用内存预算。
        stack.enter_context(patch.object(psutil, "Process", return_value=process))  # 所有父进程 RSS 查询使用同一个已知值。

    def test_interrupt_keeps_completed_rows_from_current_and_previous_cases(self):  # 第二工况中断后，第一工况、当前成功帧及保留帧都不能丢失。
        owner = self  # 供模拟执行器生成测试结果。
        interrupt = KeyboardInterrupt("synthetic interrupt after second case frame zero")  # 保留原中断对象以核对异常未被替换。

        class ImmediateExecutor:  # 模拟进程池的任务完成，不执行其传入函数。
            def __enter__(self):  # 与真实执行器的上下文接口保持一致。
                return self  # 将模拟池交给运行器调度。

            def __exit__(self, *args):  # 保留所有中断和异常的传播语义。
                return False  # 不能由模拟池吞掉 KeyboardInterrupt。

            def submit(self, function, jobs):  # 每次仅为一个帧块构造 Future。
                future = Future()  # 真实 Future 让运行器使用原有等待和取结果逻辑。
                if jobs == [("second", 1)]:  # 当前工况的首帧已成功后，下一帧触发中断。
                    future.set_exception(interrupt)  # 不调用 DXA，也不造成操作系统进程崩溃。
                else:  # 其他帧返回可序列化的模拟成功结果。
                    future.set_result([owner.record(cid, frame) for cid, frame in jobs])  # 由真实 persist 路径保存检查点并触发回调。
                return future  # 将已结束任务交回真实有界调度逻辑。

        cases = [{"id": "first"}, {"id": "second"}]  # 明确两个工况的先后顺序。
        jobs = [("first", 0), ("second", 0), ("second", 1)]  # 最后一帧中断，前两帧应已经落盘。
        estimates = {case["id"]: {"particle_count": 100, "scope": "single_frame"} for case in cases}  # 仅用于资源估算，不读取原子数据。
        retained = [self.record("first", 7)]  # 模拟续算前已验证的旧帧。
        with tempfile.TemporaryDirectory(prefix="dxa-scheduling-interrupt-") as folder, ExitStack() as stack:  # 所有产物只存在于测试自有临时目录。
            self.fixed_resources(stack)  # 固定资源，排除外部负载造成的不确定性。
            stack.enter_context(patch.object(DXA, "_sample_memory", side_effect=lambda stop, sample: sample.__setitem__(0, PARENT_RSS)))  # 采样立即完成，不创建实际内存负载。
            executor = stack.enter_context(patch.object(DXA.futures, "ProcessPoolExecutor", side_effect=lambda **kwargs: ImmediateExecutor()))  # 真正的计算进程永不启动。
            out = Path(folder) / "new" / "run"  # 使用尚不存在的嵌套输出目录。
            with self.assertRaises(KeyboardInterrupt) as raised:  # 主中断必须向调用者传播。
                DXA._run_scheduled_jobs(cases, jobs, out, self.resources(), estimates, "reliability-fingerprint", retained=retained)  # 调用真实 wrapper、调度和检查点保存路径。
            self.assertIs(raised.exception, interrupt)  # 汇总过程不能替换原中断对象。
            self.assertEqual(executor.call_count, 2)  # 两个工况分别启动一个模拟池。
            rows = self.rows(out / "frames.csv")  # 核对最终真实 CSV 中的记录。
            self.assertEqual({(row["case_id"], int(row["frame"])) for row in rows}, {("first", 0), ("first", 7), ("second", 0)})  # 跨工况及当前工况的成功结果都必须保留。
            self.assertEqual(len(rows), 3)  # 返回 batch 和回调不能导致重复计入同一帧。
            self.assertTrue(all(row["status"] == "zero_dislocations" for row in rows))  # 未完成帧不能伪装成成功结果。
            self.assertTrue((out / "checkpoints/first/000000000.json").is_file())  # 第一工况成功帧确实经过原子落盘。
            self.assertTrue((out / "checkpoints/second/000000000.json").is_file())  # 中断工况的首帧也确实已经落盘。
            self.assertFalse((out / "checkpoints/second/000000001.json").exists())  # 中断帧不应出现成功检查点。

    def test_planning_failure_preserves_original_error_and_partial_output(self):  # 第一阶段还未计算就失败时，输出目录和清理错误不能掩盖原原因。
        for secondary_write_error in (False, True):  # 同时检查正常空汇总和汇总自身失败两条收尾路径。
            with self.subTest(secondary_write_error=secondary_write_error), tempfile.TemporaryDirectory(prefix="dxa-scheduling-plan-") as folder, ExitStack() as stack:  # 每种场景使用独立临时目录。
                self.fixed_resources(stack)  # 固定总体内存观测，使失败只来自指定规划器。
                stack.enter_context(patch.object(DXA, "_sample_memory", side_effect=lambda stop, sample: sample.__setitem__(0, PARENT_RSS)))  # 不执行真实进程树扫描。
                original = RuntimeError("synthetic first-stage resource failure")  # 本测试需要原样保留的资源错误。
                stack.enter_context(patch.object(DXA, "_plan_resources", side_effect=original))  # 在尚未创建任何工作进程前触发失败。
                run_jobs = stack.enter_context(patch.object(DXA, "_run_jobs"))  # 若错误后仍调用计算器，测试将通过断言发现。
                if secondary_write_error:  # 模拟部分汇总另遇权限错误。
                    stack.enter_context(patch.object(DXA, "_write_tables", side_effect=PermissionError("synthetic summary write failure")))  # 验证次要写盘错误不替换资源错误。
                stderr = stack.enter_context(redirect_stderr(io.StringIO()))  # 捕获预期说明，避免污染测试运行日志。
                out = Path(folder) / "new" / "comparison"  # 模拟尚不存在的比较模式子目录。
                with self.assertRaises(RuntimeError) as raised:  # 不能变成 FileNotFoundError 或 PermissionError。
                    DXA._run_scheduled_jobs([{"id": "case"}], [("case", 0)], out, self.resources(), {"case": {"particle_count": 100}}, "reliability-fingerprint")  # 执行真实异常收尾。
                self.assertIs(raised.exception, original)  # 证明调用者收到最初的资源错误。
                run_jobs.assert_not_called()  # 资源规划失败后不得启动计算。
                self.assertTrue(out.is_dir())  # 即使首阶段失败，输出目录也应已建立。
                if secondary_write_error:  # 输出失败应有独立说明，同时不虚构成功表格。
                    self.assertIn("synthetic summary write failure", stderr.getvalue())  # 明确保留次要错误的可诊断信息。
                else:  # 正常收尾应生成有表头而无成功数据的部分汇总。
                    for filename in ("frames.csv", "families.csv", "lines.csv"):  # 三类结果表均需可读取。
                        self.assertEqual(self.rows(out / filename), [])  # 未处理任何帧，不能填入假零值或成功行。

    def test_final_summary_peak_is_included_in_result_and_resource_verdict(self):  # 最终 CSV 汇总峰值必须计入整体报告，且能否决资源合格性。
        budget = int(AVAILABLE_MEMORY * 0.75)  # 与明确的测试资源预留配置对应。
        for over_budget in (False, True):  # 验证预算内与超预算两种结果，防止实现恒定返回失败。
            with self.subTest(over_budget=over_budget), tempfile.TemporaryDirectory(prefix="dxa-scheduling-peak-") as folder, ExitStack() as stack:  # 每种内存情形使用隔离目录。
                self.fixed_resources(stack)  # 为全程和阶段资源判断提供固定基准。
                sampled = {}  # 保存模拟采样器的共享状态以定位最终汇总时刻。
                summary_peak = PARENT_RSS + budget + 1 if over_budget else PARENT_RSS + budget // 2  # 仅改变整数读数，不分配大数组。

                def sample_memory(stop, sample):  # 确定性地登记外层采样状态。
                    sampled.update(stop=stop, sample=sample)  # 最终写表时通过同一状态注入峰值。
                    sample[0] = PARENT_RSS  # 阶段前的初始采样值仅含父进程。

                class InlineSampler:  # 用同步接口代替采样线程，避免测试中的时间竞争。
                    def __init__(self, target, args, daemon):  # 接受运行器创建线程时的完整参数。
                        self.target, self.args = target, args  # 保留待执行采样函数和共享对象。

                    def start(self):  # 模拟启动采样而不产生后台线程。
                        self.target(*self.args)  # 立刻登记采样状态，后续峰值由写表阶段注入。

                    def join(self):  # 检查采样停止时机而不是等待真实线程。
                        self.stop_was_set = self.args[0].is_set()  # 留下停止状态供必要诊断。

                def run_jobs(cases, jobs, out, workers, threads, chunk_size, pending_factor, fingerprint, write_tables=True, on_record=None):  # 仅模拟一个资源合格的已完成计算阶段。
                    records = [self.record(cid, frame) for cid, frame in jobs]  # 小型结果不包含真实原子数据。
                    for record in records:  # 使用修复后的落盘同步接口把成功记录交给 wrapper。
                        on_record(record)  # 不依赖函数最终返回 batch 才更新外层记录。
                    return records, 0.01, PARENT_RSS + MIB  # 阶段峰值低于预算，最终失败必须来自汇总阶段。

                real_write_tables = DXA._write_tables  # 保留真实 CSV 输出以验证汇总仍可完成。

                def write_tables(out, records):  # 在最终汇总开始时注入可控峰值。
                    self.assertFalse(sampled["stop"].is_set())  # 最终写表前采样不能已经停止。
                    sampled["sample"][0] = summary_peak  # 模拟仅在最终汇总期间出现的 RSS 峰值。
                    real_write_tables(out, records)  # 继续执行真实小型 CSV 输出。

                stack.enter_context(patch.object(DXA, "_sample_memory", side_effect=sample_memory))  # 不读取真实进程树内存。
                stack.enter_context(patch.object(DXA.threading, "Thread", InlineSampler))  # 使采样与写表的先后关系完全确定。
                stack.enter_context(patch.object(DXA, "_run_jobs", side_effect=run_jobs))  # 阶段计算不会调用 DXA 或创建进程。
                stack.enter_context(patch.object(DXA, "_write_tables", side_effect=write_tables))  # 峰值只在最终汇总路径注入。
                out = Path(folder) / "run"  # 使用测试专属输出目录。
                records, elapsed, peak, plan = DXA._run_scheduled_jobs([{"id": "case"}], [("case", 0)], out, self.resources(), {"case": {"particle_count": 100}}, "reliability-fingerprint")  # 调用真实整体资源判定。
                self.assertEqual(peak, summary_peak)  # 返回峰值必须包含最终汇总阶段。
                self.assertEqual(plan["peak_rss_bytes_sampled"], summary_peak)  # 计划记录与返回值不能使用不同统计范围。
                self.assertTrue(plan["stages"][0]["resource_ok"])  # 证明工况计算自身满足资源约束。
                self.assertEqual(plan["resource_ok"], not over_budget)  # 整体判定必须纳入汇总超预算，预算内情况仍通过。
                self.assertTrue(sampled["stop"].is_set())  # 汇总结束后应停止总体采样。
                self.assertEqual(len(self.rows(out / "frames.csv")), 1)  # 内存判定不能破坏已完成帧的结果表。


if __name__ == "__main__":  # 支持独立执行或 unittest discover 收集。
    unittest.main(verbosity=2)  # 输出三项回归场景及测试结果。

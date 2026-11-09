# 仅使用 Python 标准库测试文本 dump 的头部扫描，不运行 OVITO 或 DXA。
import gzip  # 构造 gzip 压缩测试输入。
import importlib.util  # 从文件加载检查器模块，避免要求包安装。
from pathlib import Path  # 构造测试文件路径。
import tempfile  # 在系统临时目录中隔离测试文件。
import unittest  # 组织并执行自动测试。


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "ovito-dxa" / "scripts" / "inspect_inputs.py"  # 指向仓库内的检查器。
SPEC = importlib.util.spec_from_file_location("dxa_scan_test_module", SCRIPT)  # 指定独立模块名称。
MODULE = importlib.util.module_from_spec(SPEC)  # 创建供本测试加载的模块对象。
SPEC.loader.exec_module(MODULE)  # 执行模块定义，不调用其命令行主入口。


def frame(step, count):  # 构造含完整时间步头和原子数头的合成文本帧。
    atoms = "".join(f"{index + 1} 1 0 0 0\n" for index in range(count))  # 按声明数量生成原子记录。
    return f"ITEM: TIMESTEP\n{step}\nITEM: NUMBER OF ATOMS\n{count}\nITEM: BOX BOUNDS pp pp pp\n0 5\n0 5\n0 5\nITEM: ATOMS id type x y z\n{atoms}"  # 返回符合文本 dump 结构的最小帧。


class ScanCountsTests(unittest.TestCase):  # 验证头数量、最大粒子数与异常边界。
    def setUp(self):  # 为每个测试建立互不干扰的临时目录。
        self.tmp = tempfile.TemporaryDirectory()  # 创建可自动清理的临时目录。
        self.root = Path(self.tmp.name)  # 保存目录路径。
        self.addCleanup(self.tmp.cleanup)  # 确保测试结束后清理临时数据。

    def write_input(self, name, payload):  # 将字节测试输入写到临时目录。
        path = self.root / name  # 构造具体文件路径。
        path.write_bytes(payload)  # 按原样写入测试字节。
        return path  # 返回供扫描器使用的路径。

    def test_variable_atom_count_plain_and_gzip(self):  # 检查不同帧粒子数的最大值以及 gzip 行为。
        payload = (frame(0, 2) + frame(10, 7) + frame(20, 4)).encode("ascii")  # 构造三个不同规模的时间帧。
        plain = self.write_input("variable.dump", payload)  # 保存未压缩文本。
        compressed = self.root / "variable.dump.gz"  # 设置 gzip 文件路径。
        with gzip.open(compressed, "wb") as stream:  # 打开 gzip 压缩输出流。
            stream.write(payload)  # 写入相同内容以进行对照。
        for path in (plain, compressed):  # 对普通文件和 gzip 分别验证。
            with self.subTest(name=path.name):  # 独立标记每种输入。
                result = MODULE.scan_text_dump(path)  # 流式扫描单个源文件。
                self.assertTrue(result["covered"])  # 两种文本输入都应被覆盖。
                self.assertEqual(result["timestep_headers"], 3)  # 三个时间步头均应读出。
                self.assertEqual(result["atom_count_headers"], 3)  # 三个合法原子数头均应读出。
                self.assertEqual(result["raw_atom_count_headers"], 3)  # 字面原子数头也应为三个。
                self.assertEqual(result["max_particle_count"], 7)  # 最大粒子数必须取自全部帧。
                self.assertEqual(result["errors"], [])  # 正常轨迹不应报告格式错误。
                self.assertGreaterEqual(result["duration_seconds"], 0)  # 单独扫描耗时应被记录。

    def test_zero_atoms_is_legal_count(self):  # 检查零原子声明属于合法的非负整数。
        path = self.write_input("zero.dump", frame(0, 0).encode("ascii"))  # 构造零原子帧头。
        result = MODULE.scan_text_dump(path)  # 扫描粒子数元数据。
        self.assertEqual(result["atom_count_headers"], 1)  # 合法的零值仍计入完整数量头。
        self.assertEqual(result["max_particle_count"], 0)  # 最大数应为零而非未知。
        self.assertEqual(result["errors"], [])  # 此处仅检查文本结构，不做 DXA 判断。

    def test_missing_bad_negative_and_truncated_counts(self):  # 验证缺失、非整数、负数和末尾截断。
        examples = {"missing": b"ITEM: TIMESTEP\n0\n", "bad": b"ITEM: TIMESTEP\n0\nITEM: NUMBER OF ATOMS\nno\n", "negative": b"ITEM: TIMESTEP\n0\nITEM: NUMBER OF ATOMS\n-1\n", "truncated": b"ITEM: TIMESTEP\n0\nITEM: NUMBER OF ATOMS\n"}  # 列出四种不合法结构。
        for name, payload in examples.items():  # 逐个验证错误输入。
            with self.subTest(name=name):  # 让每一种错误单独显示。
                result = MODULE.scan_text_dump(self.write_input(name, payload))  # 扫描当前字节数据。
                self.assertTrue(result["covered"])  # 已识别出文本 TIMESTEP 结构。
                self.assertTrue(result["errors"])  # 所有不合法数量都必须明确报错。
                self.assertEqual(result["atom_count_headers"], 0)  # 没有任何合法的数量头。
                self.assertIsNone(result["max_particle_count"])  # 不能虚构最大粒子数。

    def test_missing_and_duplicate_heads_cannot_cancel(self):  # 防止两个帧的缺失和重复在总数上互相抵消。
        payload = b"ITEM: TIMESTEP\n0\nITEM: NUMBER OF ATOMS\n1\nITEM: NUMBER OF ATOMS\n2\nITEM: TIMESTEP\n1\n"  # 第一帧重复数量头，第二帧没有数量头。
        result = MODULE.scan_text_dump(self.write_input("mixed.dump", payload))  # 检查每帧的数量头个数。
        self.assertEqual(result["raw_timestep_headers"], result["raw_atom_count_headers"])  # 总数恰好相等以验证该边界。
        self.assertTrue(result["errors"])  # 即使总数相等也必须报告逐帧结构异常。

    def test_non_text_inputs_are_not_claimed_as_covered(self):  # 二进制和 YAML 不应伪装为已扫描的文本。
        examples = {"binary": bytes((0, 1, 2)), "yaml": b"---\nkeywords:\n - id\n"}  # 准备明确的非目标格式样本。
        for name, payload in examples.items():  # 分别检查两种格式。
            with self.subTest(name=name):  # 独立显示各输入的测试结果。
                result = MODULE.scan_text_dump(self.write_input(name, payload))  # 调用同一公共扫描函数。
                self.assertFalse(result["covered"])  # 不应声明完成文本扫描。
                self.assertIsNone(result["atom_count_headers"])  # 未覆盖时头数必须保持未知。
                self.assertIsNone(result["max_particle_count"])  # 不可虚构最大粒子数。
                self.assertEqual(result["errors"], [])  # 支持格式不因文本扫描不覆盖而被误报损坏。


if __name__ == "__main__":  # 仅作为脚本执行时启动标准库测试。
    unittest.main()  # 运行本文件的全部测试。

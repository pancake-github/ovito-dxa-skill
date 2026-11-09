"""生成可公开复现的完整晶体示例；不含私人研究数据。"""  # 说明数据来源。
import argparse  # 解析明确的输出目录。
import gzip  # 生成压缩输入示例。
import json  # 保存可直接运行的任务清单。
from pathlib import Path  # 处理跨平台文件路径。


def crystal(kind, n=8, a=3.6):  # 生成周期性立方晶胞中的完整晶体。
    basis = [(0, 0, 0), (0.5, 0.5, 0.5)] if kind == "BCC" else [(0, 0, 0), (0, 0.5, 0.5), (0.5, 0, 0.5), (0.5, 0.5, 0)]  # 按晶格选择基元。
    points = [(a * (i + x), a * (j + y), a * (k + z)) for i in range(n) for j in range(n) for k in range(n) for x, y, z in basis]  # 构造所有原子坐标。
    return points, n * a  # 同时返回完整盒边长。


def data_text(points, side, hint=True):  # 将构型写成 LAMMPS atomic 数据格式。
    header = f"Synthetic crystal\n\n{len(points)} atoms\n1 atom types\n\n0 {side} xlo xhi\n0 {side} ylo yhi\n0 {side} zlo zhi\n\nMasses\n\n1 1.0\n\nAtoms" + (" # atomic" if hint else "") + "\n\n"  # 提供显式格式提示，或创建缺提示测试。
    return header + "".join(f"{i} 1 {x:.10f} {y:.10f} {z:.10f}\n" for i, (x, y, z) in enumerate(points, 1))  # 原子编号稳定且唯一。


def dump_frame(points, side, frame):  # 构造带时间步、不同原子行顺序的轨迹帧。
    header = f"ITEM: TIMESTEP\n{frame * 10}\nITEM: NUMBER OF ATOMS\n{len(points)}\nITEM: BOX BOUNDS pp pp pp\n0 {side}\n0 {side}\n0 {side}\nITEM: ATOMS id type x y z\n"  # 周期边界由 dump 明确携带。
    records = list(enumerate(points, 1))  # 保持原子身份与坐标的对应关系。
    if frame % 2:  # 交替颠倒存储顺序来验证顺序独立性。
        records.reverse()  # 不改变任何原子身份。
    return header + "".join(f"{i} 1 {(x + frame * 0.01) % side:.10f} {y:.10f} {z:.10f}\n" for i, (x, y, z) in records)  # 平移完整晶体而不引入缺陷。


def case(case_id, sources, lattice, is_data=False):  # 创建已经明确科学设置的合成案例。
    return {  # 这些选择仅针对本文件生成的示例，不是用户数据默认值。
        "id": case_id,  # 唯一案例标识。
        "sources": sources,  # 路径相对任务清单文件。
        "input_format": "lammps/data" if is_data else "lammps/dump",  # 指定真实格式。
        "atom_style": "atomic",  # data 使用已知原子列布局。
        "frames": {"start": 0, "stop": None, "step": 1},  # 使用全部真实帧。
        "lattice": lattice,  # 与生成的晶格一致。
        "selection": {"mode": "all"},  # 保留所有晶格原子。
        "pbc": [True, True, True] if is_data else None,  # data 明确边界，dump 保留自身边界。
        "units": {"length": "angstrom", "time": "ps"},  # 定义本合成示例的单位。
        "time_mapping": {"timestep_size": 0.001, "origin_step": 0, "origin_time": 0.0},  # 有步数时才映射时间。
        "dxa": {},  # 使用当前 OVITO 默认值并由运行器记录实际设置。
        "density": {"mode": "cell", "confirmed": True},  # 完整周期晶体与整个盒体积匹配。
        "output": {"level": "statistics"},  # 示例优先轻量统计。
    }  # 返回完整案例。


def manifest(cases, output):  # 建立可执行的完整任务清单。
    return {"schema_version": 1, "confirmed": True, "output_dir": output, "cases": cases, "resources": {"workers": 2, "ovito_threads": 1, "chunk_size": 1, "benchmark_max_workers": 4, "reserve_memory_fraction": 0.25}}  # 明确资源和输出目录。


def generate(output):  # 仅在新建或空目录中生成，避免覆盖已有研究数据。
    output = Path(output).resolve()  # 将输出边界规范为绝对路径。
    if output.exists() and any(output.iterdir()):  # 拒绝向非空目录覆盖生成。
        raise FileExistsError(f"示例目录非空：{output}")  # 要求调用者选择新的示例目录。
    output.mkdir(parents=True, exist_ok=True)  # 创建明确授权的输出目录。
    bcc, side = crystal("BCC", a=2.86)  # 创建小型 BCC 晶体。
    fcc, fside = crystal("FCC", a=3.6)  # 创建小型 FCC 晶体。
    (output / "bcc.data").write_text(data_text(bcc, side), encoding="utf-8")  # 保存单构型数据。
    (output / "fcc.data").write_text(data_text(fcc, fside), encoding="utf-8")  # 保存不同晶格案例。
    (output / "bcc_no_hint.data").write_text(data_text(bcc, side, False), encoding="utf-8")  # 保存必须询问 atom_style 的案例。
    frames = [dump_frame(bcc, side, i) for i in range(3)]  # 创建三个真实时间步。
    (output / "bcc.dump").write_text("".join(frames), encoding="utf-8")  # 保存单文件多帧轨迹。
    for i, frame in enumerate(frames):  # 为同一轨迹生成分帧文件版本。
        (output / f"bcc.{i * 10}.dump").write_text(frame, encoding="utf-8")  # 使用真实时间步命名。
    with gzip.open(output / "bcc.dump.gz", "wt", encoding="utf-8") as stream:  # 创建可直接读取的 gzip 轨迹。
        stream.write("".join(frames))  # 压缩内容与普通 dump 相同。
    (output / "empty.dump").write_bytes(b"")  # 生成空输入检测案例。
    (output / "truncated.dump").write_text(frames[-1].rsplit("\n", 4)[0] + "\n", encoding="utf-8")  # 删除末尾原子行以验证截断检测。
    broken_lines = frames[1].splitlines(keepends=True)  # 保持帧结构中的行数不变。
    broken_lines[9] = "BAD ATOM RECORD\n"  # 替换首条原子记录以触发逐帧解析错误。
    (output / "partial_failure.dump").write_text(frames[0] + "".join(broken_lines) + frames[2], encoding="utf-8")  # 用于区分部分失败与全部成功。
    extra_line = frames[1].replace("ITEM: ATOMS id type x y z\n", "ITEM: ATOMS id type x y z\nBAD ATOM RECORD\n", 1)  # 构造会破坏索引的额外行。
    (output / "index_truncation.dump").write_text(frames[0] + extra_line + frames[2], encoding="utf-8")  # 验证不能把三个帧头误当成单帧完整文件。
    configs = {  # 每个配置采用不同结果目录以避免冲突。
        "task.json": manifest([case("bcc_trajectory", ["bcc.dump"], "BCC"), case("fcc_snapshot", ["fcc.data"], "FCC", True)], "results/task"),  # 多组独立数据。
        "static.json": manifest([case("bcc_snapshot", ["bcc.data"], "BCC", True)], "results/static"),  # 单构型。
        "sequence.json": manifest([case("bcc_sequence", [f"bcc.{i * 10}.dump" for i in range(3)], "BCC")], "results/sequence"),  # 同一轨迹的分文件版本。
    }  # 配置列表结束。
    for name, payload in configs.items():  # 写出没有占位符的真实清单。
        (output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")  # 使用 UTF-8 中文兼容格式。
    return output  # 方便测试调用。


if __name__ == "__main__":  # 只在直接运行时创建示例。
    parser = argparse.ArgumentParser(description=__doc__)  # 提供可查询的帮助。
    parser.add_argument("--output", required=True, type=Path)  # 使用者必须指定输出目录。
    args = parser.parse_args()  # 解析命令行。
    print(generate(args.output))  # 仅打印实际生成目录。

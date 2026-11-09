#!/usr/bin/env python
# 此工具只读取输入并检查元数据，不运行 DXA，也不会自动组合不同输入文件。
import argparse  # 解析命令行参数。
import contextlib  # 在压缩和未压缩输入间统一管理文件句柄。
import ast  # 对已有 Python 脚本进行不执行代码的静态检查。
import gzip  # 读取 gzip 压缩文件头部。
import hashlib  # 可选计算文件内容的 SHA-256。
import re  # 匹配 data 文件头中的 Atoms 样式提示。
import json  # 输出稳定的 JSON 报告。
import os  # 获取运行环境与进程资源信息。
import platform  # 获取当前 Python 与操作系统信息。
import sys  # 控制输出与退出状态。
import time  # 测量完整头扫描耗时。
from pathlib import Path  # 使用跨平台路径对象处理文件。


def describe_exception(exc):  # 将异常转换成 JSON 可读的简短说明。
    return f"{type(exc).__name__}: {exc}"  # 保留异常种类与具体消息。


def file_stat(path):  # 捕获文件元数据，用于发现检查前后的变化。
    st = path.stat()  # 读取文件的当前状态。
    return {"size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "device": st.st_dev, "inode": st.st_ino}  # 返回用于比较的记录。


def sha256_file(path):  # 仅在明确要求时完整读取文件并计算摘要。
    digest = hashlib.sha256()  # 创建 SHA-256 计算器。
    with path.open("rb") as stream:  # 以二进制方式读取原始文件。
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):  # 分块读取，避免整文件进入内存。
            digest.update(block)  # 将当前块加入摘要。
    return digest.hexdigest()  # 返回十六进制摘要。


def data_header_style(path):  # 仅读取最多 4 MiB 解压后头部，不依赖文件扩展名。
    result = {"hint": None, "inspection": "unknown", "detail": None}  # 区分没有提示和未能检查。
    try:  # 捕获压缩头部读取及可选依赖错误。
        with path.open("rb") as raw:  # 先读取压缩格式标记。
            magic = raw.read(4)  # 根据文件内容识别 gzip 或 zstd。
            raw.seek(0)  # 回到文件起点以读取头部。
            if magic[:2] == b"\x1f\x8b":  # gzip 格式的魔数。
                with gzip.GzipFile(fileobj=raw) as stream:  # 按需解压 gzip。
                    head = stream.read(4 * 1024 * 1024)  # 限制解压后的读取量。
            elif magic == b"\x28\xb5\x2f\xfd":  # zstd 帧魔数。
                import zstandard  # 使用环境中已有的 zstandard，不安装依赖。
                with zstandard.ZstdDecompressor().stream_reader(raw) as stream:  # 以流方式解压。
                    head = stream.read(4 * 1024 * 1024)  # 限制解压后的读取量。
            else:  # 没有压缩魔数时按原始内容读取。
                head = raw.read(4 * 1024 * 1024)  # 仅读取有限大小的文件头。
        match = re.search(rb"(?m)^Atoms[ \t]*(?:\#[ \t]*([^\r\n]+))?[ \t]*\r?$", head)  # 查找完整 Atoms 行。
        if not match:  # 限定范围内未找到 Atoms 行。
            result["detail"] = "有限头部内未找到完整 Atoms 行，样式提示状态未知。"  # 不误判为没有提示。
        else:  # 已找到完整头行。
            result["inspection"] = "inspected"  # 标记已完成头行检查。
            if match.group(1):  # 只有注释内含非空文本才视为提示。
                result["hint"] = match.group(1).decode("utf-8", errors="replace").strip().split()[0]  # 保存注释的首个样式名称。
    except Exception as exc:  # 无法读取 zstd 或头部时不得猜测提示。
        result["detail"] = describe_exception(exc)  # 保留解释原因。
    return result  # 返回头部提示与检查状态。


def scan_text_dump(path):  # 流式核查单个 LAMMPS 文本 dump 的完整时间步头，不运行 DXA。
    started = time.perf_counter()  # 分别记录完整流扫描的耗时。
    result = {"covered": False, "timestep_headers": None, "raw_timestep_headers": None, "errors": [], "duration_seconds": None, "reason": None}  # 初始化稳定返回结构。
    try:  # 打开或扫描失败时返回机器可读错误。
        with contextlib.ExitStack() as stack:  # 保证普通和压缩文件句柄均会关闭。
            raw = stack.enter_context(Path(path).open("rb"))  # 以二进制方式读取源文件。
            magic = raw.read(4)  # 检查实际内容的压缩魔数。
            raw.seek(0)  # 返回源文件开头。
            if magic[:2] == b"\x1f\x8b":  # gzip 文件通过标准库流式解压。
                stream = stack.enter_context(gzip.GzipFile(fileobj=raw))  # 受 ExitStack 管理 gzip 读取器。
            elif magic == b"\x28\xb5\x2f\xfd":  # zstd 文件使用可选的已有解压库。
                try:  # zstandard 不一定安装在每个 OVITO 环境。
                    import zstandard  # 不修改环境地尝试导入可选模块。
                except ImportError:  # 缺少依赖时不能伪称已经完整扫描。
                    result["reason"] = "zstd 文本头扫描需要环境中已有 zstandard；当前未覆盖。"  # 明确说明覆盖范围。
                    return result  # 不将未覆盖格式误报成坏文件。
                stream = stack.enter_context(zstandard.ZstdDecompressor().stream_reader(raw))  # 流式解压 zstd。
                import io  # 为 zstd 流提供有界 readline 支持。
                stream = stack.enter_context(io.BufferedReader(stream))  # 包装为缓冲读取器。
            else:  # 原始二进制或文本均先按原始字节读取。
                stream = raw  # 不依据文件扩展名判定格式。
            first = True  # 跟踪第一个非空行以识别 YAML。
            pending = False  # 标记是否正在等待 TIMESTEP 后面的整数。
            headers = 0  # 计算完整的 TIMESTEP 和整数对。
            raw_headers = 0  # 计算所有字面 TIMESTEP 头，含末尾不完整头。
            line_number = 0  # 保存行号供定位。
            while True:  # 逐行扫描，不加载整个轨迹。
                line = stream.readline(1048577)  # 限制单行的最大内存使用。
                if not line:  # 文件正常结束。
                    break  # 退出扫描循环。
                line_number += 1  # 更新行号。
                if len(line) > 1048576 and not line.endswith(b"\n"):  # 极长行不适合当前有界文本扫描。
                    result["reason"] = "存在超过 1 MiB 的单行；此文本扫描未覆盖。"  # 明确说明没有完成验证。
                    return result  # 不把长行当成确定的格式错误。
                if b"\x00" in line:  # 二进制内容不能套用文本行计数。
                    result["reason"] = "检测到二进制内容；文本扫描未覆盖。"  # 标明不覆盖。
                    return result  # 不拒绝 OVITO 支持的二进制文件。
                stripped = line.strip().lstrip(b"\xef\xbb\xbf") if first else line.strip()  # 容忍首行 UTF-8 BOM。
                if first and stripped:  # 仅首个非空行用于识别 YAML。
                    first = False  # 后续行不再进行首行判断。
                    if stripped.startswith((b"---", b"keywords:", b"- keywords:")):  # YAML dump 不按文本 dump 标记扫描。
                        result["reason"] = "检测到 YAML；文本 TIMESTEP 头扫描未覆盖。"  # 保持格式边界明确。
                        return result  # 不将 YAML 误报为损坏。
                if stripped == b"ITEM: TIMESTEP":  # LAMMPS 文本 dump 的精确头行。
                    if pending:  # 前一个头后尚无合法步号。
                        result["errors"].append(f"第 {line_number} 行前的 TIMESTEP 头缺少整数步号。")  # 记录结构缺失。
                    raw_headers += 1  # 记录字面头行。
                    pending = True  # 下一个行应是整数步号。
                    continue  # 本行不能同时作为步号。
                if pending:  # 刚刚遇到一个 TIMESTEP 头。
                    try:  # 验证对应步号是整数。
                        int(stripped)  # 只接受整数，而不是空行或其他头。
                        headers += 1  # 完整时间步头计数加一。
                    except ValueError:  # 头后不是合法整数。
                        result["errors"].append(f"第 {line_number} 行不是合法的 TIMESTEP 整数。")  # 记录具体位置。
                    pending = False  # 无论合法与否，结束该头检查。
            if pending:  # 文件结束时仍在等待整数步号。
                result["errors"].append("文件末尾 TIMESTEP 头缺少整数步号。")  # 记录末尾不完整。
            if raw_headers == 0:  # 没有文本 TIMESTEP 标记时无法确认格式。
                result["reason"] = "未找到文本 TIMESTEP 头；可能是其他受支持格式，文本扫描未覆盖。"  # 不根据文件名强行判断。
                return result  # 保留未覆盖状态。
            result.update({"covered": True, "timestep_headers": headers, "raw_timestep_headers": raw_headers})  # 返回实际头数及完整性信息。
    except Exception as exc:  # 捕获读取失败和压缩流损坏。
        result["errors"].append(describe_exception(exc))  # 保留异常类型和内容。
        result["reason"] = "文本头扫描读取失败，不能视作已完成。"  # 避免读取失败被解释为未损坏。
    finally:  # 所有返回路径均记录实际耗时。
        result["duration_seconds"] = time.perf_counter() - started  # 单独记录完整源扫描时间。
    return result  # 供检查器与运行器共同使用。


def inspect_script(script_path):  # 仅通过 AST 扫描脚本，不导入也不执行。
    result = {"path": str(script_path), "findings": [], "error": None}  # 准备脚本检查结果。
    try:  # 尝试读取脚本并解析语法树。
        source = script_path.read_text(encoding="utf-8-sig")  # 按 UTF-8 读取 Python 源文件。
        tree = ast.parse(source, filename=str(script_path))  # 构建语法树，不运行代码。
        parents = {}  # 创建节点到父节点的索引。
        for node in ast.walk(tree):  # 遍历所有语法节点。
            for child in ast.iter_child_nodes(node):  # 获取当前节点的直接子节点。
                parents[child] = node  # 记录子节点的父节点。
        for node in ast.walk(tree):  # 寻找可能重复初始化的调用。
            if not isinstance(node, ast.Call):  # 非函数调用无需检查。
                continue  # 跳过该节点。
            ancestors = []  # 保存当前调用的全部上层节点。
            current = parents.get(node)  # 从直接父节点开始追溯。
            while current is not None:  # 持续追溯至语法树根节点。
                ancestors.append(current)  # 记录一个上层节点。
                current = parents.get(current)  # 移动到更上层节点。
            in_loop = any(isinstance(item, (ast.For, ast.AsyncFor, ast.While)) for item in ancestors)  # 粗略判断调用是否位于循环中。
            func = node.func  # 读取被调用的函数表达式。
            if in_loop and isinstance(func, ast.Attribute) and func.attr == "append":  # 标记循环中的 append 候选。
                target = ast.unparse(func.value) if hasattr(ast, "unparse") else "unknown"  # 展示被追加的对象表达式。
                if "modifier" in target.lower():  # 仅提示名称看似与修饰器有关的追加。
                    result["findings"].append({"line": node.lineno, "kind": "modifier_append_in_loop", "detail": target})  # 标注需要人工确认的行。
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""  # 提取可能的函数名。
            if name == "import_file" and in_loop:  # 查找循环内重复导入输入的候选。
                result["findings"].append({"line": node.lineno, "kind": "import_file_in_loop", "detail": "循环内调用 import_file，需人工确认是否重复初始化。"})  # 记录候选问题。
            elif name == "import_file" and any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) for item in ancestors):  # 查找函数中的导入调用。
                result["findings"].append({"line": node.lineno, "kind": "import_file_in_function", "detail": "需确认是否在每个任务或每帧重复调用；初始化函数中调用可能合理。"})  # 避免将所有函数内导入误报为错误。
    except Exception as exc:  # 任何读取或解析失败均形成报告。
        result["error"] = describe_exception(exc)  # 记录异常信息。
    return result  # 返回静态检查结果。


def discover(paths, recursive):  # 按给定顺序展开文件与目录，不推断数据分组。
    found = []  # 保存实际发现的文件路径。
    issues = []  # 保存无法检查的输入项。
    seen = {}  # 用解析后的真实路径追踪重复引用。
    duplicates = []  # 保存明确的真实路径重复引用。
    for raw in paths:  # 逐一检查命令行输入。
        path = Path(raw).expanduser()  # 展开用户主目录写法。
        if not path.exists():  # 不存在的路径不能继续读取。
            issues.append({"path": str(path), "error": "路径不存在；不自动展开通配符。"})  # 保留具体错误。
            continue  # 继续检查其他输入。
        if path.is_dir():  # 单独处理目录输入。
            try:  # 目录可能因权限或其他原因无法列出。
                entries = sorted(path.rglob("*") if recursive else path.iterdir(), key=lambda x: str(x).lower())  # 只有明确要求才递归。
                candidates = [item for item in entries if item.is_file()]  # 只将实际文件列入检查。
                if not candidates:  # 空目录也应明确报告。
                    issues.append({"path": str(path), "error": "目录中没有发现文件。"})  # 记录空目录。
            except OSError as exc:  # 捕获目录访问失败。
                issues.append({"path": str(path), "error": describe_exception(exc)})  # 保存异常。
                continue  # 不终止其他路径检查。
        else:  # 非目录输入按单文件处理。
            candidates = [path]  # 包装成统一的文件列表。
        for candidate in candidates:  # 处理当前路径展开出的各个文件。
            try:  # 文件可能在扫描期间被删除。
                real = candidate.resolve(strict=True)  # 解析真实绝对路径。
                key = os.path.normcase(str(real))  # 在 Windows 按路径大小写规则去重。
                if key in seen:  # 同一真实路径不重复计算。
                    duplicates.append({"path": str(candidate), "same_as": seen[key]})  # 保留重复来源供用户核对。
                    continue  # 不再重复读取该文件。
                seen[key] = str(real)  # 记录已发现的真实路径。
                found.append(real)  # 将真实路径加入结果。
            except OSError as exc:  # 捕获文件状态变化或权限错误。
                issues.append({"path": str(candidate), "error": describe_exception(exc)})  # 报告失败项。
    return found, issues, duplicates  # 返回文件、错误和重复路径。


def inspect_frame(data, frame, np):  # 读取一个已载入帧的关键元数据，不运行分析修饰器。
    info = {"frame": frame, "readable": True, "timestep": data.attributes.get("Timestep"), "time": data.attributes.get("Time")}  # 保存帧索引及原始时间属性。
    cell = data.cell  # 读取仿真盒对象。
    if cell is not None and (not np.all(np.isfinite(np.asarray(cell.matrix))) or not np.isfinite(cell.volume)):  # 非有限盒参数不是有效数据。
        raise ValueError("仿真盒参数含 NaN 或无穷值。")  # 明确报告错误且保证 JSON 可序列化。
    if cell is None:  # 文件可能不含仿真盒。
        info["cell"] = None  # 显式标记盒信息缺失。
    else:  # 记录读者提供的盒子与边界信息。
        info["cell"] = {"matrix": np.asarray(cell.matrix).tolist(), "volume": float(cell.volume), "pbc_reader_reported": [bool(x) for x in cell.pbc], "is2d": bool(cell.is2D)}  # 不推断物理 PBC 是否已确认。
    particles = data.particles  # 获取粒子对象。
    if particles is None:  # 若没有粒子对象则保持信息透明。
        info.update({"particle_count": None, "types": None, "ids": {"present": False, "unique": None}})  # 缺失数据不伪造为零。
        return info  # 完成当前帧检查。
    info["particle_count"] = int(particles.count)  # 保留实际粒子总数。
    positions = particles.get("Position")  # 获取输入坐标。
    if positions is None or not np.all(np.isfinite(np.asarray(positions))):  # 缺少坐标或坐标不是有限值均无效。
        raise ValueError("粒子坐标缺失或包含 NaN/无穷值。")  # 避免将不可用的几何数据视为成功。
    types = particles.get("Particle Type")  # 获取读者识别的粒子类型。
    info["types"] = [int(v) for v in np.unique(np.asarray(types))] if types is not None else None  # 仅报告实际出现的数字类型。
    ids = particles.get("Particle Identifier")  # 获取粒子 ID 属性。
    if ids is None:  # 未提供 ID 时不能判断唯一性。
        info["ids"] = {"present": False, "unique": None}  # 与重复 ID 情况区分。
    else:  # 在当前帧检查 ID 是否有重复。
        values = np.asarray(ids)  # 获取 ID 数组。
        unique_count = int(np.unique(values).size)  # 计算唯一 ID 的数量。
        info["ids"] = {"present": True, "unique": unique_count == len(values), "unique_count": unique_count}  # 报告检查结果。
    for key in ("timestep", "time"):  # 将 NumPy 数值转换成 JSON 可序列化的 Python 标量。
        if isinstance(info[key], np.generic):  # 仅处理 NumPy 的标量类型。
            info[key] = info[key].item()  # 转成普通数字。
        if isinstance(info[key], float) and not np.isfinite(info[key]):  # 时间属性不能含非有限值。
            raise ValueError(f"{key} 含 NaN 或无穷值。")  # 明确报告不可序列化的异常。
    return info  # 返回当前帧检查结果。


def inspect_file(path, args, ovito, np):  # 用 OVITO 实际读取单个文件并提取元数据。
    report = {"path": str(path), "status": "error", "format": None, "reader": None, "frame_count": None, "frames": [], "questions": [], "errors": [], "warnings": []}  # 初始化不会掩盖失败的报告。
    before = None  # 初始化检查前的文件状态。
    try:  # 文件可能在检查期间消失或读取失败。
        before = file_stat(path)  # 在任何读取之前采集状态。
        report["stat_before"] = before  # 保存检查前元数据。
        if before["size_bytes"] == 0:  # 空文件不能作为有效输入。
            raise ValueError("输入文件为空。")  # 使用统一异常路径报告。
        if "%" in path.name:  # 含百分号的文件名可能涉及 LAMMPS 处理器分片。
            report["errors"].append("文件名含 %；可能是处理器分片，请先确认并合并，禁止自动按时间序列处理。")  # 明确提示停止自动分组。
            return report  # 不尝试把可疑分片作为独立轨迹载入。
        from ovito.io import import_file  # 在使用时导入 OVITO 文件读取入口。
        try:  # 先通过文件内容让 OVITO 选择实际读取器。
            pipeline = import_file(str(path), multiple_frames=True)  # 创建只含数据源的流水线。
        except Exception as original:  # data 缺少 atom style 时允许使用用户指定的样式重试。
            if not args.atom_style:  # 没有用户指定样式时不能猜测。
                raise original  # 将实际错误交给统一处理。
            kwargs = {"multiple_frames": True, "atom_style": args.atom_style}  # 准备已确认的样式。
            if args.atom_substyles:  # hybrid 情况下继续提供子类型。
                kwargs["atom_substyles"] = args.atom_substyles  # 保留用户指定的子类型。
            pipeline = import_file(str(path), **kwargs)  # 只按用户设置重试。
        reader = type(pipeline.source.importer).__name__  # 获取 OVITO 实际选择的读取器类名。
        if "LAMMPSDataImporter" in reader and args.atom_style:  # data 文件应遵循用户明确指定的样式。
            kwargs = {"multiple_frames": True, "atom_style": args.atom_style}  # 重新构建 data 导入选项。
            if args.atom_substyles:  # 若提供了 hybrid 子类型。
                kwargs["atom_substyles"] = args.atom_substyles  # 一并传入读取器。
            pipeline = import_file(str(path), **kwargs)  # 按确认的样式重新初始化，不静默忽略参数。
            reader = type(pipeline.source.importer).__name__  # 重新核实读取器。
        report["reader"] = reader  # 记录读取器名称供审计。
        if "LAMMPSDataImporter" in reader:  # 匹配 LAMMPS data 的实际读取器。
            report["format"] = "lammps/data"  # 标记确认的输入格式。
            header = data_header_style(path)  # 有限读取头部，提取可能的 Atoms 注释。
            reader_style = str(getattr(pipeline.source.importer, "atom_style", "unknown"))  # 如实记录读取器暴露的实际值。
            source = "user" if args.atom_style else "header" if header["hint"] else "reader_guess" if header["inspection"] == "inspected" else "unknown"  # 不将推断误报为用户确认。
            report["atom_style"] = {"reader_value": reader_style, "user_value": args.atom_style, "header_hint": header["hint"], "header_inspection": header["inspection"], "source": source, "header_detail": header["detail"]}  # 同时保留所有来源与不确定性。
            if source in ("reader_guess", "unknown"):  # 缺少样式注释或无法读取注释时需确认。
                report["questions"].append("data 的 atom style 未经明确指定或确认；请核对真实格式并指定 --atom-style。")  # 读者自动猜测不构成确认。
            if args.atom_style and header["hint"] and args.atom_style.lower() != header["hint"].lower():  # 用户指定与头部提示冲突。
                report["warnings"].append("指定的 atom style 与文件头提示不同；当前采用用户指定值，请核对。")  # 显式保留冲突。
            report["questions"].append("data 文件不可靠地记录 PBC；请根据模拟设置确认三个方向的边界。")  # 不把读者默认 PBC 作为模拟事实。
        elif "LAMMPS" in reader and "DumpImporter" in reader:  # 匹配不同版本的 LAMMPS dump 读取器。
            report["format"] = "lammps/dump"  # 标记确认的输入格式。
        else:  # 其他读者不在当前运行器接受范围。
            report["errors"].append(f"OVITO 识别为 {reader}；首版运行器仅接受 LAMMPS data/dump。")  # 不根据扩展名强行更改判断。
            return report  # 不把其他格式伪装成支持的输入。
        count = int(pipeline.source.num_frames)  # 获取读者发现的帧数。
        report["frame_count"] = count  # 将帧数写入报告。
        if args.all_frames and report["format"] == "lammps/dump":  # 仅完整检查时额外流式扫描文本头。
            scan = scan_text_dump(path)  # 单独计时并获取全部可识别时间步头。
            report["text_dump_scan"] = scan  # 始终保留覆盖状态和耗时。
            if scan["errors"]:  # 压缩流损坏或时间步头错误都不能忽略。
                report["errors"].extend("完整头扫描：" + issue for issue in scan["errors"])  # 纳入文件错误。
            if scan["covered"] and scan["timestep_headers"] != count:  # 比较原始源头数量和 OVITO 可发现帧数。
                report["errors"].append(f"文本源含 {scan['timestep_headers']} 个完整 TIMESTEP 头，但 OVITO 仅发现 {count} 帧；请检查输入。")  # 防止静默漏帧。
            if not scan["covered"]:  # 明确指出扫描没有覆盖二进制、YAML 等。
                report["warnings"].append("完整文本头扫描未覆盖：" + str(scan["reason"]))  # 不将不支持的格式自动判为损坏。
        if count < 1:  # 零帧不能视作有效数据。
            raise ValueError("未发现可读取的帧。")  # 明确报告异常。
        indices = range(count) if args.all_frames else sorted({0, count // 2, count - 1})  # 默认抽查首中末，可选完整逐帧。
        report["scan_mode"] = "all" if args.all_frames else "sample_first_middle_last"  # 明确扫描覆盖范围。
        report["warnings"].append("完整文本头比对无法证明每个原子记录的语义均正确，也不能发现缺少头部的尾部残留。")  # 不夸大完整扫描的保证。
        for frame in indices:  # 逐个读取所需帧。
            try:  # 某一帧损坏时继续保留其他帧的检查结果。
                frame_info = inspect_frame(pipeline.compute(frame), frame, np)  # 读取帧与元数据，不运行 DXA。
                report["frames"].append(frame_info)  # 追加帧的实际检查结果。
                if frame_info["particle_count"] in (None, 0):  # 空粒子帧需要明确复核。
                    report["errors"].append(f"帧 {frame} 无粒子数据。")  # 标记无效帧。
                if frame_info["cell"] is None or frame_info["cell"]["volume"] <= 0:  # DXA 需要有效盒信息。
                    report["errors"].append(f"帧 {frame} 盒信息缺失或体积无效。")  # 标记不合格盒。
                if frame_info["cell"] is not None and frame_info["cell"]["is2d"]:  # 本工具仅提供常规三维 DXA 路径。
                    report["errors"].append(f"帧 {frame} 为二维盒；不属于本工具的常规三维 DXA 支持路径。")  # 二维问题须另行确认处理。
                if not frame_info["ids"]["present"]:  # 缺少 ID 不代表单帧 DXA 自动失败。
                    report["questions"].append(f"帧 {frame} 未提供原子 ID；若要跨帧匹配原子需补充或确认。")  # 根据用途提出问题。
                elif not frame_info["ids"]["unique"]:  # 重复 ID 不适合跨帧对应。
                    report["warnings"].append(f"帧 {frame} 的原子 ID 不唯一；不能据此可靠匹配跨帧原子，单帧几何 DXA 不因该检查单独拒绝。")  # 与单帧几何分析区分。
                if frame_info["types"] is None:  # 类型缺失影响后续按类型选择。
                    report["questions"].append(f"帧 {frame} 没有粒子类型；如需类型选择请确认来源。")  # 不猜测类型。
            except Exception as exc:  # 捕获单帧损坏与其他读取异常。
                report["frames"].append({"frame": frame, "readable": False, "error": describe_exception(exc)})  # 记录失败帧和原因。
                report["errors"].append(f"帧 {frame} 读取失败：{describe_exception(exc)}")  # 将其纳入总体状态。
        if not args.all_frames and count > len(report["frames"]):  # 抽样不能确认未读取帧完好。
            report["warnings"].append("仅抽查首、中、末帧，不能排除其他帧损坏或截断。")  # 限定报告结论。
        report["questions"].append("请按输出需要确认长度单位，以及按类型选择所需的数字原子类型含义；OVITO 不自动确定物理单位。")  # 避免无条件要求时间元数据。
        if report["format"] == "lammps/dump":  # dump 可能缺少物理时间。
            report["warnings"].append("帧序号不是模拟步；仅在需要物理时间轴且 Time 缺失时，才需确认时间单位、步长与原点；单帧或几何 DXA 不以此为前提。")  # 不将帧索引冒充时间。
        report["status"] = "ok" if not report["errors"] else "error"  # 只有没有发现读取错误才标记检查通过。
        if args.hash:  # 用户明确要求时才完整计算文件哈希。
            report["sha256"] = sha256_file(path)  # 原始文件逐块读取，不解压后哈希。
    except Exception as exc:  # 捕获导入、读取及状态查询错误。
        report["errors"].append(describe_exception(exc))  # 保留原始异常信息。
    finally:  # 无论成功或失败都检查文件是否变化。
        try:  # 文件可能已在检查期间被删除。
            after = file_stat(path)  # 获取检查结束时的状态。
            report["stat_after"] = after  # 保留实际状态。
            report["changed_during_inspection"] = before is not None and before != after  # 与检查前状态比较。
            if report["changed_during_inspection"]:  # 若变化则不能视为稳定结果。
                report["errors"].append("文件在检查期间发生变化；请在文件稳定后重新检查。")  # 提示重新检查。
                report["status"] = "error"  # 将总体状态置为失败。
        except OSError as exc:  # 文件状态不可读时也不应冒充成功。
            report["changed_during_inspection"] = None  # 无法完成前后比较。
            report["errors"].append("无法复核文件状态：" + describe_exception(exc))  # 保存具体异常。
            report["status"] = "error"  # 将总体状态置为失败。
    return report  # 返回该文件的完整检查记录。


def main():  # 实现命令行入口。
    if hasattr(sys.stdout, "reconfigure"):  # 确保 Windows 重定向的 JSON 使用 UTF-8。
        sys.stdout.reconfigure(encoding="utf-8")  # 设置标准输出编码。
    parser = argparse.ArgumentParser(description="只读检查 OVITO DXA 的 LAMMPS 输入；不执行 DXA。")  # 创建参数解析器。
    parser.add_argument("--paths", nargs="+", help="一个或多个现有文件或目录。")  # 接收用户提供的路径。
    parser.add_argument("--recursive", action="store_true", help="显式递归扫描目录。")  # 默认仅列出目录直接子文件。
    parser.add_argument("--all-frames", action="store_true", help="读取全部帧；默认抽查首中末。")  # 控制读取范围。
    parser.add_argument("--atom-style", help="明确 LAMMPS data 的 atom style。")  # 处理 data 文件缺少类型提示。
    parser.add_argument("--atom-substyles", nargs="+", help="hybrid atom style 的子类型列表。")  # 处理 hybrid 样式。
    parser.add_argument("--script", help="静态检查现有 Python 脚本，不执行脚本。")  # 允许用户检查历史实现。
    parser.add_argument("--hash", action="store_true", help="额外完整读取文件以计算 SHA-256。")  # 默认不增加大文件全量读取。
    parser.add_argument("--output", help="显式指定新的 JSON 报告路径；不覆盖已有文件。")  # 保存报告必须明确授权。
    args = parser.parse_args()  # 解析用户输入。
    if not args.paths and not args.script:  # 至少要有输入文件或待审查的脚本。
        parser.error("请提供 --paths 或 --script。")  # 两项均缺失时不执行空检查。
    if args.atom_substyles and not args.atom_style:  # 子样式必须与用户明确提供的主样式配套。
        parser.error("--atom-substyles 必须与 --atom-style 一起使用。")  # 不猜测 data 文件的主样式。
    result = {"schema_version": 1, "tool": "inspect_inputs", "environment": {}, "inputs": [], "duplicates": [], "path_errors": [], "script_review": None, "questions": [], "warnings": []}  # 初始化顶层报告结构。
    result["environment"] = {"python": sys.version, "executable": sys.executable, "platform": platform.platform(), "logical_cpus": os.cpu_count()}  # 记录当前运行环境。
    try:  # psutil 可用时补充实际资源信息。
        import psutil  # 导入用于获取系统资源的库。
        memory = psutil.virtual_memory()  # 读取系统内存状态。
        result["environment"].update({"available_memory_bytes": memory.available, "total_memory_bytes": memory.total, "available_cpus": len(psutil.Process().cpu_affinity()) if hasattr(psutil.Process(), "cpu_affinity") else None})  # 报告当前资源，不据此自动配置并发。
    except Exception as exc:  # 资源信息不可用不应阻止文件检查。
        result["warnings"].append("部分资源信息不可用：" + describe_exception(exc))  # 明确说明资源信息缺失。
    files, result["path_errors"], result["duplicates"] = discover(args.paths or [], args.recursive)  # 展开路径并检查重复。
    if len(files) > 1:  # 多个输入文件的实际关系无法自动推断。
        result["questions"].append("请确认哪些文件属于同一轨迹、哪些属于独立工况，以及时间顺序；本工具没有自动分组。")  # 要求明确分组。
        result["questions"].append("如果这些文件是 LAMMPS 按处理器输出的 % 分片，请先使用受支持方法合并；文件名本身不能可靠排除分片。")  # 提醒数字文件名也可能是分片。
    if args.script:  # 用户明确指定时才读取脚本。
        result["script_review"] = inspect_script(Path(args.script).expanduser())  # 只做 AST 静态检查。
        result["warnings"].append("脚本审查是启发式静态检查，无法证明调用次数、科学正确性或运行时性能。")  # 限定审查范围。
    if args.paths:  # 只有需要检查模拟文件时才导入 OVITO。
        try:  # 使用当前解释器导入已安装的 OVITO。
            import numpy as np  # 导入数组检查所需的库。
            import ovito  # 仅导入 OVITO，不修改环境。
            result["environment"]["ovito_version"] = ovito.version_string  # 保存准确版本。
            result["inputs"] = [inspect_file(path, args, ovito, np) for path in files]  # 分别只读检查每个源文件。
        except Exception as exc:  # 如果当前环境没有正确激活或缺失依赖。
            result["environment"]["ovito_error"] = describe_exception(exc)  # 报告实际导入错误。
            result["warnings"].append("请使用已正确激活且包含 OVITO 的 Python 环境；本工具不会安装或升级依赖。")  # 给出明确但不修改环境的提示。
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False)  # 将报告转换为 UTF-8 友好的 JSON。
    print(text)  # 报告始终写入标准输出。
    if args.output:  # 仅在用户明确指定时写报告文件。
        output = Path(args.output).expanduser().resolve()  # 解析目标文件的绝对路径。
        try:  # 防止覆盖用户已有文件，包括输入文件。
            with output.open("x", encoding="utf-8", newline="\n") as stream:  # 仅当目标文件不存在时创建。
                stream.write(text + "\n")  # 将同一份报告保存为 JSON。
        except OSError as exc:  # 保存失败时将错误单独写入标准错误。
            print("报告保存失败：" + describe_exception(exc), file=sys.stderr)  # 不污染 JSON 标准输出。
            return 2  # 显示非零退出状态。
    failures = bool(result["path_errors"] or result["environment"].get("ovito_error") or any(item["status"] != "ok" for item in result["inputs"]))  # 汇总文件与环境检查错误。
    failures = failures or bool(result["script_review"] and result["script_review"]["error"])  # 静态脚本不可读同样报错。
    return 1 if failures else 0  # 返回机器可检查的退出状态。


if __name__ == "__main__":  # 仅在作为脚本运行时执行命令行入口。
    raise SystemExit(main())  # 将入口结果转换为进程退出码。

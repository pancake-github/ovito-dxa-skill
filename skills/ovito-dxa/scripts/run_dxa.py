#!/usr/bin/env python
# 支持独立解释器和 Notebook 薄入口；本模块的主进程路径不导入 OVITO。
import argparse  # 导入 argparse，用于解析命令行中的清单路径与执行模式。
import concurrent.futures as futures  # 导入 concurrent.futures，用于调度可受限的进程池任务。
from concurrent.futures.process import BrokenProcessPool  # 识别 worker 硬退出导致的不可恢复进程池损坏。
import csv  # 导入 csv，用于写出可供表格软件读取的长表。
import gzip  # 导入 gzip，用于读取 gzip 格式数据头。
import hashlib  # 导入 hashlib，用于计算输入和结果的 SHA-256。
import html  # 导入 html，用于转义报告中的文本，避免破坏 HTML。
import json  # 导入 json，用于读写清单、检查点和报告。
import math  # 导入 math，用于检查有限数值并进行容差比较。
import multiprocessing as mp  # 导入 multiprocessing，用于通过 spawn 建立独立工作进程。
import os  # 导入 os，用于处理文件环境变量及原子替换。
import platform  # 记录调度比较所用机器的匿名签名，避免跨机器套用耗时结论。
from pathlib import Path  # 导入 pathlib，用于构造跨平台绝对路径。
import re  # 导入 re，用于验证标识符及识别 data 文件头。
import sys  # 导入 sys，用于访问解释器版本和标准错误。
import threading  # 导入 threading，用于在后台采样进程树内存。
import time  # 导入 time，用于测量任务墙钟时间。
import traceback  # 导入 traceback，用于保留失败帧的异常堆栈。
import uuid  # 导入 uuid，用于为临时文件分配不冲突名称。

# 进程池 worker 的私有缓存，不在进程间传递 OVITO 对象。
_PIPELINES = {}  # 计算并保存变量 _PIPELINES 的当前值。
_CASES = {}  # 计算并保存变量 _CASES 的当前值。
_OUT = None  # 计算并保存变量 _OUT 的当前值。
_DXA_KEYS = {  # 计算并保存变量 _DXA_KEYS 的当前值。
    'trial_circuit_length', 'circuit_stretchability', 'line_smoothing_enabled',  # 设置或传递此项参数，供当前操作使用。
    'line_smoothing_level', 'line_coarsening_enabled', 'line_point_separation',  # 设置或传递此项参数，供当前操作使用。
    'only_perfect_dislocations', 'defect_mesh_smoothing_level',  # 设置或传递此项参数，供当前操作使用。
}  # 结束当前参数列表或数据结构。
_LATTICES = {'BCC', 'FCC', 'HCP', 'CubicDiamond', 'HexagonalDiamond'}  # 计算并保存变量 _LATTICES 的当前值。
_OK = {'success', 'zero_dislocations', 'no_crystalline_atoms'}  # 计算并保存变量 _OK 的当前值。


def _hash_file(path):  # 分块校验文件内容，避免将大文件整体载入内存。
    # 分块校验文件内容，避免将大文件整体载入内存。
    digest = hashlib.sha256()  # 创建 SHA-256 摘要对象，后续逐块加入源文件字节。
    with open(path, 'rb') as stream:  # 进入资源管理上下文，退出时自动释放对应资源。
        for block in iter(lambda: stream.read(1024 * 1024), b''):  # 逐项遍历当前集合，分别执行缩进内的操作。
            digest.update(block)  # 把读取到的数据块加入文件摘要。
    return digest.hexdigest()  # 返回整个文件的十六进制 SHA-256 摘要。


def _source_stat(path):  # 比较字节长度与纳秒修改时间，以发现读取期间源文件发生变化。
    # 比较字节长度与纳秒修改时间，以发现读取期间源文件发生变化。
    value = Path(path).stat()  # 读取源文件大小与最后修改时间。
    return {'size': value.st_size, 'mtime_ns': value.st_mtime_ns}  # 同时返回字节数和纳秒级时间戳用于前后比较。


def _record_digest(record):  # 摘要覆盖检查点内容和分析指纹，而不是只相信 status 字段。
    # 摘要覆盖检查点内容和分析指纹，而不是只相信 status 字段。
    content = {k: v for k, v in record.items() if k != '_content_sha256'}  # 计算检查点摘要时排除摘要自身，其余内容全部参与校验。
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()  # 返回本函数计算的结果或检查结论。


def _verify_sources(stats, out, manifest):  # 输入运行期间变动时永久标记本次运行无效，避免恢复混合数据。
    # 输入运行期间变动时永久标记本次运行无效，避免恢复混合数据。
    try:  # 开始受控操作，并在失败时交由异常分支处理。
        changed = any(_source_stat(path) != value for path, value in stats.items())  # 计算并保存变量 changed 的当前值。
    except OSError:  # 捕获指定错误并执行相应的恢复或报告。
        changed = True  # 计算并保存变量 changed 的当前值。
    if changed:  # 检查本行条件，满足时执行其下的分支。
        manifest['invalid_reason'] = '输入文件在运行期间变动或消失'  # 设置或传递此项参数，供当前操作使用。
        _atomic_json(Path(out) / 'manifest.json', manifest)  # 调用 _atomic_json 完成本行所示的处理。
        raise RuntimeError(manifest['invalid_reason'])  # 明确报告当前错误，避免继续产生误导性结果。


def _atomic_json(path, value):  # 在同目录创建临时文件并原子替换，避免中途退出留下完整假象。
    # 在同目录创建临时文件并原子替换，避免中途退出留下完整假象。
    path = Path(path)  # 计算并保存当前文件路径。
    path.parent.mkdir(parents=True, exist_ok=True)  # 调用 path.parent.mkdir 完成本行所示的处理。
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')  # 计算并保存变量 temp 的当前值。
    try:  # 开始受控操作，并在失败时交由异常分支处理。
        with temp.open('w', encoding='utf-8', newline='\n') as stream:  # 进入资源管理上下文，退出时自动释放对应资源。
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)  # 调用 json.dump 完成本行所示的处理。
            stream.flush()  # 将 Python 缓冲区写入操作系统。
            os.fsync(stream.fileno())  # 请求操作系统将检查点内容同步到磁盘。
        os.replace(temp, path)  # 同目录原子替换目标，避免把不完整文件当作结果。
    finally:  # 无论是否发生异常，都执行清理和收尾操作。
        if temp.exists():  # 检查本行条件，满足时执行其下的分支。
            temp.unlink()  # 删除未能成功替换的临时文件。


def _normalize_config(path):  # 校验科学字段，不猜测材料结构、原子类型、周期性或单位。
    # 校验科学字段，不猜测材料结构、原子类型、周期性或单位。
    path = Path(path).resolve()  # 计算并保存当前文件路径。
    config = json.loads(path.read_text(encoding='utf-8-sig'))  # 计算并保存任务清单。
    if config.get('schema_version') != 1 or config.get('confirmed') is not True:  # 不满足要求时拒绝执行：schema_version 必须为 1，且 confirmed 必须为 true。
        raise ValueError('schema_version 必须为 1，且 confirmed 必须为 true')  # 明确报告当前错误，避免继续产生误导性结果。
    if not isinstance(config.get('output_dir'), str) or not config['output_dir']:  # 不满足要求时拒绝执行：必须提供 output_dir。
        raise ValueError('必须提供 output_dir')  # 明确报告当前错误，避免继续产生误导性结果。
    config['output_dir'] = str((path.parent / config['output_dir']).resolve())  # 设置或传递此项参数，供当前操作使用。
    if not isinstance(config.get('cases'), list) or not config['cases']:  # 不满足要求时拒绝执行：cases 必须是非空列表。
        raise ValueError('cases 必须是非空列表')  # 明确报告当前错误，避免继续产生误导性结果。
    seen = set()  # 计算并保存变量 seen 的当前值。
    for case in config['cases']:  # 逐项遍历当前集合，分别执行缩进内的操作。
        ident = case.get('id')  # 计算并保存变量 ident 的当前值。
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', ident) or ident in ('.', '..') or ident.casefold() in seen:  # 不满足要求时拒绝执行：case id 非法或重复。
            raise ValueError('case id 非法或重复')  # 明确报告当前错误，避免继续产生误导性结果。
        seen.add(ident.casefold())  # 调用 seen.add 完成本行所示的处理。
        if not isinstance(case.get('sources'), list) or not case['sources']:  # 不满足要求时拒绝执行：该案例：sources 必须是非空列表。
            raise ValueError(f'{ident}: sources 必须是非空列表')  # 明确报告当前错误，避免继续产生误导性结果。
        if any(not isinstance(s, str) or any(c in s for c in '*?%') for s in case['sources']):  # 不满足要求时拒绝执行：该案例：sources 不允许通配符或处理器分片。
            raise ValueError(f'{ident}: sources 不允许通配符或处理器分片')  # 明确报告当前错误，避免继续产生误导性结果。
        case['sources'] = [str((path.parent / s).resolve()) for s in case['sources']]  # 设置或传递此项参数，供当前操作使用。
        if len(set(case['sources'])) != len(case['sources']) or any(not Path(s).is_file() for s in case['sources']):  # 不满足要求时拒绝执行：该案例：输入文件缺失或重复。
            raise ValueError(f'{ident}: 输入文件缺失或重复')  # 明确报告当前错误，避免继续产生误导性结果。
        if case.get('input_format') not in ('lammps/data', 'lammps/dump'):  # 不满足要求时拒绝执行：该案例：input_format 不受支持。
            raise ValueError(f'{ident}: input_format 不受支持')  # 明确报告当前错误，避免继续产生误导性结果。
        if case['input_format'] == 'lammps/data' and not case.get('atom_style'):  # 检查本行条件，满足时执行其下的分支。
            for source in case['sources']:  # 逐项遍历当前集合，分别执行缩进内的操作。
                opener = gzip.open if source.lower().endswith('.gz') else open  # 计算并保存变量 opener 的当前值。
                with opener(source, 'rt', encoding='utf-8', errors='replace') as stream:  # 进入资源管理上下文，退出时自动释放对应资源。
                    atoms_header = next((line for line in stream if re.match(r'^\s*Atoms(?:\s*(?:#|$))', line)), '')  # 计算并保存变量 atoms_header 的当前值。
                if not re.search(r'\bAtoms\s*#\s*\S+', atoms_header):  # 不满足要求时拒绝执行：该案例：data 文件缺少 Atoms 风格提示，必须明确 atom_style。
                    raise ValueError(f'{ident}: data 文件缺少 Atoms 风格提示，必须明确 atom_style')  # 明确报告当前错误，避免继续产生误导性结果。
        if case.get('lattice') not in _LATTICES:  # 不满足要求时拒绝执行：该案例：lattice 不受支持。
            raise ValueError(f'{ident}: lattice 不受支持')  # 明确报告当前错误，避免继续产生误导性结果。
        selection = case.get('selection', {})  # 计算并保存用户确认的原子选区。
        if selection.get('mode') not in ('all', 'types'):  # 不满足要求时拒绝执行：该案例：selection 必须明确。
            raise ValueError(f'{ident}: selection 必须明确')  # 明确报告当前错误，避免继续产生误导性结果。
        if selection['mode'] == 'types' and (not isinstance(selection.get('type_ids'), list) or not selection['type_ids'] or any(type(v) is not int or v <= 0 for v in selection['type_ids'])):  # 不满足要求时拒绝执行：该案例：type_ids 必须为非空正整数列表。
            raise ValueError(f'{ident}: type_ids 必须为非空正整数列表')  # 明确报告当前错误，避免继续产生误导性结果。
        pbc = case.get('pbc')  # 计算并保存变量 pbc 的当前值。
        if pbc is None and case['input_format'] == 'lammps/data':  # 不满足要求时拒绝执行：该案例：data 文件必须确认 pbc。
            raise ValueError(f'{ident}: data 文件必须确认 pbc')  # 明确报告当前错误，避免继续产生误导性结果。
        if pbc is not None and (not isinstance(pbc, list) or len(pbc) != 3 or any(type(x) is not bool for x in pbc)):  # 不满足要求时拒绝执行：该案例：pbc 应为三个布尔值或 null。
            raise ValueError(f'{ident}: pbc 应为三个布尔值或 null')  # 明确报告当前错误，避免继续产生误导性结果。
        if case.get('units', {}).get('length') not in ('angstrom', 'nm', 'm', 'reduced', 'unknown') or 'time' not in case.get('units', {}):  # 不满足要求时拒绝执行：该案例：必须确认 length 和 time 单位。
            raise ValueError(f'{ident}: 必须确认 length 和 time 单位')  # 明确报告当前错误，避免继续产生误导性结果。
        mapping = case.get('time_mapping')  # 读取用户确认的时间步映射参数。
        if mapping is not None and (not isinstance(mapping, dict) or not isinstance(mapping.get('timestep_size'), (int, float)) or mapping['timestep_size'] <= 0 or type(mapping.get('origin_step')) is not int or not isinstance(mapping.get('origin_time'), (int, float))):  # 不满足要求时拒绝执行：该案例：time_mapping 不完整。
            raise ValueError(f'{ident}: time_mapping 不完整')  # 明确报告当前错误，避免继续产生误导性结果。
        dxa = case.get('dxa')  # 读取用户提供的 DXA 参数字典，不能把它当作 OVITO 修改器。
        if not isinstance(dxa, dict) or set(dxa) - _DXA_KEYS:  # 不满足要求时拒绝执行：该案例：dxa 含未知参数或不是对象。
            raise ValueError(f'{ident}: dxa 含未知参数或不是对象')  # 明确报告当前错误，避免继续产生误导性结果。
        density = case.get('density', {})  # 读取用户确认的位错密度计算模式。
        if density.get('mode') not in ('none', 'cell') or (density.get('mode') == 'cell' and density.get('confirmed') is not True):  # 不满足要求时拒绝执行：该案例：density 必须明确且 cell 须确认。
            raise ValueError(f'{ident}: density 必须明确且 cell 须确认')  # 明确报告当前错误，避免继续产生误导性结果。
        if case.get('output', {}).get('level') not in ('statistics', 'geometry'):  # 不满足要求时拒绝执行：该案例：output.level 必须明确。
            raise ValueError(f'{ident}: output.level 必须明确')  # 明确报告当前错误，避免继续产生误导性结果。
        if 'frames' not in case:  # 不满足要求时拒绝执行：该案例：缺少 frames。
            raise ValueError(f'{ident}: 缺少 frames')  # 明确报告当前错误，避免继续产生误导性结果。
    resource = config.setdefault('resources', {})  # 计算并保存变量 resource 的当前值。
    resource.setdefault('scheduling', 'case_sequential')  # 默认依次处理工况，每个工况内部再按帧并行。
    if resource['scheduling'] not in ('case_sequential', 'cross_case'):  # 两种策略均保持科学计算定义不变。
        raise ValueError('scheduling 必须为 case_sequential 或 cross_case')  # 不猜测未支持的调度名称。
    if 'scheduling_evidence' in resource:  # 仅显式选择跨工况时需要已有的小样本比较证据。
        if not isinstance(resource['scheduling_evidence'], str) or not resource['scheduling_evidence']:  # 证据路径必须是实际非空路径。
            raise ValueError('scheduling_evidence 必须为非空报告路径')  # 不接受布尔值或未填入的空路径。
        resource['scheduling_evidence'] = str((path.parent / resource['scheduling_evidence']).resolve())  # 按清单位置解释相对路径。
    resource.setdefault('workers', 'auto')  # 默认根据 CPU、内存预算和实际帧块数选择进程数，不固定为两个。
    if resource['workers'] != 'auto' and (type(resource['workers']) is not int or resource['workers'] < 1):  # 显式正整数作为使用者指定的并发上限。
        raise ValueError('workers 必须为 auto 或正整数')  # 拒绝布尔值、零、负数和无法解释的字符串。
    for key, default in [('chunk_size', 1), ('max_pending_factor', 2), ('benchmark_max_workers', 4), ('benchmark_max_frames', 6)]:  # 可选对比最多抽取六帧，避免把全量串行作为前置。
        resource.setdefault(key, default)  # 调用 resource.setdefault 完成本行所示的处理。
        if type(resource[key]) is not int or resource[key] < 1:  # 不满足要求时拒绝执行：{key} 必须为正整数。
            raise ValueError(f'{key} 必须为正整数')  # 明确报告当前错误，避免继续产生误导性结果。
    resource.setdefault('ovito_threads', 1)  # 每进程默认一个 OVITO 线程，防止多进程叠加内部线程造成争抢。
    if resource['ovito_threads'] != 'auto' and (type(resource['ovito_threads']) is not int or resource['ovito_threads'] < 1):  # 不满足要求时拒绝执行：ovito_threads 必须为 auto 或正整数。
        raise ValueError('ovito_threads 必须为 auto 或正整数')  # 明确报告当前错误，避免继续产生误导性结果。
    for key, default in [('reserve_memory_fraction', 0.25), ('reserve_cpu_fraction', 0.1)]:  # 为其他程序和主进程增长保留资源余量。
        resource.setdefault(key, default)  # 使用者可按实际机器负载明确调整预留比例。
        if type(resource[key]) not in (int, float) or not math.isfinite(resource[key]) or not 0 <= resource[key] < 1:  # 仅接受有限且位于半开区间的数值。
            raise ValueError(f'{key} 必须为 [0,1) 内的有限数值')  # 不把非法比例静默当作默认值。
    return config  # 返回本函数计算的结果或检查结论。


def _available_cpu():  # 优先读取当前进程可用的逻辑 CPU，避免把受限环境的全机核数当作预算。
    import psutil  # 此处只查询资源，不导入 OVITO。
    try:  # Windows 和部分 Unix 支持查询进程的 CPU 亲和性。
        return max(1, len(psutil.Process().cpu_affinity()))  # 仅使用系统允许当前进程运行的逻辑 CPU 数。
    except (AttributeError, NotImplementedError, psutil.Error):  # 不支持亲和性时采用解释器提供的可用信息。
        process_count = getattr(os, 'process_cpu_count', os.cpu_count)  # 新版 Python 优先提供进程级计数，旧版使用系统逻辑 CPU 数。
        return max(1, process_count() or 1)  # 无法取得核数时只允许一个线程预算。


def _plan_resources(resources, job_count, cpu, available_memory, parent_rss, per_worker_estimate, platform_limit):  # 纯资源规划，不执行 DXA，不增加单进程试跑。
    cpu_budget = max(1, math.floor(cpu * (1 - resources.get('reserve_cpu_fraction', 0.1))))  # 按逻辑 CPU 数保留约一成余量，至少允许一个计算线程。
    memory_budget = int(available_memory * (1 - resources.get('reserve_memory_fraction', 0.25)))  # available 已排除现有父进程内存，此预算仅分配给新增 worker。
    chunks = math.ceil(job_count / resources.get('chunk_size', 1))  # 真正可同时提交的是帧块；一大块不能假装有许多并行任务。
    requested = resources.get('workers', 'auto')  # auto 不另设固定上限；显式正整数作为用户给出的最大进程数。
    configured_threads = resources.get('ovito_threads', 1)  # 默认每个 worker 一个 OVITO 线程，避免嵌套并行过度占用。
    threads = 1 if configured_threads == 'auto' else configured_threads  # 先按单线程估算 auto 配置的进程空间。
    limits = {'cpu': cpu_budget // threads, 'tasks': chunks, 'memory': memory_budget // per_worker_estimate, 'platform': platform_limit, 'requested': None if requested == 'auto' else requested}  # 分别保留各约束的进程上限，便于解释选择。
    workers = min(value for value in limits.values() if value is not None) if job_count else 0  # 无待算任务时不启动任何 DXA worker。
    if job_count and limits['cpu'] < 1:  # 显式线程数超过预算时不能通过强制一个进程绕过检查。
        raise ValueError(f'每进程 {threads} 个线程超过 CPU 预算 {cpu_budget}；请调整 ovito_threads 或 reserve_cpu_fraction')  # 给出具体的资源冲突。
    if job_count and limits['memory'] < 1:  # 连一个 worker 都容不下时明确停止，而不是继续触发换页或内存不足。
        raise RuntimeError(f'可用内存预算 {memory_budget / 1048576:.1f} MiB 不足以容纳一个估计需 {per_worker_estimate / 1048576:.1f} MiB 的 worker')  # 报告启发式启动估算及其预算。
    if configured_threads == 'auto' and workers == 1:  # 仅使用者选择内部自动线程且实际只有一个 worker 时，使用 CPU 线程预算。
        threads = cpu_budget  # 显式设置整数线程数，仍尊重当前进程可用 CPU 和预留余量。
    reasons = [key for key, value in limits.items() if value is not None and value == workers] if job_count else ['no_pending_frames']  # 记录决定实际进程数的约束，而不是只返回一个数字。
    return {'requested_workers': requested, 'workers': workers, 'ovito_threads': threads, 'cpu_available': cpu, 'cpu_budget': cpu_budget, 'available_memory_bytes': available_memory, 'memory_budget_bytes': memory_budget, 'parent_rss_bytes': parent_rss, 'per_worker_estimate_bytes': per_worker_estimate, 'estimated_peak_bytes_before_start': parent_rss + workers * per_worker_estimate, 'job_count': job_count, 'task_chunks': chunks, 'limits': limits, 'limiting_factors': reasons}  # 将规划依据写入执行记录供复查。


def _particle_estimates(cases, metadata, scans):  # 用已完成的文本扫描改进内存估算，不额外加载轨迹或执行 DXA。
    estimates = {}  # 每个案例单独记录粒子数依据，以便续算只考虑仍待处理案例。
    for case in cases:  # 遍历已确认的案例和实际读取元数据。
        counts = [scans[source].get('max_particle_count') for source in case['sources'] if source in scans and scans[source]['covered']]  # 已完整扫描的文本头提供最大粒子数。
        known = [count for count in counts if count is not None]  # 无法取得的粒子数不虚构为零。
        first = metadata[case['id']]['particle_count_first_frame']  # 其他格式至少保留读取器已读取的首帧粒子数。
        scope = 'all_text_dump_headers' if len(known) == len(case['sources']) else ('single_frame' if metadata[case['id']]['num_frames'] == 1 else 'first_frame_and_available_headers')  # 明确是否覆盖完整轨迹，而非声称必然知道峰值规模。
        estimates[case['id']] = {'particle_count': max([first, *known]), 'scope': scope}  # 使用已有证据中最大的粒子数作为启动估算输入。
    return estimates  # 这是粒子数依据，不是 DXA 内存峰值的实测保证。


def _benchmark_candidates(resources, job_count, cpu, platform_limit):  # 可选试跑同样遵守 CPU 预留、任务块和用户进程上限。
    cpu_budget = max(1, math.floor(cpu * (1 - resources.get('reserve_cpu_fraction', 0.1))))  # 对照线程也不得绕过预留预算。
    requested = resources.get('workers', 'auto')  # 显式并发上限同样约束可选性能探索。
    maximum = min(resources.get('benchmark_max_workers', 4), math.ceil(job_count / resources.get('chunk_size', 1)), cpu_budget, platform_limit, cpu_budget if requested == 'auto' else requested)  # 取所有适用的进程上限。
    counts = sorted({n for n in (2, 4, 8, 16, 32, 64, maximum) if 2 <= n <= maximum})  # 包含非二次幂的实际上限，避免预算三进程却只测两个。
    candidates = list(dict.fromkeys([(1, cpu_budget), (1, 1)] + [(n, 1) for n in counts]))  # 对照先测试预算内内部线程；预算为一时去除重复配置。
    return candidates, cpu_budget  # 内存准入仍在每项候选启动前根据估算和已观测峰值执行。


def _init_worker(cases, output_dir, threads):  # 每个 spawn 子进程先设置环境变量，再延迟导入 OVITO。
    # 每个 spawn 子进程先设置环境变量，再延迟导入 OVITO。
    global _CASES, _OUT, _PIPELINES  # 声明当前进程内共享的模块级工作状态。
    if threads == 'auto':  # 检查本行条件，满足时执行其下的分支。
        os.environ.pop('OVITO_THREAD_COUNT', None)  # auto 模式清除继承的线程上限，让 OVITO 选择默认线程数。
    else:  # 前面的条件不满足时执行此分支。
        os.environ['OVITO_THREAD_COUNT'] = str(threads)  # 在导入 OVITO 前为当前 worker 设置线程数。
    _CASES = {case['id']: case for case in cases}  # 计算并保存变量 _CASES 的当前值。
    _OUT = output_dir  # 计算并保存变量 _OUT 的当前值。
    _PIPELINES = {}  # 计算并保存变量 _PIPELINES 的当前值。


def _pipeline(case):  # 只在当前 worker 内构建一次管线，并按 case 复用。
    # 只在当前 worker 内构建一次管线，并按 case 复用。
    from ovito.io import import_file  # 导入 ovito，用于查询或调用当前进程的 OVITO。
    from ovito.modifiers import DislocationAnalysisModifier  # 导入 ovito，用于查询或调用当前进程的 OVITO。
    import numpy as np  # 导入 numpy，用于按原子类型构造布尔选区。
    if case['id'] in _PIPELINES:  # 检查本行条件，满足时执行其下的分支。
        return _PIPELINES[case['id']]  # 返回本函数计算的结果或检查结论。
    _PIPELINES.clear()  # 切换案例时移除旧管线引用，防止许多大案例的管线同时常驻。
    kwargs = {'input_format': case['input_format']}  # 计算并保存变量 kwargs 的当前值。
    if case['input_format'] == 'lammps/data':  # 检查本行条件，满足时执行其下的分支。
        if case.get('atom_style'):  # 检查本行条件，满足时执行其下的分支。
            kwargs['atom_style'] = case['atom_style']  # 设置或传递此项参数，供当前操作使用。
        if case.get('atom_substyles'):  # 检查本行条件，满足时执行其下的分支。
            kwargs['atom_substyles'] = tuple(case['atom_substyles'])  # 设置或传递此项参数，供当前操作使用。
    pipe = import_file(case['sources'], **kwargs)  # 计算并保存当前进程私有的 OVITO 管线。
    def prepare(frame, data):  # 选区只标记参与 DXA 的粒子，不从数据中删除其他原子。
        # 选区只标记参与 DXA 的粒子，不从数据中删除其他原子。
        if case['pbc'] is not None:  # 检查本行条件，满足时执行其下的分支。
            data.cell_.pbc = tuple(case['pbc'])  # 仅在用户明确覆盖时修改三个方向的周期边界。
        if case['selection']['mode'] == 'types':  # 检查本行条件，满足时执行其下的分支。
            types = data.particles['Particle Type']  # 读取每个原子的类型编号以生成选区。
            data.particles_.create_property('Selection', data=np.isin(types, case['selection']['type_ids']).astype(int))  # 根据已确认类型编号设置 Selection 属性，不删除原子。
    pipe.modifiers.append(prepare)  # 在 DXA 前设置明确的周期边界和原子类型选区。
    dxa = DislocationAnalysisModifier()  # 计算并保存位错分析修改器。
    dxa.input_crystal_structure = getattr(DislocationAnalysisModifier.Lattice, case['lattice'])  # 指定用户确认的参考晶格类型。
    dxa.only_selected = case['selection']['mode'] == 'types'  # 仅对用户确认的粒子选区执行 DXA，其他粒子仍保留。
    for name, value in case['dxa'].items():  # 逐项遍历当前集合，分别执行缩进内的操作。
        setattr(dxa, name, value)  # 调用 setattr 完成本行所示的处理。
    pipe.modifiers.append(dxa)  # 把已设置参数的位错分析修改器加入管线。
    defaults = {key: getattr(dxa, key) for key in sorted(_DXA_KEYS)}  # 计算并保存变量 defaults 的当前值。
    _PIPELINES[case['id']] = (pipe, defaults)  # 设置或传递此项参数，供当前操作使用。
    return pipe, defaults  # 返回本函数计算的结果或检查结论。


def _metadata(case_id):  # 在独立子进程查询实际帧数、OVITO 版本和实际生效参数。
    # 在独立子进程查询实际帧数、OVITO 版本和实际生效参数。
    import ovito  # 导入 ovito，用于查询或调用当前进程的 OVITO。
    from ovito.io import import_file  # 用同一读取器分别核对各源文件的帧数。
    case = _CASES[case_id]  # 取得本案例的已确认设置。
    pipe, defaults = _pipeline(case)  # 建立当前进程私有的案例管线。
    raw = pipe.source.compute(0)  # 读取首帧粒子数用于内存预估。
    kwargs = {'input_format': case['input_format']}  # 保持与正式管线相同的输入格式。
    if case['input_format'] == 'lammps/data' and case.get('atom_style'):  # 使用已确认的 data 原子风格。
        kwargs['atom_style'] = case['atom_style']  # 显式传递原子列定义。
    if case['input_format'] == 'lammps/data' and case.get('atom_substyles'):  # hybrid 格式还需传入子风格。
        kwargs['atom_substyles'] = tuple(case['atom_substyles'])  # 转换为 OVITO 接受的元组。
    counts = [import_file(source, **kwargs).source.num_frames for source in case['sources']]  # 每个文件单独建索引，不能将不同源隐式合并。
    return {'num_frames': pipe.source.num_frames, 'source_frame_counts': counts, 'particle_count_first_frame': raw.particles.count, 'ovito_version': ovito.version_string, 'dxa_effective': defaults}  # 返回合并帧数、逐源帧数及分析参数。


def _frame_list(spec, count):  # 明确范围使用半开区间，不允许负数、越界、重复或空结果。
    # 明确范围使用半开区间，不允许负数、越界、重复或空结果。
    if isinstance(spec, list):  # 检查本行条件，满足时执行其下的分支。
        frames = spec  # 使用用户明确指定的帧号列表，不自行抽帧。
    elif isinstance(spec, dict) and set(spec) <= {'start', 'stop', 'step'}:  # 继续检查该条件，满足时执行对应分支。
        start, stop, step = spec.get('start', 0), spec.get('stop'), spec.get('step', 1)  # 计算并保存变量 start 的当前值。
        if type(start) is not int or type(step) is not int or step <= 0 or start < 0 or (stop is not None and (type(stop) is not int or stop > count or stop < 0)):  # 不满足要求时拒绝执行：frames 范围非法。
            raise ValueError('frames 范围非法')  # 明确报告当前错误，避免继续产生误导性结果。
        frames = list(range(start, count if stop is None else stop, step))  # 展开半开帧范围；stop 为 null 时使用轨迹实际末尾。
    else:  # 前面的条件不满足时执行此分支。
        raise ValueError('frames 必须是整数列表或范围对象')  # 明确报告当前错误，避免继续产生误导性结果。
    if not frames or any(type(f) is not int or f < 0 or f >= count for f in frames) or len(frames) != len(set(frames)):  # 不满足要求时拒绝执行：frames 不得为空、重复或越界。
        raise ValueError('frames 不得为空、重复或越界')  # 明确报告当前错误，避免继续产生误导性结果。
    return frames  # 返回本函数计算的结果或检查结论。


def _plain(value):  # 将 NumPy 标量转为可 JSON 序列化的普通数值。
    # 将 NumPy 标量转为可 JSON 序列化的普通数值。
    if hasattr(value, 'item'):  # 检查本行条件，满足时执行其下的分支。
        value = value.item()  # 计算并保存变量 value 的当前值。
    if isinstance(value, float) and not math.isfinite(value):  # 检查本行条件，满足时执行其下的分支。
        return None  # 返回本函数计算的结果或检查结论。
    return value  # 返回本函数计算的结果或检查结论。


def _frame(case_id, frame):  # 此函数仅返回可序列化数据；CA 几何由对应 worker 先落盘。
    # 此函数仅返回可序列化数据；CA 几何由对应 worker 先落盘。
    case = _CASES[case_id]  # 计算并保存当前数据案例。
    result = {'case_id': case_id, 'frame': frame, 'worker_pid': os.getpid(), 'status': 'failed', 'error': '', 'families': {}, 'lines': []}  # 同时记录实际处理此帧的进程编号，便于核验进程池工作情况。
    try:  # 开始受控操作，并在失败时交由异常分支处理。
        from ovito.io import export_file  # 导入 ovito，用于查询或调用当前进程的 OVITO。
        pipe, _ = _pipeline(case)  # 计算并保存当前进程私有的 OVITO 管线。
        data = pipe.compute(frame)  # 计算并保存当前帧的数据集合。
        attrs = data.attributes  # 计算并保存当前帧的 OVITO 全局属性。
        timestep = _plain(attrs.get('Timestep'))  # 提取输入中的模拟步数；缺失时保持空值。
        simtime = _plain(attrs.get('Time'))  # 优先读取输入中已有的物理时间。
        mapping = case.get('time_mapping')  # 读取用户确认的时间步映射参数。
        if simtime is None and timestep is not None and mapping:  # 仅当输入缺少时间但存在步数与已确认映射时进行换算。
            simtime = mapping['origin_time'] + (timestep - mapping['origin_step']) * mapping['timestep_size']  # 按参考步数、参考时刻和时间步长计算时间。
        length = float(attrs['DislocationAnalysis.total_line_length'])  # 读取 DXA 总位错线长度并保留输入的长度单位。
        volume = float(attrs['DislocationAnalysis.cell_volume'])  # 读取模拟盒体积并保留输入的长度立方单位。
        counts = {k.split('.')[-1]: int(v) for k, v in attrs.items() if k.startswith('DislocationAnalysis.counts.')}  # 从 OVITO 属性动态收集各类结构计数。
        recognized = sum(v for k, v in counts.items() if k != 'OTHER')  # 累加所有非 OTHER 的结构原子数。
        denominator = sum(counts.values())  # 统计所有结构分类的原子数作为识别率分母。
        fraction = recognized / denominator if denominator else None  # 以非 OTHER 原子数除以全部结构计数，空分母时保留空值。
        status = 'no_crystalline_atoms' if recognized == 0 else ('zero_dislocations' if length == 0 else 'success')  # 区分无晶态原子、有晶态但零位错、以及识别到非零位错。
        density = length / volume if case['density']['mode'] == 'cell' and volume > 0 else None  # 仅在用户确认全盒体积适用且体积为正时计算原单位密度。
        scale = {'angstrom': 1e-10, 'nm': 1e-9, 'm': 1.0}.get(case['units']['length'])  # 建立已知长度单位至米的换算系数。
        density_m2 = density / scale ** 2 if density is not None and scale is not None else None  # 仅对已知长度单位换算为 m^-2；约化或未知单位不换算。
        result.update({'timestep': timestep, 'time': _plain(simtime), 'total_line_length': length, 'cell_volume': volume, 'density': density, 'density_m2': density_m2, 'recognized_fraction': fraction, 'status': status, 'source_file': str(attrs.get('SourceFile', '')), 'source_frame': _plain(attrs.get('SourceFrame'))})  # 调用 result.update 完成本行所示的处理。
        result['families'] = {k[len('DislocationAnalysis.length.'):]: float(v) for k, v in attrs.items() if k.startswith('DislocationAnalysis.length.')}  # 从 OVITO 动态读取各 Burgers 族长度，不预设 BCC 分类。
        if case['output']['level'] == 'geometry':  # 仅在请求几何输出时导出位错网络与逐线属性。
            for index, line in enumerate(data.dislocations.lines):  # 逐项遍历当前集合，分别执行缩进内的操作。
                result['lines'].append({'line_index': index, 'length': float(line.length), 'burgers_vector': [float(x) for x in line.true_burgers_vector]})  # 为当前帧每条位错记录线长及真实 Burgers 矢量。
            folder = Path(_OUT) / 'geometry' / case_id  # 计算并保存变量 folder 的当前值。
            folder.mkdir(parents=True, exist_ok=True)  # 调用 folder.mkdir 完成本行所示的处理。
            target = folder / f'{frame:09d}.ca'  # 计算并保存几何文件最终路径。
            temporary = folder / (target.name + '.' + uuid.uuid4().hex + '.tmp')  # 计算并保存几何文件临时路径。
            try:  # 开始受控操作，并在失败时交由异常分支处理。
                export_file(data, str(temporary), 'ca')  # 先完整导出 OVITO CA 格式位错网络到临时文件。
                os.replace(temporary, target)  # CA 导出成功后再原子替换最终帧文件。
            finally:  # 无论是否发生异常，都执行清理和收尾操作。
                if temporary.exists():  # 检查本行条件，满足时执行其下的分支。
                    temporary.unlink()  # 调用 temporary.unlink 完成本行所示的处理。
            result['geometry'] = str(target.relative_to(_OUT))  # 在检查点中记录几何文件的相对路径。
            result['geometry_sha256'] = _hash_file(target)  # 保存 CA 文件内容摘要，恢复时检验完整性。
        return result  # 返回本函数计算的结果或检查结论。
    except Exception:  # 捕获指定错误并执行相应的恢复或报告。
        result['status'] = 'failed'  # 将本帧标记为失败，不把异常误记成零位错。
        result['error'] = traceback.format_exc()  # 保留本帧完整异常堆栈以便定位错误。
        return result  # 返回本函数计算的结果或检查结论。


def _frame_chunk(jobs):  # 小批任务在一个 worker 内顺序执行，以减少调度开销。
    # 小批任务在一个 worker 内顺序执行，以减少调度开销。
    return [_frame(case_id, frame) for case_id, frame in jobs]  # 返回本函数计算的结果或检查结论。


def _sample_memory(stop, sample):  # RSS 为父进程及所有存活子进程的周期采样总和，不是精确峰值。
    # RSS 为父进程及所有存活子进程的周期采样总和，不是精确峰值。
    import psutil  # 导入 psutil，用于查询可用资源和进程 RSS。
    process = psutil.Process()  # 计算并保存变量 process 的当前值。
    while not stop.is_set():  # 在条件成立期间继续处理，直到队列或循环结束。
        try:  # 开始受控操作，并在失败时交由异常分支处理。
            procs = [process] + process.children(recursive=True)  # 收集父进程及其所有当前存活子进程。
            total = sum(p.memory_info().rss for p in procs if p.is_running())  # 求和各进程的 RSS；共享页可能被重复统计。
            sample[0] = max(sample[0], total)  # 持续保留采样过程中观察到的最大进程树 RSS。
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # 捕获指定错误并执行相应的恢复或报告。
            pass  # 此处不再执行其他操作，继续后续流程。
        stop.wait(0.05)  # 等待最多 50 毫秒后再次采样或响应停止信号。


def _run_jobs(cases, jobs, out, workers, threads, chunk_size, pending_factor, fingerprint, write_tables=True, on_record=None):  # 一个进程池只负责当前阶段的帧任务。
    # 限制队列中尚未完成的分块数，并在每帧返回后立即原子落盘。
    out = Path(out)  # 计算并保存当前模式的独立输出目录。
    out.mkdir(parents=True, exist_ok=True)  # 调用 out.mkdir 完成本行所示的处理。
    chunks = [jobs[i:i + chunk_size] for i in range(0, len(jobs), chunk_size)]  # 按确认的块大小划分帧任务以减少调度开销。
    peak, stop = [0], threading.Event()  # 计算并保存进程树采样峰值 RSS。
    sampler = threading.Thread(target=_sample_memory, args=(stop, peak), daemon=True)  # 计算并保存变量 sampler 的当前值。
    started = time.perf_counter()  # 计算并保存变量 started 的当前值。
    sampler.start()  # 开始后台采样父进程及子进程的内存。
    records = []  # 计算并保存当前批次的帧结果。
    def persist(batch):  # 所有成功和失败帧都通过同一个原子检查点路径保存。
        for record in batch:  # 逐帧记录分析指纹和内容摘要。
            record['_fingerprint'] = fingerprint  # 绑定源文件、设置和软件版本。
            record['_content_sha256'] = _record_digest(record)  # 防止半份或篡改的结果被恢复为成功。
            checkpoint = out / 'checkpoints' / record['case_id'] / f"{record['frame']:09d}.json"  # 根据案例和帧号隔离检查点。
            _atomic_json(checkpoint, record)  # 在同目录安全提交该帧。
            records.append(record)  # 汇总已成功落盘的记录。
            if on_record is not None:  # 分工况执行时及时同步外层汇总，不等整个工况返回后才保留结果。
                on_record(record)  # 只有检查点已成功落盘的帧才会加入外层结果。
    def failed(chunk, error):  # 按任务块生成明确的逐帧失败记录。
        return [{'case_id': cid, 'frame': frame, 'status': 'failed', 'error': error, 'families': {}, 'lines': []} for cid, frame in chunk]  # 失败数值保持缺失而不是伪造为零。
    try:  # 开始受控操作，并在失败时交由异常分支处理。
        with futures.ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context('spawn'), initializer=_init_worker, initargs=(cases, str(out), threads)) as pool:  # 使用 spawn 启动独立 Python 进程，避免继承 OVITO 线程状态。
            iterator = iter(chunks)  # 计算并保存变量 iterator 的当前值。
            pending = {}  # 计算并保存尚未完成的有限任务队列。
            orphaned = []  # 暂存刚取出但因进程池损坏而无法提交的任务块。
            fatal = None  # 一旦进程池损坏，停止提交并报告全部剩余帧。
            def submit_one():  # 在任务完成后再提交下一块，保持在途任务数受限。
                chunk = next(iterator, None)  # 计算并保存一次提交的帧任务块。
                if chunk is not None:  # 检查本行条件，满足时执行其下的分支。
                    try:  # 提交时进程池也可能已经损坏。
                        pending[pool.submit(_frame_chunk, chunk)] = chunk  # 保存 future 与原任务块对应关系。
                    except BrokenProcessPool:  # 保留取出但未成功提交的任务块。
                        orphaned.append(chunk)  # 后续将该块全部标记失败。
                        raise  # 交给外层停止整个调度过程。
                    return True  # 返回本函数计算的结果或检查结论。
                return False  # 返回本函数计算的结果或检查结论。
            try:  # 初始提交也可能遇到 worker 启动失败。
                for _ in range(workers * pending_factor):  # 限制同时在途的任务块数。
                    if not submit_one():  # 已无任务时停止填充队列。
                        break  # 不再提交空任务。
            except BrokenProcessPool:  # 保留损坏原因并转入失败汇总。
                fatal = traceback.format_exc()  # 包含故障堆栈以供排查。
            while pending and fatal is None:  # 仅在进程池有效时等待任务完成。
                done, _ = futures.wait(pending, return_when=futures.FIRST_COMPLETED)  # 等待至少一个任务块完成后再补充任务。
                for future in done:  # 逐项遍历当前集合，分别执行缩进内的操作。
                    chunk = pending.pop(future)  # 移除已完成 future 并取得其原始帧列表。
                    try:  # 开始受控操作，并在失败时交由异常分支处理。
                        batch = future.result()  # 取得工作进程返回的普通 Python 帧数据。
                    except BrokenProcessPool:  # worker 硬退出会使所有尚未完成的任务不可用。
                        fatal = traceback.format_exc()  # 停止继续向损坏的池提交任务。
                        batch = failed(chunk, fatal)  # 当前任务块逐帧记录失败。
                    except Exception:  # 其他单块异常不一定损坏整个进程池。
                        error = traceback.format_exc()  # 保存任务块异常堆栈。
                        batch = failed(chunk, error)  # 将该块中的帧明确标为失败。
                    persist(batch)  # 即时提交当前块每帧的成功或失败记录。
                    if fatal is not None:  # 进程池损坏时不再接收或提交任务。
                        break  # 剩余任务由下面的统一故障路径记录。
                    try:  # 正常完成后补充一个任务块。
                        submit_one()  # 保持最多 workers × pending_factor 块在途。
                    except BrokenProcessPool:  # 补充任务时若池已损坏。
                        fatal = traceback.format_exc()  # 保存错误并进入统一故障路径。
                        break  # 不再提交任务。
            if fatal is not None:  # 记录所有还未完成、尚未提交和提交失败的帧。
                for chunk in list(pending.values()) + orphaned + list(iterator):  # 按真实任务清单覆盖剩余全部块。
                    persist(failed(chunk, fatal))  # 逐帧写失败检查点，防止遗漏。
        if write_tables:  # 分阶段运行时交给外层统一写表，避免后工况覆盖前工况。
            _write_tables(out, records)  # 独立调用仍保留原来的故障汇总行为。
    finally:  # 无论是否发生异常，都执行清理和收尾操作。
        stop.set()  # 通知采样线程停止读取进程状态。
        sampler.join()  # 等待采样线程退出后读取最终采样峰值。
    return records, time.perf_counter() - started, peak[0]  # 返回实际帧结果、包含落盘的墙钟时间及采样峰值 RSS。


def _run_scheduled_jobs(cases, jobs, out, resources, particle_estimates, fingerprint, retained=None, verify=None):  # 按已选择策略执行，默认逐工况建池并在退出后再处理下一工况。
    import psutil  # 每个工况开始前读取当时可用资源，不复用前一个工况的预算。
    started = time.perf_counter()  # 总耗时包括全部进程池启动、工况切换和最终汇总。
    out = Path(out)  # 所有阶段和汇总使用同一个明确的输出根目录。
    out.mkdir(parents=True, exist_ok=True)  # 资源规划失败时也能写出空汇总，不让目录错误掩盖真正原因。
    retained = retained or []  # 续算时把已验证的旧结果纳入最终表格。
    scheduling = resources.get('scheduling', 'case_sequential')  # 没有明确选择时使用用户原脚本的调度范围。
    grouped = {case['id']: [] for case in cases}  # 保持清单中已确认的工况顺序。
    for job in jobs:  # 将待算任务归入所属工况，避免续算重复处理已完成帧。
        grouped[job[0]].append(job)  # 分组只影响调度，不改变帧映射。
    groups = [jobs] if scheduling == 'cross_case' and jobs else [grouped[case['id']] for case in cases if grouped[case['id']]]  # 跨工况只在已授权且有证据的路径使用一个混合池。
    stages, records, peak = [], [], 0  # 记录每个实际阶段及已落盘结果。
    active_ids = {cid for cid, frame in jobs}  # 仅保留仍有工作量的案例。
    plan = {'scheduling': scheduling, 'requested_workers': resources['workers'], 'job_count': len(jobs), 'particle_estimates': {cid: value for cid, value in particle_estimates.items() if cid in active_ids}, 'stages': stages}  # 汇总规划不把多个顺序阶段伪装成同时并发。
    parent_rss = psutil.Process().memory_info().rss  # 总体资源判断以所有阶段开始前的父进程占用为起点。
    memory_budget = int(psutil.virtual_memory().available * (1 - resources['reserve_memory_fraction']))  # 总体预算还覆盖最终汇总过程中的额外内存。
    full_peak, stop = [0], threading.Event()  # 外层采样覆盖阶段间切换及最终 CSV 汇总。
    sampler = threading.Thread(target=_sample_memory, args=(stop, full_peak), daemon=True)  # 阶段采样仍保留，用于每个工况独立的资源检查。
    sampler.start()  # 在第一个工况规划前开始总体内存观测。
    try:  # 即使中途停止，也保存已经完成的工况及保留结果。
        for group in groups:  # 一个阶段完成并释放进程池后才进入下一个阶段。
            ids = {cid for cid, frame in group}  # 当前阶段实际涉及的工况集合。
            stage_cases = [case for case in cases if case['id'] in ids]  # 仅把当前工况配置传给其工作进程。
            estimates = {cid: particle_estimates[cid] for cid in ids}  # 大工况不压低其他工况独立运行时的并发数。
            per_worker = max(384 * 1048576 + 4096 * max(value['particle_count'] for value in estimates.values()), max(value.get('measured_worker_memory_bytes', 0) for value in estimates.values()))  # 有同轮可选比较的实测记录时采用更保守者，默认运行不额外试跑。
            cpu = _available_cpu()  # 刷新当前进程可用逻辑 CPU 数。
            stage = _plan_resources(resources, len(group), cpu, psutil.virtual_memory().available, psutil.Process().memory_info().rss, per_worker, 61 if os.name == 'nt' else cpu)  # 每工况重新规划实际进程数。
            stage.update(case_ids=[case['id'] for case in stage_cases], particle_estimates=estimates, started_unix=time.time())  # 记录真实阶段边界及数据规模依据。
            stages.append(stage)  # 启动前即记录本阶段，便于识别中断位置。
            reason_names = {'cpu': 'CPU 预算', 'tasks': '帧块数', 'memory': '内存预算', 'platform': '平台上限', 'requested': '用户上限'}  # 把内部限制标识翻译成可核查的中文说明。
            print(f"处理工况：{', '.join(stage['case_ids'])}；{stage['workers']} 个进程 × 每进程 {stage['ovito_threads']} 个 OVITO 线程；待算 {len(group)} 帧／{stage['task_chunks']} 块；CPU 可用 {stage['cpu_available']}／预算 {stage['cpu_budget']}；内存预算 {stage['memory_budget_bytes'] / 1048576:.1f} MiB／每进程估算 {per_worker / 1048576:.1f} MiB；限制：{', '.join(reason_names[key] for key in stage['limiting_factors'])}。", flush=True)  # 每个工况启动时显示实际配置及选择依据。
            batch, elapsed, stage_peak = _run_jobs(stage_cases, group, out, stage['workers'], stage['ovito_threads'], resources['chunk_size'], resources['max_pending_factor'], fingerprint, write_tables=False, on_record=records.append)  # 每个 worker 复用流水线；每帧落盘后立即同步外层，以保留中断前已完成帧。
            peak = max(peak, stage_peak)  # 顺序阶段的峰值取最大值，不能相加冒充同时占用。
            stage.update(finished_unix=time.time(), elapsed_seconds=elapsed, peak_rss_bytes_sampled=stage_peak)  # 记录包含进程池退出的阶段完成时间。
            stage['resource_ok'] = max(0, stage_peak - stage['parent_rss_bytes']) <= stage['memory_budget_bytes'] and stage['workers'] * stage['ovito_threads'] <= stage['cpu_budget']  # 可选比较只接受资源合格的阶段。
            if verify is not None:  # 工况间及时核对来源是否发生变化。
                verify()  # 输入改变时停止下一工况，避免继续产生混合身份结果。
    finally:  # 中断时仍写出已完成帧，未处理帧可按检查点续算。
        active_error = sys.exc_info()[0] is not None  # 若已有主异常，后续汇总错误不能覆盖真正的停止原因。
        try:  # 保持统一输出也处于总体内存采样范围内。
            _write_tables(out, retained + records)  # 汇总全部已完成工况及当前工况已经成功落盘的帧。
        except Exception as exc:  # 文件系统错误应保留，但不掩盖已有计算或资源错误。
            if not active_error:  # 正常计算后的汇总失败仍必须让运行失败。
                raise  # 禁止把缺少完整汇总的运行报告成完成。
            print(f'写入部分汇总时另遇错误：{exc}；已落盘检查点可用于续算。', file=sys.stderr)  # 保留主异常并告知额外输出问题。
        finally:  # 无论汇总是否成功，都结束采样线程。
            stop.set()  # 在最终汇总结束后才停止观察总体峰值。
            sampler.join()  # 等待最后一次采样结束。
    peak = max(peak, full_peak[0])  # 不漏掉阶段间或最终表格扁平化时出现的峰值。
    plan.update(parent_rss_bytes=parent_rss, memory_budget_bytes=memory_budget, peak_rss_bytes_sampled=peak, resource_ok=all(stage['resource_ok'] for stage in stages) and max(0, peak - parent_rss) <= memory_budget)  # 同时检查各阶段及最终汇总的资源合格性。
    return records, time.perf_counter() - started, peak, plan  # 返回新计算记录及完整执行规划。


def _resource_policy(resources):  # 提取决定调度比较的配置，不把报告路径或待验证调度名称放入自身指纹。
    keys = ('workers', 'ovito_threads', 'chunk_size', 'max_pending_factor', 'reserve_cpu_fraction', 'reserve_memory_fraction', 'benchmark_max_frames')  # 只保留两种策略需一致的资源及抽样约束。
    return {key: resources[key] for key in keys}  # 配置变化后不得直接沿用旧调度比较。


def _machine_signature():  # 只保存匿名哈希，不把机器名称写进共享报告。
    import psutil  # 获取当前机器可用的 CPU 范围及总内存。
    value = [platform.node(), platform.system(), platform.machine(), _available_cpu(), psutil.virtual_memory().total]  # 相同数据在另一台机器上不能直接复用性能判断。
    return hashlib.sha256(json.dumps(value).encode('utf-8')).hexdigest()  # 保存可核验的机器签名。


def _scheduling_eligible(trials):  # 严格用两轮实测判断跨工况是否确有收益，不设固定工况数量阈值。
    groups = {mode: [row for row in trials if row.get('scheduling') == mode] for mode in ('case_sequential', 'cross_case')}  # 分别取得两种策略的完整计时。
    if len(trials) != 4 or any(len(rows) != 2 or {row.get('round') for row in rows} != {1, 2} for rows in groups.values()):  # 不接受缺轮次或重复轮次的比较。
        return False  # 证据不足时保持默认逐工况策略。
    if any(row.get('consistent') is not True or row.get('resource_ok') is not True or type(row.get('elapsed_seconds')) not in (int, float) or not math.isfinite(row['elapsed_seconds']) or row['elapsed_seconds'] <= 0 for row in trials):  # 所有候选都必须完成科学与资源检查。
        return False  # 不把失败或无效耗时解释成提速。
    return max(row['elapsed_seconds'] for row in groups['cross_case']) < min(row['elapsed_seconds'] for row in groups['case_sequential'])  # 跨工况较慢一轮仍须快于逐工况较快一轮，避免按微小单次波动切换。


def _validate_scheduling_evidence(resources, fingerprint):  # 正式运行采用跨工况前验证与当前任务对应的实测报告。
    path = resources.get('scheduling_evidence')  # 使用者应明确提供已查看的比较报告。
    if not path:  # 不能仅因为文件很多就自动改用跨工况池。
        raise ValueError('cross_case 需要 scheduling_evidence；请先按需执行 --mode scheduling 比较代表帧')  # 指出缺少实测证据，不自动启动比较。
    report = json.loads(Path(path).read_text(encoding='utf-8'))  # 仅读取已存在的报告，不执行任何额外 DXA。
    if report.get('kind') != 'scheduling_comparison' or report.get('_content_sha256') != _record_digest(report):  # 排除内容损坏或错误类型的报告。
        raise ValueError('调度比较报告类型或内容摘要不符')  # 不能凭一个手写推荐值切换。
    if report.get('fingerprint') != fingerprint or report.get('resource_policy') != _resource_policy(resources) or report.get('machine_signature') != _machine_signature():  # 科学输入、策略参数或运行机器变化后需要重新验证。
        raise ValueError('调度比较报告与当前输入、科学设置、资源策略或机器不兼容')  # 不静默复用旧的性能结论。
    if report.get('eligible_for_cross_case') is not True or not _scheduling_eligible(report.get('trials', [])):  # 从各轮记录重新核查收益，避免只相信推荐字段。
        raise ValueError('小样本比较未证明跨工况调度有稳定收益，保持 case_sequential')  # 明确此次仍应采用逐工况策略。
    return str(Path(path).resolve())  # 在执行记录中保存实际使用的证据来源。


def _compare_scheduling(cases, sample_jobs, out, resources, particle_estimates, fingerprint, verify, preflight):  # 仅按需比较调度范围，不在默认运行中额外试跑。
    trials, reference = [], None  # 第一轮逐工况结果作为科学一致性参照，不增加单进程基线。
    for round_number, order in enumerate((('case_sequential', 'cross_case'), ('cross_case', 'case_sequential')), 1):  # 第二轮反转顺序，减少单一缓存顺序造成的偏差。
        for scheduling in order:  # 两种策略使用同一代表帧、科学参数和总资源预算。
            trial_resources = dict(resources, scheduling=scheduling)  # 每种策略内部仍按资源和帧数自动确定并发。
            trial_out = out / f'round_{round_number}_{scheduling}'  # 隔离各轮输出，避免覆盖或混用检查点。
            records, elapsed, peak, plan = _run_scheduled_jobs(cases, sample_jobs, trial_out, trial_resources, particle_estimates, fingerprint, verify=verify)  # 包括工况切换、进程启动及落盘的完整耗时。
            if reference is None:  # 只保存第一轮实际结果作为参照。
                reference = records  # 失败与无晶态状态仍会由一致性检查拒绝。
            trials.append({'round': round_number, 'scheduling': scheduling, 'elapsed_seconds': elapsed, 'peak_rss_bytes_sampled': peak, 'consistent': _same_results(reference, records, cases), 'resource_ok': plan['resource_ok'], 'resource_plan': plan})  # 资源判定覆盖阶段计算和最终统一汇总。
    eligible = _scheduling_eligible(trials)  # 仅当所有结果合格且耗时区间不重叠时才证明当前样本有收益。
    report = {'schema_version': 1, 'kind': 'scheduling_comparison', 'fingerprint': fingerprint, 'resource_policy': _resource_policy(resources), 'machine_signature': _machine_signature(), 'sample_jobs': sample_jobs, 'unsampled_case_ids': [case['id'] for case in cases if case['id'] not in {cid for cid, frame in sample_jobs}], 'preflight_seconds': preflight, 'trials': trials, 'eligible_for_cross_case': eligible, 'recommended_schedule': 'cross_case' if eligible else 'case_sequential'}  # 完整记录证据适用的输入、配置、机器与样本范围。
    report['_content_sha256'] = _record_digest(report)  # 恢复或正式运行前检查报告内容是否损坏。
    _atomic_json(out / 'report.json', report)  # 先安全提交机器可读证据，再生成可视报告。
    fields = ['round', 'scheduling', 'elapsed_seconds', 'peak_rss_bytes_sampled', 'consistent', 'resource_ok']  # 表中所有数值均来自本次实际试跑。
    _write_csv(out / 'comparisons.csv', fields, trials)  # 方便使用者进一步比较与归档。
    names = {'case_sequential': '逐工况、工况内并行', 'cross_case': '跨工况共同调度'}  # 在 HTML 中使用明确的中文策略名。
    rows = ''.join(f"<tr><td>{row['round']}</td><td>{names[row['scheduling']]}</td><td>{row['elapsed_seconds']:.3f}</td><td>{row['peak_rss_bytes_sampled'] / 1048576:.1f}</td><td>{'是' if row['consistent'] else '否'}</td><td>{'是' if row['resource_ok'] else '否'}</td></tr>" for row in trials)  # 按实际执行顺序展示两轮结果。
    conclusion = '当前代表样本支持跨工况共同调度；只有使用者明确选择后才切换。' if eligible else '当前代表样本未证明跨工况调度有稳定收益，保持默认逐工况处理。'  # 不把耗时接近或失败当作提速。
    page = '<!doctype html><html lang="zh"><meta charset="utf-8"><title>DXA 调度比较</title><style>body{max-width:1000px;margin:36px auto;padding:0 20px;font:16px/1.7 sans-serif;color:#243647}table{border-collapse:collapse;width:100%}th,td{padding:10px;border-bottom:1px solid #ccd6df;text-align:left}</style><h1>DXA 调度比较</h1><p>' + conclusion + '</p><p>两轮使用相同代表帧，第二轮交换执行顺序。通过条件：全部科学结果一致且资源合格，并且跨工况较慢一轮仍快于逐工况较快一轮。此判断不证明全轨迹更快，也不建立通用工况数量阈值。</p><table><tr><th>轮次</th><th>策略</th><th>完整耗时（秒）</th><th>采样峰值 RSS（MiB）</th><th>结果一致</th><th>资源合格</th></tr>' + rows + '</table><p>代表帧：' + html.escape(str(sample_jobs)) + '</p><p>未抽样工况：' + html.escape(str(report['unsampled_case_ids'])) + '</p></html>'  # 自包含报告不依赖联网资源或绘图库。
    (out / 'report.html').write_text(page, encoding='utf-8')  # 使用者可直接查看证据再选择是否改变策略。
    print(f'调度比较完成：{out}；建议 {report["recommended_schedule"]}', flush=True)  # 明确输出建议，不自动修改正式清单。


def _write_csv(path, fields, rows):  # 汇总表仅由主进程写入，并原子替换避免出现半份表格。
    # 汇总表仅由主进程写入，并原子替换避免出现半份表格。
    path = Path(path)  # 计算并保存当前文件路径。
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')  # 计算并保存变量 temp 的当前值。
    with temp.open('w', encoding='utf-8-sig', newline='') as stream:  # 进入资源管理上下文，退出时自动释放对应资源。
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')  # 计算并保存变量 writer 的当前值。
        writer.writeheader()  # 调用 writer.writeheader 完成本行所示的处理。
        writer.writerows(rows)  # 调用 writer.writerows 完成本行所示的处理。
    os.replace(temp, path)  # 同目录原子替换目标，避免把不完整文件当作结果。


def _write_tables(out, records):  # 生成帧、位错族和逐线 CSV；此耗时纳入候选计时。
    # 行顺序固定为案例和所选帧；失败记录不填充虚构的零值。
    records = sorted(records, key=lambda r: (r['case_id'], r['frame']))  # 计算并保存当前批次的帧结果。
    _write_csv(out / 'frames.csv', ['case_id', 'frame', 'timestep', 'time', 'total_line_length', 'cell_volume', 'density', 'density_m2', 'recognized_fraction', 'status', 'error', 'worker_pid'], records)  # 末列保留实际计算进程；它不参与科学结果一致性比较。
    families = [{'case_id': r['case_id'], 'frame': r['frame'], 'family': name, 'length': length} for r in records for name, length in r.get('families', {}).items()]  # 计算并保存变量 families 的当前值。
    _write_csv(out / 'families.csv', ['case_id', 'frame', 'family', 'length'], families)  # 调用 _write_csv 完成本行所示的处理。
    lines = [{'case_id': r['case_id'], 'frame': r['frame'], 'line_index': line['line_index'], 'length': line['length'], 'burgers_x': line['burgers_vector'][0], 'burgers_y': line['burgers_vector'][1], 'burgers_z': line['burgers_vector'][2]} for r in records for line in r.get('lines', [])]  # 计算并保存变量 lines 的当前值。
    _write_csv(out / 'lines.csv', ['case_id', 'frame', 'line_index', 'length', 'burgers_x', 'burgers_y', 'burgers_z'], lines)  # 调用 _write_csv 完成本行所示的处理。


def _same_results(reference, candidate, cases):  # 已知长度先统一为 Å，再比较同一案例的代表帧。
    # 未知或约化单位保持原始量纲；不按不稳定的线 ID 或方向比较。
    a = {(x['case_id'], x['frame']): x for x in reference}  # 计算并保存变量 a 的当前值。
    b = {(x['case_id'], x['frame']): x for x in candidate}  # 计算并保存变量 b 的当前值。
    units = {case['id']: case['units']['length'] for case in cases}  # 查找每个案例声明的原始长度单位。
    if a.keys() != b.keys() or not any(x['status'] != 'no_crystalline_atoms' and x['status'] in _OK for x in reference):  # 检查本行条件，满足时执行其下的分支。
        return False  # 返回本函数计算的结果或检查结论。
    def equal(x, y, scale=1.0):  # 按指定换算因子转换后再应用固定容差。
        return x == y if x is None or y is None else math.isclose(x * scale, y * scale, rel_tol=1e-6, abs_tol=1e-8)  # 绝对容差的单位随比较对象的标准单位确定。
    for key in a:  # 逐项遍历当前集合，分别执行缩进内的操作。
        x, y = a[key], b[key]  # 计算并保存变量 x 的当前值。
        factor = {'angstrom': 1.0, 'nm': 10.0, 'm': 1e10, 'unknown': 1.0, 'reduced': 1.0}[units[key[0]]]  # 将已知长度单位换算为 Å；其余保留原单位。
        if x['status'] not in _OK or x['status'] != y['status']:  # 检查本行条件，满足时执行其下的分支。
            return False  # 返回本函数计算的结果或检查结论。
        scales = {'total_line_length': factor, 'cell_volume': factor ** 3, 'density': factor ** -2, 'density_m2': 1e-20, 'recognized_fraction': 1.0}  # 分别比较 Å、Å³、Å⁻² 以及无量纲识别率。
        if any(not equal(x.get(field), y.get(field), scale) for field, scale in scales.items()):  # 任一统计量超过对应单位下的容差即不一致。
            return False  # 返回本函数计算的结果或检查结论。
        if x.get('families', {}).keys() != y.get('families', {}).keys() or any(not equal(x['families'][k], y['families'][k], factor) for k in x.get('families', {})):  # 位错族线长采用与总线长相同的单位和容差。
            return False  # 返回本函数计算的结果或检查结论。
        lengths_x = sorted(line['length'] for line in x.get('lines', []))  # 排序逐线长度以避免把不稳定的线 ID 当作科学差异。
        lengths_y = sorted(line['length'] for line in y.get('lines', []))  # 同样排序候选结果中的逐线长度，不比较线方向。
        if len(lengths_x) != len(lengths_y) or any(not equal(i, j, factor) for i, j in zip(lengths_x, lengths_y)):  # 排序比较逐线长度，不依赖位错线 ID。
            return False  # 返回本函数计算的结果或检查结论。
    return True  # 返回本函数计算的结果或检查结论。


def _report(out, rows, recommendation, preflight, context=None):  # 本地自包含 HTML 只使用表格与原生 SVG，不依赖绘图库或联网。
    # 本地自包含 HTML 只使用表格与原生 SVG，不依赖绘图库或联网。
    comparison = {'rtol': 1e-6, 'atol': 1e-8, 'known_units': {'length': 'angstrom', 'volume': 'angstrom^3', 'density': 'angstrom^-2', 'recognized_fraction': 'dimensionless'}, 'unknown_or_reduced': '使用各自原单位'}  # 记录绝对容差所对应的量纲。
    context = context or {}  # 接受已有的样本、版本及硬件上下文，不重复分析数据。
    _atomic_json(out / 'report.json', {'preflight_seconds': preflight, 'candidates': rows, 'recommendation': recommendation, 'rss_sampling_interval_seconds': 0.05, 'comparison': comparison, 'context': context})  # 保存可复现的候选数据与比较尺度。
    fields = ['workers', 'ovito_threads', 'elapsed_seconds', 'peak_rss_bytes', 'speedup', 'completed', 'failed', 'consistent', 'resource_ok', 'recommended', 'note']  # 包含每项候选的成功与失败帧数。
    _write_csv(out / 'comparisons.csv', fields, rows)  # 调用 _write_csv 完成本行所示的处理。
    measured = [r for r in rows if r.get('elapsed_seconds') is not None]  # 计算并保存变量 measured 的当前值。
    maximum = max([r['elapsed_seconds'] for r in measured] or [1])  # 计算并保存变量 maximum 的当前值。
    bars = ''.join(f'<text x="5" y="{28 + i * 36}">{r["workers"]} × {r["ovito_threads"]}</text><rect x="95" y="{13 + i * 36}" width="{400 * r["elapsed_seconds"] / maximum:.1f}" height="18" fill="#467ba3"/><text x="{102 + 400 * r["elapsed_seconds"] / maximum:.1f}" y="{28 + i * 36}">{r["elapsed_seconds"]:.2f} 秒</text>' for i, r in enumerate(measured))  # 计算并保存变量 bars 的当前值。
    labels = ['每工况进程上限', '每进程线程数', '耗时（秒）', '采样峰值 RSS（MiB）', '相对串行倍率', '成功帧', '失败帧', '结果一致', '资源合格', '推荐', '说明']  # 与机器可读长表保持相同列序。
    def display(r, key):  # 定义 display 函数，封装这一处理步骤。
        value = r.get(key)  # 计算并保存变量 value 的当前值。
        if value is None:  # 检查本行条件，满足时执行其下的分支。
            return '—'  # 返回本函数计算的结果或检查结论。
        if key == 'peak_rss_bytes':  # 检查本行条件，满足时执行其下的分支。
            return f'{value / 1048576:.1f}'  # 返回本函数计算的结果或检查结论。
        if key in ('elapsed_seconds', 'speedup'):  # 检查本行条件，满足时执行其下的分支。
            return f'{value:.3f}'  # 返回本函数计算的结果或检查结论。
        if isinstance(value, bool):  # 检查本行条件，满足时执行其下的分支。
            return '是' if value else '否'  # 返回本函数计算的结果或检查结论。
        return str(value)  # 返回本函数计算的结果或检查结论。
    table = ''.join('<tr>' + ''.join('<td>' + html.escape(display(r, k)) + '</td>' for k in fields) + '</tr>' for r in rows)  # 计算并保存变量 table 的当前值。
    page = '<!doctype html><html lang="zh"><meta charset="utf-8"><title>DXA 基准</title><h1>DXA 基准</h1>' + f'<p>共同预检和 SHA-256 耗时 {preflight:.3f} 秒。基准为本运行器单进程、使用 CPU 预算内的 OVITO 线程；没有实测原用户脚本。峰值 RSS 为每 50 毫秒采样的进程树总和，可能低估真实峰值。加速仅代表本次抽样。</p><svg width="620" height="{max(40, len(measured) * 36 + 10)}" role="img" aria-label="候选运行时间">{bars}</svg><table border="1"><tr>' + ''.join('<th>' + html.escape(k) + '</th>' for k in labels) + '</tr>' + table + '</table><p>推荐：' + html.escape(str(recommendation)) + '</p></html>'  # 计算并保存变量 page 的当前值。
    details = f"案例数：{context.get('case_count', '—')}；代表帧数：{context.get('sample_frame_count', '—')}；可用 CPU：{context.get('available_cpu', '—')}；预检可用内存：{context.get('available_memory_bytes', 0) / 1048576:.1f} MiB；Python：{context.get('python_version', '—')}；OVITO：{context.get('ovito_version', '—')}"  # 汇总解释本次测试环境所需的最小信息。
    unsampled = context.get('unsampled_case_ids', [])  # 读取因全局样本限额而未参与计时的案例。
    if unsampled:  # 不能把未抽到的工况误表述为已经通过性能验证。
        details += '；未抽样工况（未验证）：' + ', '.join(unsampled)  # 在可视报告中同样展示覆盖边界。
    page = page.replace('</html>', '<p>' + html.escape(details) + '</p><p>比较容差：rtol=1e-6，atol=1e-8。已知长度、体积、密度分别换算为 Å、Å³、Å⁻²；识别率为无量纲。未知或约化单位按各自原单位比较。</p></html>')  # 展示样本规模、环境和固定比较尺度。
    style = (  # 为本地报告添加可读布局，保留图表中的全部实测数值。
        'body{max-width:1120px;margin:36px auto;padding:0 24px;font:15px/1.7 "Segoe UI","Microsoft YaHei",sans-serif;color:#183044;background:#f6f8fb}'  # 使用系统中文字体和适度留白。
        'h1{font-size:30px;letter-spacing:.02em;margin:0 0 16px}p{color:#40566a}svg{background:white;border-radius:10px;padding:16px;max-width:90%;margin:12px 0}'  # 突出标题和时间柱状图。
        '.table-wrap{overflow-x:auto;background:white;border-radius:10px;border:1px solid #dce4ec}table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:13px}'  # 窄屏允许横向滚动，避免裁切表格。
        'th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #e9eef3}th{background:#edf2f7;font-weight:600}tr:last-child td{border-bottom:0}'  # 使多列统计容易逐行比较。
        '.recommendation{padding:14px 18px;background:#e8f2ee;border-left:4px solid #377b65;border-radius:6px;color:#204d3f;font-weight:600}'  # 明确显示本次样本的推荐设置。
    )  # CSS 不依赖联网字体或外部资源。
    recommendation_label = '没有满足结果一致性与资源约束的候选配置。' if not recommendation else f"本次样本推荐：每工况最多 {recommendation['workers']} 个进程；每进程 {'OVITO 自动线程' if recommendation['ovito_threads'] == 'auto' else str(recommendation['ovito_threads']) + ' 个线程'}。"  # 将内部配置转为使用者可读的句子。
    page = page.replace('<meta charset="utf-8">', '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>' + style + '</style>')  # 让桌面和窄窗口均可查看报告。
    page = page.replace('<h1>DXA 基准</h1>', '<h1>DXA 性能比较</h1><p>横条越短表示耗时越少；倍率大于 1 表示比本运行器的串行基线更快。</p>')  # 解释图表和倍率的读取方式。
    page = page.replace('<p>推荐：' + html.escape(str(recommendation)) + '</p>', '<p class="recommendation">' + html.escape(recommendation_label) + '</p>')  # 展示已通过验收的推荐，不泄露实现式字典。
    page = page.replace('<table border="1">', '<div class="table-wrap"><table>').replace('</table>', '</table></div>')  # 在小窗口中保留所有统计列。
    (out / 'report.html').write_text(page, encoding='utf-8')  # 保存可直接离线打开的 HTML 报告。


def _sample_jobs(cases, frames, limit):  # 可选试跑先取各组首中末，再限制全局帧数，避免多工况退化为全量串行。
    candidates = [(case['id'], frames[case['id']][i]) for case in cases for i in sorted({0, len(frames[case['id']]) // 2, len(frames[case['id']]) - 1})]  # 每个案例生成不重复的代表帧候选。
    if len(candidates) <= limit:  # 候选数量较小时不额外减少覆盖。
        return candidates  # 小数据可能全部成为样本，但仍仅由显式对比模式运行。
    indices = [len(candidates) // 2] if limit == 1 else [i * (len(candidates) - 1) // (limit - 1) for i in range(limit)]  # 均匀抽取全局候选，并在限额至少二时保留首尾。
    return [candidates[i] for i in indices]  # 返回确定的有限样本，不宣称覆盖未抽中的工况。


def main(argv=None):  # 默认直接正式分析；Notebook 薄入口可传入参数列表并导入本模块的 worker。
    # 命令入口只读取明确确认的清单，并区分新运行与恢复运行。
    parser = argparse.ArgumentParser(description='OVITO DXA 可验证批处理')  # 计算并保存变量 parser 的当前值。
    parser.add_argument('--config', required=True)  # 调用 parser.add_argument 完成本行所示的处理。
    parser.add_argument('--mode', choices=['baseline', 'benchmark', 'scheduling', 'run', 'resume'], default='run', help='默认逐工况 run；baseline/benchmark/scheduling 仅显式请求时抽样比较')  # 不自动执行单进程对照或跨工况性能搜索。
    args = parser.parse_args(argv)  # 未传列表时读取命令行，薄入口传列表时不修改 Notebook 参数。
    config = _normalize_config(args.config)  # 计算并保存任务清单。
    resources, cases = config['resources'], config['cases']  # 计算并保存执行资源限制。
    if args.mode == 'scheduling' and (len(cases) < 2 or resources['benchmark_max_frames'] < 2):  # 一个工况无法比较跨工况调度，样本也至少需要覆盖两组。
        raise ValueError('调度比较至少需要两个工况和两个代表帧；单工况保持 case_sequential')  # 不为凑比较重复计算同一工况。
    out = Path(config['output_dir']) / ('run' if args.mode == 'resume' else args.mode)  # 计算并保存当前模式的独立输出目录。
    if args.mode == 'resume':  # 检查本行条件，满足时执行其下的分支。
        if not (out / 'manifest.json').is_file():  # 不满足要求时拒绝执行：没有可恢复的 run/manifest.json。
            raise ValueError('没有可恢复的 run/manifest.json')  # 明确报告当前错误，避免继续产生误导性结果。
    elif out.exists():  # 不满足要求时拒绝执行：结果目录已经存在，拒绝覆盖：{out}。
        raise FileExistsError(f'结果目录已经存在，拒绝覆盖：{out}')  # 明确报告当前错误，避免继续产生误导性结果。
    import psutil  # 导入 psutil，用于查询可用资源和进程 RSS。
    cpu = _available_cpu()  # 使用当前进程可用的逻辑 CPU 数，不固定采用整机核数。
    available_memory = psutil.virtual_memory().available  # 计算并保存变量 available_memory 的当前值。
    preflight_start = time.perf_counter()  # 计算并保存变量 preflight_start 的当前值。
    source_paths = list(dict.fromkeys(source for case in cases for source in case['sources']))  # 计算并保存去重后的源文件路径。
    source_stats = {source: _source_stat(source) for source in source_paths}  # 计算并保存输入文件大小及修改时间。
    source_hashes = {source: _hash_file(source) for source in source_paths}  # 计算并保存源文件内容的 SHA-256 摘要。
    if any(_source_stat(source) != source_stats[source] for source in source_paths):  # 不满足要求时拒绝执行：输入文件在 SHA-256 计算过程中发生变化。
        raise RuntimeError('输入文件在 SHA-256 计算过程中发生变化')  # 明确报告当前错误，避免继续产生误导性结果。
    from inspect_inputs import scan_text_dump  # 共用检查器的流式文本扫描，不在此复制格式解析代码。
    dump_sources = list(dict.fromkeys(source for case in cases if case['input_format'] == 'lammps/dump' for source in case['sources']))  # 只扫描明确归类为 dump 的源文件。
    scans = {source: scan_text_dump(source) for source in dump_sources}  # 按源文件计数实际 TIMESTEP 头并记录覆盖情况。
    for source, scan in scans.items():  # 检查扫描发现的读取或文本结构错误。
        if scan['errors']:  # 读取失败及不完整时间步头不能静默忽略。
            raise ValueError(f"源文件扫描失败 {source}: {'; '.join(scan['errors'])}")  # 报告具体文件与错误细节。
    with futures.ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context('spawn'), initializer=_init_worker, initargs=(cases, str(out), 1)) as pool:  # 使用 spawn 启动独立 Python 进程，避免继承 OVITO 线程状态。
        metadata = {case['id']: pool.submit(_metadata, case['id']).result() for case in cases}  # 计算并保存各案例的实际 OVITO 元数据。
    for case in cases:  # 每个案例保留其明确指定的源文件顺序与边界。
        meta = metadata[case['id']]  # 获取案例整体及逐源的 OVITO 帧索引数。
        if sum(meta['source_frame_counts']) != meta['num_frames']:  # 逐源帧数与合并索引不一致时不能可靠映射帧号。
            raise ValueError(f"{case['id']}: 各源文件帧数之和与 OVITO 案例总帧数不一致")  # 停止而非忽略尾部帧。
        if case['input_format'] == 'lammps/dump':  # 文本 dump 可进一步和真实头数核对。
            for source, indexed in zip(case['sources'], meta['source_frame_counts']):  # 按文件一一对照，避免不同案例互相合并。
                scan = scans[source]  # 获取该文件独立扫描结果。
                if scan['covered'] and scan['timestep_headers'] != indexed:  # 发现 OVITO 因异常数据提前停止索引。
                    raise ValueError(f"{source}: 文本 TIMESTEP 头数 {scan['timestep_headers']} 与 OVITO 索引帧数 {indexed} 不一致")  # 明确拒绝有漏帧风险的轨迹。
    if any(_source_stat(source) != source_stats[source] for source in source_paths):  # 确认扫描及预检期间输入文件没有改变。
        raise RuntimeError('输入文件在预检扫描期间发生变化')  # 变化时不能生成可恢复运行。
    versions = {m['ovito_version'] for m in metadata.values()}  # 计算并保存变量 versions 的当前值。
    if len(versions) != 1:  # 不满足要求时拒绝执行：预检时 OVITO 版本不一致。
        raise RuntimeError('预检时 OVITO 版本不一致')  # 明确报告当前错误，避免继续产生误导性结果。
    frames = {case['id']: _frame_list(case['frames'], metadata[case['id']]['num_frames']) for case in cases}  # 计算并保存确认后的有效帧号。
    scientific = {'schema_version': 1, 'cases': cases, 'source_sha256': source_hashes, 'source_stat': source_stats, 'metadata': metadata, 'frames': frames, 'python_version': sys.version, 'ovito_version': next(iter(versions))}  # 计算并保存变量 scientific 的当前值。
    fingerprint = hashlib.sha256(json.dumps(scientific, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()  # 将源 SHA、帧映射、科学设置和版本合并为恢复指纹。
    manifest = {'fingerprint': fingerprint, 'scientific': scientific, 'resources': resources, 'source_scan': scans}  # 记录每个文件的检查覆盖及原因；扫描耗时不参与指纹。
    if args.mode in ('run', 'resume') and resources['scheduling'] == 'cross_case':  # 正式跨工况运行必须有与当前任务对应的可核查收益证据。
        manifest['scheduling_evidence'] = _validate_scheduling_evidence(resources, fingerprint)  # 不自动进行额外比较，也不凭工况数量切换。
    preflight = time.perf_counter() - preflight_start  # 计算并保存变量 preflight 的当前值。
    if args.mode == 'resume':  # 检查本行条件，满足时执行其下的分支。
        old = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))  # 计算并保存变量 old 的当前值。
        if old.get('fingerprint') != fingerprint or old.get('invalid_reason'):  # 源文件、科学设置或软件版本变化时拒绝沿用旧结果。
            raise ValueError('输入、科学参数、帧映射或运行版本已变化，拒绝恢复')  # 明确报告当前错误，避免继续产生误导性结果。
    else:  # 前面的条件不满足时执行此分支。
        out.mkdir(parents=True, exist_ok=False)  # 调用 out.mkdir 完成本行所示的处理。
        _atomic_json(out / 'manifest.json', manifest)  # 调用 _atomic_json 完成本行所示的处理。
    all_jobs = [(case['id'], frame) for case in cases for frame in frames[case['id']]]  # 建立所有案例和所选帧的一一对应任务。
    sample_jobs = _sample_jobs(cases, frames, resources['benchmark_max_frames']) if args.mode in ('baseline', 'benchmark', 'scheduling') else []  # 正式运行不计算试跑样本，也不先执行单进程。
    particle_estimates = _particle_estimates(cases, metadata, scans)  # 复用已扫描的文本数量头，覆盖可变粒子数而不增加 DXA 试跑。
    available_memory = psutil.virtual_memory().available  # 在可能较长的哈希和预检之后重新读取可用内存。
    memory_note = '每进程启发式估算 384 MiB 加每原子 4096 字节；文本 dump 使用已扫描数量头最大值，其他来源采用首帧及可用头信息；不是 DXA 内存峰值保证，未自动进行计时或内存试跑'  # 说明默认自动选择的证据和能力边界。
    if args.mode == 'scheduling':  # 只有使用者显式要求时才比较两种调度范围。
        _compare_scheduling(cases, sample_jobs, out, resources, particle_estimates, fingerprint, lambda: _verify_sources(source_stats, out, manifest), preflight)  # 只对有限样本做双轮比较，结果仅作为待选择证据。
        return  # 不在比较后自动执行全量或改写用户配置。
    if args.mode == 'benchmark':  # 检查本行条件，满足时执行其下的分支。
        rows, reference = [], None  # 计算并保存性能报告中的候选配置记录。
        sampled_cases = {cid for cid, frame in sample_jobs}  # 可选比较只考虑实际抽到的案例。
        benchmark_estimates = {cid: dict(value) for cid, value in particle_estimates.items()}  # 可选并发比较单独累积每工况观测内存，不改变默认启动估算或科学定义。
        cpu = _available_cpu()  # 候选构建前刷新当前可用逻辑 CPU 数。
        largest_case_jobs = max(sum(cid == case_id for cid, frame in sample_jobs) for case_id in sampled_cases)  # 逐工况候选上限由最大单工况帧块数决定。
        candidates, benchmark_cpu_budget = _benchmark_candidates(resources, largest_case_jobs, cpu, 61 if os.name == 'nt' else cpu)  # 可选比较仍保持逐工况范围，不隐式尝试跨工况池。
        for workers, threads in candidates:  # 逐项遍历当前集合，分别执行缩进内的操作。
            trial = out / f'workers_{workers}_threads_{threads}'  # 计算并保存变量 trial 的当前值。
            trial_resources = dict(resources, workers=workers, ovito_threads=threads, scheduling='case_sequential')  # 只探索工况内并发上限，使用与正式默认运行相同的调度范围。
            records, elapsed, peak, trial_plan = _run_scheduled_jobs(cases, sample_jobs, trial, trial_resources, benchmark_estimates, fingerprint, verify=lambda: _verify_sources(source_stats, out, manifest))  # 每工况单独评估内存，不用大工况的规模限制所有小工况。
            for stage in trial_plan['stages']:  # 后续候选考虑已观测的每工况增量 RSS，避免只相信初始启发式。
                observed = math.ceil(max(0, stage['peak_rss_bytes_sampled'] - stage['parent_rss_bytes']) / stage['workers'])  # 观测只作为采样估算，不承诺精确的单进程独占峰值。
                for cid in stage['case_ids']:  # 当前 benchmark 每阶段只有一个工况，保留通用的对应关系。
                    benchmark_estimates[cid] = dict(benchmark_estimates[cid], measured_worker_memory_bytes=max(observed, benchmark_estimates[cid].get('measured_worker_memory_bytes', 0)))  # 用新字典避免回写已存入报告的前一候选记录。
            if reference is None:  # 检查本行条件，满足时执行其下的分支。
                reference = records  # 计算并保存单进程代表帧对照结果。
                baseline = elapsed  # 计算并保存变量 baseline 的当前值。
            memory_ok = trial_plan['resource_ok']  # 只有计算阶段和最终汇总的 CPU 与实测内存均合格时才允许推荐。
            warning = '所有代表帧均未识别到晶态原子' if records and all(r['status'] == 'no_crystalline_atoms' for r in records) else ''  # 计算并保存需要报告的科学边界提示。
            rows.append({'workers': workers, 'ovito_threads': threads, 'elapsed_seconds': elapsed, 'peak_rss_bytes': peak, 'speedup': baseline / elapsed, 'completed': sum(r['status'] in _OK for r in records), 'failed': sum(r['status'] not in _OK for r in records), 'consistent': _same_results(reference, records, cases), 'resource_ok': memory_ok, 'resource_plan': trial_plan, 'recommended': False, 'note': warning})  # 进程列表示每工况上限，各工况实际配置在阶段记录内。
        valid = [r for r in rows if r['consistent'] and r['resource_ok']]  # 计算并保存结果一致且资源合格的候选。
        best = min(valid, key=lambda r: r['elapsed_seconds']) if valid else None  # 计算并保存通过条件的最快候选配置。
        if best is not None:  # 检查本行条件，满足时执行其下的分支。
            best['recommended'] = True  # 设置或传递此项参数，供当前操作使用。
        context = {'case_count': len(cases), 'sample_frame_count': len(sample_jobs), 'sample_jobs': sample_jobs, 'unsampled_case_ids': [case['id'] for case in cases if case['id'] not in sampled_cases], 'python_version': sys.version, 'ovito_version': next(iter(versions)), 'available_cpu': cpu, 'cpu_budget': benchmark_cpu_budget, 'available_memory_bytes': available_memory}  # 明确记录抽样范围与资源预算，不能将小样本结论扩展为全部数据最优。
        _report(out, rows, {'workers': best['workers'], 'ovito_threads': best['ovito_threads']} if best else None, preflight, context)  # 输出无需外部依赖的完整报告。
        if best is None:  # 不满足要求时拒绝执行：没有结果一致且资源合格的候选配置。
            raise RuntimeError('没有结果一致且资源合格的候选配置')  # 明确报告当前错误，避免继续产生误导性结果。
        print(f'基准完成：{out}')  # 调用 print 完成本行所示的处理。
        return  # 将本步骤的实际结果返回给调用方。
    jobs = sample_jobs if args.mode == 'baseline' else all_jobs  # 计算并保存待处理帧任务。
    retained = []  # 计算并保存变量 retained 的当前值。
    if args.mode == 'resume':  # 检查本行条件，满足时执行其下的分支。
        missing = []  # 计算并保存变量 missing 的当前值。
        for cid, frame in jobs:  # 逐项遍历当前集合，分别执行缩进内的操作。
            path = out / 'checkpoints' / cid / f'{frame:09d}.json'  # 计算并保存当前文件路径。
            try:  # 开始受控操作，并在失败时交由异常分支处理。
                record = json.loads(path.read_text(encoding='utf-8'))  # 计算并保存单帧结果记录。
                required = {'total_line_length', 'cell_volume', 'density', 'density_m2', 'recognized_fraction', 'timestep', 'time', 'families', 'lines', 'error'}  # 计算并保存变量 required 的当前值。
                if record.get('case_id') != cid or record.get('frame') != frame or record.get('status') not in _OK or not required.issubset(record) or record.get('_fingerprint') != fingerprint or record.get('_content_sha256') != _record_digest(record):  # 检查帧身份、成功状态、必需字段、运行指纹及检查点摘要。
                    raise ValueError('帧检查点未成功')  # 明确报告当前错误，避免继续产生误导性结果。
                case = next(c for c in cases if c['id'] == cid)  # 计算并保存当前数据案例。
                if case['output']['level'] == 'geometry':  # 仅在请求几何输出时导出位错网络与逐线属性。
                    geometry = (out / record['geometry']).resolve()  # 解析已记录 CA 文件路径，并核对其是否仍位于结果目录。
                    if not geometry.is_relative_to(out.resolve()) or _hash_file(geometry) != record['geometry_sha256']:  # 几何文件缺失、越界或摘要不符时，不能作为已完成帧。
                        raise ValueError('几何文件缺失或损坏')  # 明确报告当前错误，避免继续产生误导性结果。
                retained.append(record)  # 只保留通过身份、内容和几何校验的成功帧。
            except (OSError, ValueError, KeyError, json.JSONDecodeError):  # 捕获指定错误并执行相应的恢复或报告。
                missing.append((cid, frame))  # 损坏、缺失或失败的检查点重新加入待处理队列。
        jobs = missing  # 计算并保存待处理帧任务。
    actual_resources = dict(resources, workers=1, ovito_threads='auto', scheduling='case_sequential') if args.mode == 'baseline' else resources  # 仅显式 baseline 做样本单进程对照，仍按工况顺序执行。
    print(f"执行策略：{actual_resources['scheduling']}；待算 {len(jobs)} 帧，已复用 {len(retained)} 帧。", flush=True)  # 每工况具体资源在其启动时再读取并显示。
    records, elapsed, peak, resource_plan = _run_scheduled_jobs(cases, jobs, out, actual_resources, particle_estimates, fingerprint, retained=retained, verify=lambda: _verify_sources(source_stats, out, manifest))  # 全部工况共享汇总器，各工况按需独立并行计算。
    _verify_sources(source_stats, out, manifest)  # 调用 _verify_sources 完成本行所示的处理。
    records = retained + records  # 计算并保存当前批次的帧结果。
    stages = resource_plan['stages']  # 每个阶段对应一个已结束的进程池，顺序执行时不相加并发数。
    workers = max([stage['workers'] for stage in stages] or [0])  # 顶层进程数表示本次最大同时使用值。
    thread_values = {stage['ovito_threads'] for stage in stages}  # auto 内部线程可能随工况并发数而改变。
    threads = next(iter(thread_values)) if len(thread_values) == 1 else None  # 不同阶段配置不同时必须查看阶段记录，不能虚构统一线程数。
    warning = '所有已处理帧均未识别到晶态原子；请检查晶格、原子选区、边界和数据' if records and all(r['status'] == 'no_crystalline_atoms' for r in records) else ''  # 计算并保存需要报告的科学边界提示。
    _atomic_json(out / 'execution.json', {'preflight_seconds': preflight, 'elapsed_seconds': elapsed, 'merge_seconds': 0.0, 'peak_rss_bytes_sampled': peak, 'workers': workers, 'ovito_threads': threads, 'scheduling': actual_resources['scheduling'], 'memory_assumption': memory_note, 'resource_plan': resource_plan, 'completed': sum(r['status'] in _OK for r in records), 'failed': sum(r['status'] not in _OK for r in records), 'warning': warning})  # 汇总合并已计入 elapsed；完整资源上限及估算在每工况阶段内记录。
    if warning:  # 检查本行条件，满足时执行其下的分支。
        print('警告：' + warning, file=sys.stderr)  # 调用 print 完成本行所示的处理。
    if any(r['status'] not in _OK for r in records):  # 不满足要求时拒绝执行：部分帧失败，请检查 {out / 。
        raise RuntimeError(f'部分帧失败，请检查 {out / "frames.csv"}')  # 明确报告当前错误，避免继续产生误导性结果。
    print(f'处理完成：{out}')  # 调用 print 完成本行所示的处理。


if __name__ == '__main__':  # 检查本行条件，满足时执行其下的分支。
    # Windows 以 spawn 启动进程时，防止子进程重复执行入口。
    mp.freeze_support()  # 调用 mp.freeze_support 完成本行所示的处理。
    try:  # 开始受控操作，并在失败时交由异常分支处理。
        main()  # 调用 main 完成本行所示的处理。
    except Exception as exc:  # 捕获指定错误并执行相应的恢复或报告。
        print(f'错误：{exc}', file=sys.stderr)  # 调用 print 完成本行所示的处理。
        sys.exit(1)  # 调用 sys.exit 完成本行所示的处理。

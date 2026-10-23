# Bus OCR：其他 Agent 的调用约定

本项目通过 Python CLI 调用，适用于具备终端执行和图片文件访问能力的 Agent。规则随仓库迁移，不依赖原聊天记录。先完整阅读本文及 [精简模式规则](endpoints-mode.md)，再执行批次。

适用代码基线：pipeline `2.4`，功能提交 `7e754c8`。核对本地版本后，保留用户已有修改；后续修复以实际源码和测试结果为准。

## 1. 确定输入、模式和运行环境

1. 找到包含 `buspipe/` 的仓库根目录，所有 CLI 命令在该目录运行。仓库的本机位置为 `E:/BaiduNetdiskDownload/bus_ocr`；其他机器使用实际路径。
2. 按 [环境准备](batch-pipeline.md#1-准备) 安装 Python 和依赖。执行 `python -m buspipe run --help`，确认模块可导入及参数存在。真实图片独立于代码仓库，需要另行提供可访问的源目录。
3. 按 [文件名与字段规则](endpoints-mode.md#字段与身份规则) 检查文件名。默认任务是提取当前站名、文件名方位、线路号及有序首末站，显式使用 `--extract-mode endpoints`。
4. 完整站序任务使用 `full`；该模式仍拒绝横版“下一站”牌。按 [横版正反面](endpoints-mode.md#横版正反面) 判断已覆盖布局，按 [当前已知问题](endpoints-mode.md#当前已知问题) 判断竖版结果风险。
5. 为本次任务选择输出目录。更换提取模式、修改提取逻辑或需要重新验证已完成图片时，使用新的输出目录和 manifest；同一任务中断续跑继续使用原目录。

完成条件：明确仓库、输入、输出、提取模式，以及是否配置远程 OCR / AI 复核。任务参数缺失时先检查现有文件和配置；明确图片来源后再启动实际 OCR。

## 2. 扫描、抽样、执行

以下为 PowerShell 示例；替换 `$sourceDir`，在仓库根目录执行。其他系统使用同样的 Python CLI 参数，按相应 shell 设置环境变量。

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$sourceDir = "D:/data/bus-signs"
$outDir = "output/agent-endpoints"

python -m buspipe run "$sourceDir" --out "$outDir" --extract-mode endpoints --dry-run
```

检查扫描数量是否符合用户指定范围。首次迁移或新样本先抽样：

```powershell
python -m buspipe run "$sourceDir" --out "$outDir" --extract-mode endpoints --limit 3 --workers 1 --threads 4 --no-vlm
```

`--limit 3` 只限制数量，不保证样本覆盖。混合横竖版或多模板时，用小样本目录或 `--only-station` 覆盖各类版式及横版正反面，再开始整批。

核对抽样的原图和导出字段，再用同一输出目录继续整批：

```powershell
python -m buspipe run "$sourceDir" --out "$outDir" --extract-mode endpoints --workers 1 --threads 4 --no-vlm
```

`--no-vlm` 明确表示本地 OCR＋程序规则，不调用 AI 复核。该流程会自动分派已支持的横竖版。站点筛选、指定图片、远程 OCR 的参数见 `--help` 和 [批处理说明](batch-pipeline.md)。

中断后重跑相同命令。模式一致时已有 `done` 图片会跳过；`--no-cache` 也不等于强制重跑所有 `done` 图片。修改代码后做验收，使用新输出目录。

多个 Agent 或独立进程并行时，各自使用明确的图片子集及独立输出目录、工作目录、manifest；合并结果时保留图片来源和版块编号。单次 CLI 内部并发通过 `--workers` 控制。

完成条件：执行进程结束，输出中存在 `batch_report.json`、`station_routes.json` / `.csv` 和复核 CSV；若部分图片失败，保留失败原因并进入结果汇报。

## 3. 可选：接入 AI 视觉复核

用户选择 AI 复核并提供可用服务配置时，使用项目的 `HttpVlmReviewer`，在本地 OCR 参数后增加以下参数，并移除 `--no-vlm`：

```powershell
python -m buspipe run "$sourceDir" --out "$outDir" --extract-mode endpoints --workers 1 --threads 4 --vlm-url "<完整接口URL>" --vlm-model "<模型名>" --vlm-max-calls 8
```

密钥由环境变量 `BUS_VLM_API_KEY` 提供。接口需支持图片和结构化 JSON；服务地址和模型名使用实际配置。当前请求采用 Chat Completions 样式，另含 `prompt_version` 字段，需按实际服务契约验证兼容性。具体请求构造、提示词、响应校验以 [vlm.py](../buspipe/vlm.py) 为准。

AI 复核约定：

- 只复核有可用裁片、且原因在 `buspipe.batch.VLM_REASONS` 中的 `open` 项；当前主要包括空识别、低置信度、跨列、框偏宽和覆盖不足。线路号未知、走向不明等标记并不保证会调用 AI。
- 输入为目标裁片、可用的相邻上下文和 OCR 候选。精简模式未提取的表头下站、开往文字可为空。裁片缺字或裁到边框时，保留不确定性；先修正定位才能恢复裁片外的信息。
- 相邻上下文仅辅助辨认目标端点，输出范围遵守 [不补全途经站点](endpoints-mode.md#不补全途经站点) 的约束。
- 模型返回 `text`、`note`、`agree`、`confidence`、`reason`。提示词和 JSON 校验规则统一由代码维护，不另写一套宽松解析。
- 同意且规范化文字一致时标记 `vlm_confirmed`；不同意见写入 `vlm_opinions` 并标记 `needs_human`；OCR 为空时写入 `vlm_candidates` 并标记 `vlm_only`。原 OCR 文字保留，候选经确认后再修正。
- 调用失败、响应不合约或置信度不足时保留复核状态。以报告中的 `vlm.calls`、错误数和缓存命中数说明实际执行情况；仅存在 AI 接口代码不代表本批次调用过 AI。

已有本地结果的 manifest 中若仍有符合条件的 `open` 项，可以再次运行并配置 VLM，复核阶段会处理这些项。`needs_human`、`vlm_only` 等状态并非自动再次调用的 `open` 项。

人工端点修正使用现有 `override` 流程，见 [批处理说明](batch-pipeline.md#6-输出)。在 `endpoints` 模式中，`SEQ=1` 是首站，`SEQ=2` 是末站。该命令按图片哈希、线路号和方位匹配已有条目，只修改端点文字/备注；未知线路号或同图多条空号记录应先定位版块并修复解析，避免把修正应用到多个条目。

完成条件：逐类说明已确认、意见冲突、AI 单独候选及未能复核的数量，并保留可追溯的裁片和原始结果。

## 4. 验收与向用户汇报

CLI 返回码为 0 只说明命令正常返回，仍须读取以下文件：

| 文件 | 必查内容 |
| --- | --- |
| `batch_report.json` | `counts.total/done/failed/incomplete`、`failures`、耗时、OCR/VLM 实际调用量 |
| `station_routes.json` / `.csv` | 记录数、`needs_review`、空线路号、缺失首末站、异常方位、`_` 等无效文字、站名中的年份污染 |
| `review/待人工复核.csv` | 未解决原因、来源图片、裁片路径；一条线路可能对应多个复核项 |
| `posters/*_raw.json` | `route_ocr`、`number_items`、端点坐标和置信度；用于解释差异 |

将抽样或人工基准与导出逐项比较：站名、文件名方位、线路号、首站、末站；首末站顺序也必须一致。比较时可统一全半角括号和空白，保留站名、括号附注及线路限定词的实际内容。

汇报“图片处理完成”和“提取字段正确”两个结果。成功标准是：指定范围有明确处理结论，失败和未完成数量已交代，记录数与复核数已核实，提供主结果、复核列表和报告路径。只有经过原图或基准核对的范围才能声称识别正确。

只运行批次时无需重跑代码回归。修改识别代码时，先覆盖实际错误样本，再运行相关回归；测试入口见 [精简模式规则](endpoints-mode.md#验证基线与回归)。修复已知问题后，同步更新对应规则、问题状态和真实样本验证证据。

## 5. 可直接交给另一个 Agent 的任务文本

替换路径后使用：

```text
在 <仓库目录> 工作。先读 AGENTS.md、docs/agent-guide.md 和 docs/endpoints-mode.md。
对 <图片目录> 运行精简模式 endpoints，输出到 <新输出目录>。
只提取站名、方位、线路号及首末站；不补全途经站点或完整站序。
本次使用本地 OCR，AI 复核关闭；保留程序生成的异常与来源记录。
先扫描确认范围，再抽样核对原图，随后完成整批。
读取报告和结果，核实图片数、记录数、失败、空字段、待复核及实际 OCR/AI 调用量。
交付 station_routes.csv、review/待人工复核.csv、batch_report.md 的路径和简要结论。
已知模板问题以文档中“当前已知问题”为准；遇到缺失保留证据并说明原因。
```

若任务包含 AI 复核，将任务文本中的 AI 开关说明改为实际服务地址、模型名和调用上限；密钥通过环境配置提供。

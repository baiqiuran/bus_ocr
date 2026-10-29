# 站牌 OCR 本地工作台

在仓库根目录启动：

```powershell
python -m buspipe.web
```

浏览器访问 http://127.0.0.1:8765 。服务仅监听本机，不需要 Node.js 或前端构建；沿用现有 Python / RapidOCR 环境。停止服务使用 `Ctrl+C`。同一个数据目录只运行一个服务实例。

可选启动参数：

```powershell
python -m buspipe.web --port 8765 --data-dir output/workbench
python -m buspipe.web --import-output output/endpoints-acceptance
```

## 操作流程

1. 点击“新建识别批次”，粘贴**运行服务的电脑**上的目录完整路径。扫描递归包含 JPG / JPEG，使用现有 manifest 的图片哈希去重规则。
2. 扫描完成后检查图片数量，点击“抽样 3 张”。数量限制不保证覆盖所有模板；混合模板应另外组织抽样目录。
3. 点击记录“详情”，对照原图核对当前站名、方位、线路号、有序首末站，以及复核原因。
4. 点击“开始整批识别”或“继续识别”。默认固定 `endpoints`、1 个 worker、4 个 OCR 线程、`--no-vlm`；不调用远程 OCR / AI。
5. 本轮结束后，线路结果写入 SQLite。图片进度每 4 秒刷新；运行中尚未导出的线路不会提前显示为结果。
6. 在左侧选择批次，使用搜索、方位和待复核筛选，查看原图，或导出本批 CSV。

扫描、识别在后台运行。服务正常停止时会终止正在运行的 OCR 子进程；下次启动将残留运行状态标为中断，可在原批次继续。强制结束服务进程后，应先检查是否仍有 OCR 子进程，避免同时处理同一个 manifest。

“导入已有结果”读取已有 endpoints 输出目录中的 `batch_report.json`、`station_routes.json` 和可用的 `.batch/manifest.sqlite`。原文件只读；重复导入同一路径会打开已有快照。它不会自动同步后来修改的历史输出。需要重新识别时创建新批次。

## 数据和字段规则

- `station`、`direction`、`station_key` 沿用流水线导出；站名与方位按文件名规则解析。
- `route_direction` 只有首末站均存在时才显示 `首站→末站`。不根据东南西北推断线路首末顺序，不补造上下行标签或途经站序。
- 反向线路、同站不同方位、不同图片和板块分别保存，不静默合并。不同批次也分别保留，因此总记录数是来源记录数，不是去重线路数。
- `needs_review` 原标记保留；空字段、未知方位也进入待复核筛选。页面“已提取”仅表示未标记待复核，不表示人工验收通过。
- 设施方位如“上客点”“下客点”“场站”原样保存，不转换为东南西北。
- 图片处理失败、无线路信息跳过、未完成与记录待复核分别展示；即使 CLI 返回 0，也按 `batch_report.json` 判断批次是否部分完成。
- 原图关联来自 manifest；同名来源无法唯一匹配时，不猜图片。没有 manifest 或原图已移动时，仍可查看入库字段，页面提示来源不可用。
- 详情展示复核原因和裁片路径。首末站人工修改仍使用既有 CLI `override` 流程，网页暂不提供直接改值或“确认通过”。
- 外部线路导出表的上下行映射是另一项操作，本工作台不自动执行。若需要，应继续遵循 `docs/direction-mapping.md` 中的走向、整板、同名、XMP 时间优先级。

## 本地文件

默认存储：

| 路径 | 内容 |
| --- | --- |
| `output/workbench/workbench.sqlite` | `jobs` 批次与 `records` 线路来源记录 |
| `output/workbench/batches/<批次 ID>/.batch/manifest.sqlite` | OCR 断点、图片状态、复核队列 |
| `output/workbench/batches/<批次 ID>/station_routes.json` / `.csv` | 现有流水线的原始导出 |
| `output/workbench/batches/<批次 ID>/batch_report.json` | 真实处理数量、耗时和调用量 |
| `output/workbench/batches/<批次 ID>/process.log` | 本轮进程输出与错误 |

原图继续存放在输入目录。备份时先停止服务，再复制整个工作台数据目录；历史导入的图片、manifest 和输出目录应另外保留。浏览器 CSV 导出来自 SQLite，会为可能被表格软件解释为公式的值添加前导单引号；数据库中的原值不变。

## 验证

```powershell
python -m pytest tests/test_web.py -q
```

覆盖入库、重启持久化、反向端点、设施方位、不确定数据保留、同名来源歧义、重复导入、真实 CLI 只扫描流程、部分失败状态、API 会话与来源检查，以及浏览器筛选和移动布局。浏览器测试在缺少 Playwright / Chromium 时跳过。

本次实现验收使用已有 `output/endpoints-acceptance` 的 16 条真实记录，并从页面提交了单张 `安里-东-141-01.jpg`。新识别的站名、方位、线路号、首站、末站与 `tests/fixtures/landscape_expected.json` 的对应项一致；这仅验证该单张样本，不代表所有模板均正确。未改变现有版面阈值和 OCR 提取规则。

界面采用 Hallmark Workbench / Cobalt：浅冷色底、单一蓝色操作色、左侧批次导航与数据表、窄屏折叠菜单。所有颜色与字体集中于根目录 `tokens.css`；为本机离线使用采用 Windows 字体，无远程字体请求。已经检查 320、375、414、768、1024、1280、1440、1920 像素宽度，表格在窄屏转为逐条记录。

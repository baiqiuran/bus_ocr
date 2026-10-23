# 批量站牌 OCR / VLM 复核流水线

这条流水线把原来逐站牌运行的脚本拆成可复用的提取核心，并增加了 manifest、断点续跑、缓存、远程 OCR、复核队列、VLM 复核和批量报告。版面理解仍在本地完成，远程服务只接收本地切好的裁片或画布。

只需“站名＋文件名方位、线路号、首站→末站”时，使用 `--extract-mode endpoints`；启动方法和输出字段见 [精简模式说明](endpoints-mode.md)。

## 1. 准备

依赖：Python 3.10+、numpy、Pillow、RapidOCR、pandas、openpyxl。测试用 pytest。

```bash
cd E:/BaiduNetdiskDownload/bus_ocr
python -m pip install rapidocr onnxruntime pillow numpy pandas openpyxl pytest
```

所有命令都在 `bus_ocr` 目录执行。默认输出目录不要放在系统盘的小分区上；20GB 源图不会复制，只保存切块、JSON 和报告。

## 2. 先 dry-run：只扫描，不调用 OCR

```bash
python -m buspipe run "E:/BaiduNetdiskDownload/底板" \
  --out "E:/BaiduNetdiskDownload/bus_ocr/output/full" \
  --dry-run
```

输出会给出 `total / pending / duplicate / failed` 数量和 manifest 路径。dry-run 只做 SHA-256、尺寸读取和重复判定；不会初始化 OCR，也不会调用远程服务。

`--station` 是文件名无法给出站点时的兜底；常规文件名 `站点-方向-序号.jpg` 会自动解析。不同站点可以混在同一批。

## 3. 本地 OCR 启动

```bash
python -m buspipe run "E:/BaiduNetdiskDownload/底板" \
  --out "E:/BaiduNetdiskDownload/bus_ocr/output/full" \
  --workers 1 --threads 4
```

- 本机无 NVIDIA GPU，本地 RapidOCR 是 CPU 推理。海报级并发默认 1；`workers > 1` 只建议在远程 OCR 或显存/内存充足时使用。
- 画布默认最多 8 列、最大宽度 1000px、列间距 30px，可用 `--max-columns` / `--max-width` 调整。调整后会改变 OCR 请求数和缓存 key。
- OCR 批量识别失败、检测框跨列、覆盖不足时会自动退回单列识别。
- 中断后直接重跑同一条命令即可；已完成的块和海报会命中缓存。
- 失败项默认会在下次运行重试，最多 3 次；`--no-retry-failed` 可关闭，`--max-attempts` 可调整。

只跑一部分：

```bash
python -m buspipe run SOURCE --out OUT --limit 50
python -m buspipe run SOURCE --out OUT --only-station 安平小区
python -m buspipe run SOURCE --out OUT --only-hash <sha256>
```

## 4. 远程 OCR

远程服务必须是无状态 HTTP 接口：接收 PNG 裁片/画布，返回每张图的文字、框坐标和置信度。不要把整张海报上传；本地负责列映射和业务校验。

```bash
export BUS_OCR_API_KEY='...'   # 只从环境变量读取，不写进命令或日志
python -m buspipe run SOURCE --out OUT \
  --remote-ocr https://ocr.example.com/v1/recognize \
  --remote-model rapidocr-prod \
  --workers 4
```

默认配置：

- 一次请求最多 6 张图，或约 1MB 负载；
- 超时 30 秒，5xx/429/网络错误重试 2 次，指数退避；429 优先使用 `Retry-After`；
- 连续失败触发熔断；失败请求自动回退本地 RapidOCR，整批任务不会停在单点故障上；
- 密钥只在 `Authorization: Bearer` 头中发送，不写入日志、JSON、报告；
- 可用 `--no-local-fallback` 关闭本地兜底（不推荐）。

接口约定示例：

```json
POST /v1/recognize
{
  "model": "rapidocr-prod",
  "params": {"box_thresh": 0.3, "text_score": 0.1, "unclip_ratio": 1.5},
  "images": [{"id": "img-0-abc", "format": "png", "data": "<base64>"}]
}
```

响应必须为每张图返回稳定 `id`、`text`、`score`、`box`；缺坐标或缺置信度会按契约错误处理并回退。

## 5. VLM 复核（可选）

VLM 只处理复核队列中的异常裁片：低置信度、跨列、覆盖不足、OCR 为空、下站不一致。正常项不会产生 VLM 请求。

```bash
export BUS_VLM_API_KEY='...'
python -m buspipe run SOURCE --out OUT \
  --vlm-url https://vlm.example.com/v1/chat/completions \
  --vlm-model qwen-vl-prod \
  --vlm-max-calls 8 \
  --vlm-min-confidence 0.6
```

处理规则：

- 输入是目标裁片 + 前后站上下文 + 线路/方向/表头下站/开往方向/OCR 候选；
- 温度为 0，要求返回结构化 JSON：`text`、`note`、`agree`、`confidence`、`reason`；
- VLM 同意且文本一致：复核项标记 `vlm_confirmed`，不修改 OCR 结果；
- 不一致或 VLM 说“同意”但文本不同：保留两份结果，标记 `needs_human`，绝不静默覆盖；
- OCR 为空：只写入 `vlm_candidates`，标记来源 `vlm_only`，仍然进入人工复核；
- 非法 JSON、低置信度、空结果直接忽略；每张海报调用数受 `--vlm-max-calls` 限制；
- 结果按裁片哈希 + 提示词版本 + 模型版本缓存。

## 6. 输出

```text
OUT/
  all_raw.json                 # 与现有 analyze_anping.py 兼容的合并结果
  batch_report.json/.md        # 总数、完成/失败、复核、耗时、缓存、OCR/VLM 调用、失败原因
  review/
    待人工复核.csv             # OCR、VLM、来源、原因、裁片路径
    crops/                     # 异常站名裁片
  posters/
    <站点>-<方向>-<序号>_<hash8>_raw.json
  .batch/
    manifest.sqlite            # 状态机 + 路径 + 复核项 + 人工修正
    cache/blocks|posters|vlm/  # 版本化原子缓存
    summaries/                 # 双向校验用的轻量摘要
```

缓存 key 包含图片哈希、版面/画布/OCR 参数、模型版本和管线版本；改配置会自动失效。写缓存使用临时文件 + 原子替换，多个 worker 同时写不会产生半文件。

复核 CSV 可直接交给人工；人工修正写入数据库后，重跑同一图片会优先采用修正值：

```bash
python -m buspipe override OUT/.batch/manifest.sqlite \
  <sha256> 50 西 18 闽江之心 --note 台江步行街
python -m buspipe run OUT/.batch/manifest.sqlite --out OUT
```

`override` 会把已完成海报的状态改为 `validated`，下一次 `run` 复用已提取 JSON，不重新 OCR。

## 7. 断点续跑、重跑和导出

```bash
# 中断后继续（同一条命令）
python -m buspipe run SOURCE --out OUT

# 只重跑失败项（默认行为；最多 3 次）
python -m buspipe run OUT/.batch/manifest.sqlite --out OUT

# 不重新扫描源目录，只导出当前 manifest
python -m buspipe export OUT/.batch/manifest.sqlite --out OUT

# 查看状态 / 失败原因
python -m buspipe list OUT/.batch/manifest.sqlite
python -m buspipe list OUT/.batch/manifest.sqlite --status failed
```

状态机：`pending → layout_done → ocr_done → validated → (vlm_review) → done`，任一步失败进入 `failed`。VLM 后仍有 `open / needs_human / vlm_only` 项的海报标记为 `vlm_review`，不再自动重跑；人工 `override` 会把它改回 `validated`。块级完成后立即写缓存，因此一个海报中途失败，下次只补未完成的块；海报完成后立即落盘 JSON。

## 8. 把结果交给现有分析器

批量导出的 `all_raw.json` 与 `analyze_anping.py` 的输入结构一致：

```bash
python analyze_anping.py \
  --raw OUT/all_raw.json \
  --out-dir OUT/analysis
```

会生成 `站点明细.csv`、`线路汇总.csv`、`待人工复核.csv`、`双向校验.csv`、`修正记录.csv`、Excel 和 `分析报告.md`。`--out-dir` 是本次新增参数，默认行为不变。

## 9. 已验收的规模数据

- 100 张合成海报、2 个 worker、fake 远程 OCR（前 3 个请求按计划返回 500）：17.6 秒完成，峰值 traced memory 18.7 MiB，100/100 done；305 张 OCR 图像、207 次远程请求、2 次重试、3 次失败、1 次本地回退。
- 5 张异常海报产生 10 个 VLM 请求（每张 2 个异常、上限 3），10 个复核项全部 `vlm_confirmed`；正常项没有 VLM 请求。
- 12 张海报先 `--limit 5` 再续跑，最终结果与一次跑完一致。
- 真实基线（安平小区 3 张牌、462 个站点）约 6 分 30 秒，约 1.19 站/秒；这是 CPU 本地 OCR 的实测，不是合成数据的吞吐。

## 10. 全量估算与注意事项

- 约 20GB、1600 张海报时，不要逐张上传原图；本地切好的站名画布约 3.2GB，表头裁片更小。
- 本地 CPU OCR 全量约几十小时；远程 OCR 的主要瓶颈是 QPS 和上行带宽。10 QPS 下站名画布约 2 小时，表头另计。
- 默认 8 列/1000px 是准确率基线。10 列/1000px 或 12 列/1400px 能减少请求数，但跨列合并风险更高，必须先做小样本 A/B；未通过前不要直接用于全量。
- 缓存对首次全量收益很低（裁片几乎不重复），主要价值是断点续跑、重跑和增量新增。
- 线路号的 OCR 裁剪按每个块内红色字形的实际横向范围计算，不再依赖固定的 300–700px 窗口；折行的线路标题（如“通学专线2（…）”）会合并为一个块。
- 折行标题先保留小字号文字片段，合并后再校验整体高度；页框和低密度站序图标会单独排除。线路号保留 `K1`、`通学专线13` 等完整标识，数字区的 `O/I/l` 纠错不会删除线路前缀。
- 当前自动适配的是多线路竖排站序牌。横版的“下一站”单线路牌（一个大线路号 + 站名 + 下一站）会以 `unsupported landscape next-stop board` 失败，不会写入 `all_raw.json` 污染结果；这类牌需要单独的提取路径。
- 横版牌按宽列拆分前的投影列数判断模板，防止一个大站名被拆成多个假站点。管线版本已升至 `2.2`；已有 `done` 项不会因升级自动重跑，验证新算法请使用新的 `--out` 目录。
- 版面算法、站名修正词典和双向校验仍属于现有分析器；本流水线不自动适配所有新模板，只把无法识别的块标记为失败/复核并保留原图。
- 真实 OCR 在不同画布分组下可能出现形近字差异，最终站名要以复核队列和分析器修正为准，不能把原始 OCR 当作绝对真值。

## 11. 当前未单独实现

- 布局/画布层没有独立缓存；块级和海报级缓存已经覆盖断点续跑与重跑。布局本身是毫秒级，不是瓶颈。
- 没有内置令牌桶 QPS 限制；并发由 `--workers` 控制，VLM 串行执行并受每海报上限约束。需要严格 QPS 时在网关侧限流。
- 双向站序冲突和新模板没有可裁剪的单站目标图，因此只进入人工复核，不发送给 VLM；VLM 不会收到整张海报。
- 线路级子集筛选尚未提供；当前支持站点、图片哈希和数量子集。线路筛选需要先读表头或维护线路索引。

## 12. 测试

```bash
python -m pytest                 # 全部测试，含 100 张 soak
python -m pytest -m "not soak"   # 日常快速回归
```

测试使用本地 fake HTTP 服务验证远程 OCR/VLM 契约，不依赖外部账号或网络。真实 RapidOCR 的单张慢速回归默认跳过，可用 `RUN_REAL_OCR=1` 单独执行（需要真实底板图片）。

真实样本不在默认底板根目录时，可用 `BUS_OCR_SAMPLE_ROOT` 指定包含“安平小区”子目录的样本目录，例如 `E:/BaiduNetdiskDownload/底板/1-10`。

设置 `RUN_SAMPLE_LAYOUT=1` 后运行 `tests/test_route_geometry.py`，可逐张检查样本目录：竖版牌的全部线路必须能定位站名列，横版下一站牌必须在 OCR 前拒绝。

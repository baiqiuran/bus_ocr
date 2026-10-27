# 6000 张站牌的远程 OCR 加速评估

核对日期：2026-10-22（America/Los_Angeles）。依据本仓库当前代码、本地容量测量及厂商官方文档；尚无可用远端接口或服务器，未上传用户样本、未开通服务、未产生 OCR 调用费用。下列速度是条件计算，尚无远端实测成绩。

## 建议

优先做付费云 OCR 的小样本 A/B：比较“整张站牌一次识别”和“每条线路整带一次识别”，首选 Google Cloud Vision `DOCUMENT_TEXT_DETECTION`，也可用百度高精度含位置版对照。继续在本地做版面归属、站序和线路解析。整张方案的小字、竖排、尺寸和坐标映射尚未验证；每线路整带方案的文件尺寸已做本地试验，识别准确率仍需实测。

不能因为腾讯、百度某个高精度接口的价格，就认为所有商业 OCR 都贵。Google 和 Azure 的通用 OCR 约为每千图 1.50 美元的已核实档价；若能把约 53万～77万张裁片改成约 4.92万条线路带，成本和请求数都可明显下降。若云 API 的准确率、吞吐或总体成本不合适，再比较“远程 NVIDIA GPU + 传统 PaddleOCR + 本项目协议适配层”。已有 `RemoteOcrBackend` 可复用，但不原生兼容任何厂商接口。

## 可落地路线

### 1. 自建 GPU 服务：云 API 试验后的备选

- PaddleOCR 官方提供 `paddlex --serve --pipeline OCR --device gpu:0` 形式的 HTTP 服务，可通过配置文件指定模型、设备和批大小；官方建议先用基础服务验证，再按需要考虑基于 Triton 的方案。更复杂的服务方案不保证更快。[官方部署说明](https://www.paddleocr.ai/main/en/version3.x/inference_deployment/serving/serving.html)
- 传统 OCR 输出 `rec_texts`、`rec_scores`、`rec_polys` / `rec_boxes`，可映射到本项目 `text/score/box`。PP-OCRv5 的识别模型文档明确覆盖中文竖排场景；这不等于已经验证公交站牌准确率。`text_recognition_batch_size` 是识别模型内部批大小，不等于一次 HTTP 上传图片数量。[OCR 管线与返回字段](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/OCR.html)
- 先固定与现有结果最接近的模型做基线，再比较 server/mobile 模型及识别批大小。确认 GPU 实际被调用，预热后再计时；需要加速时再试 HPI。官方说明 HPI 的收益受模型和后端支持影响，首次还可能构建引擎，不能照抄单模块毫秒数计算整批用时。[高性能推理说明](https://www.paddleocr.ai/main/en/version3.x/inference_deployment/local_inference/high_performance_inference.html)
- 若继续沿用 RapidOCR，可验证其 Paddle GPU 或 TensorRT 引擎。官方当前仍不推荐在 RapidOCR 中使用 ONNX Runtime GPU，因此不能认为把 `onnxruntime` 换成 `onnxruntime-gpu` 就一定加速。[RapidOCR 推理引擎说明](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/how_to_use_infer_engine/)
- RapidOCRAPI 已有 FastAPI/uvicorn 封装，但官方说明其目标是快速调用，没有专门处理多进程并发。它的原生请求、返回格式也与本项目不同，仍需适配。[RapidOCRAPI 官方说明](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr_api/usage/)

部署要点：常驻模型；限流队列；按相似尺寸组成批次；返回每张裁片原坐标；记录真实模型版本；GPU 内部批处理和 HTTP 并发分开调参。以上为实施建议，尚未在本项目远端验证。不要默认启动多个模型进程就会更快，它们可能争抢同一张卡。

### 2. 百度：高精度含位置版，适合付费 API 对照

接口为 `POST /rest/2.0/ocr/v1/accurate`，不是不含位置的 `accurate_basic`。当前 REST 文档允许最长边 8192px，最短边至少 15px，Base64 与 URL 编码后不超过 10M。设置 `probability=true` 获得行置信度；`vertexes_location=true` 获得四点；需要单字时试 `recognize_granularity=small` 与 `char_probability=true`；混合方向可测试 `multidirectional_recognize=true`。多方向识别不构成本站竖排站序准确率承诺。[官方 REST 文档](https://cloud.baidu.com/doc/OCR/s/tk3h7y2aq)

当前刊例：高精度含位置版 1 万次包 380 元、50 万次包 11000 元、100 万次包 18000 元；后付费分档为每次 0.040 / 0.034 / 0.029 / 0.025 / 0.022 / 0.020 元。计费按 API 调用图片，不按原始站牌数。以实际购买页及账号权益为准。[2026-09-23 价格页](https://cloud.baidu.com/doc/OCR/s/9k3h7xuv6)

官方 FAQ 表示大部分 OCR 接口未付费时提供 2 QPS，开通付费后提升至 10 QPS；这是通用描述，应查当前账号该接口配额，不能当作已获配额或实际吞吐。[官方配额 FAQ](https://ai.baidu.com/ai-doc/REFERENCE/lkrue6ybx)

### 3. 腾讯：GeneralAccurateOCR，适合坐标/小字对照

默认限额 10 请求/秒；单图 Base64 不超过 10M。`ConfigID=OCR` 下可用 `IsWords=true` 获得单字信息；`EnableDetectSplit=true` 针对大图小字场景，值得 A/B。输入是单张 `ImageBase64` / `ImageUrl`，不是图片数组。[2026-09-08 接口文档](https://cloud.tencent.com/document/product/866/34937)

响应 `TextDetections` 的 `Confidence` 范围为 0～100，需除以 100。优先取原图 `Polygon`；`ItemPolygon` 是旋转纠正后的图像坐标，不能直接当原始裁片坐标。`WordCoordPoint` 是单字原图四点，可用于检查竖列跨站合并。[官方数据结构](https://cloud.tencent.com/document/api/866/33527)

当前刊例高精度版预付费 1 万次 3000 元、10 万次 15000 元；后付费档价为每次 0.50 / 0.35 / 0.20 元。大量裁片直接调用成本较高，优先作为对照或低置信度补识别。地域、资源包和活动价会影响最终费用。[2026-09-30 计费页](https://cloud.tencent.com/document/product/866/17619)

### 4. Google Cloud Vision：优先做整牌/整线路带 A/B

`DOCUMENT_TEXT_DETECTION` 面向密集文字，支持中文；`fullTextAnnotation` 的 Word/Symbol 都有四点 `boundingBox` 和 0～1 的 `confidence`，可转换为本项目条目。语言支持表列出 `zh`（Hans/Hant），但本次未找到明确保证“竖排中文站名列”的官方说明；不能把支持中文、旋转文字或自然阅读顺序当作本站牌竖排准确率证明。[OCR 用法](https://docs.cloud.google.com/vision/docs/ocr)、[返回结构](https://docs.cloud.google.com/vision/docs/reference/rest/v1/AnnotateImageResponse)、[语言支持](https://docs.cloud.google.com/vision/docs/languages)

OCR 按图/页、按功能计费；每月前 1000 单位免费，1001～500万单位为每千图 1.50 美元。本项目应只请求所需 OCR 功能，不能把一次多图请求当一次计费。只测约 4.92万线路带时，忽略免费额约 73.80 美元，免费额可用则约 72.30 美元；另计存储、税费及币种差异。[官方价格](https://cloud.google.com/vision/pricing)

原生同步 `images:annotate` 最多 16 图/请求，异步最多 2000 图/请求。默认请求额度 1800/min，文字检测功能还另按图/页限 1800/min；批量请求不会绕过图像配额，均需核实账号额度。异步输出写入 Cloud Storage，也需额外集成。[官方配额与限制](https://docs.cloud.google.com/vision/quotas)、[异步批量说明](https://docs.cloud.google.com/vision/docs/batch)

原始图像上限 20MB，JSON 请求上限 10MB；Base64 会增大体积。OCR 大于 7500万像素的图片会缩放，否则官方说明使用原图尺寸。不能笼统认为所有整牌都会缩小；也不能因此保证细小竖排字能识别。本目录有 8 张超过 1.78亿像素的大图，至少这部分需考虑切片。[官方图像规格](https://docs.cloud.google.com/vision/docs/supported-files)

### 5. Azure Read：可作对照，注意版本生命周期

Azure Image Analysis 4.0 的 Read 是同步单图 OCR；行返回 `boundingPolygon`，词返回 0～1 的 `confidence`，中文等无空格语言按字符作为 word 返回。支持中文，但本次同样未核实针对本项目竖排站名列的官方保证。[官方 REST 结构](https://learn.microsoft.com/en-us/rest/api/computervision/analyze/image?view=rest-computervision-v4.0%20%282024-02-01%29)、[语言支持](https://learn.microsoft.com/en-us/azure/ai-services/computer-vision/language-support)

4.0 图像需小于 20MB，尺寸大于 50×50、小于 16000×16000；官方 FAQ 表示不支持单请求多图，S1 可达 20 TPS，具体账号需确认。不要把异步 v3.2 Read、旧 OCR、4.0 Read 或 Document Intelligence 的限制混用。[4.0 图像限制](https://learn.microsoft.com/en-us/azure/ai-services/computer-vision/overview-image-analysis)、[批量/速率 FAQ](https://learn.microsoft.com/en-us/azure/ai-services/computer-vision/faq)

价格页将 Read 列为 Image Analysis Group 2。营销页本次读取为 `$-`，因此另用微软官方 Retail Prices API 核实：East US、Azure Vision、Image Analysis Group 2，单位 1K，起始价 1.50 美元，1000K 后为 0.60 美元。约 4.92万线路带按起始价是 73.80 美元，不计免费额与合同折扣；不能把该美国区域价格直接套在中国区。[官方功能分组](https://azure.microsoft.com/en-us/pricing/details/computer-vision/)、[官方价格 API 查询](https://prices.azure.com/api/retail/prices?%24filter=productName%20eq%20%27Azure%20Vision%27%20and%20armRegionName%20eq%20%27eastus%27%20and%20skuName%20eq%20%27Image%20Analysis%20Group%202%27)、[价格 API 说明](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)

官方明确 Image Analysis 4.0 已弃用，将于 2028-09-25 退役。因此可用于一次性对照，但新建长期服务时不列首选；这不等于其当下不可用。[官方生命周期说明](https://learn.microsoft.com/en-us/azure/ai-services/computer-vision/overview-image-analysis)

## 50万～100万张裁片的条件成本

假设每张裁片调用一次对应 OCR，忽略已有免费额度，不含重试、扩容、存储和网络费用。中国接口按人民币预付费刊例，Google/Azure 按美元按量价计算；这些不是质量等价的模型：

| 方案 | 50 万次调用 | 100 万次调用 |
| --- | --- | --- |
| 百度高精度含位置版 | 50 万次包：11000 元 | 100 万次包：18000 元 |
| 腾讯 GeneralAccurateOCR | 5 个 10 万次包：75000 元 | 100 万次包：80000 元 |
| Google DOCUMENT_TEXT_DETECTION | 750 美元 | 1500 美元 |
| Azure Read / East US Group 2 按量价 | 750 美元 | 1500 美元 |
| 自建 GPU | 实测总小时 × 实际 GPU 小时报价，加网络/存储 | 同左，按实际运行时长计 |

资源包均不是 6000 张站牌固定费用。百度价格及包规格见[百度官方价格](https://cloud.baidu.com/doc/OCR/s/9k3h7xuv6)，腾讯规格及可叠加购买示例见[腾讯官方价格](https://cloud.tencent.com/document/product/866/17619)。上述人民币价格仅用于这两种具体接口和地区口径，不代表所有 OCR 产品的价格；实际采购前应再次核对优惠和账号适用区域。

若自建服务在真实裁片上达到 20 裁片/秒，50 万/100 万张的纯 OCR 时长分别为 6.9/13.9 小时；这是假设计算，不是任何 GPU 型号的实测值。比较自建成本时还要计入部署验证时间，机器可能在上传和等待期间继续计费。本次未核实租 GPU 报价，也未开通资源。

默认 10 请求/秒的单图 API，在足够并发下，50 万/100 万次的理论下限仍为 13.9/27.8 小时。提高额度另付费：百度本接口 QPS 叠加包刊例 10 元/天/QPS；腾讯高精度版 B 类叠加包在购买量少于 10 QPS 时为 30 元/QPS/日。两者扩容费用都不抵扣图片调用费，实际额度须以账号为准。[百度 QPS 价格](https://cloud.baidu.com/doc/OCR/s/9k3h7xuv6)、[腾讯 QPS 价格](https://cloud.tencent.com/document/product/866/17619)

因此，经济性上应同时比较厂商和输入粒度。约 4.92万线路带可用百度 5万次包（1800 元）或 Google 约 73.80 美元（忽略免费额）；不能直接拿几十万高精度裁片费用决定必须租 GPU。准确率合格的便宜云 API 可能省去部署工作，GPU 是否更划算要用真实报价与实测比较。

## 本项目接入差距

`buspipe/remote.py` 接收的请求为 `{model, params, images:[{id,format,data}]}`，期望 `{model_version, results:[{id,items:[{text,score,box}]}]}`。目前三个参数是 `box_thresh/text_score/unclip_ratio`。

PaddleOCR 官方服务及以上云 API 均不原生兼容这份协议。自建服务应做一层转换；付费 API 可新增专门 backend，或在自建网关转换并统一限流。不能只把 `--remote-ocr` 指向厂商地址。厂商不支持的模型阈值不能静默假装已生效。

其他现状与调整建议：

- `RemoteOcrBackend.recognize()` 会将图片拆成最多 6 张、约 1MB 的多个请求，并依次发送。HTTP 多图只能节省请求开销，不证明远端用了 GPU 批推理；本地 RapidOCR 也在 `for image in images` 中逐图执行。
- 远端吞吐需要服务端批处理和多个站牌任务提供足够待处理图像；增加客户端 workers 后仍要限制全局请求率，避免 429 重试放大。
- 必须锁定可追溯的 `model_version`，保持缓存与模型绑定；云 API 通常不会提供与本协议完全相同的模型版本字段，需要定义并记录 backend 的配置版本。
- 校准现有置信度阈值。不同引擎的 0.75 不一定表示同等可靠，不能仅比较文本是否非空。
- 横版模板仍由本地布局判定，换远程 OCR 不能自动解除不支持模板的限制。
- 将待单列回退的裁片收集成批量请求，保留现有覆盖率与置信度规则；不要通过放宽质量阈值来凑速度。不同海报的请求共享有界并发池与全局 QPS 限制。
- 记录每类裁片的上传字节、请求等待、远端耗时、重试与回退数量。远端故障时的大量 CPU 同步兜底应单独统计，避免把混合运行误记为远程吞吐。

以上依据 `buspipe/remote.py`、`buspipe/ocr.py`、`buspipe/core.py`，与厂商原生接口做对照。

建议的实施顺序：先接一个云 OCR backend，增加整线路带读取模式并把原图坐标映射回现有线路/站名列；再补回退批处理、独立请求并发与计量。整牌模式作为对照实验，已有画布模式保留作质量兜底。输入模式和 backend 配置需进入缓存版本，避免复用旧结果。若云 OCR 的竖排准确率不足，再部署 GPU 服务比较相同样本。

## 6000 张要多快才够

当前每条线路有 header/time/date 3 个裁片，加路线号 3 个留白版本，即 6 张；还需若干站名画布及低置信度回退。20 张竖版样本有 164 条线路、5602 站名列，产生 984 张标题裁片及 788 张画布，共 1772 张基础 OCR 输入。离线预处理 98.5 秒，PNG 62.90MB、Base64 JSON 84.22MB，按现有自定义协议分为 335 次 HTTP 请求；未做远端识别。

若 6000 张全部是同类竖版，按样本平均值外推：基础输入约 531600 张；再按历史每画布单列调用率 `788/787` 估算，约 768300 张。该回退估计不是远端模型的真实回退率。

每线路整带的本地尺寸试验：3 张站牌、19 条线路生成 19 张 PNG，共 6.79MB，宽 4253/4726px、高 512～913px，文件尺寸具备可行性，但没有远程准确率结果。按样本平均每牌 8.2 条线路，6000 张对应约 49200 张线路带。10 QPS 下提交这些线路带的理论下限约 1.37 小时，不能当成端到端工期。

现场清点：6721 张 JPG、35.65GB，其中 2185 张竖版、4536 张横版；2185 张竖版按样本均值估算约 193591 张基础输入，含上述历史回退率约 279789 张。这里按宽高归类，尚未全量校验模板，不能据此认定每张竖版都受支持或每张横版都失败。已知横版下一站模板需要补独立提取路径，才能交付全量结果。机器为 i5-12600K + UHD 770，无 NVIDIA GPU。历史批次记录为 1154 次 backend 调用、2231 张 OCR 输入、787 张画布、788 次单列调用；backend 调用与实际 HTTP 请求不是同一计数。

输入合并主要减少请求数，未必同比减少带宽。3 张样本的 19 条线路整带平均约 358KB PNG，外推 6000 张同类竖版约需 23.45GB Base64 负载；旧裁片策略基础负载约 25.27GB。以实际持续上行 20Mbps 为假设，上传本身约 2.6～2.8 小时，不含回退、重传和协议额外开销；网络可与识别重叠。上传速度尚未实测。

| 实测稳定有效吞吐（裁片/秒） | 约53.2万～76.8万裁片的纯 OCR 时间 |
| --- | --- |
| 5 | 29.5～42.7 小时 |
| 10 | 14.8～21.3 小时 |
| 20 | 7.4～10.7 小时 |
| 50 | 3.0～4.3 小时 |

这些不是任意 GPU 的基准。沿用当前输入粒度时，目标 24 小时需要平均 6.2～8.9 裁片/秒；目标 12 小时需要 12.3～17.8 裁片/秒，再为上传、解码、投影、输出和异常留余量。纯 OCR 时间通常只是整体耗时的一部分。

百度/腾讯默认或常见 10 请求/秒的单图接口，在现有粒度下理论上限为 10 裁片/秒；Google 另有按图配额，不能用 HTTP 批大小乘出图像吞吐。并发数、延迟、上传带宽都会限制实际速度。整牌或每线路整带识别需要改裁片策略与坐标映射，准确率仍须验证，不能当成现成能力。

## 最小 A/B 验证

1. 用现有完整样本覆盖正常竖牌、密集小字、竖排站名、折行标题、`K1` / `通学专线`、红色本站、斜拍/模糊，并统计被拒绝的横版。先准备人工校对真值；已有 OCR 结果只能作为比较基线。
2. 同一批原图先比较 Google 整牌与每线路整带，再用现有 CPU 结果及人工真值核对；可加百度对照。只重试失败区域，避免直接沿用当前高回退率。单字/行框均需还原原图位置，检查跨站拼接；已有 OCR 结果不能替代人工真值。
3. 分别记录原始站牌、线路、裁片、HTTP 请求与重试计数，不能混用。测冷启动和预热后吞吐、p50/p95、上传字节、回退率、线路号完整率、站名字符错误率、完整站序一致率。
4. 云 API 并发从小到大，在已获配额内测试，保留超时、限流和失败处理。若云方案未满足质量/速度/成本，再测 GPU 并发 1/2/4/8、识别内部 batch 1/8/16。只在准确率没有明显退化、回退率没有反升时扩大批量。
5. 依据实测 `实际有效裁片数 / 总墙钟时间` 推算 6000 张总耗时，并预留重试与人工复核；达到目标后再全量执行。

最终可行性结论：远程 OCR 有明确接入路径，有望明显压缩处理时间。先用整牌/每线路整带的小样本云 OCR 验证费用、准确率和吞吐，再决定是否租 GPU；当前没有远程实测，不承诺具体小时数。横版布局支持需另行完成，不能把只跑竖版的工期称为全部 6721 张的工期。

本地测量产物：`output/remote-evaluation/inventory.json`、`capacity.json`、`line_band_capacity.json`。仅包含统计与源文件路径，没有把原图上传到第三方。
